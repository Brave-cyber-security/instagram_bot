import asyncio
import aiohttp
import base64
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


def extract_audio_from_video(video_path: str, output_path: str) -> bool:
    ffmpeg_exe = get_ffmpeg_path()
    
    cmd = [
        str(ffmpeg_exe),
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "128k",
        "-t", "15",  
        "-y",
        output_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        return Path(output_path).exists()
    except Exception as e:
        logger.error(f"Audio extraction failed: {e}")
        return False


async def recognize_song_audd(audio_path: str, api_token: str = "") -> SongInfo | None:
    try:
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
                    
                    if result.get("status") == "success" and result.get("result"):
                        song = result["result"]
                        return SongInfo(
                            title=song.get("title", "Unknown"),
                            artist=song.get("artist", "Unknown"),
                            album=song.get("album", ""),
                            youtube_query=f"{song.get('artist', '')} {song.get('title', '')} official audio"
                        )
    except Exception as e:
        logger.error(f"AudD recognition failed: {e}")
    
    return None


def _sync_download_song(query: str, output_dir: Path) -> str | None:
    from config import YOUTUBE_COOKIES_FILE
    ffmpeg_path = get_ffmpeg_path()
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_dir / '%(title)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'ffmpeg_location': ffmpeg_path,
        'cookiefile': str(YOUTUBE_COOKIES_FILE) if YOUTUBE_COOKIES_FILE else None,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
    
    try:
        if not extract_audio_from_video(video_path, audio_path):
            logger.warning("Could not extract audio from video")
            return None
        
        song_info = await recognize_song_audd(audio_path, api_token)
        
        if not song_info:
            logger.info("No song recognized in video")
            return None
        
        logger.info(f"Recognized: {song_info['artist']} - {song_info['title']}")
        return song_info
    finally:
        try:
            Path(audio_path).unlink(missing_ok=True)
        except:
            pass


async def recognize_and_download(video_path: str, output_dir: Path, api_token: str = "") -> tuple[SongInfo | None, str | None]:
    audio_path = str(output_dir / "temp_audio.mp3")
    
    if not extract_audio_from_video(video_path, audio_path):
        logger.warning("Could not extract audio from video")
        return None, None
    
    song_info = await recognize_song_audd(audio_path, api_token)
    
    try:
        Path(audio_path).unlink()
    except:
        pass
    
    if not song_info:
        logger.info("No song recognized in video")
        return None, None
    
    logger.info(f"Recognized: {song_info['artist']} - {song_info['title']}")
    
    song_path = await download_full_song(song_info, output_dir)
    
    return song_info, song_path
