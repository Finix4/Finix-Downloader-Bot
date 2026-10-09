from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from finix_bot.downloader import (
    DownloadProblem,
    DurationLimitExceeded,
    FileTooLarge,
    ProcessingFailed,
    PrivateOrUnavailable,
    download_media,
)

BOT_NAME = "Finix"
BOT_USERNAME = "Finix_Downloader_bot"
CAPTION = "تم التحميل بواسطة بوت Finix 🚀 | @Finix_Downloader_bot"
PROCESSING_TEXT = "⏳ جاري معالجة وتحميل الفيديو عبر Finix..."
SHARE_URL = "https://t.me/share/url?url=https://t.me/Finix_Downloader_bot"
ABOUT_TEXT = (
    "حول بوت Finix\n\n"
    "بوت Finix يساعدك على تنزيل الفيديوهات واستخراج الصوت من الروابط العامة "
    "على TikTok وInstagram وYouTube وFacebook.\n\n"
    "• يدعم مقاطع Reels والمنشورات وYouTube Shorts والفيديوهات.\n"
    "• الحد الأقصى لمدة الفيديو هو 60 دقيقة.\n"
    "• يضيف علامة مائية بسيطة إلى الفيديوهات.\n"
    "• يمكنك استخراج الصوت بصيغة MP3.\n\n"
    "يجب أن يكون الرابط متاحاً للعامة. لا يمكن تنزيل المحتوى الخاص أو المحذوف."
)
WELCOME_TEXT = (
    "أهلاً بك في بوت Finix!\n\n"
    "أرسل رابط فيديو عام من TikTok أو Instagram أو YouTube أو Facebook، "
    "وسأجهزه لك للتنزيل. الحد الأقصى لمدة الفيديو 60 دقيقة."
)
MAX_PARALLEL_DOWNLOADS = 3
REQUEST_TTL_SECONDS = 2 * 60 * 60
REQUESTS_KEY = "finix_download_requests"
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
BASE_HOSTS = {
    "tiktok.com",
    "instagram.com",
    "instagr.am",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("finix_bot")


@dataclass
class DownloadRequest:
    url: str
    owner_id: int
    created_at: float
    ready: bool = False


def _request_store(application: Application) -> dict[str, DownloadRequest]:
    return application.bot_data.setdefault(REQUESTS_KEY, {})


def _prune_requests(requests: dict[str, DownloadRequest]) -> None:
    cutoff = time.time() - REQUEST_TTL_SECONDS
    for request_id, item in list(requests.items()):
        if item.created_at < cutoff:
            requests.pop(request_id, None)


def _keyboard(request_id: str | None = None) -> InlineKeyboardMarkup:
    audio_target = request_id or "none"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "تحويل إلى صوت MP3",
                    callback_data=f"audio:{audio_target}",
                )
            ],
            [
                InlineKeyboardButton("حول البوت", callback_data="about"),
                InlineKeyboardButton("مشاركة مع الأصدقاء", url=SHARE_URL),
            ],
        ]
    )


def _valid_supported_url(raw_url: str) -> bool:
    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        if parsed.username or parsed.password or parsed.port:
            return False
        return any(host == base or host.endswith(f".{base}") for base in BASE_HOSTS)
    except ValueError:
        return False


def _first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0).rstrip(".,!?;:،؛)]}»")


def _error_text(problem: Exception) -> str:
    if isinstance(problem, DurationLimitExceeded):
        return (
            "عذراً، مدة الفيديو تتجاوز الحد المسموح به. "
            "يدعم Finix الفيديوهات حتى 60 دقيقة فقط."
        )
    if isinstance(problem, FileTooLarge):
        return (
            "حجم الملف كبير جداً لإرساله عبر تيليجرام. "
            "الحد الأقصى المدعوم هو 50 ميغابايت."
        )
    if isinstance(problem, ProcessingFailed):
        return "تعذرت معالجة الفيديو حالياً. حاول استخدام رابط آخر أو أعد المحاولة لاحقاً."
    if isinstance(problem, PrivateOrUnavailable):
        return (
            "تعذّر الوصول إلى الرابط. قد يكون خاصاً أو محذوفاً أو يتطلب تسجيل الدخول. "
            "تأكد أن الرابط متاح للعامة ثم أرسله مجدداً."
        )
    return "حدث خطأ أثناء معالجة الرابط. تأكد من صحته ثم حاول مجدداً."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(WELCOME_TEXT, reply_markup=_keyboard())


async def about_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if query.message is not None:
        await query.message.reply_text(ABOUT_TEXT, reply_markup=_keyboard())


async def about_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(ABOUT_TEXT, reply_markup=_keyboard())


async def _complete_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    request_id: str,
    mode: str,
    status_message,
) -> None:
    user = update.effective_user
    records = _request_store(context.application)
    try:
        async with DOWNLOAD_SEMAPHORE:
            with tempfile.TemporaryDirectory(prefix="finix-") as temp_dir:
                file_path = await asyncio.to_thread(
                    download_media,
                    url,
                    temp_dir,
                    mode,
                )
                with open(file_path, "rb") as media_file:
                    upload = InputFile(
                        media_file,
                        filename="Finix-Audio.mp3"
                        if mode == "audio"
                        else "Finix-Video.mp4",
                    )
                    if mode == "audio":
                        await context.bot.send_audio(
                            chat_id=update.effective_chat.id,
                            audio=upload,
                            caption=CAPTION,
                            title="Finix Bot",
                            reply_markup=_keyboard(request_id),
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            video=upload,
                            caption=CAPTION,
                            supports_streaming=True,
                            reply_markup=_keyboard(request_id),
                        )
        await _remove_status_message(status_message)
        records[request_id].ready = True
    except DownloadProblem as problem:
        await _show_error(status_message, _error_text(problem), request_id)
    except (BadRequest, TelegramError) as problem:
        text = str(problem).lower()
        if "file is too big" in text or "file size" in text or "too large" in text:
            friendly = _error_text(FileTooLarge())
        else:
            friendly = "تعذر إرسال الملف عبر تيليجرام. حاول مرة أخرى أو أرسل رابطاً آخر."
        await _show_error(status_message, friendly, request_id)
    except Exception as problem:
        logger.error("Download failed for user_id=%s (%s)", user.id if user else "unknown", type(problem).__name__)
        await _show_error(
            status_message,
            "حدث خطأ أثناء معالجة الرابط. حاول مجدداً بعد قليل.",
            request_id,
        )


async def _remove_status_message(status_message) -> None:
    try:
        await status_message.delete()
    except TelegramError:
        try:
            await status_message.edit_text("✅ تم إرسال الملف بنجاح.")
        except TelegramError:
            pass


async def _show_error(status_message, text: str, request_id: str) -> None:
    try:
        await status_message.edit_text(text, reply_markup=_keyboard(request_id))
    except TelegramError:
        try:
            await status_message.reply_text(text, reply_markup=_keyboard(request_id))
        except TelegramError:
            pass


async def handle_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None or message.text is None:
        return

    url = _first_url(message.text)
    if url is None:
        await message.reply_text(
            "أرسل رابط فيديو من TikTok أو Instagram أو YouTube أو Facebook.",
            reply_markup=_keyboard(),
        )
        return
    if not _valid_supported_url(url):
        await message.reply_text(
            "هذا الرابط غير مدعوم. أرسل رابطاً عاماً من TikTok أو Instagram أو YouTube أو Facebook.",
            reply_markup=_keyboard(),
        )
        return

    request_id = uuid.uuid4().hex[:16]
    records = _request_store(context.application)
    _prune_requests(records)
    records[request_id] = DownloadRequest(
        url=url,
        owner_id=user.id,
        created_at=time.time(),
    )

    status_message = await message.reply_text(
        PROCESSING_TEXT,
        reply_markup=_keyboard(request_id),
    )
    await _complete_download(
        update,
        context,
        url,
        request_id,
        "video",
        status_message,
    )


async def audio_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None:
        return

    _, _, request_id = (query.data or "").partition(":")
    records = _request_store(context.application)
    _prune_requests(records)
    item = records.get(request_id)

    if request_id == "none" or item is None:
        await query.answer(
            "أرسل رابط فيديو أولاً، ثم اضغط على زر تحويل إلى MP3.",
            show_alert=True,
        )
        return
    if user is None or user.id != item.owner_id:
        await query.answer("هذا الزر متاح للشخص الذي أرسل الرابط فقط.", show_alert=True)
        return
    if not item.ready:
        await query.answer("ما زال الفيديو قيد المعالجة، حاول بعد قليل.", show_alert=True)
        return
    if chat is None or query.message is None:
        return

    status_message = await query.message.reply_text("⏳ جاري استخراج الصوت بصيغة MP3...")
    audio_request_id = uuid.uuid4().hex[:16]
    records[audio_request_id] = DownloadRequest(
        url=item.url,
        owner_id=user.id,
        created_at=time.time(),
    )
    await _complete_download(
        update,
        context,
        item.url,
        audio_request_id,
        "audio",
        status_message,
    )


async def unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "استخدم /start لبدء المحادثة، ثم أرسل رابط فيديو عام.",
            reply_markup=_keyboard(),
        )


async def on_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    error = context.error
    logger.error(
        "Unhandled Telegram update error (%s)",
        type(error).__name__ if error is not None else "unknown",
    )


def main() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add the bot token in Replit Secrets."
        )

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .connection_pool_size(16)
        .pool_timeout(30)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(about_button, pattern=r"^about$"))
    application.add_handler(CallbackQueryHandler(audio_button, pattern=r"^audio:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)

    logger.info("%s bot is starting in polling mode", BOT_NAME)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
