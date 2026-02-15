import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, TypedDict
from concurrent.futures import ThreadPoolExecutor
import requests
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
        'noplaylist': True,
    }


def _get_strategies() -> list[dict[str, Any]]:
    """YouTube uchun sinab ko'rish strategiyalari.
    
    Har bir strategiya — player_client + cookies kombinatsiyasi.
    Birinchi ishlagani ishlatiladi.
    """
    from config import YOUTUBE_COOKIES_FILE
    
    strategies: list[dict[str, Any]] = []
    
    # 1. tv_embedded (eng kam bloklangan)
    strategies.append({
        'label': 'tv_embedded (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['tv_embedded']}},
    })
    
    # 2. android (cookies kerak emas)
    strategies.append({
        'label': 'android (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['android']}},
    })
    
    # 3. ios (cookies kerak emas)
    strategies.append({
        'label': 'ios (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['ios']}},
    })
    
    # 4. mweb (mobile web, ko'pincha bot tekshiruv yo'q)
    strategies.append({
        'label': 'mweb (no cookies)',
        'extractor_args': {'youtube': {'player_client': ['mweb']}},
    })
    
    # 5. web_creator + cookies (webpage skip)
    if YOUTUBE_COOKIES_FILE:
        strategies.append({
            'label': 'web_creator + cookies',
            'extractor_args': {'youtube': {'player_client': ['web_creator'], 'player_skip': ['webpage']}},
            'cookiefile': str(YOUTUBE_COOKIES_FILE),
        })
    
    # 6. web + cookies (webpage skip)
    if YOUTUBE_COOKIES_FILE:
        strategies.append({
            'label': 'web + cookies',
            'extractor_args': {'youtube': {'player_client': ['web'], 'player_skip': ['webpage']}},
            'cookiefile': str(YOUTUBE_COOKIES_FILE),
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


# ===== Piped / Invidious / Cobalt API fallback (YouTube bot detection uchun) =====

# Fallback (hardcoded) instancelar — dinamik ro'yxat ishlamasa ishlatiladi
_FALLBACK_PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
]
_FALLBACK_INVIDIOUS = [
    "https://vid.puffyan.us",
    "https://invidious.fdn.fr",
    "https://yt.artemislena.eu",
]

# Keshlar (bir marta olib, qayta ishlatish)
_cached_piped: list[str] | None = None
_cached_invidious: list[str] | None = None
_cache_time: float = 0
_CACHE_TTL = 3600  # 1 soat


def _fetch_piped_instances() -> list[str]:
    """Piped instances ro'yxatini API dan olish."""
    global _cached_piped, _cache_time
    import time
    now = time.time()
    if _cached_piped and (now - _cache_time) < _CACHE_TTL:
        return _cached_piped

    instances: list[str] = []
    try:
        resp = requests.get(
            "https://piped-instances.kavin.rocks/",
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        if resp.status_code == 200:
            for item in resp.json():
                api_url = item.get('api_url', '').rstrip('/')
                if api_url and api_url.startswith('https://'):
                    instances.append(api_url)
            if instances:
                logger.info(f"Fetched {len(instances)} Piped instances")
                _cached_piped = instances[:8]  # top 8 ta
                _cache_time = now
                return _cached_piped
    except Exception as e:
        logger.warning(f"Failed to fetch Piped instances: {e}")

    return _FALLBACK_PIPED


def _fetch_invidious_instances() -> list[str]:
    """Invidious instances ro'yxatini API dan olish."""
    global _cached_invidious, _cache_time
    import time
    now = time.time()
    if _cached_invidious and (now - _cache_time) < _CACHE_TTL:
        return _cached_invidious

    instances: list[str] = []
    try:
        resp = requests.get(
            "https://api.invidious.io/instances.json?sort_by=api,health",
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        if resp.status_code == 200:
            for item in resp.json():
                if len(item) >= 2 and isinstance(item[1], dict):
                    info = item[1]
                    uri = info.get('uri', '').rstrip('/')
                    api_ok = info.get('api', False)
                    itype = info.get('type', '')
                    if uri and api_ok and itype == 'https':
                        instances.append(uri)
            if instances:
                logger.info(f"Fetched {len(instances)} Invidious instances")
                _cached_invidious = instances[:8]  # top 8 ta
                _cache_time = now
                return _cached_invidious
    except Exception as e:
        logger.warning(f"Failed to fetch Invidious instances: {e}")

    return _FALLBACK_INVIDIOUS


COBALT_API_KEY = os.getenv("COBALT_API_KEY", "")
COBALT_INSTANCES = [
    "https://api.cobalt.tools",
]


def _extract_video_id(url: str) -> str | None:
    """YouTube URL dan video ID ajratib olish."""
    match = re.search(
        r'(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([\w-]+)', url
    )
    return match.group(1) if match else None


def _piped_get_streams(video_id: str) -> dict[str, Any] | None:
    """Piped API orqali video stream ma'lumotlarini olish."""
    instances = _fetch_piped_instances()
    for instance in instances:
        try:
            url = f"{instance}/streams/{video_id}"
            logger.info(f"Trying Piped: {url}")
            resp = requests.get(
                url,
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'},
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('title'):
                    logger.info(f"Piped API ({instance}) succeeded")
                    return data
                else:
                    logger.warning(f"Piped {instance}: empty response")
            else:
                logger.warning(f"Piped {instance}: status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Piped {instance} failed: {e}")
            continue
    return None


def _download_stream_file(url: str, output_path: Path) -> bool:
    """Stream URL dan faylni yuklab olish."""
    try:
        resp = requests.get(
            url, stream=True, timeout=120,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        resp.raise_for_status()
        with open(output_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return output_path.stat().st_size > 0
    except Exception as e:
        logger.error(f"Stream download failed: {e}")
        return False


def _merge_av(video_path: Path, audio_path: Path, output_path: Path) -> bool:
    """FFmpeg bilan video va audio birlashtirish."""
    from config import get_ffmpeg_path
    ffmpeg = get_ffmpeg_path()
    try:
        subprocess.run([
            ffmpeg, '-y',
            '-i', str(video_path),
            '-i', str(audio_path),
            '-c:v', 'copy', '-c:a', 'aac',
            '-movflags', '+faststart',
            str(output_path)
        ], capture_output=True, timeout=180, check=True)
        return output_path.exists()
    except Exception as e:
        logger.error(f"FFmpeg merge failed: {e}")
        return False


def _piped_get_video_info(url: str) -> dict[str, Any] | None:
    """Piped orqali video info olish (yt-dlp ishlamasa)."""
    video_id = _extract_video_id(url)
    if not video_id:
        return None

    data = _piped_get_streams(video_id)
    if not data:
        return None

    return {
        'title': data.get('title', 'Unknown'),
        'duration': data.get('duration', 0),
        'thumbnail': data.get('thumbnailUrl', ''),
        'uploader': data.get('uploader', 'Unknown'),
        'webpage_url': url,
    }


def _piped_download_video(
    url: str, quality: str, download_dir: Path
) -> dict[str, Any] | None:
    """Piped orqali video yuklab olish (yt-dlp ishlamasa)."""
    video_id = _extract_video_id(url)
    if not video_id:
        return None

    data = _piped_get_streams(video_id)
    if not data:
        return None

    title = data.get('title', 'video')[:50]
    safe_title = re.sub(r'[^\w\s-]', '', title).strip() or 'video'
    is_audio = quality == "audio"

    if is_audio:
        audio_streams = data.get('audioStreams', [])
        if not audio_streams:
            return None
        best_audio = max(audio_streams, key=lambda s: s.get('bitrate', 0))
        tmp_path = download_dir / f"{safe_title}_tmp.m4a"
        if not _download_stream_file(best_audio['url'], tmp_path):
            return None
        # MP3 ga o'girish
        from config import get_ffmpeg_path
        mp3_path = download_dir / f"{safe_title}.mp3"
        try:
            subprocess.run([
                get_ffmpeg_path(), '-y', '-i', str(tmp_path),
                '-codec:a', 'libmp3lame', '-qscale:a', '2',
                str(mp3_path)
            ], capture_output=True, timeout=120, check=True)
            tmp_path.unlink(missing_ok=True)
        except Exception:
            mp3_path = tmp_path  # Konvertatsiya ishlamasa, asl faylni qaytarish
    else:
        target_h = int(quality) if quality in ["1080", "720", "480", "360"] else 720
        video_streams = data.get('videoStreams', [])
        audio_streams = data.get('audioStreams', [])
        if not video_streams:
            return None

        # MP4 video stream topish
        mp4_vids = [
            s for s in video_streams
            if 'video/mp4' in s.get('mimeType', '')
        ] or video_streams
        suitable = [s for s in mp4_vids if (s.get('height') or 0) <= target_h]
        if suitable:
            best_video = max(suitable, key=lambda s: s.get('height') or 0)
        else:
            best_video = min(mp4_vids, key=lambda s: s.get('height') or 0)

        video_only = best_video.get('videoOnly', True)

        if not video_only:
            # Combined stream
            output_path = download_dir / f"{safe_title}.mp4"
            if not _download_stream_file(best_video['url'], output_path):
                return None
        else:
            # Video + Audio alohida yuklab, birlashtirish
            vid_tmp = download_dir / f"{safe_title}_v.mp4"
            if not _download_stream_file(best_video['url'], vid_tmp):
                return None

            m4a_audios = [
                s for s in audio_streams
                if 'audio/mp4' in s.get('mimeType', '')
                   or 'audio/m4a' in s.get('mimeType', '')
            ] or audio_streams

            output_path = download_dir / f"{safe_title}.mp4"
            if m4a_audios:
                best_aud = max(m4a_audios, key=lambda s: s.get('bitrate', 0))
                aud_tmp = download_dir / f"{safe_title}_a.m4a"
                if _download_stream_file(best_aud['url'], aud_tmp):
                    if _merge_av(vid_tmp, aud_tmp, output_path):
                        vid_tmp.unlink(missing_ok=True)
                        aud_tmp.unlink(missing_ok=True)
                    else:
                        aud_tmp.unlink(missing_ok=True)
                        vid_tmp.rename(output_path)
                else:
                    vid_tmp.rename(output_path)
            else:
                vid_tmp.rename(output_path)

    logger.info(f"Piped download completed: {safe_title}")
    return {
        'title': data.get('title', 'Unknown'),
        'duration': data.get('duration', 0),
    }


# ===== Invidious API fallback =====

def _parse_quality_label(label: str) -> int:
    """'720p' → 720, 'unknown' → 0."""
    m = re.search(r'(\d+)p', str(label))
    return int(m.group(1)) if m else 0


def _invidious_get_video_info(url: str) -> dict[str, Any] | None:
    """Invidious API orqali video info olish."""
    video_id = _extract_video_id(url)
    if not video_id:
        return None
    instances = _fetch_invidious_instances()
    for instance in instances:
        try:
            logger.info(f"Trying Invidious info: {instance}")
            resp = requests.get(
                f"{instance}/api/v1/videos/{video_id}",
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('title'):
                    thumbs = data.get('videoThumbnails', [])
                    thumbnail = thumbs[0]['url'] if thumbs else ''
                    logger.info(f"Invidious info ({instance}) succeeded")
                    return {
                        'title': data['title'],
                        'duration': data.get('lengthSeconds', 0),
                        'thumbnail': thumbnail,
                        'uploader': data.get('author', 'Unknown'),
                        'webpage_url': url,
                    }
            else:
                logger.warning(f"Invidious {instance}: status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Invidious {instance} failed: {e}")
    return None


def _invidious_download_video(
    url: str, quality: str, download_dir: Path
) -> dict[str, Any] | None:
    """Invidious API orqali video yuklab olish."""
    video_id = _extract_video_id(url)
    if not video_id:
        return None

    instances = _fetch_invidious_instances()
    for instance in instances:
        try:
            logger.info(f"Trying Invidious download: {instance}")
            resp = requests.get(
                f"{instance}/api/v1/videos/{video_id}",
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if resp.status_code != 200:
                logger.warning(f"Invidious {instance}: status {resp.status_code}")
                continue

            data = resp.json()
            if not data.get('title'):
                continue

            title = data.get('title', 'video')[:50]
            safe_title = re.sub(r'[^\w\s-]', '', title).strip() or 'video'
            is_audio = quality == "audio"
            adaptive = data.get('adaptiveFormats', [])
            combined = data.get('formatStreams', [])

            if is_audio:
                audios = [f for f in adaptive if f.get('type', '').startswith('audio/')]
                if not audios:
                    continue
                best = max(audios, key=lambda f: int(f.get('bitrate', '0')))
                tmp = download_dir / f"{safe_title}_tmp.m4a"
                if not _download_stream_file(best['url'], tmp):
                    continue
                from config import get_ffmpeg_path
                mp3 = download_dir / f"{safe_title}.mp3"
                try:
                    subprocess.run([
                        get_ffmpeg_path(), '-y', '-i', str(tmp),
                        '-codec:a', 'libmp3lame', '-qscale:a', '2', str(mp3)
                    ], capture_output=True, timeout=120, check=True)
                    tmp.unlink(missing_ok=True)
                except Exception:
                    mp3 = tmp
            else:
                target_h = int(quality) if quality in ["1080", "720", "480", "360"] else 720
                videos = [f for f in adaptive if f.get('type', '').startswith('video/')]
                audios = [f for f in adaptive if f.get('type', '').startswith('audio/')]

                if videos:
                    suitable = [
                        v for v in videos
                        if _parse_quality_label(v.get('qualityLabel', '')) <= target_h
                    ]
                    if suitable:
                        best_v = max(
                            suitable,
                            key=lambda f: _parse_quality_label(f.get('qualityLabel', '')),
                        )
                    else:
                        best_v = min(
                            videos,
                            key=lambda f: _parse_quality_label(f.get('qualityLabel', '')) or 9999,
                        )

                    vid_tmp = download_dir / f"{safe_title}_v.mp4"
                    if not _download_stream_file(best_v['url'], vid_tmp):
                        continue

                    output = download_dir / f"{safe_title}.mp4"
                    if audios:
                        best_a = max(audios, key=lambda f: int(f.get('bitrate', '0')))
                        aud_tmp = download_dir / f"{safe_title}_a.m4a"
                        if _download_stream_file(best_a['url'], aud_tmp):
                            if _merge_av(vid_tmp, aud_tmp, output):
                                vid_tmp.unlink(missing_ok=True)
                                aud_tmp.unlink(missing_ok=True)
                            else:
                                aud_tmp.unlink(missing_ok=True)
                                vid_tmp.rename(output)
                        else:
                            vid_tmp.rename(output)
                    else:
                        vid_tmp.rename(output)
                elif combined:
                    output = download_dir / f"{safe_title}.mp4"
                    if not _download_stream_file(combined[0]['url'], output):
                        continue
                else:
                    continue

            logger.info(f"Invidious download completed: {safe_title}")
            return {
                'title': data.get('title', 'Unknown'),
                'duration': data.get('lengthSeconds', 0),
            }
        except Exception as e:
            logger.warning(f"Invidious {instance} download failed: {e}")
            continue
    return None


# ===== Cobalt API fallback =====

def _cobalt_download(url: str, quality: str, download_dir: Path) -> dict[str, Any] | None:
    """Cobalt API orqali video/audio yuklab olish."""
    is_audio = quality == "audio"
    
    for instance in COBALT_INSTANCES:
        try:
            payload: dict[str, Any] = {
                'url': url,
                'downloadMode': 'audio' if is_audio else 'auto',
            }
            if not is_audio and quality in ["1080", "720", "480", "360"]:
                payload['videoQuality'] = quality
            
            logger.info(f"Trying Cobalt: {instance}")
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0',
            }
            if COBALT_API_KEY:
                headers['Authorization'] = f'Api-Key {COBALT_API_KEY}'
            resp = requests.post(
                instance,
                json=payload,
                timeout=30,
                headers=headers,
            )
            
            if resp.status_code != 200:
                try:
                    body = resp.text[:300]
                except Exception:
                    body = ""
                logger.warning(f"Cobalt {instance}: status {resp.status_code}, body: {body}")
                continue
            
            data = resp.json()
            status = data.get('status', '')
            
            if status in ('tunnel', 'redirect'):
                stream_url = data.get('url', '')
                if not stream_url:
                    continue
                
                ext = 'mp3' if is_audio else 'mp4'
                output_path = download_dir / f"cobalt_video.{ext}"
                
                logger.info(f"Cobalt downloading: {status}")
                if _download_stream_file(stream_url, output_path):
                    logger.info("Cobalt download succeeded!")
                    return {
                        'title': 'YouTube Video',
                        'duration': 0,
                    }
            elif status == 'picker':
                # Multiple options - birinchisini olish
                picker = data.get('picker', [])
                if picker:
                    stream_url = picker[0].get('url', '')
                    if stream_url:
                        ext = 'mp3' if is_audio else 'mp4'
                        output_path = download_dir / f"cobalt_video.{ext}"
                        if _download_stream_file(stream_url, output_path):
                            logger.info("Cobalt picker download succeeded!")
                            return {'title': 'YouTube Video', 'duration': 0}
            elif status == 'error':
                logger.warning(f"Cobalt error: {data.get('error', {}).get('code', 'unknown')}")
            else:
                logger.warning(f"Cobalt unknown status: {status}")
                
        except Exception as e:
            logger.warning(f"Cobalt {instance} failed: {e}")
            continue
    
    return None


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
    
    # Barcha yt-dlp strategiyalari ishlamadi — Piped API bilan sinash
    logger.info("All yt-dlp strategies failed, trying Piped API...")
    piped_info = _piped_get_video_info(url)
    if piped_info:
        return piped_info

    # Piped ishlamadi — Invidious API bilan sinash
    logger.info("Piped failed, trying Invidious API...")
    invidious_info = _invidious_get_video_info(url)
    if invidious_info:
        return invidious_info

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
    
    # Barcha yt-dlp strategiyalari ishlamadi — Piped API bilan sinash
    logger.info("All yt-dlp strategies failed, trying Piped API download...")
    piped_result = _piped_download_video(url, quality, download_dir)
    if piped_result:
        return piped_result

    # Piped ham ishlamadi — Invidious API bilan sinash
    logger.info("Piped failed, trying Invidious API...")
    invidious_result = _invidious_download_video(url, quality, download_dir)
    if invidious_result:
        return invidious_result

    # Invidious ham ishlamadi — Cobalt API bilan sinash
    logger.info("Invidious failed, trying Cobalt API...")
    cobalt_result = _cobalt_download(url, quality, download_dir)
    if cobalt_result:
        return cobalt_result

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
