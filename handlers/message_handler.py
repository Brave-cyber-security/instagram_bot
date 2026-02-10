import logging
from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaVideo

from config import extract_instagram_urls, extract_youtube_urls, MESSAGES, AUDD_API_TOKEN
from utils.downloader import (
    download_instagram_media,
    cleanup_download_result,
    DownloadError,
    DownloadResult
)
from utils.music_recognition import recognize_only
from handlers.youtube_handler import process_youtube_url
from keyboards.inline import get_eshitbot_keyboard

logger = logging.getLogger(__name__)
router = Router(name="message_handler")


@router.message(F.text)
async def handle_message(message: Message) -> None:
    if not message.text:
        return
    youtube_urls = extract_youtube_urls(message.text)
    if youtube_urls:
        for url in youtube_urls:
            await process_youtube_url(message, url)
        return
    instagram_urls = extract_instagram_urls(message.text)
    if instagram_urls:
        for url in instagram_urls:
            await process_instagram_url(message, url)
        return
    await message.reply(
        text=MESSAGES["error_invalid_url"],
        parse_mode="HTML"
    )


async def process_instagram_url(message: Message, url: str) -> None:
    from handlers.callback_handler import user_song_data

    progress_msg = await message.reply(
        text=MESSAGES["downloading"],
        parse_mode="HTML"
    )

    try:
        result = await download_instagram_media(url)

        await progress_msg.edit_text(
            text=MESSAGES["processing"],
            parse_mode="HTML"
        )

        song_info = None
        if result["media_type"] == "video" and result["files"]:
            video_path = result["files"][0]
            try:
                await progress_msg.edit_text(
                    text="🎵 Qo'shiq aniqlanmoqda...",
                    parse_mode="HTML"
                )
                song_info = await recognize_only(video_path, AUDD_API_TOKEN)
            except Exception as e:
                logger.warning(f"Music recognition failed: {e}")

        has_song = song_info is not None
        branding_caption = "📥 <b>@softdownloader_bot</b> orqali yuklab olindi 📥"

        sent_msg = await send_media(message, result, branding_caption, has_song)
        await cleanup_download_result(result)

        if song_info and sent_msg:
            user_song_data[sent_msg.message_id] = {
                "artist": song_info["artist"],
                "title": song_info["title"],
                "youtube_query": song_info["youtube_query"],
            }

        await progress_msg.delete()

    except DownloadError as e:
        logger.error(f"Download error for {url}: {e.message} ({e.error_type})")

        error_messages = {
            "private": MESSAGES["error_private"],
            "not_found": MESSAGES["error_not_found"],
            "file_too_large": MESSAGES["error_file_too_large"],
            "download_failed": MESSAGES["error_download"],
            "timeout": MESSAGES["error_download"],
            "age_restricted": MESSAGES["error_age_restricted"],
            "rate_limit": MESSAGES["error_rate_limit"],
            "invalid_url": MESSAGES["error_invalid_url"],
        }

        error_text = error_messages.get(e.error_type, MESSAGES["error_unknown"])
        await progress_msg.edit_text(text=error_text, parse_mode="HTML")

    except Exception as e:
        logger.exception(f"Unexpected error processing {url}: {e}")
        await progress_msg.edit_text(
            text=MESSAGES["error_unknown"],
            parse_mode="HTML"
        )


async def send_media(message: Message, result: DownloadResult, caption: str = None, has_song: bool = False) -> Message | None:
    files = result["files"]
    media_type = result["media_type"]

    if not files:
        raise DownloadError("No files to send", "download_failed")

    if media_type == "album" and len(files) > 1:
        msgs = await send_media_group(message, files, caption, has_song)
        return msgs[0] if msgs else None
    elif media_type == "video":
        return await send_video(message, files[0], caption, has_song)
    else:
        return await send_photo(message, files[0], caption, has_song)


async def send_photo(message: Message, file_path: str, caption: str = None, has_song: bool = False) -> Message:
    photo = FSInputFile(file_path)
    keyboard = get_eshitbot_keyboard(0, has_song=has_song) if caption else None
    sent = await message.reply_photo(
        photo=photo,
        caption=caption,
        parse_mode="HTML",
        reply_markup=keyboard
    )
    if caption and sent:
        await sent.edit_reply_markup(
            reply_markup=get_eshitbot_keyboard(sent.message_id, has_song=has_song)
        )
    return sent


async def send_video(message: Message, file_path: str, caption: str = None, has_song: bool = False) -> Message:
    video = FSInputFile(file_path)
    keyboard = get_eshitbot_keyboard(0, has_song=has_song) if caption else None
    sent = await message.reply_video(
        video=video,
        caption=caption,
        parse_mode="HTML",
        supports_streaming=True,
        reply_markup=keyboard
    )
    if caption and sent:
        await sent.edit_reply_markup(
            reply_markup=get_eshitbot_keyboard(sent.message_id, has_song=has_song)
        )
    return sent


async def send_media_group(message: Message, files: list[str], caption: str = None, has_song: bool = False) -> list[Message]:
    media_group: list[InputMediaPhoto | InputMediaVideo] = []

    for i, file_path in enumerate(files):
        path = Path(file_path)
        ext = path.suffix.lower()
        file = FSInputFile(file_path)

        item_caption = caption if i == 0 else None

        if ext in [".mp4", ".webm", ".mkv", ".mov", ".avi"]:
            media_group.append(
                InputMediaVideo(media=file, caption=item_caption, parse_mode="HTML" if item_caption else None)
            )
        else:
            media_group.append(
                InputMediaPhoto(media=file, caption=item_caption, parse_mode="HTML" if item_caption else None)
            )

    if len(media_group) > 10:
        all_msgs = []
        for i in range(0, len(media_group), 10):
            chunk = media_group[i:i+10]
            msgs = await message.reply_media_group(media=chunk)
            all_msgs.extend(msgs)
        return all_msgs
    else:
        return await message.reply_media_group(media=media_group)

