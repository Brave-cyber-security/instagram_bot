
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_download_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(
            text="🔗 Instagram'da ochish",
            url=url
        )
    )
    
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(
            text="❌ Bekor qilish",
            callback_data="cancel"
        )
    )
    
    return builder.as_markup()


def get_retry_keyboard(url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(
            text="🔄 Qayta urinish",
            callback_data=f"retry:{url[:50]}"
        )
    )
    
    builder.row(
        InlineKeyboardButton(
            text="🔗 Instagram'da ochish",
            url=url
        )
    )
    
    return builder.as_markup()


def get_youtube_quality_keyboard(url_hash: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    qualities = [
        ("📹 1080p (Full HD)", "1080"),
        ("📹 720p (HD)", "720"),
        ("📹 480p (SD)", "480"),
        ("📹 360p", "360"),
        ("🎵 Audio (MP3)", "audio"),
    ]
    
    for i in range(0, len(qualities), 2):
        row_buttons = []
        for j in range(2):
            if i + j < len(qualities):
                label, quality_id = qualities[i + j]
                row_buttons.append(
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"ytq:{quality_id}:{url_hash}"
                    )
                )
        builder.row(*row_buttons)
    
    return builder.as_markup()


def get_eshitbot_keyboard(message_id: int, has_song: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(
            text="💾 Saqlash",
            callback_data=f"SAVE:{message_id}"
        )
    )
    
    if has_song:
        builder.row(
            InlineKeyboardButton(
                text="🎵 Qo'shiqni yuklab olish",
                callback_data=f"SEARCH_MUSIC:{message_id}"
            )
        )
    
    return builder.as_markup()
