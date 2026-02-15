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


def _get_common_ydl_opts(use_cookies: bool = True) -> dict[str, Any]:
    """yt-dlp uchun umumiy sozlamalar.
    
    Serverda (Docker): cookies mavjud → cookies + default client ishlatiladi.
    Lokalda yoki cookies ishlamasa: android_vr client ishlatiladi.
    """
    from config import YOUTUBE_COOKIES_FILE
    
    opts: dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'js_runtimes': {'node': {}, 'deno': {}},
        'remote_components': ['ejs:github'],
    }
    
    if use_cookies and YOUTUBE_COOKIES_FILE:
        opts['cookiefile'] = str(YOUTUBE_COOKIES_FILE)
        opts['extractor_args'] = {'youtube': {
            'player_client': ['default'],
        }}
        logger.info("Using YouTube cookies for authentication")
    else:
        opts['extractor_args'] = {'youtube': {
            'player_client': ['default', 'android_vr'],
        }}
        if not use_cookies:
            logger.info("Retrying YouTube without cookies")
    
    return opts


def _sync_get_video_info(url: str) -> dict[str, Any]:
    # Avval cookies bilan sinash
    ydl_opts: dict[str, Any] = {
        **_get_common_ydl_opts(use_cookies=True),
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
            return dict(info)  # type: ignore[arg-type]
    except Exception as e:
        error_msg = str(e).lower()
        if "sign in" in error_msg or "bot" in error_msg or "cookies" in error_msg:
            logger.warning("YouTube cookies expired, retrying without cookies...")
            # Cookiessiz qayta urinish
            ydl_opts = {
                **_get_common_ydl_opts(use_cookies=False),
                'extract_flat': False,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=False)
                    return dict(info)  # type: ignore[arg-type]
            except Exception as e2:
                logger.error(f"Error getting video info (no cookies): {e2}")
                raise YouTubeDownloadError(str(e2), "info_failed")
        logger.error(f"Error getting video info: {e}")
        raise YouTubeDownloadError(str(e), "info_failed")


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
    
    ydl_opts: dict[str, Any] = {
        **_get_common_ydl_opts(use_cookies=True),
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
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=True)
            return dict(info)  # type: ignore[arg-type]
    except Exception as e:
        error_msg = str(e).lower()
        # Cookies xatosi bo'lsa — cookiessiz qayta urinish
        if "sign in" in error_msg or "bot" in error_msg or "cookies" in error_msg:
            logger.warning("YouTube cookies expired during download, retrying without cookies...")
            ydl_opts_no_cookies: dict[str, Any] = {
                **_get_common_ydl_opts(use_cookies=False),
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
            try:
                with yt_dlp.YoutubeDL(ydl_opts_no_cookies) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=True)
                    return dict(info)  # type: ignore[arg-type]
            except Exception as e2:
                error_msg2 = str(e2).lower()
                if "private" in error_msg2:
                    raise YouTubeDownloadError("Video is private", "private")
                if "unavailable" in error_msg2 or "not available" in error_msg2:
                    raise YouTubeDownloadError("Video is unavailable", "not_found")
                if "age" in error_msg2:
                    raise YouTubeDownloadError("Age-restricted video", "age_restricted")
                raise YouTubeDownloadError(str(e2), "download_failed")
        if "private" in error_msg:
            raise YouTubeDownloadError("Video is private", "private")
        if "unavailable" in error_msg or "not available" in error_msg:
            raise YouTubeDownloadError("Video is unavailable", "not_found")
        if "age" in error_msg:
            raise YouTubeDownloadError("Age-restricted video", "age_restricted")
        raise YouTubeDownloadError(str(e), "download_failed")


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
