import os
import json
import time
import uuid
import re
import logging
import asyncio
import threading
import subprocess
import requests
import boto3
import aiohttp
import yt_dlp
import asyncpraw
import traceback
import asyncpg
import ssl
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler, ContextTypes
from telegram.error import TimedOut

# Always disable .pyc generation regardless of launch mode.
sys.dont_write_bytecode = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CONFIG_PATH = os.path.join(IMPORTANT_DIR, 'config.json')
SSL_ROOT_CERT = os.path.join(IMPORTANT_DIR, 'root.crt')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MediaBot")

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: config = json.load(f)
except FileNotFoundError: exit(f"CRITICAL: Config not found at {CONFIG_PATH}")

def cfg(path, default=None):
    cur = config
    for key in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def extract_primary_url(text):
    match = re.search(r"https?://\S+", text or "")
    if not match:
        return (text or "").strip()
    return match.group(0).rstrip(").,]}>\"'")


def normalize_link_for_cache(link):
    raw = (link or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
        scheme = (parsed.scheme or "https").lower()
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")

        pairs = parse_qsl(parsed.query, keep_blank_values=False)
        if "pornhub.com" in host:
            # Keep only stable identity key for PH links.
            keep = {"viewkey"}
            pairs = [(k, v) for (k, v) in pairs if k.lower() in keep]
        elif host in {"youtube.com", "m.youtube.com", "youtu.be"}:
            # Drop tracking/timestamp keys so repeated links hit cache.
            keep = {"v", "list"}
            pairs = [(k, v) for (k, v) in pairs if k.lower() in keep]
        else:
            drop_exact = {"si", "feature", "fbclid", "gclid", "yclid"}
            pairs = [
                (k, v)
                for (k, v) in pairs
                if not k.lower().startswith("utm_") and k.lower() not in drop_exact
            ]

        query = urlencode(sorted(pairs))
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:
        return raw

# Core config
BOT_TOKEN = cfg("telegram.bot_token")
ADMIN_ID = cfg("telegram.admin_id")
DB_CONFIG = cfg("database")
TELEGRAM_API_BASE_URL = cfg("telegram.api_base_url", "https://tg.s-grishin.ru")

if not BOT_TOKEN: exit("CRITICAL: BOT_TOKEN missing")

# Integration/network config
PROXIES = cfg("network.proxies", {})
COOKIES = {k: os.path.join(BASE_DIR, v) for k, v in cfg("network.cookies", {}).items()}
HEADERS = cfg("network.headers", {})
YSK = cfg("integrations.yandex.speechkit", {})
YGPT = cfg("integrations.yandex.gpt", {})
RAPID_API_KEY = cfg("integrations.rapid_api.key")
REDDIT_CONFIG = cfg("integrations.reddit", {})

# Limits/performance config
MAX_FILE_SIZE = int(cfg("limits.max_file_size_mb", 200)) * 1024 * 1024
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(int(cfg("limits.download_concurrency", 4)))
ADMIN_BUTTON_CHUNK_SIZE = int(cfg("limits.admin_button_chunk_size", 50))
CLEANUP_INTERVAL_SEC = int(cfg("limits.cleanup_interval_sec", 3600))
CLEANUP_TTL_SEC = int(cfg("limits.cleanup_ttl_sec", 3600))

# UX/messages config
ERROR_MSG_USER = cfg("messages.error_user", "Error. Try again later or check the link")
TOO_LARGE_MSG = cfg("messages.too_large", "⚠️ File is too large (>200MB).")
STATUS_ANALYZING = cfg("messages.status.analyzing", "⏳ Analyzing...")
STATUS_SENDING = cfg("messages.status.sending", "📤 Sending...")
STATUS_LISTENING = cfg("messages.status.listening", "☁️ Listening...")
STATUS_ALBUM = cfg("messages.status.album", "💿 Album: {count} tracks...")
START_MESSAGE = cfg("messages.start", "MediaBot Ready (DB Caching).")

# Download behavior config
YTDLP_DEFAULT_FORMAT = cfg(
    "downloads.ytdlp.default_format",
    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]"
)
YTDLP_SOCKET_TIMEOUT = int(cfg("downloads.ytdlp.socket_timeout_sec", 30))
SKIP_CONVERSION_SERVICES = set(cfg("features.skip_conversion_services", ["YouTube", "TikTok", "PornHub"]))

# Feature toggles/data
EXCLUDED_CHATS = set(int(x) for x in cfg("features.excluded_chats", []))
EXACT_MATCHES = cfg("features.exact_matches", {})

# Telegram API timeouts
TG_CONNECT_TIMEOUT = int(cfg("telegram.timeouts.connect_sec", 30))
TG_READ_TIMEOUT = int(cfg("telegram.timeouts.read_sec", 600))
TG_WRITE_TIMEOUT = int(cfg("telegram.timeouts.write_sec", 600))
TG_POOL_TIMEOUT = int(cfg("telegram.timeouts.pool_sec", 30))
MAX_UPDATE_AGE_SEC = int(cfg("features.max_update_age_sec", 300))

reddit = asyncpraw.Reddit(**REDDIT_CONFIG) if REDDIT_CONFIG.get("client_id") else None
s3_client = None
if YSK.get("S3_ACCESS_KEY_ID"):
    try:
        s3_client = boto3.client('s3', endpoint_url='https://storage.yandexcloud.net',
                                 aws_access_key_id=YSK.get("S3_ACCESS_KEY_ID"),
                                 aws_secret_access_key=YSK.get("S3_SECRET_ACCESS_KEY"))
    except Exception as e: logger.error(f"S3 Init Error: {e}")

db_pool = None

async def init_db(app):
    """Connect to DB and create table on startup"""
    global db_pool
    if not DB_CONFIG:
        logger.warning("⚠️ Database config missing. Skipping DB setup.")
        return

    try:
        dsn = f"postgresql://{DB_CONFIG['USER']}:{DB_CONFIG['PASSWORD']}@{DB_CONFIG['HOST']}:{DB_CONFIG['PORT']}/{DB_CONFIG['DB_NAME']}"
        logger.info(f"🔌 Connecting to DB: {DB_CONFIG['HOST']}...")
        
        ssl_ctx = ssl.create_default_context(cafile=SSL_ROOT_CERT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED

        db_pool = await asyncpg.create_pool(dsn, ssl=ssl_ctx)
        
        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS requests_log (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    chat_id BIGINT,
                    link TEXT,
                    service TEXT,
                    file_id TEXT,
                    is_published BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_user_id ON requests_log(user_id);
                CREATE INDEX IF NOT EXISTS idx_link ON requests_log(link);
            ''')
            
        logger.info("✅ Database connected and schema ready.")
    except Exception as e:
        logger.error(f"❌ Database Init Error: {e}")

async def save_log(user_id, username, chat_id, link, service, file_id=None):
    """Save request to DB"""
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                '''INSERT INTO requests_log (user_id, username, chat_id, link, service, file_id) 
                   VALUES ($1, $2, $3, $4, $5, $6)''',
                user_id, username, chat_id, link, service, file_id
            )
        logger.info(f"📝 Logged: {username} -> {service}")
    except Exception as e:
        logger.error(f"⚠️ Log Error: {e}")

async def check_db_cache(link):
    """Check DB cache (returns file_id or None)"""
    if not db_pool: return None
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id FROM requests_log WHERE link = $1 AND file_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                link
            )
            return row['file_id'] if row else None
    except Exception as e:
        logger.error(f"⚠️ DB Cache Error: {e}")
        return None

def cleanup_loop():
    while True:
        time.sleep(CLEANUP_INTERVAL_SEC)
        now = time.time()
        for f in os.listdir(BASE_DIR):
            if f.endswith(('.mp3', '.mp4', '.part', '.webm', '.jpg', '.png', '.ogg')):
                if now - os.path.getmtime(os.path.join(BASE_DIR, f)) > CLEANUP_TTL_SEC:
                    try: os.remove(os.path.join(BASE_DIR, f))
                    except: pass

async def notify_error(update: Update, context, exception_obj, context_info="Unknown"):
    """
    Sends error notification to admin and user.
    """
    logger.error(f"🔥 Error in {context_info}: {exception_obj}")
    msg = update.effective_message
    
    # Notify user
    if msg:
        try: 
            await msg.reply_text(ERROR_MSG_USER)
        except Exception:
            # If reply fails (message deleted), try sending to chat directly
            try:
                await context.bot.send_message(chat_id=msg.chat_id, text=ERROR_MSG_USER)
            except: pass
        
        # Send sad cat
        try:
            await context.bot.send_photo(chat_id=msg.chat_id, photo=f"https://cataas.com/cat/sad?random={uuid.uuid4()}")
        except Exception: pass
    
    # Notify admin
    if ADMIN_ID:
        try:
            user_info = f"{msg.chat_id} (@{msg.from_user.username})" if msg else "Unknown"
            content = "No text"
            if msg:
                if msg.text: content = msg.text
                elif msg.caption: content = msg.caption
            
            admin_text = (
                f"🚨 **Error**\n"
                f"👤 {user_info}\n"
                f"💬 `{content}`\n"
                f"🛠 {context_info}\n"
                f"❌ `{exception_obj}`"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")

async def upload_s3(path, key):
    if not s3_client: return None
    try:
        await asyncio.to_thread(s3_client.upload_file, path, YSK["S3_BUCKET_NAME"], key)
        return f"https://storage.yandexcloud.net/{YSK['S3_BUCKET_NAME']}/{key}"
    except: return None

async def delete_s3(key):
    if s3_client:
        try: await asyncio.to_thread(s3_client.delete_object, Bucket=YSK["S3_BUCKET_NAME"], Key=key)
        except: pass

async def download_reddit_cli(url):
    fname = os.path.join(BASE_DIR, f"reddit_{uuid.uuid4().hex}.mp4")
    proxy = REDDIT_CONFIG.get("proxy") or PROXIES.get("reddit")
    cookie_file = COOKIES.get('reddit')
    cmd = ["nice", "-n", "19", "/root/venv/bin/yt-dlp", "--impersonate", "chrome", "--output", fname, "--no-warnings"]
    if proxy: cmd.extend(["--proxy", proxy])
    if cookie_file and os.path.exists(cookie_file): cmd.extend(["--cookies", cookie_file])
    cmd.append(url)
    logger.info(f"🚀 Executing Reddit CMD: {' '.join(cmd)}")
    try:
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(fname): return fname
        else:
            logger.error(f"❌ Reddit DL Failed. RC: {proc.returncode}\nERR: {proc.stderr}")
            return None
    except Exception as e:
        logger.error(f"❌ Reddit Exception: {e}")
        return None

async def generic_download(url, opts_update=None):
    dl_id = f"dl_{uuid.uuid4().hex}"
    outtmpl = os.path.join(BASE_DIR, f"{dl_id}.%(ext)s")
    opts = {'outtmpl': outtmpl, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': YTDLP_SOCKET_TIMEOUT, 'format': YTDLP_DEFAULT_FORMAT}
    if opts_update: opts.update(opts_update)
    
    def _run_ydl():
        produced_files = []
        def _hook(d):
            fn = d.get("filename")
            if d.get("status") == "finished" and fn:
                produced_files.append(fn)

        opts['progress_hooks'] = [_hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Check estimated size if available
                if (info.get('filesize') or info.get('filesize_approx') or 0) > MAX_FILE_SIZE: 
                    logger.warning(f"File too large (estimated): {url}")
                    return "TOO_LARGE"
                ydl.download([url])

            # Use actual output path reported by yt-dlp, then normalize to our random name.
            candidates = [p for p in produced_files if os.path.exists(p)]
            if not candidates:
                prefix = f"{dl_id}."
                for f in os.listdir(BASE_DIR):
                    if f.startswith(prefix) and not f.endswith((".part", ".ytdl", ".tmp")):
                        p = os.path.join(BASE_DIR, f)
                        if os.path.isfile(p):
                            candidates.append(p)

            if candidates:
                produced = max(candidates, key=os.path.getsize)
                ext = os.path.splitext(produced)[1] or ".mp4"
                final_path = os.path.join(BASE_DIR, f"{dl_id}{ext}")
                if os.path.abspath(produced) != os.path.abspath(final_path):
                    os.replace(produced, final_path)
                if os.path.getsize(final_path) > MAX_FILE_SIZE:
                    logger.warning(f"File too large (actual): {final_path}")
                    os.remove(final_path)
                    return "TOO_LARGE"
                return final_path
            return None
        except Exception as e:
            logger.error(f"YDL Error: {e}")
            return None

    return await asyncio.to_thread(_run_ydl)

async def download_pinterest(url):
    try:
        headers = {
            'x-rapidapi-key': RAPID_API_KEY,
            'x-rapidapi-host': "pinterest-video-and-image-downloader.p.rapidapi.com"
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://pinterest-video-and-image-downloader.p.rapidapi.com/pinterest", params={"url": url}, headers=headers) as resp:
                if resp.status != 200: return None
                data = await resp.json()
        
        if not data.get('success'): return None
        
        media_data = data.get('data', {})
        target_url = media_data.get('url')
        if not target_url: return None
        
        ext = 'jpg' if data.get('type') == 'image' else 'mp4'
        fname = f"pin_{uuid.uuid4().hex}.{ext}"
        
        async with aiohttp.ClientSession() as sess:
            async with sess.get(target_url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if len(content) > MAX_FILE_SIZE:
                        logger.warning(f"Pinterest file too large: {len(content)}")
                        return "TOO_LARGE"
                    with open(fname, 'wb') as f: f.write(content)
                    return fname
    except Exception as e:
        logger.error(f"Pinterest error: {e}")
    return None

async def download_router(url):
    low_url = (url or "").lower()
    if "pinterest" in low_url or "pin.it" in low_url:
        return await download_pinterest(url)
    elif "instagram.com" in low_url:
        fname = f"inst_{uuid.uuid4().hex}.mp4"
        try:
            # Use nice/ionice for the shell script too
            proc = await asyncio.to_thread(subprocess.run, ["nice", "-n", "19", "/root/MediaKit/download_instagram.sh", url, fname], capture_output=True)
            if proc.returncode == 0 and os.path.exists(fname):
                if os.path.getsize(fname) > MAX_FILE_SIZE:
                    os.remove(fname)
                    return "TOO_LARGE"
                return fname
            return None
        except: return None
    elif "reddit" in low_url:
        res = await download_reddit_cli(url)
        if res and os.path.exists(res) and os.path.getsize(res) > MAX_FILE_SIZE:
            os.remove(res)
            return "TOO_LARGE"
        return res
    elif "pornhub" in low_url:
        # Unified Pornhub path
        opts = {
            'proxy': PROXIES.get('pornhub') or PROXIES.get('youtube'),
            'nocheckcertificate': True,
            'quiet': True,
            'http_headers': {
                'Referer': 'https://www.pornhub.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
            },
            'format': YTDLP_DEFAULT_FORMAT,
            'max_filesize': MAX_FILE_SIZE
        }
        return await generic_download(url, opts)
    
    opts = {}
    if "youtube" in url or "youtu.be" in url: opts = {'cookiefile': COOKIES.get('youtube'), 'proxy': PROXIES.get('youtube')}
    elif "tiktok" in url: opts = {'proxy': PROXIES.get('tiktok'), 'cookiefile': COOKIES.get('tiktok')}
    # VK logic removed
    
    return await generic_download(url, opts)

async def convert_media(path, to_audio=False):
    if not path or not os.path.exists(path): return None
    if path == "TOO_LARGE": return "TOO_LARGE"
    
    if os.path.getsize(path) > MAX_FILE_SIZE:
        logger.warning(f"File too large for conversion: {path}")
        return "TOO_LARGE"

    out = f"{os.path.splitext(path)[0]}_c.{'mp3' if to_audio else 'mp4'}"
    # Add nice -n 19 to ffmpeg calls
    cmd = ["nice", "-n", "19", "ffmpeg", "-i", path, "-vn", "-b:a", "192k", out, "-y", "-loglevel", "error"] if to_audio else \
          ["nice", "-n", "19", "ffmpeg", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", out, "-y", "-loglevel", "error"]
    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True)
        os.remove(path)
        if os.path.exists(out) and os.path.getsize(out) > MAX_FILE_SIZE:
            os.remove(out)
            return "TOO_LARGE"
        return out
    except: return None

async def extract_opus(video_path):
    out = f"{video_path}_speech.ogg"
    cmd = ["nice", "-n", "19", "ffmpeg", "-i", video_path, "-vn", "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", out, "-y", "-loglevel", "error"]
    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True)
        return out
    except: return None

def get_proxies(): return {"http": PROXIES["yandex"], "https": PROXIES["yandex"]} if PROXIES.get("yandex") else None
def get_ym_track_info(url):
    try:
        resp = requests.get(f"https://api.music.yandex.net/tracks/{url.split('/')[-1].split('?')[0]}", headers={"Authorization": HEADERS.get("yandex_auth")}, proxies=get_proxies(), timeout=15)
        t = resp.json()['result'][0]
        return t['title'], ', '.join([a['name'] for a in t['artists']])
    except: return None, None
def get_spotify_info(url):
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        title = re.search(r'<meta property="og:title" content="(.*?)"', resp.text).group(1)
        artist = re.search(r'<meta property="og:description" content="(.*?)"', resp.text).group(1).split('·')[0].strip()
        return title, artist
    except: return None, None
def get_ym_album_info(url):
    try:
        match = re.search(r'/album/(\d+)', url)
        if not match: return []
        resp = requests.get(f"https://api.music.yandex.net/albums/{match.group(1)}/with-tracks", headers={"Authorization": HEADERS.get("yandex_auth")}, proxies=get_proxies(), timeout=15)
        tracks = []
        for volume in resp.json()['result'].get('volumes', []):
            for t in volume: tracks.append((t['title'], ', '.join([a['name'] for a in t['artists']])))
        return tracks
    except: return []

async def transcribe(s3_uri):
    headers = {"Authorization": f"Api-Key {YSK.get('API_KEY')}"}
    body = {"config": {"specification": {"languageCode": "ru-RU", "audioEncoding": "OGG_OPUS"}}, "folderId": YSK.get("FOLDER_ID"), "audio": {"uri": s3_uri}}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post("https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize", headers=headers, json=body) as resp:
                op_id = (await resp.json()).get("id")
        for _ in range(30):
            await asyncio.sleep(5)
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"https://operation.api.cloud.yandex.net/operations/{op_id}", headers=headers) as resp:
                    data = await resp.json()
                    if data.get("done"): return " ".join(c["alternatives"][0]["text"] for c in data.get("response", {}).get("chunks", []))
    except: return None
    return None

async def summarize_text(text):
    if not YGPT.get("API_KEY") or not text or len(text) < 10: return None
    body = {
        "modelUri": YGPT.get("MODEL_URI"),
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": 2000},
        "messages": [{"role": "system", "text": YGPT.get("SYSTEM_PROMPT")}, {"role": "user", "text": f"Text to process:\n{text}"}]
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion", headers={"Authorization": f"Api-Key {YGPT['API_KEY']}"}, json=body) as resp:
                return (await resp.json())["result"]["alternatives"][0]["message"]["text"]
    except: return None

async def update_status(context, chat_id, text, message_obj=None, reply_to_id=None, parse_mode=None):
    """
    Attempts to edit the message_obj.
    If the message does not exist (deleted) or message_obj is None - sends a new one.
    """
    if message_obj:
        try:
            await message_obj.edit_text(text, parse_mode=parse_mode)
            return message_obj
        except Exception:
            pass

    # Sending new message
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_id, parse_mode=parse_mode)
    except Exception as e:
        try:
            return await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=None, parse_mode=parse_mode)
        except Exception as e2:
            logger.error(f"Failed to send status update (fallback): {e2}")
        return None

async def download_tg_file_via_cloud(file_id, dst_path):
    """
    Fallback for local Bot API servers running in --local mode.
    In that mode getFile may return an absolute server-side path that is not downloadable via /file.
    """
    cloud_api = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(cloud_api, params={"file_id": file_id}, timeout=30) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                file_path = data.get("result", {}).get("file_path")
                if not file_path:
                    return False

            cloud_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            async with sess.get(cloud_file_url, timeout=120) as f_resp:
                if f_resp.status != 200:
                    return False
                content = await f_resp.read()
                with open(dst_path, "wb") as f:
                    f.write(content)
                return True
    except Exception as e:
        logger.warning(f"Cloud file fallback failed for {file_id}: {e}")
        return False


def is_stale_message(msg, max_age_sec=MAX_UPDATE_AGE_SEC):
    """Ignore old updates to prevent replay storms after endpoint switches/restarts."""
    try:
        msg_dt = msg.date
        if not msg_dt:
            return False
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        age_sec = (datetime.now(timezone.utc) - msg_dt).total_seconds()
        return age_sec > max_age_sec
    except Exception:
        return False

async def handle_message(update: Update, context):
    msg = update.effective_message
    if not msg or not msg.text: return
    if is_stale_message(msg):
        logger.info("Skipping stale text update")
        return
    txt, chat_id = msg.text.strip(), msg.chat_id
    user = msg.from_user
    source_link = extract_primary_url(txt)
    cache_link = normalize_link_for_cache(source_link)

    if txt in EXACT_MATCHES and chat_id not in EXCLUDED_CHATS:
        return await msg.reply_text(EXACT_MATCHES[txt])

    # VK removed from trigger list
    if not any(d in txt.lower() for d in ["youtube", "youtu.be", "instagram", "tiktok", "reddit", "music.yandex", "spotify", "music.youtube", "pornhub", "pinterest", "pin.it"]): return

    detected_service = "Unknown"
    if "youtube" in txt or "youtu.be" in txt: detected_service = "YouTube"
    elif "instagram" in txt: detected_service = "Instagram"
    elif "tiktok" in txt: detected_service = "TikTok"
    elif "reddit" in txt: detected_service = "Reddit"
    # VK removed from detection logic
    elif "music.yandex" in txt: detected_service = "YandexMusic"
    elif "spotify" in txt: detected_service = "Spotify"
    elif "pornhub" in txt.lower(): detected_service = "PornHub"
    elif "pinterest" in txt.lower() or "pin.it" in txt.lower(): detected_service = "Pinterest"

    cached_file_id = await check_db_cache(cache_link)
    if not cached_file_id and cache_link != source_link:
        cached_file_id = await check_db_cache(source_link)
    if cached_file_id:
        try:
            try:
                if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
                    await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id, reply_to_message_id=msg.message_id)
                else:
                    await context.bot.send_video(chat_id=chat_id, video=cached_file_id, reply_to_message_id=msg.message_id)
            except Exception:
                if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
                    await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id, reply_to_message_id=None)
                else:
                    await context.bot.send_video(chat_id=chat_id, video=cached_file_id, reply_to_message_id=None)
            
            await save_log(user.id, user.username or "Unknown", chat_id, cache_link, "Cached_Media", cached_file_id)
            return
        except Exception:
            logger.warning(f"Cache failed for {cache_link}, downloading again...")

    # Wait for slot
    # Spawn background task so main loop isn't blocked
    asyncio.create_task(process_download(update, context, txt, source_link, cache_link, detected_service, user, chat_id, msg))

async def process_download(update, context, txt, source_link, cache_link, detected_service, user, chat_id, msg):
    # Move semaphore here so we block inside the task, not the main loop
    try:
        async with DOWNLOAD_SEMAPHORE:
             await _process_download_inner(update, context, txt, source_link, cache_link, detected_service, user, chat_id, msg)
    except Exception as e:
         logger.error(f"Processing Error: {e}")
         await notify_error(update, context, e, "Download Semaphore Block")

async def _process_download_inner(update, context, txt, source_link, cache_link, detected_service, user, chat_id, msg):
    st_msg, f_path = None, None
    try:
        st_msg = await update_status(context, chat_id, STATUS_ANALYZING, reply_to_id=msg.message_id)

        if "music.yandex" in txt and "/album/" in txt and "/track/" not in txt:
            detected_service = "YandexAlbum"
            tracks = await asyncio.to_thread(get_ym_album_info, txt)
            if not tracks: raise Exception("Empty album")
            
            st_msg = await update_status(
                context, chat_id, STATUS_ALBUM.format(count=len(tracks)), message_obj=st_msg, reply_to_id=msg.message_id
            )

            for i, (title, artist) in enumerate(tracks):
                try:
                    dl_url = f"ytsearch1:{title} {artist}"
                    raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
                    if raw:
                        f_path_track = await convert_media(raw, to_audio=True)
                        with open(f_path_track, 'rb') as f: 
                            try:
                                await context.bot.send_audio(chat_id, f, title=title, performer=artist)
                            except: pass 
                        os.remove(f_path_track)
                except: pass
            
            await save_log(user.id, user.username or "Unknown", chat_id, cache_link, detected_service)
            if st_msg: await st_msg.delete()
            return

        f_type, caption, title, artist = "video", "", None, None
        if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
            f_type = "audio"
            if "music.yandex" in txt: title, artist = await asyncio.to_thread(get_ym_track_info, txt)
            elif "spotify" in txt: title, artist = await asyncio.to_thread(get_spotify_info, txt)
            dl_url = f"ytsearch1:{title} {artist}" if (title and artist) else txt
            raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
            if not raw: raise Exception("Audio DL failed")
            if raw == "TOO_LARGE": raise Exception("TOO_LARGE")
            f_path = await convert_media(raw, to_audio=True)
            caption = f"{artist} - {title}" if title else ""
        else:
            raw = await download_router(source_link)
            if not raw: raise Exception("DL failed")
            if raw == "TOO_LARGE": raise Exception("TOO_LARGE")
            
            if raw.endswith(('.jpg', '.png', '.jpeg')):
                f_type = "image"
                f_path = raw
            elif detected_service in SKIP_CONVERSION_SERVICES:
                # Optitizaton: Skip conversion for YT/TikTok to save CPU
                logger.info(f"🚀 Skipping conversion for {detected_service}")
                f_path = raw
            else:
                f_path = await convert_media(raw)
                if f_path == "TOO_LARGE": raise Exception("TOO_LARGE")

        if f_path and os.path.exists(f_path):
            st_msg = await update_status(context, chat_id, STATUS_SENDING, message_obj=st_msg, reply_to_id=msg.message_id)
            
            with open(f_path, 'rb') as f:
                sent = None
                try:
                    if f_type == "audio":
                        sent = await context.bot.send_audio(
                            chat_id, f, title=title, performer=artist, caption=caption, reply_to_message_id=msg.message_id,
                            read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                        )
                    elif f_type == "image":
                        sent = await context.bot.send_photo(
                            chat_id, f, caption=caption, reply_to_message_id=msg.message_id,
                            read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                        )
                    else:
                        sent = await context.bot.send_video(
                            chat_id, f, caption=caption, reply_to_message_id=msg.message_id,
                            read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                        )
                except TimedOut as e:
                    # Upload may still complete on Telegram side; avoid duplicate resend and noisy false errors.
                    raise Exception("SEND_TIMEOUT") from e
                except Exception:
                    f.seek(0) 
                    try:
                        if f_type == "audio":
                            sent = await context.bot.send_audio(
                                chat_id, f, title=title, performer=artist, caption=caption, reply_to_message_id=None,
                                read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                            )
                        elif f_type == "image":
                            sent = await context.bot.send_photo(
                                chat_id, f, caption=caption, reply_to_message_id=None,
                                read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                            )
                        else:
                            sent = await context.bot.send_video(
                                chat_id, f, caption=caption, reply_to_message_id=None,
                                read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                            )
                    except TimedOut as e:
                        raise Exception("SEND_TIMEOUT") from e

            if sent:
                if f_type == "audio": file_id = sent.audio.file_id
                elif f_type == "image": file_id = sent.photo[-1].file_id
                else: file_id = sent.video.file_id
                await save_log(user.id, user.username or "Unknown", chat_id, cache_link, detected_service, file_id)
            
            if st_msg:
                try: await st_msg.delete()
                except: pass
        else: raise Exception("Conversion failed")

    except Exception as e:
        if st_msg: 
            try: await st_msg.delete()
            except: pass
        
        if str(e) == "TOO_LARGE" or "TOO_LARGE" in str(e):
            await context.bot.send_message(chat_id=chat_id, text=TOO_LARGE_MSG)
        elif str(e) == "SEND_TIMEOUT" or "SEND_TIMEOUT" in str(e):
            logger.warning(f"Media send timeout for chat {chat_id}, link: {source_link}")
        else:
            await notify_error(update, context, e, "Handle Message")
    finally:
        if f_path and os.path.exists(f_path): 
            try: os.remove(f_path)
            except: pass


async def handle_voice_video(update: Update, context):
    msg = update.effective_message
    if not msg:
        return
    if is_stale_message(msg):
        logger.info("Skipping stale voice/video update")
        return
    if not all([YSK.get("API_KEY"), YSK.get("FOLDER_ID"), s3_client]): return
    st_msg, raw, audio, s3_key = None, None, None, None
    try:
        st_msg = await update_status(context, msg.chat_id, STATUS_LISTENING, reply_to_id=msg.message_id)

        is_note = bool(msg.video_note)
        media_obj = msg.video_note if is_note else msg.voice
        f_obj = await media_obj.get_file()
        raw = os.path.join(BASE_DIR, f"raw_{uuid.uuid4()}.{'mp4' if is_note else 'ogg'}")
        try:
            await f_obj.download_to_drive(raw)
        except Exception:
            # Local Bot API may return absolute server-side file paths (not downloadable via /file).
            ok = await download_tg_file_via_cloud(media_obj.file_id, raw)
            if not ok:
                raise
        audio = await extract_opus(raw) if is_note else raw
        s3_key = f"speech/{os.path.basename(audio)}"
        
        uri = await upload_s3(audio, s3_key)
        if uri:
            full_text = await transcribe(uri)
            if full_text:
                summary = await summarize_text(full_text)
                final_text = f"📝 **Summary:**\n{summary}" if summary else f"🗣 **Text:**\n{full_text}"
                
                st_msg = await update_status(context, msg.chat_id, final_text, message_obj=st_msg, reply_to_id=msg.message_id, parse_mode="Markdown")
                
                user = msg.from_user
                await save_log(user.id, user.username or "Unknown", msg.chat_id, "Voice Message", "AI_SpeechKit")
            else:
                st_msg = await update_status(context, msg.chat_id, "🤔 Text not recognized.", message_obj=st_msg, reply_to_id=msg.message_id)
        else: raise Exception("S3 Upload Fail")
    except Exception as e:
        if st_msg: 
            try: await st_msg.delete()
            except: pass
        await notify_error(update, context, e, "Voice Handler")
    finally:
        if s3_key: asyncio.create_task(delete_s3(s3_key))
        for p in [raw, audio]:
            if p and os.path.exists(p): 
                try: os.remove(p)
                except: pass

# --- Admin Panel ---
BROADCAST_MSG = 1
DIRECT_MSG = 2

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return
    
    keyboard = [
        [InlineKeyboardButton("📢 Send Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📋 Show Chats", callback_data="admin_show_chats")]
    ]
    await update.message.reply_text("Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return
    await query.answer()
    
    if query.data == "admin_show_chats":
        await admin_show_chats(query, context)
        return ConversationHandler.END
    elif query.data == "admin_broadcast":
        await query.message.reply_text("Enter message to broadcast (or /cancel):")
        return BROADCAST_MSG
    elif query.data.startswith("admin_msg_"):
        target_id = query.data.split("_")[-1]
        context.user_data['target_chat_id'] = target_id
        await query.message.reply_text(f"Enter message for ID {target_id} (or /cancel):")
        return DIRECT_MSG

async def admin_show_chats(query, context):
    if not db_pool:
        await query.message.reply_text("DB not connected.")
        return

    try:
        async with db_pool.acquire() as conn:
            # unique chat_ids (groups/channels/users)
            chat_rows = await conn.fetch("SELECT DISTINCT chat_id FROM requests_log")
            
        users = []
        chats = []
        
        await query.message.reply_text("🔄 Fetching info...")

        for row in chat_rows:
            cid = row['chat_id']
            try:
                chat = await context.bot.get_chat(cid)
                title = chat.title or chat.username or chat.first_name or "Unknown"
                # Use standard chat types
                if chat.type == "private":
                    users.append({"id": cid, "name": f"{title} (@{chat.username})" if chat.username else title})
                else:
                    chats.append({"id": cid, "name": title})
            except Exception:
                # If we can't access it, assume it's just an ID we can't label
                pass

        # Helper to chunk buttons
        def chunk_buttons(items, header):
            buttons = []
            for item in items:
                buttons.append([InlineKeyboardButton(f"✉️ {item['name']}", callback_data=f"admin_msg_{item['id']}")])
            return buttons

        # Send Users
        if users:
            # Split if too many (limit 50 per message for safety, though TG allows 100)
            # We will just show list of buttons. 
            # If extremely large list, we might need multiple pages, but sticking to simple first.
            chunked_users = [users[i:i + ADMIN_BUTTON_CHUNK_SIZE] for i in range(0, len(users), ADMIN_BUTTON_CHUNK_SIZE)]
            for i, chunk in enumerate(chunked_users):
                kb = chunk_buttons(chunk, "User")
                await query.message.reply_text(f"👤 **Users** (Part {i+1}):", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        else:
            await query.message.reply_text("No active users found.")

        # Send Chats
        if chats:
            chunked_chats = [chats[i:i + ADMIN_BUTTON_CHUNK_SIZE] for i in range(0, len(chats), ADMIN_BUTTON_CHUNK_SIZE)]
            for i, chunk in enumerate(chunked_chats):
                kb = chunk_buttons(chunk, "Chat")
                await query.message.reply_text(f"📢 **Chats** (Part {i+1}):", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        else:
            await query.message.reply_text("No active chats found.")
            
    except Exception as e:
        logger.error(f"Admin Show Chats Error: {e}")
        await query.message.reply_text(f"Error: {e}")

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return ConversationHandler.END
    
    msg = update.effective_message
    txt = msg.text
    
    if not db_pool:
        await msg.reply_text("DB Error.")
        return ConversationHandler.END

    status_msg = await msg.reply_text("🚀 Starting broadcast...")
    
    success = 0
    fail = 0
    
    try:
        async with db_pool.acquire() as conn:
            # Get all unique users and chats
            rows = await conn.fetch("SELECT DISTINCT chat_id FROM requests_log UNION SELECT DISTINCT user_id AS chat_id FROM requests_log")
            
        targets = set(r['chat_id'] for r in rows if r['chat_id'])
        
        for cid in targets:
            try:
                await context.bot.send_message(chat_id=cid, text=txt)
                success += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05) # Rate limit safety
            
        await status_msg.edit_text(f"✅ Broadcast Complete.\nSuccess: {success}\nFailed: {fail}")
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
        
    return ConversationHandler.END

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def admin_direct_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return ConversationHandler.END
    
    target_id = context.user_data.get('target_chat_id')
    if not target_id:
        await update.message.reply_text("Error: No target selected.")
        return ConversationHandler.END

    msg = update.effective_message.text
    try:
        await context.bot.send_message(chat_id=target_id, text=msg)
        await update.message.reply_text(f"✅ Message sent to {target_id}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")
    
    return ConversationHandler.END

def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    api_base = TELEGRAM_API_BASE_URL.rstrip("/")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .base_url(f"{api_base}/bot")
        .base_file_url(f"{api_base}/file/bot")
        .connect_timeout(TG_CONNECT_TIMEOUT)
        .read_timeout(TG_READ_TIMEOUT)
        .write_timeout(TG_WRITE_TIMEOUT)
        .pool_timeout(TG_POOL_TIMEOUT)
        .post_init(init_db)
        .build()
    )
    
    # Admin Handlers
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_buttons, pattern="^admin_broadcast$"),
            CallbackQueryHandler(admin_buttons, pattern="^admin_msg_")
        ],
        states={
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)],
            DIRECT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_direct_send)],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)]
    )
    
    app.add_handler(CommandHandler("admin", admin_start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern="^admin_show_chats$"))

    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text(START_MESSAGE)))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice_video))
    logger.info("Bot Started with PostgreSQL Caching")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
