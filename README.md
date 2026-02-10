# Instagram & YouTube Downloader Bot 🤖

Telegram bot for downloading media from Instagram (Reels, Posts, Stories) and YouTube (Video, Shorts, Audio).
Features @Eshitbot-style behavior: no captions on media, branded "saved" message, and lazy music recognition/download.

## Features ✨

- 📸 **Instagram**: Download Reels, Posts, Stories
- 🎬 **YouTube**: Download Videos (1080p, 720p, etc.), Shorts
- 🎵 **Music**: Auto-recognize music in videos and download MP3 on demand
- 🧹 **Clean**: Videos sent without original captions
- 💾 **Save**: "Saqlash" button to save content
- 🚀 **Fast**: Async architecture

## Requirements 🛠

- Python 3.10+
- FFmpeg (installed automatically via Docker or `imageio-ffmpeg` locally)
- Telegram Bot Token
- (Optional) AudD API Token for better music recognition

## Local Installation 💻

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Brave-cyber-security/instagram_bot.git
   cd instagram_bot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create .env file:**
   Rename `.env.example` to `.env` (or create new) and add your tokens:
   ```env
   BOT_TOKEN=your_telegram_bot_token_here
   AUDD_API_TOKEN=your_audd_token_here
   ```

4. **Run the bot:**
   ```bash
   python bot.py
   ```

## Deployment 🚀

### Option 1: Docker (Recommended)

1. **Build and Run:**
   ```bash
   docker-compose up -d --build
   ```

### Option 2: DigitalOcean / VPS

1. **SSH into your server:**
   ```bash
   ssh root@your_server_ip
   ```

2. **Clone and Setup:**
   ```bash
   git clone https://github.com/Brave-cyber-security/instagram_bot.git
   cd instagram_bot
   ```

3. **Install Docker (if not installed):**
   ```bash
   curl -fsSL https://get.docker.com -o get-docker.sh
   sh get-docker.sh
   ```

4. **Run with Docker:**
   ```bash
   # Create .env file with your tokens
   nano .env 
   
   # Build and run
   docker build -t insta-bot .
   docker run -d --restart unless-stopped --name insta-bot --env-file .env insta-bot
   ```

## Project Structure mb
- `bot.py`: Entry point
- `handlers/`: Message handling logic
- `utils/`: Downloaders & helper functions
- `keyboards/`: Inline keyboards
- `config.py`: Configuration
