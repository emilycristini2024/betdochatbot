import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from .settings import Settings, get_current_datetime
from .football_api import (
    FootballApiClient,
    build_analysis_payload,
    normalize_fixture_lineups,
)
from .sportsdb import SportsDbClient
from .llm import ask_llm_for_morning_report, ask_llm_for_reminder, sanitize_public_analysis_message
from .telegram_bot import send_to_telegram, send_to_telegram_sync

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler

# Memória em processo para relatórios matinais
MORNING_REPORT_MEMORY: dict[str, str] = {}
PRE_MATCH_REMINDER_MINUTES = 50


def get_scheduled_fixtures(settings: Settings, target_date: str) -> list[dict[str, Any]]:
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=settings.request_delay_seconds,
        )
        try:
            fixtures = build_analysis_payload(
                api_client=api_client,
                league_ids=settings.league_ids,
                target_date=target_date,
                timezone=settings.timezone,
                bookmaker_name=settings.bookmaker_name,
                max_fixtures=settings.max_fixtures,
                request_delay_seconds=settings.request_delay_seconds,
            )
            if fixtures:
                logging.info("API-Football: %d jogos enriquecidos para %s", len(fixtures), target_date)
                return fixtures
            logging.info("API-Football sem jogos para %s. Usando TheSportsDB...", target_date)
        except Exception as exc:
            logging.warning("API-Football falhou ao enriquecer jogos: %s. Usando TheSportsDB...", exc)

    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(target_date)
    logging.info("TheSportsDB: %d jogos para %s", len(fixtures), target_date)
    return fixtures


def enrich_fixture_with_lineups(settings: Settings, fixture: dict[str, Any]) -> dict[str, Any]:
    fixture_id = fixture.get("fixture_id")
    if not settings.rapidapi_key or not fixture_id:
        return fixture

    api_client = FootballApiClient(
        api_key=settings.rapidapi_key,
        host=settings.rapidapi_host,
        request_delay_seconds=settings.request_delay_seconds,
    )

    try:
        lineups_payload = api_client.get_fixture_lineups(int(fixture_id))
    except Exception as exc:
        logging.warning("Nao foi possivel buscar escalacoes do jogo %s: %s", fixture_id, exc)
        return fixture

    if not lineups_payload:
        logging.info("Escalacoes oficiais ainda indisponiveis para fixture %s", fixture_id)
        return fixture

    enriched = {**fixture}
    enriched["official_lineups"] = normalize_fixture_lineups(lineups_payload)
    logging.info("Escalacoes oficiais adicionadas ao fixture %s", fixture_id)
    return enriched


def remember_morning_report(target_date: str, report: str) -> None:
    if report.strip():
        MORNING_REPORT_MEMORY[target_date] = report.strip()


def get_fixture_date(fixture: dict[str, Any], timezone_name: str) -> str:
    kickoff = str(fixture.get("kickoff") or "")
    match = re.search(r"\d{4}-\d{2}-\d{2}", kickoff)
    if match:
        return match.group(0)
    return get_current_datetime(timezone_name).strftime("%Y-%m-%d")


def get_morning_report_context(fixture: dict[str, Any], settings: Settings) -> str:
    target_date = get_fixture_date(fixture, settings.timezone)
    report = MORNING_REPORT_MEMORY.get(target_date, "").strip()
    if not report:
        return "Relatorio Matinal nao encontrado em memoria para este jogo."
    return report[:6000]


def is_unavailable_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    text = str(value).strip().lower()
    return text in {"n/d", "nd", "none", "null"} or "nao disponivel" in text or "não disponível" in text


def count_objective_evidence(fixture: dict[str, Any]) -> int:
    evidence = 0

    for team_key in ("home_team", "away_team"):
        team = fixture.get(team_key) or {}
        for stat_key in ("avg_goals_scored", "avg_goals_conceded", "last_5_form"):
            if not is_unavailable_value(team.get(stat_key)):
                evidence += 1

    odds = fixture.get("odds") or {}
    for market_key in ("match_winner", "over_under", "both_teams_score", "corners"):
        market = odds.get(market_key)
        if isinstance(market, dict) and any(
            not is_unavailable_value(value) for value in market.values()
        ):
            evidence += 1

    technical_metrics = fixture.get("technical_metrics") or {}
    if isinstance(technical_metrics, dict):
        for value in technical_metrics.values():
            if not is_unavailable_value(value):
                evidence += 1

    official_lineups = fixture.get("official_lineups")
    if isinstance(official_lineups, list) and len(official_lineups) >= 2:
        evidence += 3

    return evidence


def build_low_data_reminder_message(
    fixture: dict[str, Any],
    morning_report_context: str,
) -> str:
    home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "Time Casa"
    away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "Time Fora"
    league = fixture.get("league") or "Liga"
    kickoff = fixture.get("kickoff") or "horário não confirmado"

    morning_line = (
        "Não há relatório matinal salvo para comparar este jogo."
        if morning_report_context.startswith("Relatorio Matinal nao encontrado")
        else (
            "O relatório matinal ficou como contexto, mas a checagem pré-jogo "
            "não trouxe dados novos para validar uma entrada."
        )
    )

    return (
        f"⏰ JOGO EM {PRE_MATCH_REMINDER_MINUTES} MINUTOS\n"
        f"⚽ {home} x {away}\n"
        f"🏆 {league} | 🕐 {kickoff}\n\n"
        "📌 STATUS DOS DADOS\n"
        "Dados pré-jogo insuficientes: só há confirmação de times, liga e horário.\n\n"
        "📊 LEITURA PRÉ-JOGO\n"
        f"{morning_line} Sem estatísticas atualizadas, odds confirmadas, escalações ou desfalques, "
        "a leitura precisa ser tratada como limitada.\n\n"
        "🎯 RECOMENDAÇÃO\n"
        "Mercado: SEM ENTRADA\n"
        "Odd: não confirmada\n"
        "Stake: 0\n"
        "Confiança: 4/10\n\n"
        "🧠 JUSTIFICATIVA\n"
        "Não há pelo menos 3 evidências objetivas a favor de um mercado. "
        "A decisão mais prudente é preservar banca em vez de manter uma confiança alta sem validação.\n\n"
        "⚠️ Gestão de risco: aposta não é certeza. Use banca definida e não aumente stake para recuperar perdas."
    )


def send_game_reminder(settings: Settings, fixture: dict[str, Any]) -> None:
    fixture = enrich_fixture_with_lineups(settings, fixture)
    home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "?"
    away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "?"
    league = fixture.get("league", "?")
    kickoff = fixture.get("kickoff", "")
    morning_report_context = get_morning_report_context(fixture, settings)
    evidence_count = count_objective_evidence(fixture)

    logging.info("Enviando lembrete: %s x %s", home, away)

    if evidence_count < 3:
        message = build_low_data_reminder_message(fixture, morning_report_context)
        send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
        return

    try:
        message = ask_llm_for_reminder(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            fixture=fixture,
            morning_report_context=morning_report_context,
            evidence_count=evidence_count,
        )
    except Exception as exc:
        logging.error("Erro ao gerar lembrete para %s x %s: %s", home, away, exc)
        message = f"⏰ Em {PRE_MATCH_REMINDER_MINUTES} minutos!\n⚽ {home} x {away} — {league}\n🕐 {kickoff}"

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            send_to_telegram(settings.telegram_token, settings.telegram_chat_id, message)
        )
    finally:
        loop.close()


def schedule_game_reminders(
    settings: Settings,
    fixtures: list[dict[str, Any]],
    apscheduler: "BackgroundScheduler",
) -> int:
    from apscheduler.triggers.date import DateTrigger

    now_utc = datetime.now(UTC)
    scheduled = 0

    for fixture in fixtures:
        kickoff_str = fixture.get("kickoff", "")
        if not kickoff_str:
            continue

        kickoff_utc: datetime | None = None
        formats_to_try = [
            ("%Y-%m-%d %H:%M BRT", timezone(timedelta(hours=-3))),
            ("%Y-%m-%d %H:%M:%S BRT", timezone(timedelta(hours=-3))),
            ("%Y-%m-%d %H:%M", timezone(timedelta(hours=-3))),
            ("%Y-%m-%dT%H:%M:%S%z", None),
            ("%Y-%m-%dT%H:%M:%S", timezone(timedelta(hours=-3))),
        ]

        for fmt, tz in formats_to_try:
            try:
                clean = kickoff_str.replace(" BRT", "").strip()
                dt = datetime.strptime(clean, fmt.replace(" BRT", "").strip())
                if tz:
                    kickoff_utc = dt.replace(tzinfo=tz).astimezone(UTC)
                else:
                    kickoff_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
                break
            except ValueError:
                continue

        if kickoff_utc is None:
            logging.warning("Não foi possível parsear horário '%s' — lembrete ignorado", kickoff_str)
            continue

        reminder_time = kickoff_utc - timedelta(minutes=PRE_MATCH_REMINDER_MINUTES)
        home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "?"
        away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "?"

        if reminder_time <= now_utc + timedelta(minutes=2):
            logging.info(
                "Lembrete de %s x %s ignorado (horário já passou: %s UTC)",
                home, away, reminder_time.strftime("%H:%M"),
            )
            continue

        job_id = f"reminder_{home}_{away}_{kickoff_str}".replace(" ", "_").replace("/", "-")
        apscheduler.add_job(
            send_game_reminder,
            trigger=DateTrigger(run_date=reminder_time),
            args=[settings, fixture],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,
        )
        logging.info(
            "Lembrete agendado: %s x %s às %s UTC (kickoff %s)",
            home, away, reminder_time.strftime("%H:%M"), kickoff_str,
        )
        scheduled += 1

    return scheduled


def send_morning_report(
    settings: Settings,
    apscheduler: "BackgroundScheduler | None" = None,
) -> None:
    today = get_current_datetime(settings.timezone).strftime("%Y-%m-%d")
    logging.info("Gerando relatório matinal para %s", today)

    fixtures = get_scheduled_fixtures(settings, today)

    if not fixtures:
        message = f"📋 Relatório matinal {today}\n\nNenhuma partida encontrada nas ligas configuradas."
        send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
        return

    try:
        report = ask_llm_for_morning_report(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            fixtures=fixtures,
            today=today,
            max_fixtures=settings.max_fixtures,
        )
        logging.info("Relatório matinal gerado com sucesso.")
    except Exception as exc:
        logging.error("Erro ao gerar relatório matinal: %s", exc)
        report = f"❌ Erro ao gerar relatório matinal: {exc}"

    if "Erro ao gerar relatório matinal" not in report:
        remember_morning_report(today, report)

    total = len(fixtures[:settings.max_fixtures])
    header = f"🌅 BetChat — Relatório Matinal {today}\n\n"
    if total < 10:
        header += (
            f"⚠️ Hoje encontramos apenas {total} jogo(s) nas ligas monitoradas. "
            f"Em dias com poucos jogos a cobertura pode ser limitada.\n\n"
        )

    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, header + report)
    logging.info("Relatório matinal enviado para o Telegram.")

    if apscheduler is not None:
        count = schedule_game_reminders(settings, fixtures[:settings.max_fixtures], apscheduler)
        logging.info("%d lembretes agendados para hoje.", count)
