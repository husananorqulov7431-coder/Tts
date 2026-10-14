from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import uuid
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from processor import (
    MAX_VIDEO_MINUTES,
    ProcessingError,
    TRANSLATE_BY_DEFAULT,
    WORK_ROOT,
    process_video,
)

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tts-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(item.strip()) for item in ALLOWED_IDS_RAW.split(",")
    if item.strip().isdigit()
}
API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "http://telegram-bot-api:8081/bot")
API_FILE_URL = os.getenv("TELEGRAM_API_FILE_URL", "http://telegram-bot-api:8081/file/bot")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "uz-UZ-SardorNeural")
MAX_SRT_MB = int(os.getenv("MAX_SRT_MB", "15"))
BOT_RUNTIME_SECONDS = int(os.getenv("BOT_RUNTIME_SECONDS", "0"))
STOP_AFTER_JOB = os.getenv("STOP_AFTER_JOB", "false").lower() in {"1", "true", "yes", "on"}
INBOX_ROOT = Path(os.getenv("INBOX_DIR", "/work/inbox"))
INBOX_ROOT.mkdir(parents=True, exist_ok=True)
JOB_SEMAPHORE = asyncio.Semaphore(1)

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def _deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Bu botdan foydalanishga ruxsat berilmagan. Administrator ALLOWED_USER_IDS sozlamasini tekshirsin."
        )


async def _guard(update: Update) -> bool:
    if not _authorized(update):
        await _deny(update)
        return False
    return True


def _safe_youtube_url(text: str) -> str | None:
    match = URL_PATTERN.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,);]}>")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        return None
    if not parsed.path or parsed.path == "/":
        return None
    return url


def _new_state(user_id: int) -> dict:
    job_id = uuid.uuid4().hex
    job_dir = INBOX_ROOT / f"{user_id}-{job_id}"
    job_dir.mkdir(parents=True, exist_ok=False)
    return {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "srt_path": str(job_dir / "captions.srt"),
        "url": None,
        "started": False,
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    old = context.user_data.pop("pending_job", None)
    if old:
        shutil.rmtree(old.get("job_dir", ""), ignore_errors=True)
    context.user_data.setdefault("voice", DEFAULT_VOICE)
    context.user_data.setdefault("translate", TRANSLATE_BY_DEFAULT)
    await update.effective_message.reply_text(
        "YouTube videoni o‘zbekcha ovozlashtirish\n\n"
        "1/2 — avval YouTube video havolasini yuboring.\n"
        "2/2 — keyin aynan shu videoning .srt faylini yuboring.\n\n"
        "Inline tugmalar orqali erkak/ayol ovozini tanlang. Inglizcha SRT Google Translate orqali o‘zbekchaga tarjima qilinadi. Tarjimani o‘tkazib yuborish ham mumkin. AI tarjima modeli ishlatilmaydi.\n\n"
        "Asl audio 10% darajada qoladi, video oqimi qayta kodlanmaydi. Har bir vazifa uchun maksimal vaqt: 10 daqiqa.",
        reply_markup=_settings_keyboard(context),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.effective_message.reply_text(
        "Tartib qat’iy: 1) YouTube havolasi, 2) .srt fayli.\n"
        "Inline tugmalar erkak/ayol ovozi, Google Translate va tarjimani o‘tkazib yuborishni boshqaradi.\n"
        "Asl audio 10% darajada qoladi; video oqimi qayta kodlanmaydi. Vazifa 10 daqiqada tugamasa, vaqt limiti bo‘yicha to‘xtatiladi.",
        reply_markup=_settings_keyboard(context, include_cancel=bool(context.user_data.get("pending_job"))),
    )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    choice = (context.args[0].lower() if context.args else "").strip()
    if choice in {"female", "ayol", "madina"}:
        context.user_data["voice"] = "uz-UZ-MadinaNeural"
        name = "Madina (ayol)"
    elif choice in {"male", "erkak", "sardor"}:
        context.user_data["voice"] = "uz-UZ-SardorNeural"
        name = "Sardor (erkak)"
    else:
        await update.effective_message.reply_text("Ovoz tanlang: /voice male yoki /voice female")
        return
    await update.effective_message.reply_text(f"Keyingi video uchun ovoz: {name}.")


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    choice = (context.args[0].lower() if context.args else "").strip()
    if choice in {"on", "yes", "ha"}:
        context.user_data["translate"] = True
        message = "Google Translate yoqildi: subtitr o‘zbekchaga tarjima qilinadi."
    elif choice in {"off", "no", "yo‘q", "yoq"}:
        context.user_data["translate"] = False
        message = "Tarjima o‘chirildi: SRT asl holicha ovozlashtiriladi."
    else:
        current = "yoqilgan" if context.user_data.get("translate", TRANSLATE_BY_DEFAULT) else "o‘chirilgan"
        message = f"Tarjima hozir {current}. /translate on yoki /translate off."
    await update.effective_message.reply_text(message)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    state = context.user_data.pop("pending_job", None)
    if state:
        shutil.rmtree(state.get("job_dir", ""), ignore_errors=True)
        await update.effective_message.reply_text("Kutayotgan topshiriq bekor qilindi.")
    else:
        await update.effective_message.reply_text(
            "Kutilayotgan topshiriq yo‘q. Allaqachon render boshlangan bo‘lsa, /cancel uni to‘xtatmaydi."
        )


def _settings_keyboard(context: ContextTypes.DEFAULT_TYPE, *, include_cancel: bool = False) -> InlineKeyboardMarkup:
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    is_female = voice == "uz-UZ-MadinaNeural"
    translate = bool(context.user_data.get("translate", TRANSLATE_BY_DEFAULT))
    rows = [
        [
            InlineKeyboardButton(("✅ " if not is_female else "") + "👨 Erkak ovozi", callback_data="voice:male"),
            InlineKeyboardButton(("✅ " if is_female else "") + "👩 Ayol ovozi", callback_data="voice:female"),
        ],
        [
            InlineKeyboardButton(
                "🌐 Google tarjima: YOQILGAN" if translate else "🌐 Google tarjima: O‘CHIQ",
                callback_data="translate:toggle",
            ),
        ],
        [
            InlineKeyboardButton(
                "⏭ Tarjimani o‘tkazib yuborish" if translate else "🔁 Google tarjimani yoqish",
                callback_data="translate:skip" if translate else "translate:on",
            ),
        ],
    ]
    if include_cancel:
        rows.append([InlineKeyboardButton("❌ Kutayotgan topshiriqni bekor qilish", callback_data="job:cancel")])
    return InlineKeyboardMarkup(rows)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not _authorized(update):
        await query.answer("Bu botdan foydalanishga ruxsat yo‘q.", show_alert=True)
        return

    action = query.data or ""
    if action == "voice:male":
        context.user_data["voice"] = "uz-UZ-SardorNeural"
        notice = "Erkak ovozi tanlandi: Sardor."
    elif action == "voice:female":
        context.user_data["voice"] = "uz-UZ-MadinaNeural"
        notice = "Ayol ovozi tanlandi: Madina."
    elif action == "translate:toggle":
        context.user_data["translate"] = not bool(context.user_data.get("translate", TRANSLATE_BY_DEFAULT))
        notice = "Google Translate sozlamasi o‘zgartirildi."
    elif action == "translate:skip":
        context.user_data["translate"] = False
        notice = "Tarjima o‘chirildi. SRT asl holicha ovozlashtiriladi."
    elif action == "translate:on":
        context.user_data["translate"] = True
        notice = "Google Translate orqali tarjima yoqildi."
    elif action == "job:cancel":
        state = context.user_data.pop("pending_job", None)
        if state:
            shutil.rmtree(state.get("job_dir", ""), ignore_errors=True)
            notice = "Kutayotgan topshiriq bekor qilindi."
        else:
            notice = "Bekor qilinadigan kutayotgan topshiriq yo‘q."
        await query.answer(notice)
        if query.message:
            await query.edit_message_text(notice)
        return
    else:
        await query.answer()
        return

    await query.answer(notice)
    if query.message:
        try:
            await query.edit_message_reply_markup(
                reply_markup=_settings_keyboard(
                    context,
                    include_cancel=bool(context.user_data.get("pending_job")),
                )
            )
        except TelegramError:
            log.debug("Inline keyboard refresh failed", exc_info=True)

async def _maybe_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.user_data.get("pending_job")
    if not state or state.get("started"):
        return
    url = state.get("url")
    srt_path = Path(state.get("srt_path", ""))
    if not url or not srt_path.is_file() or srt_path.stat().st_size == 0:
        return

    state["started"] = True
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    voice = context.user_data.get("voice", DEFAULT_VOICE)
    translate = bool(context.user_data.get("translate", TRANSLATE_BY_DEFAULT))
    staged_dir = state["job_dir"]
    staged_srt = str(srt_path)
    job_url = url
    context.user_data.pop("pending_job", None)

    await update.effective_message.reply_text(
        "Topshiriq qabul qilindi. Video qayta ishlash serverida navbatga qo‘yiladi."
    )
    asyncio.create_task(
        _run_job(
            context.application, chat_id, user_id, job_url, Path(staged_srt),
            staged_dir, voice, translate,
        )
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    message = update.effective_message
    text = message.text or ""
    url = _safe_youtube_url(text)
    if not url:
        await message.reply_text("Avval YouTube havolasini yuboring (youtube.com yoki youtu.be).")
        return

    old_state = context.user_data.pop("pending_job", None)
    if old_state:
        shutil.rmtree(old_state.get("job_dir", ""), ignore_errors=True)
    state = _new_state(update.effective_user.id)
    state["url"] = url
    context.user_data["pending_job"] = state
    await message.reply_text(
        "✅ 1/2 — YouTube havolasi saqlandi.\n"
        "Endi 2/2: aynan shu videoga tegishli .srt faylini yuboring.\n"
        "Ovoz va tarjima sozlamalarini pastdagi tugmalardan o‘zgartirishingiz mumkin.",
        reply_markup=_settings_keyboard(context, include_cancel=True),
    )
    await _maybe_queue(update, context)

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    message = update.effective_message
    document = message.document
    if not document:
        return
    filename = (document.file_name or "").lower()
    if not filename.endswith(".srt"):
        await message.reply_text("Faqat .srt subtitr faylini yuboring.")
        return
    if document.file_size and document.file_size > MAX_SRT_MB * 1024 * 1024:
        await message.reply_text(f"SRT fayli {MAX_SRT_MB} MB dan kichik bo‘lishi kerak.")
        return

    state = context.user_data.get("pending_job")
    if not state or not state.get("url"):
        await message.reply_text(
            "Tartib bo‘yicha avval YouTube linkini yuboring. Shundan keyingina .srt faylini qabul qilaman."
        )
        return

    staged_dir = Path(state["job_dir"])
    staged_dir.mkdir(parents=True, exist_ok=True)
    target = Path(state["srt_path"])
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        source_path = Path(str(telegram_file.file_path or ""))
        if source_path.is_file():
            shutil.copy2(source_path, target)
        else:
            downloaded = await telegram_file.download_to_drive(custom_path=str(target))
            downloaded_path = Path(str(downloaded))
            if downloaded_path.is_file() and downloaded_path.resolve() != target.resolve():
                shutil.copy2(downloaded_path, target)
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError("SRT fayli saqlanmadi")
    except Exception:
        log.exception("Could not save incoming SRT file")
        await message.reply_text("SRT faylini saqlab bo‘lmadi. Faylni yana bir marta yuboring.")
        return

    await message.reply_text(
        "✅ 2/2 — SRT fayli qabul qilindi. "
        + ("Google Translate yoqilgan. " if context.user_data.get("translate", TRANSLATE_BY_DEFAULT) else "Tarjima o‘tkazib yuboriladi. ")
        + ("Ovoz: Madina (ayol)." if context.user_data.get("voice", DEFAULT_VOICE) == "uz-UZ-MadinaNeural" else "Ovoz: Sardor (erkak).")
    )
    await _maybe_queue(update, context)

async def _run_job(
    application: Application,
    chat_id: int,
    user_id: int,
    url: str,
    srt_path: Path,
    staged_dir: str,
    voice: str,
    translate: bool,
) -> None:
    status_message = None
    rendered_dir: Path | None = None
    try:
        status_message = await application.bot.send_message(
            chat_id=chat_id,
            text="Ish boshlandi. Video yuklanadi, keyin subtitrlar ovozlashtiriladi.",
        )

        async def progress(text: str) -> None:
            if status_message is None:
                return
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text=text[:3900],
                )
            except BadRequest as exc:
                if "Message is not modified" not in str(exc):
                    log.debug("Unable to edit progress message: %s", exc)
            except TelegramError:
                log.exception("Progress update failed")

        async with JOB_SEMAPHORE:
            rendered_dir, output_path = await process_video(
                url=url,
                srt_path=srt_path,
                voice=voice,
                translate=translate,
                progress=progress,
            )

        await progress("Render tugadi. Yakuniy videoni Telegram’ga yuklayapman…")
        size_mb = output_path.stat().st_size / (1024 * 1024)
        caption = (
            "O‘zbekcha ovozlashtirish tayyor.\n"
            f"Video: {size_mb:.0f} MB\n"
            f"Ovoz: {voice}\n"
            "Asl audio: 10% • Video oqimi: qayta kodlanmagan"
        )
        with output_path.open("rb") as stream:
            await application.bot.send_video(
                chat_id=chat_id,
                video=InputFile(stream, filename="uzbek_dub.mp4"),
                caption=caption,
                supports_streaming=True,
                reply_to_message_id=status_message.message_id if status_message else None,
                read_timeout=900,
                write_timeout=900,
                connect_timeout=60,
                pool_timeout=60,
            )
        if status_message:
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text="✅ Vazifa tugadi. Yakuniy video ushbu xabarga reply sifatida yuborildi.",
                )
            except TelegramError:
                pass
    except ProcessingError as exc:
        log.warning("Job failed for user %s: %s", user_id, exc)
        try:
            if status_message:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text=f"Xatolik: {exc}"[:3900],
                )
            else:
                await application.bot.send_message(chat_id=chat_id, text=f"Xatolik: {exc}"[:3900])
        except TelegramError:
            pass
    except Exception:
        log.exception("Unhandled job error for user %s", user_id)
        try:
            if status_message:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text="Kutilmagan xatolik yuz berdi. Server loglarini tekshiring va qayta urinib ko‘ring.",
                )
            else:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="Kutilmagan xatolik yuz berdi. Server loglarini tekshiring va qayta urinib ko‘ring.",
                )
        except TelegramError:
            pass
    finally:
        if rendered_dir:
            shutil.rmtree(rendered_dir, ignore_errors=True)
        shutil.rmtree(staged_dir, ignore_errors=True)
        if STOP_AFTER_JOB and application.running:
            log.info("One-shot job finished; stopping bot process.")
            application.stop_running()


async def _stop_after_runtime(application: Application) -> None:
    await asyncio.sleep(BOT_RUNTIME_SECONDS)
    if application.running:
        log.info("Configured bot runtime limit reached; stopping bot process.")
        application.stop_running()


async def _post_init(application: Application) -> None:
    if BOT_RUNTIME_SECONDS > 0:
        asyncio.create_task(_stop_after_runtime(application), name="runtime-shutdown-timer")


def _wait_for_local_api() -> None:
    # The local Bot API container may need a few seconds to initialize.
    endpoint = f"{API_BASE_URL.rstrip('/')}{TOKEN}/getMe"
    for _ in range(60):
        try:
            with urlopen(endpoint, timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Telegram Bot API serveri 60 soniyada ishga tushmadi. docker compose logs telegram-bot-api ni tekshiring.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN .env/secret ichida kiritilmagan.")
    if not ALLOWED_USER_IDS:
        raise RuntimeError("ALLOWED_USER_IDS kiritilmagan. Xavfsizlik uchun ruxsatsiz foydalanish bloklandi.")

    _wait_for_local_api()
    application = (
        ApplicationBuilder()
        .post_init(_post_init)
        .token(TOKEN)
        .base_url(API_BASE_URL)
        .base_file_url(API_FILE_URL)
        .local_mode(True)
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .media_write_timeout(900)
        .pool_timeout(60)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("translate", translate_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(
        settings_callback,
        pattern=r"^(voice:(male|female)|translate:(toggle|skip|on)|job:cancel)$",
    ))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting. Authorized user count: %d", len(ALLOWED_USER_IDS))
    application.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
