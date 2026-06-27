import asyncio
import logging
from typing import Any

from telegram import Bot, Message, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .fixtures import (
    extract_date_from_message,
    filter_future_fixtures,
    get_fixtures_for_chat,
    get_next_fixtures_for_chat,
    get_sources_diagnostics,
    message_wants_fixtures,
    message_wants_next_fixture,
)
from .llm import ask_llm_for_chat_reply
from .settings import Settings, get_current_datetime


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
            normalized_parts.append(part[index: index + max_length])

    return normalized_parts


async def send_to_telegram(token: str, chat_id: str, message: str) -> None:
    bot = Bot(token=token)
    async with bot:
        for chunk in split_message(message):
            await bot.send_message(chat_id=chat_id, text=chunk)


def send_to_telegram_sync(token: str, chat_id: str, message: str) -> None:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_to_telegram(token, chat_id, message))
    finally:
        loop.close()


async def reply_with_chunks(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply_text(chunk)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "BetChat online. Analiso jogos com foco em gols, ambas marcam e escanteios. "
        "No canal, use /proximo ou /jogos."
    )


def build_sources_status(settings: Settings) -> str:
    return (
        "Fontes conectadas:\n"
        f"- API-Football: {'configurada' if settings.rapidapi_key else 'nao configurada'}\n"
        f"- football-data.org: {'configurada' if settings.football_data_api_key else 'nao configurada'}\n"
        "- TheSportsDB: fallback gratuito ativo\n"
        "- StatsBomb Open Data: base historica gratuita"
    )


def build_sources_status_with_diagnostics(settings: Settings) -> str:
    diagnostics = get_sources_diagnostics(settings)
    return f"{build_sources_status(settings)}\n\nTeste rapido:\n" + "\n".join(diagnostics)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    await message.reply_text(
        "Comandos:\n"
        "/start - inicia o bot\n"
        "/help - mostra esta ajuda\n"
        "/proximo - busca proximos jogos futuros\n"
        "/jogos - lista jogos de hoje\n"
        "/status - testa fontes conectadas\n\n"
        "No privado ou grupo, tambem pode escrever: jogos de hoje, jogos de amanha, "
        "proximo jogo, apostas de hoje ou uma partida especifica.\n\n"
        "Em grupo, me mencione com @Betchatdo_bot ou responda uma mensagem minha."
    )


def should_answer_message(update: Update, bot_username: str | None) -> bool:
    message = update.message
    if not message or not message.text:
        return False
    if message.chat.type == "private":
        return True
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.is_bot
    ):
        return True
    if bot_username and f"@{bot_username.lower()}" in message.text.lower():
        return True
    return False


async def build_fixtures_reply(
    settings: Settings,
    user_text: str,
    wants_next_fixture: bool,
) -> str:
    fixtures_context: list[dict[str, Any]] | None
    fixtures_source = "football_api"
    target_date: str | None
    fallback_note = ""

    if wants_next_fixture:
        logging.info("Buscando proximos jogos")
        fixtures_context, fixtures_source, target_date = await asyncio.to_thread(
            get_next_fixtures_for_chat,
            settings,
        )
    else:
        now = get_current_datetime(settings.timezone)
        target_date = extract_date_from_message(
            user_text,
            now.strftime("%Y-%m-%d"),
            settings.timezone,
        )
        logging.info("Buscando jogos para o chat: %s", target_date)
        fixtures_context, fixtures_source = await asyncio.to_thread(
            get_fixtures_for_chat,
            settings,
            target_date,
        )
        fixtures_context = filter_future_fixtures(
            fixtures_context,
            settings.timezone,
            now,
        )

    if not fixtures_context:
        sources_status = build_sources_status(settings)
        if wants_next_fixture:
            return f"Nao encontrei proximos jogos futuros nas fontes conectadas.\n\n{sources_status}"
        fallback_note = (
            f"Nao encontrei partidas para {target_date} nas fontes conectadas. "
            "Busquei os proximos jogos futuros:"
        )
        fixtures_context, fixtures_source, target_date = await asyncio.to_thread(
            get_next_fixtures_for_chat,
            settings,
        )
        user_text = "proximos jogos futuros"
        if not fixtures_context:
            return (
                f"Nao encontrei partidas para {target_date} nem proximos jogos futuros "
                f"nas fontes conectadas.\n\n{sources_status}"
            )

    logging.info(
        "Encontrados %d jogos via %s para %s",
        len(fixtures_context),
        fixtures_source,
        target_date,
    )

    reply = await asyncio.to_thread(
        ask_llm_for_chat_reply,
        settings.llm_api_key,
        settings.llm_base_url,
        settings.llm_model,
        user_text,
        fixtures_context,
        fixtures_source,
        target_date,
    )
    return f"{fallback_note}\n\n{reply}".strip()


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    settings: Settings = context.application.bot_data["settings"]
    await message.chat.send_action("typing")

    try:
        reply = await build_fixtures_reply(settings, "proximo jogo", True)
    except Exception as exc:
        logging.error("Erro ao processar /proximo: %s", exc)
        reply = "Ocorreu um erro ao buscar os proximos jogos. Tente novamente em instantes."

    await reply_with_chunks(message, reply)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    settings: Settings = context.application.bot_data["settings"]
    await message.chat.send_action("typing")

    try:
        reply = await build_fixtures_reply(settings, "jogos de hoje", False)
    except Exception as exc:
        logging.error("Erro ao processar /jogos: %s", exc)
        reply = "Ocorreu um erro ao buscar os jogos de hoje. Tente novamente em instantes."

    await reply_with_chunks(message, reply)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    settings: Settings = context.application.bot_data["settings"]
    await message.chat.send_action("typing")
    reply = await asyncio.to_thread(build_sources_status_with_diagnostics, settings)
    await reply_with_chunks(message, reply)


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if not message or not message.text:
        return

    command = message.text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command not in {"/jogos", "/proximo", "/proximos", "/status"}:
        return

    settings: Settings = context.application.bot_data["settings"]
    await message.chat.send_action("typing")

    try:
        if command == "/status":
            reply = await asyncio.to_thread(build_sources_status_with_diagnostics, settings)
        elif command == "/jogos":
            reply = await build_fixtures_reply(settings, "jogos de hoje", False)
        else:
            reply = await build_fixtures_reply(settings, "proximo jogo", True)
    except Exception as exc:
        logging.error("Erro ao processar comando no canal %s: %s", command, exc)
        reply = "Ocorreu um erro ao buscar os jogos. Tente novamente em instantes."

    await reply_with_chunks(message, reply)


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    bot_username: str | None = context.application.bot_data.get("bot_username")

    if not update.message or not update.message.text:
        return

    if not should_answer_message(update, bot_username):
        return

    await update.message.chat.send_action("typing")

    user_text = update.message.text
    wants_next_fixture = message_wants_next_fixture(user_text)

    try:
        if message_wants_fixtures(user_text):
            reply = await build_fixtures_reply(settings, user_text, wants_next_fixture)
        else:
            reply = await asyncio.to_thread(
                ask_llm_for_chat_reply,
                settings.llm_api_key,
                settings.llm_base_url,
                settings.llm_model,
                user_text,
            )
    except Exception as exc:
        logging.error("Erro ao chamar a LLM: %s", exc)
        reply = "Ocorreu um erro ao processar sua mensagem. Tente novamente em instantes."

    await reply_with_chunks(update.message, reply)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    logging.info("Bot conversacional conectado como @%s", me.username)


def run_chat_bot(settings: Settings) -> None:
    application = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler(["proximo", "proximos"], next_command))
    application.add_handler(CommandHandler("jogos", today_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.TEXT, channel_post_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    logging.info("Iniciando modo chat por polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
