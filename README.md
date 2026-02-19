# MediaKit Telegram Bot

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-v20+-26A5E4?logo=telegram&logoColor=white)](https://github.com/python-telegram-bot/python-telegram-bot)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-asyncpg-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-downloader-ff0000)](https://github.com/yt-dlp/yt-dlp)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-media%20processing-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![Yandex SpeechKit](https://img.shields.io/badge/Yandex-SpeechKit%20%2B%20GPT-FFCC00)](https://cloud.yandex.com/)

MediaKit is a Telegram bot for media downloading, cached delivery, and voice/video-note transcription.

## Stack and Tooling

- `Python` + `python-telegram-bot` for Telegram polling and handlers
- `PostgreSQL` (`asyncpg`) for request log and media cache (`file_id`)
- `yt-dlp` + `ffmpeg` for download and conversion pipelines
- `Yandex SpeechKit` + `YandexGPT` for speech-to-text and summarization
- Local Telegram Bot API support via `telegram.api_base_url`

## Core Features

- Download media from:
  - YouTube, Instagram, TikTok, Reddit, PornHub, Pinterest
  - Yandex Music, Spotify, YouTube Music
- Normalize links for stable DB cache hits
- Reuse cached `file_id` when possible (faster repeat delivery)
- Voice/video-note transcription and summary
- Admin panel for chat list, direct message, and broadcast
- Background cleanup for temporary files

## Important: 5-Minute Update Expiration During Downtime

To prevent replay storms after restart, the bot intentionally drops stale updates.

- `drop_pending_updates=True` is enabled at polling startup.
- `features.max_update_age_sec` is set to `300` (5 minutes).

What this means:

- If the bot is down and a message waits too long (older than 5 minutes), that update can be skipped after restart.
- This is by design to avoid repeated old downloads/transcriptions.
- Users can resend links/messages if they were sent during downtime and got skipped.

This is update-age logic, not content deduplication by text.

## Configuration

Main config file: `important/config.json`

- `telegram`: token, admin ID, API base URL, request timeouts
- `database`: PostgreSQL connection settings
- `network`: proxies, cookies, and headers
- `integrations`: RapidAPI, Reddit, Yandex services
- `limits`: upload size, concurrency, cleanup intervals
- `downloads`: yt-dlp default format and socket timeout
- `messages`: all user-facing status and error strings
- `features`: excluded chats, exact matches, skip-conversion list, stale-update window

Current skip-conversion list:

- YouTube
- TikTok
- PornHub
- Pinterest

## Installation

1. Clone repository:

```bash
git clone https://github.com/JohnGenri/MediaKit.git
cd MediaKit
```

2. Create virtual environment and install dependencies:

```bash
python3 -m venv /root/venv
source /root/venv/bin/activate
pip install -r requirements.txt
```

3. Prepare runtime files:

```bash
cp download_instagram.sh.example download_instagram.sh
chmod +x download_instagram.sh
cp important/config.json.example important/config.json
```

4. Fill `important/config.json` with valid credentials and tokens.

## systemd Service Example

```ini
[Unit]
Description=MediaKit Telegram Bot
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/root/MediaKit
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/root/venv/bin/python3 /root/MediaKit/MediaKit.py
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Useful commands:

```bash
systemctl daemon-reload
systemctl enable mediakit.service
systemctl restart mediakit.service
systemctl status mediakit.service
journalctl -u mediakit -f
```
