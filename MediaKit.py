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
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CONFIG_PATH = os.path.join(IMPORTANT_DIR, 'config.json')
SSL_ROOT_CERT = os.path.join(IMPORTANT_DIR, 'root.crt')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MediaBot")

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: config = json.load(f)
except FileNotFoundError: exit(f"CRITICAL: Config not found at {CONFIG_PATH}")

BOT_TOKEN = config.get("BOT_TOKEN")
ADMIN_ID = config.get("ADMIN_ID")
DB_CONFIG = config.get("DATABASE")

if not BOT_TOKEN: exit("CRITICAL: BOT_TOKEN missing")

PROXIES = config.get("PROXIES", {})
COOKIES = {k: os.path.join(BASE_DIR, v) for k, v in config.get("COOKIES", {}).items()}
HEADERS = config.get("HEADERS", {})
VK_CFG = config.get("VK", {})
YSK = config.get("YANDEX_SPEECHKIT", {})
YGPT = config.get("YANDEX_GPT", {})
EXCLUDED_CHATS = set(int(x) for x in config.get("EXCLUDED_CHATS", []))

ERROR_MSG_USER = "Error. Try again later or check the link"

EXACT_MATCHES = {
    "Да": "Пизда", "Нет": "Пидора ответ", "нет": "Пидора ответ", "да": "Пизда",
    "300": "Отсоси у тракториста", "Алло": "Хуем по лбу не дало?", "алло": "Хуем по лбу не дало?",
    "РКН": "Пидорасы", "Ркн": "Пидорасы", "ркн": "Пидорасы", "Звук говно": "Пиво дорогое"
}

reddit = asyncpraw.Reddit(**config["REDDIT"]) if config.get("REDDIT", {}).get("client_id") else None
s3_client = None
if YSK.get("S3_ACCESS_KEY_ID"):
    try:
        s3_client = boto3.client('s3', endpoint_url='https://storage.yandexcloud.net',
                                 aws_access_key_id=YSK.get("S3_ACCESS_KEY_ID"),
                                 aws_secret_access_key=YSK.get("S3_SECRET_ACCESS_KEY"))
    except Exception as e: logger.error(f"S3 Init Error: {e}")

db_pool = None

async def init_db(app):
    """Подключение к БД и создание таблицы при старте бота"""
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
    """Сохранение запроса в БД"""
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
    """Проверка кэша в БД (возвращает file_id или None)"""
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
        time.sleep(3600)
        now = time.time()
        for f in os.listdir(BASE_DIR):
            if f.endswith(('.mp3', '.mp4', '.part', '.webm', '.jpg', '.png', '.ogg')):
                if now - os.path.getmtime(os.path.join(BASE_DIR, f)) > 3600:
                    try: os.remove(os.path.join(BASE_DIR, f))
                    except: pass

async def notify_error(update: Update, context, exception_obj, context_info="Unknown"):
    """
    Отправляет уведомление об ошибке админу и пользователю.
    UPD: Добавлено отображение текста/ссылки, которая вызвала ошибку.
    """
    logger.error(f"🔥 Error in {context_info}: {exception_obj}")
    msg = update.effective_message
    
    # Уведомляем пользователя
    if msg:
        try: await msg.reply_text(ERROR_MSG_USER)
        except: pass
    
    # Уведомляем админа
    if ADMIN_ID:
        try:
            user_info = f"{msg.chat_id} (@{msg.from_user.username})" if msg else "Unknown"
            
            # Извлекаем текст сообщения (ссылку)
            content = "No text"
            if msg:
                if msg.text:
                    content = msg.text
                elif msg.caption:
                    content = msg.caption
            
            # Формируем сообщение для админа с ссылкой
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
    proxy = config.get("REDDIT", {}).get("proxy")
    cookie_file = COOKIES.get('reddit')
    cmd = ["/root/venv/bin/yt-dlp", "--impersonate", "chrome", "--output", fname, "--no-warnings"]
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
    fname = f"dl_{uuid.uuid4().hex}.mp4"
    opts = {'outtmpl': fname, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': 30, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'}
    if opts_update: opts.update(opts_update)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if (info.get('filesize') or 0) > 50 * 1024 * 1024: return None
            await asyncio.to_thread(ydl.download, [url])
        return fname if os.path.exists(fname) else None
    except: return None

async def download_router(url):
    if "instagram.com" in url:
        fname = f"inst_{uuid.uuid4().hex}.mp4"
        try:
            proc = await asyncio.to_thread(subprocess.run, ["/root/MediaKit/download_instagram.sh", url, fname], capture_output=True)
            return fname if proc.returncode == 0 and os.path.exists(fname) else None
        except: return None
    elif "reddit" in url: return await download_reddit_cli(url)
    opts = {}
    if "youtube" in url or "youtu.be" in url: opts = {'cookiefile': COOKIES.get('youtube'), 'proxy': PROXIES.get('youtube')}
    elif "tiktok" in url: opts = {'proxy': PROXIES.get('tiktok'), 'cookiefile': COOKIES.get('tiktok')}
    elif "vk.com" in url and VK_CFG.get('username'): opts = {'username': VK_CFG.get('username'), 'password': VK_CFG.get('password')}
    return await generic_download(url, opts)

async def convert_media(path, to_audio=False):
    if not path or not os.path.exists(path): return None
    out = f"{os.path.splitext(path)[0]}_c.{'mp3' if to_audio else 'mp4'}"
    cmd = ["ffmpeg", "-i", path, "-vn", "-b:a", "192k", out, "-y", "-loglevel", "error"] if to_audio else \
          ["ffmpeg", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", out, "-y", "-loglevel", "error"]
    try:
        await asyncio.to_thread(subprocess.run, cmd, check=True)
        os.remove(path)
        return out
    except: return None

async def extract_opus(video_path):
    out = f"{video_path}_speech.ogg"
    cmd = ["ffmpeg", "-i", video_path, "-vn", "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", out, "-y", "-loglevel", "error"]
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
        "messages": [{"role": "system", "text": YGPT.get("SYSTEM_PROMPT")}, {"role": "user", "text": f"Текст для обработки:\n{text}"}]
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post("https://llm.api.cloud.yandex.net/foundationModels/v1/completion", headers={"Authorization": f"Api-Key {YGPT['API_KEY']}"}, json=body) as resp:
                return (await resp.json())["result"]["alternatives"][0]["message"]["text"]
    except: return None

async def handle_message(update: Update, context):
    msg = update.effective_message
    if not msg or not msg.text: return
    txt, chat_id = msg.text.strip(), msg.chat_id
    user = msg.from_user

    if txt in EXACT_MATCHES and chat_id not in EXCLUDED_CHATS:
        return await msg.reply_text(EXACT_MATCHES[txt])

    if not any(d in txt for d in ["youtube", "youtu.be", "instagram", "tiktok", "reddit", "vk.com", "music.yandex", "spotify", "music.youtube"]): return

    detected_service = "Unknown"
    if "youtube" in txt or "youtu.be" in txt: detected_service = "YouTube"
    elif "instagram" in txt: detected_service = "Instagram"
    elif "tiktok" in txt: detected_service = "TikTok"
    elif "reddit" in txt: detected_service = "Reddit"
    elif "vk.com" in txt: detected_service = "VK"
    elif "music.yandex" in txt: detected_service = "YandexMusic"
    elif "spotify" in txt: detected_service = "Spotify"

    cached_file_id = await check_db_cache(txt)
    if cached_file_id:
        try:
            if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
                await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id, reply_to_message_id=msg.message_id)
            else:
                await context.bot.send_video(chat_id=chat_id, video=cached_file_id, reply_to_message_id=msg.message_id)
            
            await save_log(user.id, user.username or "Unknown", chat_id, txt, "Cached_Media", cached_file_id)
            return
        except Exception:
            logger.warning(f"Cache failed for {txt}, downloading again...")

    st_msg, f_path = None, None
    
    try:
        st_msg = await msg.reply_text("⏳ Analyzing...")

        if "music.yandex" in txt and "/album/" in txt and "/track/" not in txt:
            detected_service = "YandexAlbum"
            tracks = await asyncio.to_thread(get_ym_album_info, txt)
            if not tracks: raise Exception("Empty album")
            await st_msg.edit_text(f"💿 Альбом: {len(tracks)} треков...")
            for i, (title, artist) in enumerate(tracks):
                try:
                    dl_url = f"ytsearch1:{title} {artist}"
                    raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
                    if raw:
                        f_path_track = await convert_media(raw, to_audio=True)
                        with open(f_path_track, 'rb') as f: await context.bot.send_audio(chat_id, f, title=title, performer=artist)
                        os.remove(f_path_track)
                except: pass
            
            await save_log(user.id, user.username or "Unknown", chat_id, txt, detected_service)
            await st_msg.delete()
            return

        f_type, caption, title, artist = "video", "", None, None
        if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
            f_type = "audio"
            if "music.yandex" in txt: title, artist = await asyncio.to_thread(get_ym_track_info, txt)
            elif "spotify" in txt: title, artist = await asyncio.to_thread(get_spotify_info, txt)
            dl_url = f"ytsearch1:{title} {artist}" if (title and artist) else txt
            raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
            if not raw: raise Exception("Audio DL failed")
            f_path = await convert_media(raw, to_audio=True)
            caption = f"{artist} - {title}" if title else ""
        else:
            raw = await download_router(txt)
            if not raw: raise Exception("DL failed")
            f_path = await convert_media(raw)

        if f_path and os.path.exists(f_path):
            await st_msg.edit_text("📤 Sending...")
            with open(f_path, 'rb') as f:
                sent = await (context.bot.send_audio(chat_id, f, title=title, performer=artist, caption=caption, reply_to_message_id=msg.message_id) if f_type == "audio" else context.bot.send_video(chat_id, f, caption=caption, reply_to_message_id=msg.message_id))
            
            file_id = sent.audio.file_id if f_type == "audio" else sent.video.file_id
            await save_log(user.id, user.username or "Unknown", chat_id, txt, detected_service, file_id)
            
            await st_msg.delete()
        else: raise Exception("Conversion failed")

    except Exception as e:
        if st_msg: 
            try: await st_msg.delete()
            except: pass
        await notify_error(update, context, e, "Handle Message")
    finally:
        if f_path and os.path.exists(f_path): 
            try: os.remove(f_path)
            except: pass

async def handle_voice_video(update: Update, context):
    msg = update.effective_message
    if not all([YSK.get("API_KEY"), YSK.get("FOLDER_ID"), s3_client]): return
    st_msg, raw, audio, s3_key = None, None, None, None
    try:
        st_msg = await msg.reply_text("☁️ Listening...")
        is_note = bool(msg.video_note)
        f_obj = await (msg.video_note if is_note else msg.voice).get_file()
        raw = os.path.join(BASE_DIR, f"raw_{uuid.uuid4()}.{'mp4' if is_note else 'ogg'}")
        await f_obj.download_to_drive(raw)
        audio = await extract_opus(raw) if is_note else raw
        s3_key = f"speech/{os.path.basename(audio)}"
        
        uri = await upload_s3(audio, s3_key)
        if uri:
            full_text = await transcribe(uri)
            if full_text:
                summary = await summarize_text(full_text)
                await st_msg.edit_text(f"📝 **Суть:**\n{summary}" if summary else f"🗣 **Текст:**\n{full_text}", parse_mode="Markdown")
                user = msg.from_user
                await save_log(user.id, user.username or "Unknown", msg.chat_id, "Voice Message", "AI_SpeechKit")
            else: await st_msg.edit_text("🤔 Текст не распознан.")
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

def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(init_db).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("MediaBot Ready (DB Caching).")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice_video))
    logger.info("Bot Started with PostgreSQL Caching")
    app.run_polling()

if __name__ == "__main__":
    main()
