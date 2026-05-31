import logging
import time
import threading

import schedule
from apscheduler.schedulers.background import BackgroundScheduler

from betchat.settings import load_settings, get_current_datetime
from betchat.football_api import FootballApiClient, FootballApiRateLimitError, build_analysis_payload
from betchat.sportsdb import SportsDbClient
from betchat.llm import ask_llm_for_predictions
from betchat.telegram_bot import run_chat_bot, send_to_telegram_sync
from betchat.reminders import send_morning_report, schedule_game_reminders


def build_no_games_message(target_date: str) -> str:
    return f"Nenhuma partida encontrada para {target_date} nas ligas configuradas."


def build_rate_limit_message(target_date: str) -> str:
    return (
        f"BetChat nao conseguiu concluir a analise de {target_date} porque a cota da API-Football foi atingida. "
        "Tente novamente mais tarde ou reduza MAX_FIXTURES."
    )


def _send_cron_analysis(settings, cleaned_payload) -> None:
    logging.info("Enviando %s partidas para analise LLM...", len(cleaned_payload))
    try:
        message = ask_llm_for_predictions(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            cleaned_payload=cleaned_payload,
        )
        logging.info("LLM respondeu com sucesso.")
    except Exception as exc:
        logging.error("Erro ao chamar LLM: %s", exc, exc_info=True)
        send_to_telegram_sync(
            settings.telegram_token,
            settings.telegram_chat_id,
            f"❌ Erro ao gerar análise: {exc}",
        )
        return

    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
    logging.info("Fluxo finalizado com sucesso")


def run_cron_bot(settings) -> None:
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=settings.request_delay_seconds,
        )
        logging.info("Buscando partidas de %s via API-Football", settings.target_date)
        try:
            cleaned_payload = build_analysis_payload(
                api_client=api_client,
                league_ids=settings.league_ids,
                target_date=settings.target_date,
                timezone=settings.timezone,
                bookmaker_name=settings.bookmaker_name,
                max_fixtures=settings.max_fixtures,
                request_delay_seconds=settings.request_delay_seconds,
            )
            if cleaned_payload:
                _send_cron_analysis(settings, cleaned_payload)
                return
            logging.info("API-Football sem jogos, tentando TheSportsDB...")
        except FootballApiRateLimitError as exc:
            logging.warning("%s", exc)
            send_to_telegram_sync(
                settings.telegram_token,
                settings.telegram_chat_id,
                build_rate_limit_message(settings.target_date),
            )
            return
        except Exception as exc:
            logging.warning("API-Football falhou: %s. Tentando TheSportsDB...", exc)

    logging.info("Buscando partidas de %s via TheSportsDB", settings.target_date)
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(settings.target_date)

    if not fixtures:
        message = build_no_games_message(settings.target_date)
        logging.info("Nenhuma partida encontrada em nenhuma fonte")
    else:
        logging.info("TheSportsDB retornou %d jogos. Enviando para LLM...", len(fixtures))
        try:
            message = ask_llm_for_predictions(
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                cleaned_payload=fixtures[:settings.max_fixtures],
            )
        except Exception as exc:
            logging.error("Erro ao chamar LLM: %s", exc, exc_info=True)
            send_to_telegram_sync(
                settings.telegram_token,
                settings.telegram_chat_id,
                f"❌ Erro ao gerar análise: {exc}",
            )
            return

    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
    logging.info("Fluxo finalizado com sucesso")


def run_scheduled_bot(settings) -> None:
    apscheduler = BackgroundScheduler(timezone="UTC")
    apscheduler.start()
    logging.info("APScheduler iniciado.")

    schedule_time_utc = "10:00"
    logging.info("Agendando relatório matinal para 07:00 BRT (10:00 UTC) todos os dias.")

    def job() -> None:
        try:
            send_morning_report(settings, apscheduler)
        except Exception as exc:
            logging.error("Erro no job do relatório matinal: %s", exc, exc_info=True)

    schedule.every().day.at(schedule_time_utc).do(job)

    # Re-agenda lembretes se bot reiniciou após 7h BRT
    now_brt = get_current_datetime(settings.timezone)
    report_time_brt = now_brt.replace(hour=7, minute=0, second=0, microsecond=0)
    if now_brt > report_time_brt:
        logging.info("Bot iniciado após 07h BRT — re-agendando lembretes do dia...")
        try:
            today = now_brt.strftime("%Y-%m-%d")
            sportsdb = SportsDbClient()
            fixtures = sportsdb.get_fixtures_for_date(today)
            if fixtures:
                count = schedule_game_reminders(settings, fixtures[:settings.max_fixtures], apscheduler)
                logging.info("Re-agendados %d lembretes para hoje (%s).", count, today)
        except Exception as exc:
            logging.warning("Erro ao re-agendar lembretes: %s", exc)

    def run_scheduler() -> None:
        logging.info("Scheduler de horário fixo iniciado.")
        while True:
            schedule.run_pending()
            time.sleep(30)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    logging.info("Iniciando bot de chat com relatório matinal e lembretes agendados.")
    run_chat_bot(settings)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = load_settings()

    if settings.bot_mode == "chat":
        run_chat_bot(settings)
        return

    if settings.bot_mode == "scheduled":
        run_scheduled_bot(settings)
        return

    run_cron_bot(settings)


if __name__ == "__main__":
    main()
