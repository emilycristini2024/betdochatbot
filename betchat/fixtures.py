import logging
import re
from datetime import timedelta
from typing import Any

from .settings import Settings, get_current_datetime
from .football_api import FootballApiClient, unavailable_technical_metrics, LEAGUE_NAMES
from .football_data import FootballDataClient
from .sportsdb import SportsDbClient

# Palavras-chave que indicam que o usuário quer jogos do dia
FIXTURES_KEYWORDS = [
    "jogos", "partidas", "hoje", "amanhã", "amanha", "manhã", "manha",
    "tarde", "noite", "dia", "grade", "agenda", "programação", "programacao",
    "fixtures", "jogos de hoje", "o que tem hoje", "tem jogo",
    "próximo", "proximo", "próximos", "proximos", "próxima", "proxima",
    "semana", "fim de semana", "fds", "quando joga", "quando é o jogo",
    "apostas", "palpites", "tips", "dicas",
]


def message_wants_fixtures(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in FIXTURES_KEYWORDS)


def extract_date_from_message(text: str, current_date: str, timezone_name: str) -> str:
    text_lower = text.lower()

    if "amanhã" in text_lower or "amanha" in text_lower:
        dt = get_current_datetime(timezone_name) + timedelta(days=1)
        extracted = dt.strftime("%Y-%m-%d")
        logging.info("Detectado 'amanhã': %s (hoje: %s)", extracted, current_date)
        return extracted

    if "hoje" in text_lower:
        logging.info("Detectado 'hoje': %s", current_date)
        return current_date

    match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if match:
        day = match.group(1).zfill(2)
        month = match.group(2).zfill(2)
        year = match.group(3) or get_current_datetime(timezone_name).strftime("%Y")
        extracted = f"{year}-{month}-{day}"
        logging.info("Detectado data específica: %s", extracted)
        return extracted

    logging.info("Nenhuma data específica detectada, usando hoje: %s", current_date)
    return current_date


def get_fixtures_for_chat(
    settings: Settings,
    target_date: str,
) -> tuple[list[dict[str, Any]], str]:
    """
    Busca jogos do dia para o modo chat.
    Tenta API-Football primeiro; se falhar, usa TheSportsDB como fallback.
    Retorna (lista_de_jogos, fonte).
    """
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=0.5,
        )
        try:
            fixtures = api_client.get_daily_fixtures(
                league_ids=settings.league_ids,
                target_date=target_date,
                timezone=settings.timezone,
            )
            if fixtures:
                result = [
                    {
                        "kickoff": f.get("fixture", {}).get("date"),
                        "league": f.get("league", {}).get("name") or LEAGUE_NAMES.get(
                            f.get("league", {}).get("id"), "Liga"
                        ),
                        "home": f.get("teams", {}).get("home", {}).get("name"),
                        "away": f.get("teams", {}).get("away", {}).get("name"),
                        "technical_metrics": unavailable_technical_metrics(),
                        "source": "football_api",
                    }
                    for f in fixtures[:15]
                ]
                logging.info("API-Football retornou %d jogos para %s", len(result), target_date)
                return result, "football_api"
            else:
                logging.info(
                    "API-Football retornou 0 jogos para %s, tentando football-data.org",
                    target_date,
                )
        except Exception as exc:
            logging.warning("API-Football falhou (%s), tentando football-data.org", exc)

    if settings.football_data_api_key:
        try:
            football_data = FootballDataClient(
                api_key=settings.football_data_api_key,
                timezone_name=settings.timezone,
            )
            fixtures = football_data.get_matches_for_date(target_date)
            if fixtures:
                result = fixtures[:15]
                logging.info(
                    "football-data.org retornou %d jogos para %s",
                    len(result),
                    target_date,
                )
                return result, "football_data"
            logging.info("football-data.org retornou 0 jogos para %s", target_date)
        except Exception as exc:
            logging.warning("football-data.org falhou (%s), usando TheSportsDB como fallback", exc)

    logging.info("Buscando jogos via TheSportsDB para %s", target_date)
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(target_date)
    return fixtures, "thesportsdb"
