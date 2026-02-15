
import os
import re
import shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables!")

TEMP_DIR: Path = Path(__file__).parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

COOKIES_FILE: Path | None = Path(__file__).parent / "instagram.com_cookies.txt"
if not COOKIES_FILE.exists():
    COOKIES_FILE = None

YOUTUBE_COOKIES_FILE: Path | None = Path(__file__).parent / "cookies.txt"
if not YOUTUBE_COOKIES_FILE.exists():
    YOUTUBE_COOKIES_FILE = Path(__file__).parent / "youtube.com_cookies.txt"
    if not YOUTUBE_COOKIES_FILE.exists():
        YOUTUBE_COOKIES_FILE = None

# Telegram Bot API Server (local = 2GB, default = 50MB)
LOCAL_BOT_API: bool = os.getenv("LOCAL_BOT_API_URL", "") != ""
LOCAL_BOT_API_URL: str = os.getenv("LOCAL_BOT_API_URL", "")
MAX_FILE_SIZE: int = 2000 * 1024 * 1024 if LOCAL_BOT_API else 50 * 1024 * 1024

AUDD_API_TOKEN: str = os.getenv("AUDD_API_TOKEN", "")

INSTAGRAM_URL_PATTERNS: list[re.Pattern] = [
    re.compile(r"https?://(?:www\.)?instagram\.com/p/[\w-]+/?"),
    re.compile(r"https?://(?:www\.)?instagram\.com/reel/[\w-]+/?"),
    re.compile(r"https?://(?:www\.)?instagram\.com/stories/[\w.-]+/\d+/?"),
    re.compile(r"https?://(?:www\.)?instagram\.com/tv/[\w-]+/?"),
    re.compile(r"https?://(?:www\.)?instagram\.com/share/[\w-]+/?"),
]

INSTAGRAM_URL_REGEX: re.Pattern = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|stories|tv|share)/[\w.-]+(?:/\d+)?/?"
)


def extract_instagram_urls(text: str) -> list[str]:
    urls: list[str] = []
    matches = INSTAGRAM_URL_REGEX.findall(text)
    
    for url in matches:
        for pattern in INSTAGRAM_URL_PATTERNS:
            if pattern.match(url):
                if url not in urls:
                    urls.append(url)
                break
    
    return urls


def is_valid_instagram_url(url: str) -> bool:
    for pattern in INSTAGRAM_URL_PATTERNS:
        if pattern.match(url):
            return True
    return False


YOUTUBE_URL_PATTERNS: list[re.Pattern] = [
    re.compile(r"https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+"),
    re.compile(r"https?://youtu\.be/[\w-]+"),
    re.compile(r"https?://(?:www\.)?youtube\.com/shorts/[\w-]+"),
    re.compile(r"https?://(?:www\.)?youtube\.com/embed/[\w-]+"),
]

YOUTUBE_URL_REGEX: re.Pattern = re.compile(
    r"https?://(?:(?:www\.)?youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)[\w-]+"
)


def extract_youtube_urls(text: str) -> list[str]:
    urls: list[str] = []
    
    for pattern in YOUTUBE_URL_PATTERNS:
        matches = pattern.findall(text)
        for url in matches:
            if url not in urls:
                urls.append(url)
    
    return urls


def is_valid_youtube_url(url: str) -> bool:
    for pattern in YOUTUBE_URL_PATTERNS:
        if pattern.match(url):
            return True
    return False


def get_ffmpeg_path() -> str:
    """FFmpeg yo'lini aniqlash: system PATH → imageio-ffmpeg."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    
    possible_paths = [
        Path(os.getcwd()) / "bin" / "ffmpeg.exe",
        Path.home() / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.0.1-full_build/bin/ffmpeg.exe"
    ]
    
    for p in possible_paths:
        if p.exists():
            return str(p)
            
    return "ffmpeg"


MESSAGES = {
    "welcome": (
        "👋 <b>Salom! Media Downloader Botga xush kelibsiz!</b>\n\n"
        "📥 Menga Instagram yoki YouTube URL manzilini yuboring, "
        "men uni yuklab beraman!\n\n"
        "📌 <b>Qo'llab-quvvatlanadigan platformalar:</b>\n"
        "• 📸 Instagram (post, reel, story)\n"
        "• 🎬 YouTube (video, shorts)\n\n"
        "💡 Yordam uchun /help buyrug'ini yuboring."
    ),
    "help": (
        "📖 <b>Yordam</b>\n\n"
        "🔹 <b>Instagram:</b>\n"
        "• Post/reel/story URL'ini nusxalab yuboring\n"
        "• Avtomatik yuklab beriladi\n\n"
        "🔹 <b>YouTube:</b>\n"
        "• Video URL'ini nusxalab yuboring\n"
        "• Sifatni tanlang (1080p, 720p, 480p, 360p)\n"
        "• Faqat audio (MP3) ham mavjud\n\n"
        "🔹 <b>Misollar:</b>\n"
        "<code>https://www.instagram.com/p/ABC123/</code>\n"
        "<code>https://www.youtube.com/watch?v=dQw4w9WgXcQ</code>\n"
        "<code>https://youtu.be/dQw4w9WgXcQ</code>\n\n"
        "⚠️ <b>Eslatma:</b>\n"
        "• Fayl cheklovi: 2GB gacha\n"
        "• Yopiq kontentni yuklab bo'lmaydi"
    ),
    "downloading": "⏳ Yuklab olinmoqda, iltimos kuting...",
    "processing": "🔄 Media qayta ishlanmoqda...",
    "success": "✅ Muvaffaqiyatli yuklandi!",
    "error_invalid_url": "❌ Noto'g'ri URL. Iltimos, to'g'ri Instagram yoki YouTube URL yuboring.",
    "error_private": "🔒 Bu kontent yopiq (private). Faqat ochiq kontentni yuklab olish mumkin.",
    "error_not_found": "❌ Media topilmadi. URL to'g'riligini tekshiring.",
    "error_download": "❌ Yuklashda xatolik yuz berdi. Keyinroq qayta urinib ko'ring.",
    "error_file_too_large": "❌ Fayl juda katta. Kichikroq sifat tanlang.",
    "error_age_restricted": "🔞 Bu kontent yosh cheklovi bilan himoyalangan.",
    "error_rate_limit": "⏱️ Cheklov qo'yildi. Bir necha daqiqadan keyin qayta urinib ko'ring.",
    "error_youtube_download": "❌ YouTube'dan yuklashda xatolik. Keyinroq qayta urinib ko'ring.",
    "error_unknown": "❌ Noma'lum xatolik yuz berdi.",
}
