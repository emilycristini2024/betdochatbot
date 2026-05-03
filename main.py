import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_BASE_URL = "https://v3.football.api-sports.io"
DEFAULT_SYSTEM_PROMPT = """
Voce e um Trader Esportivo de elite e analista quantitativo. Sua missao e ler o JSON de partidas e estatisticas fornecido e selecionar as 10 melhores oportunidades de aposta do dia.

Regras de Selecao:
- Valor: procure por odds entre 1.50 e 2.20 quando a probabilidade estatistica parecer dominante.
- Gols: se dois times tiverem medias ofensivas e defensivas favoraveis, considere Over 2.5 ou Ambas Marcam, mas so quando houver odd disponivel nos dados.
- Favoritos extremos: se a odd do favorito estiver muito baixa, prefira mercados alternativos apenas se os dados enviados incluirem cotacao para isso.

Formato de Saida:
[NOME DA LIGA]
[MANDANTE] x [VISITANTE]
Aposta Sugerida: [Mercado] @ [Odd Aproximada]
Raciocinio: [Explicar em ate 10 palavras]
Confianca: [X]/10

---

Responda apenas com os 10 palpites ou menos, sem introducao e sem texto extra.
""".strip()
CHAT_SYSTEM_PROMPT = """
Você é o BetChat, trader esportivo especializado em futebol e apostas.

Suas respostas seguem estas regras sem exceção:
1. Sempre em português do Brasil.
2. Nunca cumprimente com "bom dia", "boa tarde" ou "boa noite". Vá direto ao assunto.
3. Nunca diga que não tem acesso a dados em tempo real. Você tem conhecimento profundo de futebol e usa isso para analisar.
4. Quando perguntarem de um jogo, analise: forma recente dos times, histórico do confronto, jogadores-chave, mercados recomendados (1x2, over/under 2.5, ambas marcam, handicap asiático).
5. Seja direto e opinativo. Diga qual lado favorece e por quê.
6. Não invente odds numéricas. Analise o jogo, não as cotações.
7. Só mencione o cron do grupo se o usuário pedir explicitamente os palpites do dia.
""".strip()
LEAGUE_NAMES = {
    39: "Premier League",
    61: "Ligue 1",
    71: "Brasileirao Serie A",
    78: "Bundesliga",
    135: "Serie A",
    140: "La Liga",
}


@dataclass
class Settings:
    telegram_token: str
    telegram_chat_id: str
    llm_provider: str
    llm_api_key: str
    llm_base_url: str
    rapidapi_key: str
    rapidapi_host: str
    llm_model: str
    timezone: str
    target_date: str
    bookmaker_name: str
    league_ids: list[int]
    max_fixtures: int
    request_delay_seconds: float
    bot_mode: str


class FootballApiError(Exception):
    pass


class FootballApiRateLimitError(FootballApiError):
    pass


class FootballApiClient:
    def __init__(self, api_key: str, host: str, request_delay_seconds: float = 0.0) -> None:
        self.session = requests.Session()
        self.request_delay_seconds = request_delay_seconds
        self.session.headers.update(
            {
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": host,
            }
        )

    def get(self, path: str, params: dict[str, Any]) -> Any:
        response = self.session.get(
            f"{API_BASE_URL}{path}",
            params=params,
            timeout=30,
        )
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)

        if response.status_code == 429:
            raise FootballApiRateLimitError(
                "Limite de requisicoes da API-Football atingido. Aguarde a renovacao da cota e tente novamente."
            )

        response.raise_for_status()
        payload = response.json()
        return payload.get("response", [])

    def get_daily_fixtures(
        self,
        league_ids: list[int],
        target_date: str,
        timezone: str,
    ) -> list[dict[str, Any]]:
        response = self.get(
            "/fixtures",
            {
                "date": target_date,
                "timezone": timezone,
            },
        )

        allowed_leagues = set(league_ids)
        return [
            fixture
            for fixture in response
            if fixture.get("league", {}).get("id") in allowed_leagues
        ]

    def get_team_statistics(
        self,
        team_id: int,
        league_id: int,
        season: int,
    ) -> dict[str, Any]:
        response = self.get(
            "/teams/statistics",
            {
                "team": team_id,
                "league": league_id,
                "season": season,
            },
        )

        if isinstance(response, dict):
            return response

        return {}

    def get_fixture_odds(self, fixture_id: int) -> list[dict[str, Any]]:
        response = self.get("/odds", {"fixture": fixture_id})

        if isinstance(response, list):
            return response

        return []


def load_settings() -> Settings:
    load_dotenv()

    timezone = os.getenv("TIMEZONE", "America/Sao_Paulo")
    target_date = os.getenv("TARGET_DATE")

    if not target_date:
        target_date = get_current_datetime(timezone).strftime("%Y-%m-%d")

    league_ids = [
        int(league_id.strip())
        for league_id in os.getenv("LEAGUE_IDS", "39,140,135,78,61,71").split(",")
        if league_id.strip()
    ]

    settings = Settings(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        llm_provider=os.getenv("LLM_PROVIDER", "groq").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").strip(),
        rapidapi_key=os.getenv("RAPIDAPI_KEY", "").strip(),
        rapidapi_host=os.getenv("RAPIDAPI_HOST", "v3.football.api-sports.io").strip(),
        llm_model=os.getenv("LLM_MODEL", "mixtral-8x7b-32768").strip(),
        timezone=timezone,
        target_date=target_date,
        bookmaker_name=os.getenv("BOOKMAKER_NAME", "Bet365").strip(),
        league_ids=league_ids,
        max_fixtures=int(os.getenv("MAX_FIXTURES", "10")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")),
        bot_mode=os.getenv("BOT_MODE", "cron").strip().lower(),
    )

    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    missing_fields = []

    if not settings.telegram_token:
        missing_fields.append("TELEGRAM_TOKEN")
    if not settings.llm_api_key:
        missing_fields.append("LLM_API_KEY")

    if settings.bot_mode == "cron":
        if not settings.telegram_chat_id:
            missing_fields.append("TELEGRAM_CHAT_ID")
        if not settings.rapidapi_key:
            missing_fields.append("RAPIDAPI_KEY")

    if missing_fields:
        fields = ", ".join(missing_fields)
        raise ValueError(f"Variaveis obrigatorias ausentes: {fields}")


def get_current_datetime(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        fallback_offsets = {
            "America/Sao_Paulo": -3,
            "UTC": 0,
        }
        offset_hours = fallback_offsets.get(timezone_name, 0)
        fallback_timezone = timezone(timedelta(hours=offset_hours))
        logging.warning(
            "Timezone %s indisponivel no ambiente. Usando fallback UTC%+d.",
            timezone_name,
            offset_hours,
        )
        return datetime.now(UTC).astimezone(fallback_timezone)


def parse_average(value: Any) -> float | None:
    if value in (None, ""):
        return None

    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def serialize_form(value: str | None) -> str:
    if not value:
        return "N/D"

    return value[-5:]


def pick_bookmaker(
    odds_payload: list[dict[str, Any]],
    preferred_name: str,
) -> dict[str, Any] | None:
    bookmakers: list[dict[str, Any]] = []

    for fixture_odds in odds_payload:
        bookmakers.extend(fixture_odds.get("bookmakers", []))

    if not bookmakers:
        return None

    for bookmaker in bookmakers:
        if bookmaker.get("name", "").lower() == preferred_name.lower():
            return bookmaker

    return bookmakers[0]


def extract_market_odds(bookmaker: dict[str, Any] | None) -> dict[str, Any]:
    if not bookmaker:
        return {}

    extracted: dict[str, Any] = {}

    for bet in bookmaker.get("bets", []):
        name = bet.get("name", "").lower()
        values = bet.get("values", [])

        if "match winner" in name or "1x2" in name:
            extracted["match_winner"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
        elif "goals over/under" in name or "over/under" in name:
            over_under = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
            if "Over 2.5" in over_under or "Under 2.5" in over_under:
                extracted["over_under_2_5"] = {
                    "Over 2.5": over_under.get("Over 2.5"),
                    "Under 2.5": over_under.get("Under 2.5"),
                }
        elif "both teams score" in name:
            extracted["both_teams_score"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }

    return extracted


def normalize_team_stats(team_name: str, stats: dict[str, Any], side: str) -> dict[str, Any]:
    goals_for = stats.get("goals", {}).get("for", {}).get("average", {})
    goals_against = stats.get("goals", {}).get("against", {}).get("average", {})

    return {
        "name": team_name,
        "avg_goals_scored": parse_average(goals_for.get(side)),
        "avg_goals_conceded": parse_average(goals_against.get(side)),
        "last_5_form": serialize_form(stats.get("form")),
    }


def simplify_fixture(
    fixture: dict[str, Any],
    home_stats: dict[str, Any],
    away_stats: dict[str, Any],
    odds_payload: list[dict[str, Any]],
    bookmaker_name: str,
) -> dict[str, Any]:
    league = fixture.get("league", {})
    teams = fixture.get("teams", {})
    fixture_info = fixture.get("fixture", {})
    bookmaker = pick_bookmaker(odds_payload, bookmaker_name)

    return {
        "fixture_id": fixture_info.get("id"),
        "kickoff": fixture_info.get("date"),
        "league": league.get("name") or LEAGUE_NAMES.get(league.get("id"), "Liga"),
        "country": league.get("country"),
        "season": league.get("season"),
        "home_team": normalize_team_stats(
            teams.get("home", {}).get("name", "Mandante"),
            home_stats,
            "home",
        ),
        "away_team": normalize_team_stats(
            teams.get("away", {}).get("name", "Visitante"),
            away_stats,
            "away",
        ),
        "odds": {
            "bookmaker": bookmaker.get("name") if bookmaker else None,
            **extract_market_odds(bookmaker),
        },
    }


def build_analysis_payload(
    api_client: FootballApiClient,
    settings: Settings,
) -> list[dict[str, Any]]:
    fixtures = api_client.get_daily_fixtures(
        league_ids=settings.league_ids,
        target_date=settings.target_date,
        timezone=settings.timezone,
    )

    if not fixtures:
        return []

    simplified_fixtures: list[dict[str, Any]] = []

    for fixture in fixtures[: settings.max_fixtures]:
        league = fixture.get("league", {})
        teams = fixture.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})

        try:
            home_stats = api_client.get_team_statistics(
                team_id=home.get("id"),
                league_id=league.get("id"),
                season=league.get("season"),
            )
            away_stats = api_client.get_team_statistics(
                team_id=away.get("id"),
                league_id=league.get("id"),
                season=league.get("season"),
            )
            odds_payload = api_client.get_fixture_odds(fixture.get("fixture", {}).get("id"))
        except FootballApiRateLimitError:
            if simplified_fixtures:
                logging.warning(
                    "Cota da API-Football esgotada apos %s partidas. Continuando com analise parcial.",
                    len(simplified_fixtures),
                )
                break
            raise

        simplified_fixtures.append(
            simplify_fixture(
                fixture=fixture,
                home_stats=home_stats,
                away_stats=away_stats,
                odds_payload=odds_payload,
                bookmaker_name=settings.bookmaker_name,
            )
        )

    return simplified_fixtures


def ask_llm_for_predictions(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    cleaned_payload: list[dict[str, Any]],
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    user_prompt = (
        "Analise as partidas abaixo e retorne os melhores palpites no formato pedido.\n\n"
        f"{json.dumps(cleaned_payload, ensure_ascii=False, indent=2)}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or ""
    logging.info("Analise gerada com provider %s e modelo %s", provider, model)
    return content.strip()


def ask_llm_for_chat_reply(settings: Settings, user_message: str) -> str:
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": CHAT_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )
    content = response.choices[0].message.content or ""
    return content.strip() or "Nao consegui responder agora. Tente novamente em instantes."


def split_message(text: str, max_length: int = 4000) -> list[str]:
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    chunk = ""

    for block in text.split("\n---\n"):
        candidate = f"{chunk}\n---\n{block}".strip() if chunk else block

        if len(candidate) <= max_length:
            chunk = candidate
            continue

        if chunk:
            parts.append(chunk)
        chunk = block

    if chunk:
        parts.append(chunk)

    normalized_parts: list[str] = []

    for part in parts:
        if len(part) <= max_length:
            normalized_parts.append(part)
            continue

        for index in range(0, len(part), max_length):
            normalized_parts.append(part[index : index + max_length])

    return normalized_parts


async def send_to_telegram(token: str, chat_id: str, message: str) -> None:
    bot = Bot(token=token)

    async with bot:
        for chunk in split_message(message):
            await bot.send_message(chat_id=chat_id, text=chunk)


def build_no_games_message(target_date: str) -> str:
    return f"Nenhuma partida encontrada para {target_date} nas ligas configuradas."


def build_rate_limit_message(target_date: str) -> str:
    return (
        f"BetChat nao conseguiu concluir a analise de {target_date} porque a cota da API-Football foi atingida. "
        "Tente novamente mais tarde ou reduza MAX_FIXTURES."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "BetChat online. Posso responder perguntas curtas sobre futebol e apostas, "
        "e o cron diario continua enviando os palpites programados."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Comandos:\n"
        "/start - inicia o bot\n"
        "/help - mostra esta ajuda\n\n"
        "Em grupo, respondo quando voce falar comigo ou responder uma mensagem minha."
    )


def should_answer_message(update: Update, bot_username: str | None) -> bool:
    message = update.message
    if not message or not message.text:
        return False

    if message.chat.type == "private":
        return True

    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_bot:
        return True

    if bot_username and f"@{bot_username.lower()}" in message.text.lower():
        return True

    return False


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    bot_username: str | None = context.application.bot_data.get("bot_username")

    if not update.message or not update.message.text:
        return

    if not should_answer_message(update, bot_username):
        return

    await update.message.chat.send_action("typing")
    try:
        reply = await asyncio.to_thread(
            ask_llm_for_chat_reply,
            settings,
            update.message.text,
        )
    except Exception as exc:
        logging.error("Erro ao chamar a LLM: %s", exc)
        await update.message.reply_text(
            "Ocorreu um erro ao processar sua mensagem. Verifique as configuracoes da LLM e tente novamente."
        )
        return
    await update.message.reply_text(reply)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    logging.info("Bot conversacional conectado como @%s", me.username)


def run_chat_bot(settings: Settings) -> None:
    application = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    logging.info("Iniciando modo chat por polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def run_cron_bot(settings: Settings) -> None:
    api_client = FootballApiClient(
        api_key=settings.rapidapi_key,
        host=settings.rapidapi_host,
        request_delay_seconds=settings.request_delay_seconds,
    )

    logging.info("Buscando partidas de %s", settings.target_date)
    try:
        cleaned_payload = build_analysis_payload(api_client, settings)
    except FootballApiRateLimitError as exc:
        logging.warning("%s", exc)
        message = build_rate_limit_message(settings.target_date)
        asyncio.run(
            send_to_telegram(
                token=settings.telegram_token,
                chat_id=settings.telegram_chat_id,
                message=message,
            )
        )
        logging.info("Fluxo finalizado com aviso de limite da API")
        return

    if not cleaned_payload:
        message = build_no_games_message(settings.target_date)
        logging.info("Nenhuma partida encontrada para envio")
    else:
        logging.info("Enviando %s partidas para analise", len(cleaned_payload))
        message = ask_llm_for_predictions(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            cleaned_payload=cleaned_payload,
        )

    asyncio.run(
        send_to_telegram(
            token=settings.telegram_token,
            chat_id=settings.telegram_chat_id,
            message=message,
        )
    )
    logging.info("Fluxo finalizado com sucesso")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = load_settings()

    if settings.bot_mode == "chat":
        run_chat_bot(settings)
        return

    run_cron_bot(settings)


if __name__ == "__main__":
    main()
