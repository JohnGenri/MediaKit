# MediaKit Telegram Bot

MediaKit is a Telegram bot that downloads media links, sends files to chat, caches sent `file_id` values in PostgreSQL, and processes voice/video-note messages with Yandex SpeechKit + YandexGPT.

## What Changed

- Bot works through a local Telegram Bot API endpoint (`telegram.api_base_url` in config).
- Upload size limit was increased from **50 MB** to **200 MB** (`limits.max_file_size_mb`).
- Download format is capped at 720p by default (`downloads.ytdlp.default_format`).
- Service-specific conversion behavior is controlled by `features.skip_conversion_services`.

Current skip-conversion list:
- YouTube
- TikTok
- PornHub
- Pinterest

Instagram and Reddit are still converted with ffmpeg. Music flow is unchanged (audio conversion to MP3 is kept).

## Main Features

- Media download: YouTube, Instagram, TikTok, Reddit, PornHub, Pinterest, Yandex Music, Spotify, YouTube Music.
- DB cache by normalized link: repeated links are served from cache when possible.
- Admin logging and request history in PostgreSQL.
- Voice and video-note transcription + summary (Yandex cloud services).
- Local temp file cleanup loop.

## Installation

1. Clone repository:
```bash
git clone https://github.com/JohnGenri/MediaKit.git
cd MediaKit
```

2. Create venv and install dependencies:
```bash
python3 -m venv /root/venv
source /root/venv/bin/activate
pip install -r requirements.txt
```

3. Prepare files:
```bash
cp download_instagram.sh.example download_instagram.sh
chmod +x download_instagram.sh
cp important/config.json.example important/config.json
```

4. Fill `important/config.json` with real credentials and tokens.

## Config Overview

`important/config.json` contains all runtime settings in one structured file.

- `telegram`: bot token, admin id, local API base URL, Telegram request timeouts.
- `database`: PostgreSQL credentials and host/port/db name.
- `network`: cookies, proxies, and request headers.
- `integrations`: external API keys and integration settings (RapidAPI, Reddit, Yandex services).
- `limits`: file size limit, concurrency, cleanup timers, admin pagination size.
- `downloads`: yt-dlp defaults (format and socket timeout).
- `messages`: all user-facing status/error/start texts.
- `features`: excluded chats, exact text replies, and skip-conversion services.

## Run With systemd

Example service:

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

Commands:
```bash
systemctl daemon-reload
systemctl enable mediakit.service
systemctl restart mediakit.service
systemctl status mediakit.service
```
