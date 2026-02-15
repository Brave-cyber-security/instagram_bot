import asyncio
import aiohttp
import base64
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict
import requests
import yt_dlp
from shazamio import Shazam, Serialize
from concurrent.futures import ThreadPoolExecutor

from config import TEMP_DIR, get_ffmpeg_path

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)
_audd_token_valid = True  # AudD token ishlayaptimi yoki yo'q


class SongInfo(TypedDict):
    title: str
    artist: str
    album: str
    youtube_query: str


class MusicRecognitionError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


def _get_video_duration(video_path: str) -> float:
    """Video davomiyligini sekundlarda qaytaradi."""
    ffmpeg_exe = get_ffmpeg_path()
    # ffprobe yoki ffmpeg orqali duration olish
    ffprobe_exe = str(ffmpeg_exe).replace("ffmpeg", "ffprobe")
    cmd = [
        ffprobe_exe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return float(data.get("format", {}).get("duration", 0))
    except Exception:
        pass
    return 0.0


def extract_audio_from_video(video_path: str, output_path: str, start_sec: float = 0) -> bool:
    """Video dan audio chiqarish. start_sec — qaysi sekunddan boshlash."""
    ffmpeg_exe = get_ffmpeg_path()
    
    cmd = [
        str(ffmpeg_exe),
        "-ss", str(start_sec),
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "128k",
        "-t", "20",
        "-y",
        output_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        return Path(output_path).exists() and Path(output_path).stat().st_size > 1000
    except Exception as e:
        logger.error(f"Audio extraction failed: {e}")
        return False


async def recognize_song_shazam(audio_path: str) -> SongInfo | None:
    """Shazam orqali musiqa aniqlash — bepul, API key kerak emas."""
    try:
        shazam = Shazam()
        result = await shazam.recognize(audio_path)
        
        track = result.get("track")
        if track:
            title = track.get("title", "Unknown")
            artist = track.get("subtitle", "Unknown")
            # Metadata dan album olish
            album = ""
            for section in track.get("sections", []):
                if section.get("type") == "SONG":
                    for meta in section.get("metadata", []):
                        if meta.get("title") == "Album":
                            album = meta.get("text", "")
                            break
            
            logger.info(f"Shazam recognized: {artist} - {title}")
            return SongInfo(
                title=title,
                artist=artist,
                album=album,
                youtube_query=f"{artist} {title} official audio"
            )
        else:
            logger.info("Shazam: no track found in audio")
    except Exception as e:
        logger.error(f"Shazam recognition failed: {e}")
    
    return None


async def recognize_song_audd(audio_path: str, api_token: str = "") -> SongInfo | None:
    global _audd_token_valid
    
    if not api_token or not _audd_token_valid:
        return None
    
    try:
        file_size = Path(audio_path).stat().st_size
        logger.info(f"Sending audio to AudD API (size: {file_size} bytes, token: {'custom' if api_token else 'test'})")
        
        with open(audio_path, "rb") as f:
            audio_data = base64.b64encode(f.read()).decode()
        
        async with aiohttp.ClientSession() as session:
            data = {
                "audio": audio_data,
                "return": "spotify,apple_music",
                "api_token": api_token if api_token else "test"
            }
            
            async with session.post("https://api.audd.io/", data=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    
                    logger.info(f"AudD API response: status={result.get('status')}, result={'found' if result.get('result') else 'none'}")
                    
                    if result.get("status") != "success":
                        error_msg = result.get('error', {}).get('error_message', 'unknown')
                        logger.warning(f"AudD API error: {error_msg}")
                        # Token noto'g'ri bo'lsa, qayta urinmaslik
                        if 'authorization' in error_msg.lower() or 'api_token' in error_msg.lower():
                            _audd_token_valid = False
                            logger.warning("AudD token invalid — disabling AudD for this session")
                        return None
                    
                    if result.get("result"):
                        song = result["result"]
                        return SongInfo(
                            title=song.get("title", "Unknown"),
                            artist=song.get("artist", "Unknown"),
                            album=song.get("album", ""),
                            youtube_query=f"{song.get('artist', '')} {song.get('title', '')} official audio"
                        )
                else:
                    logger.error(f"AudD API HTTP error: {resp.status}")
    except Exception as e:
        logger.error(f"AudD recognition failed: {e}")
    
    return None


def _sync_download_song(query: str, output_dir: Path) -> str | None:
    from config import YOUTUBE_COOKIES_FILE
    ffmpeg_path = get_ffmpeg_path()
    
    base_opts: dict[str, object] = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'ffmpeg_location': ffmpeg_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }
    
    # Bir nechta strategiya bilan sinash
    strategies: list[tuple[str, dict[str, object]]] = [
        ("android", {
            **base_opts,
            'extractor_args': {'youtube': {'player_client': ['android']}},
        }),
        ("web_creator+cookies", {
            **base_opts,
            'cookiefile': str(YOUTUBE_COOKIES_FILE) if YOUTUBE_COOKIES_FILE else None,
            'extractor_args': {'youtube': {'player_client': ['web_creator']}},
        }),
        ("mweb", {
            **base_opts,
            'extractor_args': {'youtube': {'player_client': ['mweb']}},
        }),
        ("ios", {
            **base_opts,
            'extractor_args': {'youtube': {'player_client': ['ios']}},
        }),
    ]
    # cookiefile=None bo'lsa, o'chirish
    for _, opts in strategies:
        if opts.get('cookiefile') is None:
            opts.pop('cookiefile', None)
    
    for label, ydl_opts in strategies:
        # Oldingi urinishdan qolgan fayllarni tozalash
        for f in output_dir.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        
        try:
            logger.info(f"Song download strategy: {label}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                ydl.download([query])
            
            for file in output_dir.iterdir():
                if file.suffix == '.mp3':
                    logger.info(f"Song download '{label}' succeeded!")
                    return str(file)
        except Exception as e:
            error_msg = str(e).lower()
            if "sign in" in error_msg or "bot" in error_msg or "cookies" in error_msg:
                logger.warning(f"Song download '{label}' blocked, trying next...")
                continue
            logger.error(f"Song download '{label}' failed: {e}")
            continue
    
    # Barcha yt-dlp strategiyalar ishlamadi — Piped API bilan sinash
    logger.info("All yt-dlp song strategies failed, trying Piped API...")
    return _piped_download_song(query, output_dir)


# ===== Piped API fallback for song download =====

PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://pipedapi.mha.fi",
    "https://api.piped.yt",
]


def _piped_download_song(query: str, output_dir: Path) -> str | None:
    """Piped API orqali qo'shiqni qidirish va yuklab olish."""
    ffmpeg_path = get_ffmpeg_path()
    
    # 1. Piped orqali qidirish
    video_id = None
    for instance in PIPED_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/search",
                params={'q': query, 'filter': 'music_songs'},
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'},
                allow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get('items', [])
                if items:
                    url_path = items[0].get('url', '')
                    match = re.search(r'v=([\w-]+)', url_path)
                    if not match:
                        match = re.search(r'/shorts/([\w-]+)', url_path)
                    if match:
                        video_id = match.group(1)
                        logger.info(f"Piped search found: {video_id} on {instance}")
                        break
        except Exception as e:
            logger.warning(f"Piped search {instance} failed: {e}")
            continue
    
    if not video_id:
        logger.error("Piped: no search results")
        return None
    
    # 2. Piped orqali stream olish
    for instance in PIPED_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/streams/{video_id}",
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'},
                allow_redirects=False,
            )
            if resp.status_code != 200:
                continue
            streams = resp.json()
            audio_streams = streams.get('audioStreams', [])
            if not audio_streams:
                continue
            
            best_audio = max(audio_streams, key=lambda s: s.get('bitrate', 0))
            title = streams.get('title', 'song')[:50]
            safe_title = re.sub(r'[^\w\s-]', '', title).strip() or 'song'
            
            # 3. Audio yuklab olish
            tmp_path = output_dir / f"{safe_title}_tmp.m4a"
            audio_resp = requests.get(
                best_audio['url'], stream=True, timeout=120,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            audio_resp.raise_for_status()
            with open(tmp_path, 'wb') as f:
                for chunk in audio_resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            
            if tmp_path.stat().st_size == 0:
                tmp_path.unlink(missing_ok=True)
                continue
            
            # 4. MP3 ga o'girish
            mp3_path = output_dir / f"{safe_title}.mp3"
            try:
                subprocess.run([
                    ffmpeg_path, '-y', '-i', str(tmp_path),
                    '-codec:a', 'libmp3lame', '-qscale:a', '2',
                    str(mp3_path)
                ], capture_output=True, timeout=120, check=True)
                tmp_path.unlink(missing_ok=True)
                logger.info(f"Piped song download succeeded: {safe_title}")
                return str(mp3_path)
            except Exception:
                if tmp_path.exists():
                    logger.info("FFmpeg failed, returning raw audio")
                    return str(tmp_path)
        except Exception as e:
            logger.warning(f"Piped stream {instance} failed: {e}")
            continue
    
    return None


async def download_full_song(song_info: SongInfo, output_dir: Path) -> str | None:
    loop = asyncio.get_event_loop()
    query = song_info["youtube_query"]
    
    return await loop.run_in_executor(_executor, _sync_download_song, query, output_dir)


async def recognize_only(video_path: str, api_token: str = "") -> SongInfo | None:
    audio_dir = Path(video_path).parent
    audio_path = str(audio_dir / "temp_audio_recognize.mp3")
    
    # Video davomiyligini aniqlash
    duration = _get_video_duration(video_path)
    logger.info(f"Video duration: {duration:.1f}s")
    
    # Sinab ko'rish kerak bo'lgan segmentlar (sekundlarda)
    segments = [0]  # har doim boshidan sinash
    if duration > 30:
        segments.append(duration * 0.3)  # 30% dan
    if duration > 60:
        segments.append(duration * 0.5)  # o'rtasidan
    if duration > 15:
        segments.append(max(5, duration * 0.15))  # 15% dan
    
    try:
        for i, start_sec in enumerate(segments):
            logger.info(f"Trying segment {i+1}/{len(segments)}: start={start_sec:.1f}s")
            
            if not extract_audio_from_video(video_path, audio_path, start_sec=start_sec):
                logger.warning(f"Could not extract audio from segment {i+1} (start={start_sec:.1f}s)")
                continue
            
            # 1. Shazam bilan sinash (bepul, aniqroq)
            song_info = await recognize_song_shazam(audio_path)
            
            # 2. Agar Shazam topmasa — AudD bilan sinash (fallback)
            if not song_info and api_token:
                logger.info("Shazam failed, trying AudD as fallback...")
                song_info = await recognize_song_audd(audio_path, api_token)
            
            # Temp faylni o'chirish
            try:
                Path(audio_path).unlink(missing_ok=True)
            except:
                pass
            
            if song_info:
                logger.info(f"Recognized (segment {i+1}): {song_info['artist']} - {song_info['title']}")
                return song_info
        
        logger.info(f"No song recognized in video after trying {len(segments)} segments")
        return None
    finally:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except:
            pass


async def recognize_and_download(video_path: str, output_dir: Path, api_token: str = "") -> tuple[SongInfo | None, str | None]:
    audio_path = str(output_dir / "temp_audio.mp3")
    
    # Video davomiyligini aniqlash
    duration = _get_video_duration(video_path)
    logger.info(f"Video duration: {duration:.1f}s")
    
    segments = [0]
    if duration > 30:
        segments.append(duration * 0.3)
    if duration > 60:
        segments.append(duration * 0.5)
    if duration > 15:
        segments.append(max(5, duration * 0.15))
    
    song_info = None
    for i, start_sec in enumerate(segments):
        logger.info(f"Trying segment {i+1}/{len(segments)}: start={start_sec:.1f}s")
        
        if not extract_audio_from_video(video_path, audio_path, start_sec=start_sec):
            logger.warning(f"Could not extract audio from segment {i+1}")
            continue
        
        # 1. Shazam bilan sinash (bepul, aniqroq)
        song_info = await recognize_song_shazam(audio_path)
        
        # 2. Agar Shazam topmasa — AudD bilan sinash (fallback)
        if not song_info and api_token:
            logger.info("Shazam failed, trying AudD as fallback...")
            song_info = await recognize_song_audd(audio_path, api_token)
        
        try:
            Path(audio_path).unlink()
        except:
            pass
        
        if song_info:
            logger.info(f"Recognized (segment {i+1}): {song_info['artist']} - {song_info['title']}")
            break
    
    if not song_info:
        logger.info(f"No song recognized in video after trying {len(segments)} segments")
        return None, None
    
    song_path = await download_full_song(song_info, output_dir)
    
    return song_info, song_path
