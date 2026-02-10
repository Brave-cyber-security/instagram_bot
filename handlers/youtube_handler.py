import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile

from config import MESSAGES
from utils.youtube_downloader import (
    get_video_info,
    download_youtube_video,
    cleanup_youtube_result,
    get_cached_url,
    format_duration,
    is_shorts_url,
    YouTubeDownloadError,
    QUALITY_OPTIONS
)
from keyboards.inline import get_youtube_quality_keyboard

logger = logging.getLogger(__name__)
router = Router(name="youtube_handler")


async def process_youtube_url(message: Message, url: str) -> None:
    if is_shorts_url(url):
        await download_shorts(message, url)
        return
    
    await show_quality_selection(message, url)


async def download_shorts(message: Message, url: str) -> None:
    progress_msg = await message.reply(
        text="⏳ Shorts yuklab olinmoqda...",
        parse_mode="HTML"
    )
    
    try:
        result = await download_youtube_video(url, "720")
        
        file = FSInputFile(result["file_path"])
        
        caption = f"🎬 {result['title']}"
        if len(caption) > 200:
            caption = caption[:200] + "..."
        
        await message.reply_video(
            video=file,
            caption=caption,
            supports_streaming=True,
            duration=result["duration"]
        )
        
        await progress_msg.delete()
        await cleanup_youtube_result(result)
        
    except YouTubeDownloadError as e:
        logger.error(f"Shorts download error: {e.message}")
        error_text = MESSAGES.get("error_youtube_download", MESSAGES["error_unknown"])
        await progress_msg.edit_text(text=error_text, parse_mode="HTML")
        
    except Exception as e:
        logger.exception(f"Unexpected error downloading Shorts: {e}")
        await progress_msg.edit_text(text=MESSAGES["error_unknown"], parse_mode="HTML")


async def show_quality_selection(message: Message, url: str) -> None:
    progress_msg = await message.reply(
        text="🔍 Video ma'lumotlari olinmoqda...",
        parse_mode="HTML"
    )
    
    try:
        video_info = await get_video_info(url)
        duration_str = format_duration(video_info["duration"])
        
        info_text = (
            f"🎬 <b>{video_info['title']}</b>\n\n"
            f"👤 Kanal: {video_info['uploader']}\n"
            f"⏱ Davomiylik: {duration_str}\n\n"
            f"📥 <b>Sifatni tanlang:</b>"
        )
        
        keyboard = get_youtube_quality_keyboard(video_info["url_hash"])
        
        await progress_msg.edit_text(
            text=info_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
    except YouTubeDownloadError as e:
        logger.error(f"YouTube error for {url}: {e.message}")
        error_messages = {
            "private": MESSAGES["error_private"],
            "not_found": MESSAGES["error_not_found"],
            "age_restricted": MESSAGES["error_age_restricted"],
        }
        error_text = error_messages.get(e.error_type, MESSAGES["error_youtube_download"])
        await progress_msg.edit_text(text=error_text, parse_mode="HTML")
        
    except Exception as e:
        logger.exception(f"Unexpected error for YouTube URL {url}: {e}")
        await progress_msg.edit_text(text=MESSAGES["error_unknown"], parse_mode="HTML")


@router.callback_query(F.data.startswith("ytq:"))
async def handle_quality_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.message.edit_text("❌ Xatolik yuz berdi.")
            return
        
        _, quality, url_hash = parts
        
        url = get_cached_url(url_hash)
        if not url:
            await callback.message.edit_text(
                "❌ Sessiya tugadi. Iltimos, URL'ni qayta yuboring."
            )
            return
        
        quality_label = quality
        for q in QUALITY_OPTIONS:
            if q["id"] == quality:
                quality_label = q["label"]
                break
        
        await callback.message.edit_text(
            f"⏳ Yuklab olinmoqda ({quality_label})...\n"
            "Bu bir necha daqiqa vaqt olishi mumkin.",
            parse_mode="HTML"
        )
        
        result = await download_youtube_video(url, quality)
        
        file = FSInputFile(result["file_path"])
        
        caption = f"🎬 {result['title']}"
        if len(caption) > 200:
            caption = caption[:200] + "..."
        
        original_message = callback.message.reply_to_message
        
        if result["is_audio"]:
            if original_message:
                await original_message.reply_audio(
                    audio=file,
                    caption=caption,
                    title=result["title"][:64] if result["title"] else None,
                    duration=result["duration"]
                )
            else:
                await callback.message.answer_audio(
                    audio=file,
                    caption=caption,
                    title=result["title"][:64] if result["title"] else None,
                    duration=result["duration"]
                )
        else:
            if original_message:
                await original_message.reply_video(
                    video=file,
                    caption=caption,
                    supports_streaming=True,
                    duration=result["duration"]
                )
            else:
                await callback.message.answer_video(
                    video=file,
                    caption=caption,
                    supports_streaming=True,
                    duration=result["duration"]
                )
        
        await callback.message.delete()
        await cleanup_youtube_result(result)
        
    except YouTubeDownloadError as e:
        logger.error(f"YouTube download error: {e.message}")
        error_messages = {
            "file_too_large": MESSAGES["error_file_too_large"],
            "private": MESSAGES["error_private"],
            "not_found": MESSAGES["error_not_found"],
        }
        error_text = error_messages.get(e.error_type, MESSAGES["error_youtube_download"])
        await callback.message.edit_text(text=error_text, parse_mode="HTML")
        
    except Exception as e:
        logger.exception(f"Unexpected error in YouTube download: {e}")
        await callback.message.edit_text(text=MESSAGES["error_unknown"], parse_mode="HTML")
