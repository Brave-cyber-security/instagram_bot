import asyncio
import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, TypedDict
from concurrent.futures import ThreadPoolExecutor
import yt_dlp
from config import TEMP_DIR, MAX_FILE_SIZE

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=3)

# Diagnostic: log yt-dlp version and plugins on import
try:
    logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")
except Exception:
    pass

QUALITY_OPTIONS = [
    {"id": "1080", "label": "1080p (Full HD)", "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]"},
    {"id": "720", "label": "720p (HD)", "format": "bestvideo[height<=720]+bestaudio/best[height<=720]"},
    {"id": "480", "label": "480p (SD)", "format": "bestvideo[height<=480]+bestaudio/best[height<=480]"},
    {"id": "360", "label": "360p", "format": "bestvideo[height<=360]+bestaudio/best[height<=360]"},
    {"id": "audio", "label": "🎵 Faqat audio (MP3)", "format": "bestaudio/best"},
]


def is_shorts_url(url: str) -> bool:
    return "/shorts/" in url


class YouTubeVideoInfo(TypedDict):
    title: str
    duration: int
    thumbnail: str
    uploader: str
    url: str
    url_hash: str


class YouTubeDownloadResult(TypedDict):
    file_path: str
    title: str
    duration: int
    is_audio: bool


class YouTubeDownloadError(Exception):
    def __init__(self, message: str, error_type: str = "unknown"):
        self.message = message
        self.error_type = error_type
        super().__init__(self.message)


def get_url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]

_url_cache: dict[str, str] = {}


def cache_url(url: str) -> str:
    url_hash = get_url_hash(url)
    _url_cache[url_hash] = url
    return url_hash


def get_cached_url(url_hash: str) -> str | None:
    return _url_cache.get(url_hash)


def _get_base_opts() -> dict[str, Any]:
    """Asosiy yt-dlp sozlamalari (player_client va cookies yo'q)."""
    return {
        'quiet': True,
        'no_warnings': True,
        'js_runtimes': {'node': {}, 'deno': {}},
        'remote_components': ['ejs:github'],
    }


def _get_strategies() -> list[dict[str, Any]]:
    """YouTube uchun sinab ko'rish strategiyalari.
    
    Har bir strategiya — player_client + cookies kombinatsiyasi.
    Birinchi ishlagani ishlatiladi.
    """
    from config import YOUTUBE_COOKIES_FILE
    
    strategies: list[dict[str, Any]] = []
    
    # 1. web_creator + cookies (eng ishonchli)
    if YOUTUBE_COOKIES_FILE:
        strategies.append({
            'label': 'web_creator + cookies',
            'extractor_args': {'youtube': {'player_client': ['web_creator']}},
            'cookiefile': str(YOUTUBE_COOKIES_FILE),
        })
    
    # 2. mweb (mobile web, ko'pincha bot tekshiruv yo'q)
    strategies.append({
        'label': 'mweb (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['mweb']}},
    })
    
    # 3. web + cookies
    if YOUTUBE_COOKIES_FILE:
        strategies.append({
            'label': 'web + cookies',
            'extractor_args': {'youtube': {'player_client': ['web']}},
            'cookiefile': str(YOUTUBE_COOKIES_FILE),
        })
    
    # 4. ios (cookies kerak emas)
    strategies.append({
        'label': 'ios (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['ios']}},
    })
    
    # 5. android (cookies kerak emas)
    strategies.append({
        'label': 'android (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['android']}},
    })
    
    return strategies


def _is_bot_error(error_msg: str) -> bool:
    """YouTube bot detection xatosimi?"""
    lower = error_msg.lower()
    return "sign in" in lower or "bot" in lower or "confirm" in lower


def _classify_error(error_msg: str) -> str:
    """Xato turini aniqlash."""
    lower = error_msg.lower()
    if "private" in lower:
        return "private"
    if "unavailable" in lower or "not available" in lower:
        return "not_found"
    if "age" in lower:
        return "age_restricted"
    return "unknown"


def _sync_get_video_info(url: str) -> dict[str, Any]:
    strategies = _get_strategies()
    last_error = None
    
    for strategy in strategies:
        label = strategy.pop('label')
        ydl_opts: dict[str, Any] = {
            **_get_base_opts(),
            **strategy,
            'extract_flat': False,
        }
        strategy['label'] = label  # restore for next use
        
        try:
            logger.info(f"Trying strategy: {label}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(url, download=False)
                logger.info(f"Strategy '{label}' succeeded!")
                return dict(info)  # type: ignore[arg-type]
        except Exception as e:
            last_error = e
            error_msg = str(e)
            if _is_bot_error(error_msg):
                logger.warning(f"Strategy '{label}' blocked by YouTube, trying next...")
                continue
            # Bot xatosi emas — boshqa muammo
            error_type = _classify_error(error_msg)
            if error_type != "unknown":
                raise YouTubeDownloadError(error_msg, error_type)
            logger.error(f"Strategy '{label}' failed: {e}")
            continue
    
    raise YouTubeDownloadError(str(last_error), "info_failed")


async def get_video_info(url: str) -> YouTubeVideoInfo:
    loop = asyncio.get_event_loop()
    
    try:
        info = await loop.run_in_executor(_executor, _sync_get_video_info, url)
        
        url_hash = cache_url(url)
        
        return YouTubeVideoInfo(
            title=info.get('title', 'Unknown'),
            duration=info.get('duration', 0) or 0,
            thumbnail=info.get('thumbnail', ''),
            uploader=info.get('uploader', 'Unknown'),
            url=url,
            url_hash=url_hash
        )
    except YouTubeDownloadError:
        raise
    except Exception as e:
        logger.error(f"Error getting video info: {e}")
        raise YouTubeDownloadError(str(e), "info_failed")


def _sync_download_video(url: str, quality: str, download_dir: Path) -> dict[str, Any]:
    is_audio = quality == "audio"
    output_template = str(download_dir / "%(title).50s.%(ext)s")
    
    from config import get_ffmpeg_path
    ffmpeg_path = get_ffmpeg_path()
    
    if is_audio:
        format_str = "bestaudio[ext=m4a]/bestaudio/best"
        merge_format = None
    else:
        if quality in ["1080", "720", "480", "360"]:
            height = quality
            format_str = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}]/best"
            )
        else:
            format_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        merge_format = 'mp4'
    
    strategies = _get_strategies()
    last_error = None
    
    for strategy in strategies:
        label = strategy.pop('label')
        ydl_opts: dict[str, Any] = {
            **_get_base_opts(),
            **strategy,
            'format': format_str,
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': merge_format,
            'ffmpeg_location': ffmpeg_path,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }] if is_audio else [],
        }
        strategy['label'] = label  # restore
        
        # Har bir strategiyadan oldin temp fayllarni tozalash
        for f in download_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        
        try:
            logger.info(f"Download strategy: {label}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(url, download=True)
                logger.info(f"Download strategy '{label}' succeeded!")
                return dict(info)  # type: ignore[arg-type]
        except Exception as e:
            last_error = e
            error_msg = str(e)
            if _is_bot_error(error_msg):
                logger.warning(f"Download strategy '{label}' blocked, trying next...")
                continue
            error_type = _classify_error(error_msg)
            if error_type != "unknown":
                raise YouTubeDownloadError(error_msg, error_type)
            logger.error(f"Download strategy '{label}' failed: {e}")
            continue
    
    raise YouTubeDownloadError(str(last_error), "download_failed")


async def download_youtube_video(url: str, quality: str) -> YouTubeDownloadResult:
    download_id = str(uuid.uuid4())[:8]
    download_dir = TEMP_DIR / f"yt_{download_id}"
    download_dir.mkdir(parents=True, exist_ok=True)
    
    is_audio = quality == "audio"
    
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            _executor,
            _sync_download_video,
            url,
            quality,
            download_dir
        )
        
        files = list(download_dir.iterdir())
        if not files:
            raise YouTubeDownloadError("No file downloaded", "download_failed")
        
        file_path = str(files[0])
        
        file_size = os.path.getsize(file_path)
        if file_size > MAX_FILE_SIZE:
            await cleanup_files(download_dir)
            raise YouTubeDownloadError(
                f"File too large: {file_size / (1024*1024):.1f}MB (max 50MB)",
                "file_too_large"
            )
        
        return YouTubeDownloadResult(
            file_path=file_path,
            title=info.get('title', 'Unknown'),
            duration=info.get('duration', 0) or 0,
            is_audio=is_audio
        )
        
    except YouTubeDownloadError:
        await cleanup_files(download_dir)
        raise
    except Exception as e:
        await cleanup_files(download_dir)
        logger.error(f"Error downloading YouTube video: {e}")
        raise YouTubeDownloadError(str(e), "download_failed")


async def cleanup_files(path: Path | str) -> None:
    try:
        path = Path(path)
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    except Exception as e:
        logger.warning(f"Failed to cleanup {path}: {e}")


async def cleanup_youtube_result(result: YouTubeDownloadResult) -> None:
    file_path = Path(result["file_path"])
    if file_path.parent.exists():
        await cleanup_files(file_path.parent)


def format_duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:02d}"
