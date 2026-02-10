import asyncio
import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.types import CallbackQuery, FSInputFile

from config import TEMP_DIR
from utils.music_recognition import download_full_song
from utils.downloader import cleanup_files

logger = logging.getLogger(__name__)
router = Router(name="callback_handler")

user_song_data: dict[int, dict[str, str]] = {}


@router.callback_query(F.data.startswith("SEARCH_MUSIC:"))
async def handle_search_music(callback: CallbackQuery) -> None:
    await callback.answer()

    msg_id_str = callback.data.split(":", 1)[1]
    msg_id = int(msg_id_str)

    song = user_song_data.get(msg_id)
    if not song:
        await callback.message.answer("❌ Qo'shiq ma'lumotlari topilmadi. Qayta urinib ko'ring.")
        return

    progress_msg = await callback.message.answer("🎵 Qo'shiq yuklanmoqda, iltimos kuting...")

    try:
        song_info = {
            "title": song["title"],
            "artist": song["artist"],
            "album": "",
            "youtube_query": song["youtube_query"],
        }

        download_dir = TEMP_DIR / f"song_{msg_id}"
        download_dir.mkdir(parents=True, exist_ok=True)

        song_path = await download_full_song(song_info, download_dir)

        if song_path and Path(song_path).exists():
            audio_file = FSInputFile(song_path)
            await callback.message.answer_audio(
                audio=audio_file,
                caption=f"🎵 {song['artist']} - {song['title']}",
                title=song["title"][:64],
                performer=song["artist"][:64],
            )

            await asyncio.sleep(1)
            await cleanup_files(download_dir)
            
            try:
                await progress_msg.delete()
            except:
                pass
        else:
            await progress_msg.edit_text("❌ Qo'shiq topilmadi. Boshqa manbadan qidirib ko'ring.")

    except Exception as e:
        logger.error(f"Error downloading song for msg {msg_id}: {e}")
        await progress_msg.edit_text("❌ Qo'shiq yuklashda xatolik yuz berdi.")


@router.callback_query(F.data.startswith("SAVE:"))
async def handle_save(callback: CallbackQuery) -> None:
    await callback.answer("✅ Saqlandi!", show_alert=False)
