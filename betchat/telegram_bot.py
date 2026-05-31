import asyncio
import logging

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .settings import Settings, get_current_datetime
from .fixtures import get_fixtures_for_chat, message_wants_fixtures, extract_date_from_message
from .llm import ask_llm_for_chat_reply


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
    """Versão síncrona segura — cria novo event loop para evitar conflito com o loop do bot."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_to_telegram(token, chat_id, message))
    finally:
        loop.close()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "BetChat online. Analiso jogos com foco em gols, ambas marcam e escanteios. "
        "Pergunte sobre jogos de hoje, amanhã ou qualquer partida específica."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Comandos:\n"
        "/start - inicia o bot\n"
        "/help - mostra esta ajuda\n\n"
        "Exemplos de perguntas:\n"
        "- Jogos de hoje\n"
        "- Jogos de amanhã\n"
        "- Apostas de hoje\n"
        "- Vasco x Flamengo, analisa\n"
        "- Real Madrid x Barcelona escanteios\n\n"
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


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    bot_username: str | None = context.application.bot_data.get("bot_username")

    if not update.message or not update.message.text:
        return

    if not should_answer_message(update, bot_username):
        return

    await update.message.chat.send_action("typing")

    user_text = update.message.text
    fixtures_context = None
    fixtures_source = "football_api"
    target_date = None

    if message_wants_fixtures(user_text):
        target_date = extract_date_from_message(
            user_text,
            get_current_datetime(settings.timezone).strftime("%Y-%m-%d"),
            settings.timezone,
        )
        logging.info("Buscando jogos para o chat: %s", target_date)
        fixtures_context, fixtures_source = await asyncio.to_thread(
            get_fixtures_for_chat, settings, target_date
        )
        if fixtures_context:
            logging.info(
                "Encontrados %d jogos via %s para %s",
                len(fixtures_context), fixtures_source, target_date,
            )

    try:
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
    except Exception as exc:
        logging.error("Erro ao chamar a LLM: %s", exc)
        await update.message.reply_text(
            "Ocorreu um erro ao processar sua mensagem. Tente novamente em instantes."
        )
        return

    for chunk in split_message(reply):
        await update.message.reply_text(chunk)


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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    logging.info("Iniciando modo chat por polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
