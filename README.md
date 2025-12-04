# 🤖 MediaKit Telegram Bot

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?style=for-the-badge&logo=python)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-13%2B-blue?style=for-the-badge&logo=postgresql)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue?style=for-the-badge&logo=telegram)
![Yandex Cloud](https://img.shields.io/badge/Yandex_Cloud-AI_Powered-red?style=for-the-badge&logo=yandex)

**MediaKit** is a robust, enterprise-grade Telegram bot designed to bridge the gap between popular media platforms and Telegram. It combines seamless **media downloading** with advanced **AI-powered voice processing**, backed by a high-performance PostgreSQL database.

The bot intelligently listens to chat messages. If it detects a link, it downloads the content; if it detects a voice message, it transcribes and summarizes it using Yandex Cloud Neural Networks.

---

## 🚀 Key Features

### ⚙️ Core Engineering & Database
* **PostgreSQL Backend:** Migrated from file-based caching to a robust **PostgreSQL** database. This ensures instant cache lookups, persistent logs, and concurrent handling of user requests.
* **High Performance:** Uses `asyncpg` for non-blocking database operations, significantly speeding up response times compared to the legacy JSON implementation.
* **Real-time Admin Alerts:** Includes a proactive error monitoring system. Critical errors, download failures, or exceptions are instantly reported to the administrator via Telegram, allowing for rapid debugging and maintenance.
* **Smart Caching:** Stores Telegram File IDs in the database. If User A requests a viral video, and User B requests it later, the bot retrieves the file ID from the DB and sends it instantly without re-downloading.

### 📥 Advanced Media Downloader
* **Resilient Reddit Scraper:** Features a custom **CLI-based wrapper** for Reddit downloads. It utilizes browser impersonation (`chrome`) and dedicated proxy support to bypass aggressive rate limits and bot detection systems, ensuring high success rates where standard libraries fail.
* **YouTube:** Downloads videos (optimized for size) and extracts audio.
* **Instagram:** Supports Reels, Stories, and Posts (via custom scripts with proxy support).
* **TikTok:** Downloads videos without watermarks.
* **VKontakte:** Downloads videos from VK.
* **Music Platforms:**
    * **Yandex.Music:** Downloads tracks and **full albums** in MP3 with metadata.
    * **Spotify:** Auto-matches Spotify links to YouTube Audio for seamless downloading.

### 🧠 AI & Intelligence
* **Voice Transcription:** Uses **Yandex SpeechKit** to convert voice messages and video notes (round messages) into text with high accuracy.
* **Smart Summarization of Audio Messages:** Integrated with **YandexGPT** to analyze long speech-to-text results. It provides a concise summary (TL;DR), automatically distinguishing between short phrases and long monologues.

---

## 🛠️ Installation

### System Requirements
Ensure these tools are installed on your server:
1.  **PostgreSQL** (Database server)
2.  **FFmpeg** (Crucial for media conversion)
3.  **yt-dlp** (Core media extractor)
4.  **aria2c** (Used for accelerated downloads)

### Setup Guide

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/JohnGenri/MediaKit.git](https://github.com/JohnGenri/MediaKit.git)
    cd MediaKit
    ```

2.  **Set up Virtual Environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install Python Dependencies:**
    Create a `requirements.txt` file (or use the provided one):
    ```txt
    python-telegram-bot
    asyncpraw
    yt-dlp
    requests
    boto3
    aiohttp
    asyncpg
    ```
    Install them:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Database Setup:**
    Ensure you have a PostgreSQL database created. You will need the connection details (Host, User, Password, DB Name) for the configuration step.

5.  **Prepare Helper Scripts:**
    ```bash
    chmod +x download_instagram.sh
    ```

---

## ⚙️ Configuration

The bot relies on a `config.json` file located in the `important/` directory.

1.  **Create the config file:**
    ```bash
    mkdir -p important
    touch important/config.json
    ```

2.  **Configuration Structure:**
    Copy the JSON below and fill in your credentials.

### `config.json` Example

```json
{
  "BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
  "ADMIN_ID": 123456789,

  "DATABASE": {
    "USER": "your_db_user",
    "PASSWORD": "your_db_password",
    "HOST": "your_db_host.yandexcloud.net",
    "PORT": "6432",
    "DB_NAME": "MediaKit"
  },

  "YANDEX_SPEECHKIT": {
    "API_KEY": "YOUR_YANDEX_API_KEY",
    "FOLDER_ID": "YOUR_YANDEX_FOLDER_ID",
    "S3_BUCKET_NAME": "your-s3-bucket-name",
    "S3_ACCESS_KEY_ID": "YOUR_AWS_ACCESS_KEY",
    "S3_SECRET_ACCESS_KEY": "YOUR_AWS_SECRET_KEY"
  },
  "YANDEX_GPT": {
    "API_KEY": "YOUR_YANDEX_API_KEY",
    "FOLDER_ID": "YOUR_YANDEX_FOLDER_ID",
    "MODEL_URI": "gpt://YOUR_FOLDER_ID/yandexgpt/rc",
    "SYSTEM_PROMPT": "Summarization service system prompt..."
  },
  "REDDIT": {
    "client_id": "YOUR_REDDIT_CLIENT_ID",
    "client_secret": "YOUR_REDDIT_SECRET",
    "user_agent": "MediaBot/1.0",
    "proxy": "socks5://user:pass@ip:port" 
  },
  "PROXIES": {
    "yandex": "http://user:pass@ip:port",
    "spotify": null,
    "tiktok": null,
    "youtube": null
  },
  "HEADERS": {
    "yandex_auth": "Bearer YOUR_YANDEX_MUSIC_TOKEN"
  },
  "COOKIES": {
    "youtube": "important/www.youtube.com_cookies.txt",
    "reddit": "important/www.reddit.com_cookies.txt",
    "tiktok": "important/www.tiktok.com_cookies.txt"
  },
  "VK": {
    "username": "phone_or_email",
    "password": "password"
  },
  "EXCLUDED_CHATS": [
    -1001234567890
  ]
}
