import asyncio
import aiohttp
import base64
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict
import yt_dlp
from concurrent.futures import ThreadPoolExecutor

from config import TEMP_DIR, get_ffmpeg_path

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2)


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


async def recognize_song_audd(audio_path: str, api_token: str = "") -> SongInfo | None:
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
                        logger.warning(f"AudD API error: {result.get('error', {}).get('error_message', 'unknown')}")
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
    
    ydl_opts: dict[str, object] = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'ffmpeg_location': ffmpeg_path,
        'js_runtimes': {'node': {}, 'deno': {}},
        'remote_components': ['ejs:github'],
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }
    
    # Cookies mavjud bo'lsa — server uchun
    if YOUTUBE_COOKIES_FILE:
        ydl_opts['cookiefile'] = str(YOUTUBE_COOKIES_FILE)
        ydl_opts['extractor_args'] = {'youtube': {
            'player_client': ['default'],
        }}
    else:
        ydl_opts['extractor_args'] = {'youtube': {
            'player_client': ['default', 'android_vr'],
        }}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
            ydl.download([query])
        
        for file in output_dir.iterdir():
            if file.suffix == '.mp3':
                return str(file)
    except Exception as e:
        logger.error(f"Song download failed: {e}")
    
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
