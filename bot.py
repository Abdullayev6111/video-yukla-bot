import os
import re
import uuid
import shutil
import tempfile
import logging
import asyncio
import urllib.parse
from aiohttp import web

from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN .env faylida topilmadi.")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "telegram_video_bot"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

FFMPEG_PATH = shutil.which("ffmpeg")

if not FFMPEG_PATH:
    raise RuntimeError(
        "FFmpeg topilmadi. FFmpeg PATH sozlamasini tekshiring."
    )

# Vaqtinchalik URL'lar: key = tasodifiy ID
pending_downloads: dict[str, dict] = {}

# Bir foydalanuvchi bir vaqtning o'zida ko'p yuklash boshlamasin
active_users: set[int] = set()



async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    return runner

# =========================================================
# URL VALIDATION
# =========================================================

def get_platform(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url.strip())
        hostname = (parsed.hostname or "").lower()

        if parsed.scheme not in ("http", "https"):
            return None

        if hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
        }:
            return "youtube"

        if hostname in {
            "instagram.com",
            "www.instagram.com",
        }:
            return "instagram"

        return None
    except (ValueError, TypeError):
        return None


# =========================================================
# YOUTUBE FORMATLARINI ANIQLASH
# =========================================================


async def get_youtube_formats(url: str) -> list[dict]:
    def _extract() -> list[dict]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 20,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        all_formats = info.get("formats", [])

        # Audio oqimlarini aniqlash
        audio_formats = [
            fmt for fmt in all_formats
            if fmt.get("acodec") not in (None, "none")
            and fmt.get("vcodec") in (None, "none")
        ]

        best_audio = max(
            audio_formats,
            key=lambda fmt: (
                fmt.get("filesize")
                or fmt.get("filesize_approx")
                or 0
            ),
            default=None,
        )

        audio_size = (
            (best_audio.get("filesize")
             or best_audio.get("filesize_approx")
             or 0)
            if best_audio else 0
        )

        # Har bir sifat uchun bitta eng mos format
        best_by_height = {}

        for fmt in all_formats:
            height = fmt.get("height")
            format_id = fmt.get("format_id")
            ext = fmt.get("ext")
            vcodec = fmt.get("vcodec")

            if not height or not format_id:
                continue

            if ext not in {"mp4", "webm"}:
                continue

            if vcodec in (None, "none"):
                continue

            has_audio = fmt.get("acodec") not in (None, "none")

            video_size = (
                fmt.get("filesize")
                or fmt.get("filesize_approx")
                or 0
            )

            total_size = (
                video_size
                if has_audio
                else video_size + audio_size
            )

            # MP4 va audio bilan tayyor formatlarga ustunlik
            score = (
                ext == "mp4",
                has_audio,
                video_size > 0,
                video_size,
            )

            item = {
                "format_id": format_id,
                "height": height,
                "ext": ext,
                "filesize": total_size or None,
                "has_audio": has_audio,
            }

            current = best_by_height.get(height)

            if current is None or score > current["_score"]:
                item["_score"] = score
                best_by_height[height] = item

        formats = list(best_by_height.values())

        for fmt in formats:
            fmt.pop("_score", None)

        return sorted(
            formats,
            key=lambda item: item["height"],
        )

    return await asyncio.to_thread(_extract)



# =========================================================
# YOUTUBE FORMAT TUGMALARI
# =========================================================


def formats_keyboard(
    request_id: str,
    formats: list[dict],
) -> InlineKeyboardMarkup:
    buttons = []

    for fmt in formats:
        height = fmt["height"]
        size = fmt["filesize"]

        if height >= 2160:
            quality_label = "4K"
        elif height >= 1440:
            quality_label = "2K"
        else:
            quality_label = f"{height}p"

        size_text = (
            f" · {size / (1024 * 1024):.0f} MB"
            if size else ""
        )

        buttons.append(
            InlineKeyboardButton(
                text=f"📹 {quality_label}{size_text}",
                callback_data=(
                    f'format:{request_id}:{fmt["format_id"]}'
                ),
            )
        )

    # Har qatorda 3 ta tugma
    rows = [
        buttons[i:i + 3]
        for i in range(0, len(buttons), 3)
    ]

    return InlineKeyboardMarkup(inline_keyboard=rows)




# =========================================================
# VIDEO DOWNLOADER
# =========================================================

async def download_video(
    url: str,
    platform: str,
    quality: int | None = None,
    format_id: str | None = None,
    has_audio: bool = False,
) -> Path:
    tmp_dir = Path(
        tempfile.mkdtemp(prefix="video_bot_", dir=DOWNLOAD_ROOT)
    )

    def _download() -> Path:
        try:
            base_opts = {
                "outtmpl": str(tmp_dir / "%(title).100s_%(id)s.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "retries": 3,
                "fragment_retries": 3,
                "ignoreerrors": False,
                "cachedir": False,
                "noplaylist": True,
                "socket_timeout": 30,
                "ffmpeg_location": FFMPEG_PATH,
            }

            if platform == "youtube":
                if not format_id:
                    raise RuntimeError(
                        "YouTube formati tanlanmagan."
                    )

                format_selector = (
                    format_id
                    if has_audio
                    else f"{format_id}+bestaudio"
                )

                opts = {
                    **base_opts,
                    "format": format_selector,
                    "merge_output_format": "mp4",
                }

                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)

                files = [
                    p for p in tmp_dir.iterdir()
                    if p.is_file()
                    and p.stat().st_size > 0
                    and p.suffix.lower() not in {
                        ".part", ".ytdl", ".temp", ".json"
                    }
                ]

                if not files:
                    raise RuntimeError(
                        "Yuklangan YouTube video fayli topilmadi."
                    )

                return max(
                    files,
                    key=lambda p: p.stat().st_mtime,
                )


            # Instagram: sifat tanlash yo'q.
            # Sayt taqdim etgan eng yaxshi mavjud format.
            opts = {
                **base_opts,
                "format": "best",
            }

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

            files = [
                p for p in tmp_dir.iterdir()
                if p.is_file()
                and p.stat().st_size > 0
                and p.suffix.lower() not in {
                    ".part", ".ytdl", ".temp"
                }
            ]

            if not files:
                raise RuntimeError(
                    "Instagram video fayli topilmadi."
                )

            return max(
                files,
                key=lambda p: p.stat().st_mtime,
            )

        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    return await asyncio.to_thread(_download)


# =========================================================
# SEND VIDEO AND CLEANUP
# =========================================================

async def process_video(
    message: types.Message,
    url: str,
    platform: str,
    quality: int | None = None,
    status_message: types.Message | None = None,
    format_id: str | None = None,
    has_audio: bool = False
):
    user_id = message.from_user.id if message.from_user else 0

    if user_id in active_users:
        if status_message:
            await status_message.edit_text(
                "⏳ Sizning boshqa videongiz yuklanmoqda. "
                "Iltimos, kuting."
            )
        else:
            await message.answer(
                "⏳ Boshqa videongiz yuklanmoqda. Iltimos, kuting."
            )
        return

    active_users.add(user_id)
    video_path: Path | None = None

    try:
        if status_message:
            await status_message.edit_text(
                f"⏳ Video yuklanmoqda ({quality}p)..."
                if quality
                else "⏳ Instagram videosi yuklanmoqda..."
            )
        else:
            status_message = await message.answer(
                f"⏳ Video yuklanmoqda ({quality}p)..."
                if quality
                else "⏳ Instagram videosi yuklanmoqda..."
            )

        video_path = await download_video(
            url=url,
            platform=platform,
            quality=quality,
            format_id=format_id,
            has_audio=has_audio,
        )

        if not video_path.exists():
            raise RuntimeError("Video fayli topilmadi.")

        file_size = video_path.stat().st_size

        if file_size > MAX_FILE_SIZE:
            await status_message.edit_text(
                "⚠️ Video hajmi 50 MB dan katta. "
                "Pastroq sifatni tanlab qayta urinib ko'ring."
            )
            return

        with video_path.open("rb") as file:
            video_data = file.read()

        await bot.send_video(
            chat_id=message.chat.id,
            video=BufferedInputFile(
                video_data,
                filename=video_path.name,
            ),
            caption=(
                f"✅ Video tayyor! ({quality}p)"
                if quality
                else "✅ Instagram videosi tayyor!"
            ),
            reply_to_message_id=message.message_id,
        )

        await status_message.delete()

    except Exception:
        logger.exception("Video download/send failed")

        error_text = (
            "❌ Videoni yuklab olish yoki yuborishda xatolik yuz berdi. "
            "Havolani tekshirib, qayta urinib ko'ring."
        )

        if status_message:
            try:
                await status_message.edit_text(error_text)
            except Exception:
                await message.answer(error_text)
        else:
            await message.answer(error_text)
    finally:
        active_users.discard(user_id)


# =========================================================
# START COMMAND
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Assalomu alaykum! 🎥\n\n"
        "YouTube havolasini yuboring — mavjud formatlar "
        "avtomatik aniqlanadi.\n"
        "Instagram havolalari esa avtomatik yuklanadi.\n\n"
        "Foydalanish uchun video havolasini yuboring."
    )


# =========================================================
# YOUTUBE FORMAT CALLBACK
# =========================================================

@dp.callback_query(F.data.startswith("format:"))
async def format_callback(callback: CallbackQuery):
    if not callback.data or not callback.message:
        await callback.answer(
            "So'rovni qayta yuboring.",
            show_alert=True,
        )
        return

    try:
        _, request_id, format_id = callback.data.split(":", 2)

        request = pending_downloads.get(request_id)

        if not request:
            await callback.answer(
                "Havola eskirgan. Qayta yuboring.",
                show_alert=True,
            )
            return

        if request["user_id"] != callback.from_user.id:
            await callback.answer(
                "Bu tugma boshqa foydalanuvchi uchun.",
                show_alert=True,
            )
            return

        # Format ID ushbu so'rovga tegishli ekanini tekshirish
        allowed_formats = {
            item["format_id"]
            for item in request["formats"]
        }

        if format_id not in allowed_formats:
            await callback.answer(
                "Format noto'g'ri. Havolani qayta yuboring.",
                show_alert=True,
            )
            return

        # Bir foydalanuvchi uchun takroriy tanlovni bloklash
        pending_downloads.pop(request_id, None)

        await callback.answer("Format tanlandi.")

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

        selected_format = next(
            item for item in request["formats"]
            if item["format_id"] == format_id
        )

        await process_video(
            message=request["message"],
            url=request["url"],
            platform="youtube",
            format_id=format_id,
            quality=selected_format["height"],
            status_message=callback.message,
            has_audio=selected_format["has_audio"],
        )

    except Exception:
        logger.exception("YouTube format callback xatosi")

        try:
            await callback.answer(
                "Formatni tanlashda xatolik yuz berdi.",
                show_alert=True,
            )
        except Exception:
            pass


# =========================================================
# HANDLE URL MESSAGES
# =========================================================

@dp.message(F.text)
async def handle_message(message: types.Message):
    if not message.text:
        return

    url = message.text.strip()
    platform = get_platform(url)

    if platform is None:
        await message.reply(
            "Iltimos, YouTube yoki Instagram video havolasini yuboring."
        )
        return

    if platform == "youtube":
        status_message = await message.answer(
            "🔎 YouTube formatlari aniqlanmoqda..."
        )

        try:
            formats = await get_youtube_formats(url)

            if not formats:
                await status_message.edit_text(
                    "❌ Ushbu videoda mos formatlar topilmadi."
                )
                return

            request_id = uuid.uuid4().hex[:12]

            pending_downloads[request_id] = {
                "url": url,
                "user_id": message.from_user.id,
                "message": message,
                "formats": formats,
            }

            await status_message.edit_text(
                "🎬 Mavjud video formatlaridan birini tanlang:",
                reply_markup=formats_keyboard(
                    request_id,
                    formats,
                ),
            )

        except Exception:
            logger.exception("YouTube formatlarini aniqlashda xato")

            await status_message.edit_text(
                "❌ Video formatlarini aniqlab bo'lmadi. "
                "Havolani tekshirib, qayta urinib ko'ring."
            )

        return

    # Instagram: to'g'ridan-to'g'ri yuklab olish
    await process_video(
        message=message,
        url=url,
        platform="instagram",
    )



# =========================================================
# RUN BOT
# =========================================================

async def main():
    logger.info("Bot ishga tushmoqda...")

    runner = await start_web_server()

    try:
        logger.info("HTTP server ishga tushdi.")
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

