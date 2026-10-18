
import os
import re
import uuid
import shutil
import tempfile
import logging
import asyncio
import urllib.parse

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

FFMPEG_PATH = (
    r"C:\Users\HP\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\ffmpeg-9.0.2-full_build-shared\bin"
)

# Vaqtinchalik URL'lar: key = tasodifiy ID
pending_downloads: dict[str, dict] = {}

# Bir foydalanuvchi bir vaqtning o'zida ko'p yuklash boshlamasin
active_users: set[int] = set()


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
# YOUTUBE QUALITY KEYBOARD
# =========================================================

def quality_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="360p",
                    callback_data=f"quality:360:{request_id}",
                ),
                InlineKeyboardButton(
                    text="720p",
                    callback_data=f"quality:720:{request_id}",
                ),
            ]
        ]
    )


# =========================================================
# VIDEO DOWNLOADER
# =========================================================

async def download_video(
    url: str,
    platform: str,
    quality: int | None = None,
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
                height = quality or 360

                # Avval tayyor MP4 format, so'ng alohida
                # video/audio oqimlari, keyin pastroq sifat.
                formats = [
                    f"best[height<={height}][ext=mp4]/"
                    f"best[height<={height}]",

                    f"bestvideo[height<={height}][ext=mp4]+"
                    f"bestaudio[ext=m4a]/"
                    f"bestvideo[height<={height}]+bestaudio/"
                    f"best[height<={height}]",

                    "best",
                ]

                last_error = None

                for fmt in formats:
                    try:
                        opts = {
                            **base_opts,
                            "format": fmt,
                            "merge_output_format": "mp4",
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

                        if files:
                            return max(
                                files,
                                key=lambda p: p.stat().st_mtime,
                            )

                        raise RuntimeError(
                            "Yuklangan video fayli topilmadi."
                        )

                    except Exception as exc:
                        last_error = exc
                        logger.warning(
                            "YouTube format %s ishlamadi: %s",
                            fmt,
                            exc,
                        )

                raise RuntimeError(
                    f"YouTube yuklab olish muvaffaqiyatsiz: {last_error}"
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

    except Exception as exc:
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
        if video_path:
            shutil.rmtree(video_path.parent, ignore_errors=True)

        active_users.discard(user_id)


# =========================================================
# START COMMAND
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Assalomu alaykum! 🎥\n\n"
        "YouTube havolasini yuboring va 360p yoki 720p sifatni tanlang.\n"
        "Instagram havolalari esa avtomatik yuklanadi.\n\n"
        "Foydalanish uchun video havolasini yuboring."
    )


# =========================================================
# YOUTUBE QUALITY CALLBACK
# =========================================================

@dp.callback_query(F.data.startswith("quality:"))
async def quality_callback(callback: CallbackQuery):
    if not callback.data or not callback.message:
        await callback.answer(
            "So'rovni qayta yuboring.",
            show_alert=True,
        )
        return

    try:
        _, quality_text, request_id = callback.data.split(":", 2)
        quality = int(quality_text)

        if quality not in (360, 720):
            raise ValueError("Noto'g'ri sifat.")

        request = pending_downloads.get(request_id)

        if not request:
            await callback.answer(
                "Havola eskirgan. Iltimos, qayta yuboring.",
                show_alert=True,
            )
            return

        if request["user_id"] != callback.from_user.id:
            await callback.answer(
                "Bu tugma boshqa foydalanuvchi uchun.",
                show_alert=True,
            )
            return

        # Tugmani qayta bosishni bloklash
        pending_downloads.pop(request_id, None)

        await callback.answer(f"{quality}p tanlandi.")

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

        original_message = request["message"]

        await process_video(
            message=original_message,
            url=request["url"],
            platform="youtube",
            quality=quality,
            status_message=callback.message,
        )

    except (ValueError, KeyError):
        logger.exception("Invalid quality callback")
        await callback.answer(
            "Sifatni tanlashda xatolik yuz berdi.",
            show_alert=True,
        )


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
        request_id = uuid.uuid4().hex[:12]

        pending_downloads[request_id] = {
            "url": url,
            "user_id": message.from_user.id,
            "message": message,
        }

        await message.answer(
            "🎬 YouTube videosi uchun sifatni tanlang:",
            reply_markup=quality_keyboard(request_id),
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
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
