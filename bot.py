"""Telegram bot: an audio attachment in, a playable voice message out."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv
from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from converter import (
    MAX_INPUT_BYTES, ConversionError, Tools, check_tools, convert_to_voice,
    is_audio_document, resolve_tools,
)

PROJECT_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("voice_bot")
HELP_TEXT = (
    "Привет! Пришлите музыку или аудиозапись — я верну её голосовым сообщением.\n\n"
    "MP3 можно отправить как музыку, переслать из другого чата или прикрепить как файл. "
    "Также подходят M4A, WAV, OGG, FLAC и другие аудиоформаты.\n\n"
    "Размер — до 20 МБ, длительность — до 60 минут. "
    "Содержание и скорость записи сохраняются; звук сжимается в формат голосового.\n\n"
    "Готовое голосовое можно переслать в любой чат.\n"
    "/help — инструкция\n/id — ваш Telegram ID"
)


@dataclass
class Runtime:
    tools: Tools
    allowed_user_ids: frozenset[int]
    active_users: set[int] = field(default_factory=set)
    max_jobs: int = 2


def parse_allowed_users(value: str) -> frozenset[int]:
    try:
        values = frozenset(int(item.strip()) for item in value.split(",") if item.strip())
        if any(user_id <= 0 for user_id in values):
            raise ValueError
        return values
    except ValueError as error:
        raise ValueError("ALLOWED_USER_IDS должен содержать положительные ID через запятую.") from error


async def access_allowed(update: Update, runtime: Runtime) -> bool:
    user = update.effective_user
    if not user or not update.effective_message:
        return False
    if runtime.allowed_user_ids and user.id not in runtime.allowed_user_ids:
        await update.effective_message.reply_text("Этот бот доступен только владельцу.")
        return False
    return True


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await access_allowed(update, context.bot_data["runtime"]):
        await update.effective_message.reply_text(HELP_TEXT)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def update_status(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except TelegramError:
        # A deleted status message must not prevent conversion or delivery.
        pass


async def remove_status(status: Message) -> None:
    try:
        await status.delete()
    except TelegramError:
        pass


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: Runtime = context.bot_data["runtime"]
    if not await access_allowed(update, runtime):
        return
    message = update.effective_message
    user_id = update.effective_user.id
    attachment = message.audio or message.voice or message.document
    if message.document and not is_audio_document(
        message.document.file_name, message.document.mime_type
    ):
        await message.reply_text("Пришлите аудиофайл: например MP3, M4A, WAV, OGG или FLAC.")
        return
    if attachment.file_size is not None and attachment.file_size > MAX_INPUT_BYTES:
        await message.reply_text("Файл слишком большой. Отправьте аудио до 20 МБ.")
        return
    if user_id in runtime.active_users:
        await message.reply_text("Ваша запись ещё обрабатывается. Дождитесь готового голосового.")
        return
    if len(runtime.active_users) >= runtime.max_jobs:
        await message.reply_text("Сейчас обрабатываю другие записи. Пришлите файл чуть позже.")
        return

    # Updates share one event loop: claim a slot before the first await.
    runtime.active_users.add(user_id)
    status = None
    try:
        status = await message.reply_text("Скачиваю запись…")
        with TemporaryDirectory(prefix="telegram_voice_") as temporary_dir:
            source = Path(temporary_dir) / "input.audio"
            destination = Path(temporary_dir) / "voice.ogg"
            remote_file = await context.bot.get_file(attachment.file_id, read_timeout=30)
            if remote_file.file_size is not None and remote_file.file_size > MAX_INPUT_BYTES:
                raise ConversionError("Файл слишком большой. Отправьте аудио до 20 МБ.")
            await remote_file.download_to_drive(custom_path=source, read_timeout=120)
            await update_status(status, "Преобразую в голосовое…")
            result = await asyncio.to_thread(convert_to_voice, source, destination, runtime.tools)
            await update_status(status, "Отправляю голосовое…")
            try:
                await context.bot.send_chat_action(
                    chat_id=message.chat_id,
                    action=ChatAction.UPLOAD_VOICE,
                    message_thread_id=message.message_thread_id,
                )
            except TelegramError:
                pass
            with result.path.open("rb") as voice:
                await message.reply_voice(
                    voice=voice,
                    filename="voice.ogg",
                    duration=result.duration,
                    do_quote=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
        await remove_status(status)
    except ConversionError as error:
        if status:
            await update_status(status, str(error))
        else:
            await message.reply_text(str(error))
    except NetworkError:
        LOGGER.warning("Network failure while processing audio")
        if status:
            await update_status(status, "Ошибка связи с Telegram. Попробуйте отправить файл ещё раз.")
    except BadRequest as error:
        LOGGER.warning("Telegram rejected an audio operation (%s)", type(error).__name__)
        description = str(error).lower()
        text = (
            "Telegram не позволяет боту скачать этот файл. Отправьте аудио меньше 20 МБ."
            if "too big" in description or "too large" in description
            else "Telegram не смог принять голосовое. Проверьте файл и отправьте его ещё раз."
        )
        if status:
            await update_status(status, text)
    except (TelegramError, OSError):
        LOGGER.warning("Audio processing or delivery failed")
        if status:
            await update_status(status, "Не удалось обработать запись. Попробуйте ещё раз.")
    finally:
        runtime.active_users.discard(user_id)


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await access_allowed(update, context.bot_data["runtime"]):
        await update.effective_message.reply_text("Пришлите музыку или аудиофайл — я сделаю голосовое. /help")


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Начать"),
        BotCommand("help", "Как преобразовать запись"),
        BotCommand("id", "Мой Telegram ID"),
    ])
    LOGGER.info("Бот запущен. Отправьте ему MP3 в Telegram. Для остановки нажмите Ctrl+C.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # HTTP exception text and request URLs can contain the bot token.
    error_name = type(context.error).__name__
    LOGGER.error("Ошибка обработчика: %s", error_name)
    if isinstance(context.error, Conflict):
        LOGGER.error("Уже запущен другой экземпляр этого бота. Остановите его.")
        context.application.stop_running()
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Не удалось обработать сообщение. Попробуйте ещё раз.")
        except TelegramError:
            pass


def build_application(token: str, runtime: Runtime) -> Application:
    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .connection_pool_size(16)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .post_init(post_init)
        .build()
    )
    application.bot_data["runtime"] = runtime
    private = filters.ChatType.PRIVATE
    application.add_handler(CommandHandler(["start", "help"], help_command, filters=private))
    application.add_handler(CommandHandler("id", id_command, filters=private))
    application.add_handler(MessageHandler(
        private & (filters.AUDIO | filters.VOICE | filters.Document.ALL), handle_audio
    ))
    application.add_handler(MessageHandler(private & filters.ALL, fallback))
    application.add_error_handler(handle_error)
    return application


def main() -> int:
    parser = argparse.ArgumentParser(description="MP3 → голосовое сообщение Telegram")
    parser.add_argument("--check", action="store_true", help="Проверить FFmpeg без подключения к Telegram")
    arguments = parser.parse_args()
    load_dotenv(PROJECT_DIR / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        tools = resolve_tools(os.getenv("FFMPEG_PATH", "ffmpeg"), os.getenv("FFPROBE_PATH", "ffprobe"))
        check_tools(tools)
        allowed_users = parse_allowed_users(os.getenv("ALLOWED_USER_IDS", ""))
        token = os.getenv("BOT_TOKEN", "").strip()
        if arguments.check:
            print("FFmpeg, FFprobe и libopus готовы.")
            print("Токен настроен." if token else "Токен пока не указан. Добавьте BOT_TOKEN в .env.")
            return 0
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            print("Добавьте BOT_TOKEN в .env. Получите токен у https://t.me/BotFather через /newbot.")
            return 1
        runtime = Runtime(tools=tools, allowed_user_ids=allowed_users)
        application = build_application(token, runtime)
        application.run_polling(
            allowed_updates=["message"],
            drop_pending_updates=False,
            stop_signals=None if os.name == "nt" else (2, 15, 6),
        )
        return 0
    except (ConversionError, ValueError) as error:
        print(f"Ошибка настройки: {error}", file=sys.stderr)
    except InvalidToken:
        print("Telegram отклонил токен. Проверьте BOT_TOKEN в .env.", file=sys.stderr)
    except Conflict:
        print("Уже запущен другой экземпляр бота. Остановите его.", file=sys.stderr)
    except TelegramError as error:
        print(f"Не удалось подключиться к Telegram ({type(error).__name__}). Проверьте интернет и токен.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
