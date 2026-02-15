import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from config import BOT_TOKEN, LOCAL_BOT_API, LOCAL_BOT_API_URL

from handlers import start, help, message_handler
from handlers import youtube_handler
from handlers import callback_handler


def setup_logging() -> None:
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                logs_dir / "bot.log",
                encoding="utf-8"
            )
        ]
    )
    
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("Starting Instagram + YouTube Downloader Bot...")
    
    # Local Bot API server (2GB gacha fayl yuklash)
    session = None
    if LOCAL_BOT_API and LOCAL_BOT_API_URL:
        api_url = LOCAL_BOT_API_URL.rstrip('/')
        server = TelegramAPIServer.from_base(api_url)
        session = AiohttpSession(api=server)
        logger.info(f"Using Local Bot API: {api_url}")
    else:
        logger.info("Using official Telegram Bot API (50MB limit)")
    
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    
    dp = Dispatcher()
    
    dp.include_router(start.router)
    dp.include_router(help.router)
    dp.include_router(callback_handler.router)
    dp.include_router(youtube_handler.router)
    dp.include_router(message_handler.router)
    
    logger.info("Handlers registered successfully")
    try:
        logger.info("Bot is running! Press Ctrl+C to stop.")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Error during polling: {e}")
        raise
    finally:
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
