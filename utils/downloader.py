import asyncio
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import TypedDict, Literal
from concurrent.futures import ThreadPoolExecutor

import instaloader

from config import TEMP_DIR, MAX_FILE_SIZE

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=3)

_loader: instaloader.Instaloader | None = None


def _parse_netscape_cookies(cookies_file: Path) -> dict[str, str]:
    cookies = {}
    
    if not cookies_file.exists():
        return cookies
    
    try:
        with open(cookies_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split('\t')
                if len(parts) >= 7:
                    name = parts[5]
                    value = parts[6]
                    cookies[name] = value
    except Exception as e:
        logger.warning(f"Failed to parse cookies file: {e}")
    
    return cookies


def _get_loader() -> instaloader.Instaloader:
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            max_connection_attempts=3,
            request_timeout=60,
            quiet=True,
        )
        _try_load_session(_loader)
    return _loader


def _try_load_session(loader: instaloader.Instaloader) -> bool:
    cookies_file = TEMP_DIR.parent / "instagram.com_cookies.txt"
    
    if not cookies_file.exists():
        logger.warning("Cookies file not found, running without authentication")
        return False
    
    try:
        cookies = _parse_netscape_cookies(cookies_file)
        
        if not cookies:
            logger.warning("No cookies found in cookies file")
            return False
        
        sessionid = cookies.get('sessionid', '')
        csrftoken = cookies.get('csrftoken', '')
        ds_user_id = cookies.get('ds_user_id', '')
        
        if not sessionid:
            logger.warning("sessionid cookie not found")
            return False
        
        if not ds_user_id:
            logger.warning("ds_user_id cookie not found")
            return False
        
        loader.context._session.cookies.set('sessionid', sessionid, domain='.instagram.com')
        loader.context._session.cookies.set('csrftoken', csrftoken, domain='.instagram.com')
        loader.context._session.cookies.set('ds_user_id', ds_user_id, domain='.instagram.com')
        
        for name, value in cookies.items():
            if name not in ['sessionid', 'csrftoken', 'ds_user_id']:
                loader.context._session.cookies.set(name, value, domain='.instagram.com')
        
        loader.context._session.headers.update({
            'X-CSRFToken': csrftoken,
            'X-IG-App-ID': '936619743392459',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        
        loader.context.username = ds_user_id
        loader.context._session.cookies.set('ig_did', cookies.get('ig_did', ''), domain='.instagram.com')
        loader.context._session.cookies.set('mid', cookies.get('mid', ''), domain='.instagram.com')
        loader.context._session.cookies.set('rur', cookies.get('rur', ''), domain='.instagram.com')
        
        logger.info(f"Loaded Instagram session from cookies (user_id: {ds_user_id})")
        return True
        
    except Exception as e:
        logger.error(f"Failed to load session from cookies: {e}")
        return False


class DownloadResult(TypedDict):
    files: list[str]
    caption: str
    media_type: Literal["photo", "video", "album"]


class DownloadError(Exception):
    def __init__(self, message: str, error_type: str = "unknown"):
        self.message = message
        self.error_type = error_type
        super().__init__(self.message)


def _extract_shortcode(url: str) -> str | None:
    patterns = [
        r"instagram\.com/p/([A-Za-z0-9_-]+)",
        r"instagram\.com/reel/([A-Za-z0-9_-]+)",
        r"instagram\.com/tv/([A-Za-z0-9_-]+)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None


def _extract_story_info(url: str) -> tuple[str, str] | None:
    pattern = r"instagram\.com/stories/([A-Za-z0-9_.]+)/(\d+)"
    match = re.search(pattern, url)
    
    if match:
        return (match.group(1), match.group(2))
    
    return None


def _is_story_url(url: str) -> bool:
    return "instagram.com/stories/" in url


def _sync_download_post(shortcode: str, download_dir: Path) -> tuple[list[str], str, str]:
    loader = _get_loader()
    
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        
        caption = post.caption or ""
        if len(caption) > 900:
            caption = caption[:900] + "..."
        
        if post.typename == "GraphSidecar":
            media_type = "album"
        elif post.is_video:
            media_type = "video"
        else:
            media_type = "photo"
        
        loader.dirname_pattern = str(download_dir)
        loader.filename_pattern = "{shortcode}_{mediaid}"
        
        loader.download_post(post, target="")
        
        files = _collect_media_files(download_dir)
        
        return files, caption, media_type
        
    except instaloader.exceptions.LoginRequiredException:
        raise DownloadError("Login required for this content", "private")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise DownloadError("This is a private profile", "private")
    except instaloader.exceptions.ProfileNotExistsException:
        raise DownloadError("Profile or post not found", "not_found")
    except instaloader.exceptions.PostChangedException:
        raise DownloadError("Post was deleted or changed", "not_found")
    except instaloader.exceptions.QueryReturnedNotFoundException:
        raise DownloadError("Content not found", "not_found")
    except instaloader.exceptions.ConnectionException as e:
        error_msg = str(e).lower()
        if "429" in str(e):
            raise DownloadError("Rate limited, please try again later", "rate_limit")
        if "401" in str(e) or "403" in str(e):
            raise DownloadError("Authentication error", "private")
        raise DownloadError(f"Connection error: {e}", "download_failed")
    except Exception as e:
        logger.error(f"Error downloading post {shortcode}: {e}")
        raise DownloadError(str(e), "download_failed")


def _sync_download_story(username: str, story_id: str, download_dir: Path) -> tuple[list[str], str, str]:
    loader = _get_loader()
    
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        
        loader.dirname_pattern = str(download_dir)
        loader.filename_pattern = "{profile}_{mediaid}"
        
        stories = loader.get_stories(userids=[profile.userid])
        
        found_story = False
        caption = ""
        media_type = "video"
        
        for story in stories:
            for item in story.get_items():
                if str(item.mediaid) == story_id:
                    loader.download_storyitem(item, target="")
                    found_story = True
                    
                    if item.is_video:
                        media_type = "video"
                    else:
                        media_type = "photo"
                    
                    caption = item.caption or f"Story by @{username}"
                    break
            
            if found_story:
                break
        
        if not found_story:
            logger.warning(f"Story {story_id} not found in current stories, story may have expired")
            raise DownloadError("Story not found or has expired", "not_found")
        
        files = _collect_media_files(download_dir)
        
        if not files:
            raise DownloadError("No story files downloaded", "download_failed")
        
        return files, caption, media_type
        
    except DownloadError:
        raise
    except instaloader.exceptions.LoginRequiredException:
        raise DownloadError("Login required for stories", "private")
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise DownloadError("This is a private profile", "private")
    except instaloader.exceptions.ProfileNotExistsException:
        raise DownloadError("Profile not found", "not_found")
    except instaloader.exceptions.ConnectionException as e:
        if "429" in str(e):
            raise DownloadError("Rate limited, please try again later", "rate_limit")
        if "401" in str(e) or "403" in str(e):
            raise DownloadError("Authentication required for stories", "private")
        raise DownloadError(f"Connection error: {e}", "download_failed")
    except Exception as e:
        logger.error(f"Error downloading story {story_id} from {username}: {e}")
        raise DownloadError(str(e), "download_failed")


def _collect_media_files(download_dir: Path) -> list[str]:
    files: list[str] = []
    
    if not download_dir.exists():
        return files
    
    for file_path in download_dir.rglob("*"):
        if file_path.is_file():
            ext = file_path.suffix.lower()
            if ext in [".mp4", ".webm", ".mkv", ".mov", ".avi"]:
                files.append(str(file_path))
            elif ext in [".jpg", ".jpeg", ".png", ".webp"]:
                if "_thumb" not in file_path.stem.lower():
                    files.append(str(file_path))
    
    files.sort()
    
    return files


async def download_instagram_media(url: str) -> DownloadResult:
    download_id = str(uuid.uuid4())[:8]
    download_dir = TEMP_DIR / download_id
    download_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        if _is_story_url(url):
            story_info = _extract_story_info(url)
            if not story_info:
                raise DownloadError("Could not extract story info from URL", "invalid_url")
            
            username, story_id = story_info
            
            loop = asyncio.get_event_loop()
            files, caption, media_type = await loop.run_in_executor(
                _executor,
                _sync_download_story,
                username,
                story_id,
                download_dir
            )
        else:
            shortcode = _extract_shortcode(url)
            
            if not shortcode:
                raise DownloadError("Could not extract post ID from URL", "invalid_url")
            
            loop = asyncio.get_event_loop()
            files, caption, media_type = await loop.run_in_executor(
                _executor,
                _sync_download_post,
                shortcode,
                download_dir
            )
        
        if not files:
            raise DownloadError("No media files downloaded", "download_failed")
        
        for file_path in files:
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                raise DownloadError(
                    f"File too large: {file_size / (1024*1024):.1f}MB",
                    "file_too_large"
                )
        
        return DownloadResult(
            files=files,
            caption=caption,
            media_type=media_type
        )
        
    except DownloadError:
        await cleanup_files(download_dir)
        raise
    except Exception as e:
        logger.error(f"Unexpected error downloading {url}: {e}")
        await cleanup_files(download_dir)
        raise DownloadError(str(e), "unknown")


async def cleanup_files(path: Path | str) -> None:
    try:
        path = Path(path)
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        logger.debug(f"Cleaned up: {path}")
    except Exception as e:
        logger.warning(f"Failed to cleanup {path}: {e}")


async def cleanup_download_result(result: DownloadResult) -> None:
    for file_path in result.get("files", []):
        path = Path(file_path)
        if path.parent.exists() and path.parent.parent == TEMP_DIR:
            await cleanup_files(path.parent)
            break
        else:
            await cleanup_files(path)
