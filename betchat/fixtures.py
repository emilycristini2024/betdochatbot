import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .football_api import FootballApiClient, LEAGUE_NAMES, unavailable_technical_metrics
from .football_data import FootballDataClient
from .settings import Settings, get_current_datetime
from .sportsdb import SportsDbClient

FIXTURES_KEYWORDS = [
    "jogos", "partidas", "hoje", "amanha", "manha", "tarde", "noite",
    "dia", "grade", "agenda", "programacao", "fixtures", "jogos de hoje",
    "o que tem hoje", "tem jogo", "proximo", "proximos", "proxima",
    "semana", "fim de semana", "fds", "quando joga", "quando e o jogo",
    "apostas", "palpites", "tips", "dicas",
]

NEXT_FIXTURE_KEYWORDS = [
    "proximo jogo", "proximos jogos", "proxima partida", "proximas partidas",
    "proximo", "proximos", "proxima", "quando joga", "quando e o jogo",
]


def normalize_message_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def message_wants_fixtures(text: str) -> bool:
    normalized_text = normalize_message_text(text)
    return any(keyword in normalized_text for keyword in FIXTURES_KEYWORDS)


def message_wants_next_fixture(text: str) -> bool:
    normalized_text = normalize_message_text(text)
    return any(keyword in normalized_text for keyword in NEXT_FIXTURE_KEYWORDS)


def get_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        fallback_offsets = {"America/Sao_Paulo": -3, "UTC": 0}
        offset_hours = fallback_offsets.get(timezone_name, 0)
        return timezone(timedelta(hours=offset_hours))


def parse_fixture_kickoff(kickoff: Any, timezone_name: str) -> datetime | None:
    if not kickoff:
        return None

    text = str(kickoff).strip()
    target_timezone = get_timezone(timezone_name)

    if text.endswith(" BRT"):
        clean = text.removesuffix(" BRT").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(clean, fmt)
                return parsed.replace(tzinfo=timezone(timedelta(hours=-3))).astimezone(
                    target_timezone
                )
            except ValueError:
                continue

    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=target_timezone)
            return parsed.astimezone(target_timezone)
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=target_timezone)
        except ValueError:
            continue

    return None


def filter_fixtures_by_local_date(
    fixtures: list[dict[str, Any]],
    target_date: str,
    timezone_name: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []

    for fixture in fixtures:
        kickoff_dt = parse_fixture_kickoff(fixture.get("kickoff"), timezone_name)
        if kickoff_dt is None:
            filtered.append(fixture)
            continue
        if kickoff_dt.strftime("%Y-%m-%d") == target_date:
            filtered.append(fixture)

    return filtered


def filter_future_fixtures(
    fixtures: list[dict[str, Any]],
    timezone_name: str,
    now: datetime,
) -> list[dict[str, Any]]:
    future: list[tuple[datetime, dict[str, Any]]] = []

    for fixture in fixtures:
        kickoff_dt = parse_fixture_kickoff(fixture.get("kickoff"), timezone_name)
        if kickoff_dt is None:
            continue
        if kickoff_dt >= now:
            future.append((kickoff_dt, fixture))

    future.sort(key=lambda item: item[0])
    return [fixture for _, fixture in future]


def extract_date_from_message(text: str, current_date: str, timezone_name: str) -> str:
    normalized_text = normalize_message_text(text)

    if "amanha" in normalized_text:
        dt = get_current_datetime(timezone_name) + timedelta(days=1)
        extracted = dt.strftime("%Y-%m-%d")
        logging.info("Detectado 'amanha': %s (hoje: %s)", extracted, current_date)
        return extracted

    if "hoje" in normalized_text:
        logging.info("Detectado 'hoje': %s", current_date)
        return current_date

    match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if match:
        day = match.group(1).zfill(2)
        month = match.group(2).zfill(2)
        year = match.group(3) or get_current_datetime(timezone_name).strftime("%Y")
        extracted = f"{year}-{month}-{day}"
        logging.info("Detectado data especifica: %s", extracted)
        return extracted

    logging.info("Nenhuma data especifica detectada, usando hoje: %s", current_date)
    return current_date


def normalize_api_football_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "kickoff": fixture.get("fixture", {}).get("date"),
        "league": fixture.get("league", {}).get("name")
        or LEAGUE_NAMES.get(fixture.get("league", {}).get("id"), "Liga"),
        "country": fixture.get("league", {}).get("country"),
        "home": fixture.get("teams", {}).get("home", {}).get("name"),
        "away": fixture.get("teams", {}).get("away", {}).get("name"),
        "status": fixture.get("fixture", {}).get("status", {}).get("short"),
        "technical_metrics": unavailable_technical_metrics(),
        "source": "football_api",
    }


def get_fixtures_for_chat(
    settings: Settings,
    target_date: str,
) -> tuple[list[dict[str, Any]], str]:
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
                    normalize_api_football_fixture(fixture)
                    for fixture in fixtures[:15]
                ]
                result = filter_fixtures_by_local_date(result, target_date, settings.timezone)
                logging.info("API-Football retornou %d jogos para %s", len(result), target_date)
                if result:
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
                result = filter_fixtures_by_local_date(
                    fixtures[:15],
                    target_date,
                    settings.timezone,
                )
                logging.info(
                    "football-data.org retornou %d jogos para %s",
                    len(result),
                    target_date,
                )
                if result:
                    return result, "football_data"
            logging.info("football-data.org retornou 0 jogos para %s", target_date)
        except Exception as exc:
            logging.warning("football-data.org falhou (%s), usando TheSportsDB como fallback", exc)

    logging.info("Buscando jogos via TheSportsDB para %s", target_date)
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(target_date)
    return fixtures, "thesportsdb"


def get_next_api_football_fixtures(
    settings: Settings,
    now: datetime,
    max_days_ahead: int,
) -> list[dict[str, Any]]:
    if not settings.rapidapi_key:
        return []

    api_client = FootballApiClient(
        api_key=settings.rapidapi_key,
        host=settings.rapidapi_host,
        request_delay_seconds=0.5,
    )
    collected: list[dict[str, Any]] = []

    for days_ahead in range(max_days_ahead + 1):
        target_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        try:
            raw_fixtures = api_client.get_fixtures_for_date(target_date, settings.timezone)
        except Exception as exc:
            logging.warning("API-Football ampla falhou para %s: %s", target_date, exc)
            continue

        normalized = [normalize_api_football_fixture(fixture) for fixture in raw_fixtures]
        future_fixtures = filter_future_fixtures(normalized, settings.timezone, now)
        collected.extend(future_fixtures)
        if collected:
            break

    return sorted(
        collected,
        key=lambda fixture: parse_fixture_kickoff(fixture.get("kickoff"), settings.timezone) or now,
    )


def get_next_sportsdb_fixtures(
    settings: Settings,
    now: datetime,
) -> list[dict[str, Any]]:
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_next_fixtures(limit=15)
    future_fixtures = filter_future_fixtures(fixtures, settings.timezone, now)
    if future_fixtures:
        return future_fixtures
    if fixtures:
        logging.warning(
            "TheSportsDB retornou %d proximos jogos, mas nenhum horario passou no filtro futuro. "
            "Usando lista bruta.",
            len(fixtures),
        )
    return fixtures


def get_next_fixtures_for_chat(
    settings: Settings,
    max_days_ahead: int = 7,
) -> tuple[list[dict[str, Any]], str, str]:
    now = get_current_datetime(settings.timezone)
    current_date = now.strftime("%Y-%m-%d")

    api_football_fixtures = get_next_api_football_fixtures(settings, now, max_days_ahead)
    if api_football_fixtures:
        target_date = (
            parse_fixture_kickoff(api_football_fixtures[0].get("kickoff"), settings.timezone)
            or now
        ).strftime("%Y-%m-%d")
        return api_football_fixtures[:5], "football_api", target_date

    if not settings.rapidapi_key and not settings.football_data_api_key:
        sportsdb_fixtures = get_next_sportsdb_fixtures(settings, now)
        if sportsdb_fixtures:
            target_date = (
                parse_fixture_kickoff(sportsdb_fixtures[0].get("kickoff"), settings.timezone)
                or now
            ).strftime("%Y-%m-%d")
            return sportsdb_fixtures[:5], "thesportsdb", target_date
        return [], "thesportsdb", current_date

    for days_ahead in range(max_days_ahead + 1):
        target_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        fixtures, source = get_fixtures_for_chat(settings, target_date)
        future_fixtures = filter_future_fixtures(fixtures, settings.timezone, now)
        if future_fixtures:
            logging.info(
                "Proximos jogos encontrados via %s para %s",
                source,
                target_date,
            )
            return future_fixtures[:5], source, target_date

    sportsdb_fixtures = get_next_sportsdb_fixtures(settings, now)
    if sportsdb_fixtures:
        target_date = (
            parse_fixture_kickoff(sportsdb_fixtures[0].get("kickoff"), settings.timezone)
            or now
        ).strftime("%Y-%m-%d")
        return sportsdb_fixtures[:5], "thesportsdb", target_date

    logging.info(
        "Nenhum jogo futuro encontrado entre %s e os proximos %d dias",
        current_date,
        max_days_ahead,
    )
    return [], "football_api", current_date
