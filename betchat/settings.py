import os
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timezone, UTC
import logging


@dataclass
class Settings:
    telegram_token: str
    telegram_chat_id: str
    llm_provider: str
    llm_api_key: str
    llm_base_url: str
    rapidapi_key: str
    rapidapi_host: str
    football_data_api_key: str
    llm_model: str
    timezone: str
    target_date: str
    bookmaker_name: str
    league_ids: list[int]
    max_fixtures: int
    request_delay_seconds: float
    bot_mode: str


def get_current_datetime(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        fallback_offsets = {"America/Sao_Paulo": -3, "UTC": 0}
        offset_hours = fallback_offsets.get(timezone_name, 0)
        fallback_timezone = timezone(timedelta(hours=offset_hours))
        logging.warning(
            "Timezone %s indisponivel. Usando fallback UTC%+d.",
            timezone_name,
            offset_hours,
        )
        return datetime.now(UTC).astimezone(fallback_timezone)


def load_settings() -> Settings:
    from dotenv import load_dotenv
    load_dotenv()

    tz = os.getenv("TIMEZONE", "America/Sao_Paulo")
    target_date = os.getenv("TARGET_DATE", "").strip()

    if not target_date:
        target_date = get_current_datetime(tz).strftime("%Y-%m-%d")

    league_ids = [
        int(lid.strip())
        for lid in os.getenv("LEAGUE_IDS", "39,140,135,78,61,71").split(",")
        if lid.strip()
    ]

    settings = Settings(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        llm_provider=os.getenv("LLM_PROVIDER", "groq").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").strip(),
        rapidapi_key=os.getenv("RAPIDAPI_KEY", "").strip(),
        rapidapi_host=os.getenv("RAPIDAPI_HOST", "v3.football.api-sports.io").strip(),
        football_data_api_key=os.getenv("FOOTBALL_DATA_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip(),
        timezone=tz,
        target_date=target_date,
        bookmaker_name=os.getenv("BOOKMAKER_NAME", "Bet365").strip(),
        league_ids=league_ids,
        max_fixtures=int(os.getenv("MAX_FIXTURES", "10")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")),
        bot_mode=os.getenv("BOT_MODE", "cron").strip().lower(),
    )

    _validate_settings(settings)
    return settings


def _validate_settings(settings: Settings) -> None:
    missing_fields = []

    if not settings.telegram_token:
        missing_fields.append("TELEGRAM_TOKEN")
    if not settings.llm_api_key:
        missing_fields.append("LLM_API_KEY")

    if settings.bot_mode == "cron":
        if not settings.telegram_chat_id:
            missing_fields.append("TELEGRAM_CHAT_ID")

    if missing_fields:
        fields = ", ".join(missing_fields)
        raise ValueError(f"Variaveis obrigatorias ausentes: {fields}")
