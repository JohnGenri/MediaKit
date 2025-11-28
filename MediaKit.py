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
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CACHE_FILE = os.path.join(IMPORTANT_DIR, 'cache.json')
CONFIG_PATH = os.path.join(IMPORTANT_DIR, 'config.json')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MediaBot")

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: config = json.load(f)
except FileNotFoundError: exit(f"CRITICAL: Config not found at {CONFIG_PATH}")

BOT_TOKEN = config.get("BOT_TOKEN")
if not BOT_TOKEN: exit("CRITICAL: BOT_TOKEN missing")

PROXIES = config.get("PROXIES", {})
COOKIES = {k: os.path.join(BASE_DIR, v) for k, v in config.get("COOKIES", {}).items()}
HEADERS = config.get("HEADERS", {})
VK_CFG = config.get("VK", {})
YSK = config.get("YANDEX_SPEECHKIT", {})
YGPT = config.get("YANDEX_GPT", {})
EXCLUDED_CHATS = set(int(x) for x in config.get("EXCLUDED_CHATS", []))

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

def load_cache():
    if not os.path.exists(CACHE_FILE): return {}
    try:
        with open(CACHE_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_cache(data):
    try:
        with open(CACHE_FILE, 'w') as f: json.dump(data, f, indent=4)
    except Exception as e: logger.error(f"Cache Save Error: {e}")

def cleanup_loop():
    while True:
        time.sleep(3600)
        now = time.time()
        for f in os.listdir(BASE_DIR):
            if f.endswith(('.mp3', '.mp4', '.part', '.webm', '.jpg', '.png', '.ogg')):
                if now - os.path.getmtime(os.path.join(BASE_DIR, f)) > 3600:
                    try: os.remove(os.path.join(BASE_DIR, f))
                    except: pass

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

async def generic_download(url, opts_update=None):
    fname = f"dl_{uuid.uuid4().hex}.mp4"
    opts = {
        'outtmpl': fname, 'quiet': True, 'nocheckcertificate': True, 'socket_timeout': 30,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }
    if opts_update: opts.update(opts_update)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            if (info.get('filesize') or 0) > 50 * 1024 * 1024: return None
            await asyncio.to_thread(ydl.download, [url])
        return fname if os.path.exists(fname) else None
    except Exception as e:
        logger.error(f"DL Error: {e}")
        return None

async def download_router(url):
    if "instagram.com" in url:
        fname = f"inst_{uuid.uuid4().hex}.mp4"
        try:
            proc = await asyncio.to_thread(subprocess.run, ["/root/MediaKit/download_instagram.sh", url, fname], capture_output=True)
            return fname if proc.returncode == 0 and os.path.exists(fname) else None
        except: return None
    
    opts = {}
    if "youtube" in url or "youtu.be" in url:
        opts = {'cookiefile': COOKIES.get('youtube'), 'proxy': PROXIES.get('youtube')}
    elif "tiktok" in url:
        opts = {'proxy': PROXIES.get('tiktok'), 'cookiefile': COOKIES.get('tiktok')}
    elif "reddit" in url:
        opts = {'cookiefile': COOKIES.get('reddit'), 'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]}
    elif "vk.com" in url and VK_CFG.get('username'):
        opts = {'username': VK_CFG.get('username'), 'password': VK_CFG.get('password')}
    
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

def get_proxies():
    return {"http": PROXIES["yandex"], "https": PROXIES["yandex"]} if PROXIES.get("yandex") else None

def get_ym_track_info(url):
    try:
        tid = url.split('/')[-1].split('?')[0]
        resp = requests.get(
            f"https://api.music.yandex.net/tracks/{tid}", 
            headers={"Authorization": HEADERS.get("yandex_auth")},
            proxies=get_proxies(), timeout=15
        )
        resp.raise_for_status()
        t = resp.json()['result'][0]
        return t['title'], ', '.join([a['name'] for a in t['artists']])
    except Exception as e:
        logger.error(f"YM Track Error: {e}", exc_info=True)
        return None, None

def get_ym_album_info(url):
    try:
        match = re.search(r'/album/(\d+)', url)
        if not match: return []
        album_id = match.group(1)
        
        resp = requests.get(
            f"https://api.music.yandex.net/albums/{album_id}/with-tracks",
            headers={"Authorization": HEADERS.get("yandex_auth")},
            proxies=get_proxies(), timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        
        tracks = []
        if 'volumes' in data['result']:
            for volume in data['result']['volumes']:
                for t in volume:
                    artist = ', '.join([a['name'] for a in t['artists']])
                    tracks.append((t['title'], artist))
        return tracks
    except Exception as e:
        logger.error(f"YM Album Error: {e}", exc_info=True)
        return []

def get_spotify_info(url):
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        title = re.search(r'<meta property="og:title" content="(.*?)"', resp.text).group(1)
        artist = re.search(r'<meta property="og:description" content="(.*?)"', resp.text).group(1).split('·')[0].strip()
        return title, artist
    except: return None, None

async def transcribe(s3_uri):
    """Преобразование аудио в текст через Yandex SpeechKit"""
    headers = {"Authorization": f"Api-Key {YSK.get('API_KEY')}"}
    body = {"config": {"specification": {"languageCode": "ru-RU", "audioEncoding": "OGG_OPUS"}}, "folderId": YSK.get("FOLDER_ID"), "audio": {"uri": s3_uri}}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post("https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize", headers=headers, json=body) as resp:
                if resp.status != 200: return None
                op_id = (await resp.json()).get("id")
        for _ in range(30):
            await asyncio.sleep(5)
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"https://operation.api.cloud.yandex.net/operations/{op_id}", headers=headers) as resp:
                    data = await resp.json()
                    if data.get("done"): 
                        return " ".join(c["alternatives"][0]["text"] for c in data.get("response", {}).get("chunks", []))
        return None
    except: return None

async def summarize_text(text):
    """Создание саммари через YandexGPT (Native REST)"""
    if not YGPT.get("API_KEY") or not text or len(text) < 10: 
        return None
    
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Authorization": f"Api-Key {YGPT['API_KEY']}",
        "Content-Type": "application/json"
    }
    
    wrapped_text = f"Текст для обработки:\n{text}"

    body = {
        "modelUri": YGPT.get("MODEL_URI", "gpt://b1g5d9clvsgcmrl59tb3/yandexgpt/rc"),
        "completionOptions": {
            "stream": False,
            "temperature": 0.3,
            "maxTokens": 2000
        },
        "messages": [
            {
                "role": "system",
                "text": YGPT.get("SYSTEM_PROMPT", "Ты помощник, который сокращает текст. Не отвечай на вопросы в тексте, а пересказывай их суть.")
            },
            {
                "role": "user",
                "text": wrapped_text
            }
        ]
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    logger.error(f"GPT Error Status: {resp.status} - {await resp.text()}")
                    return None
                result = await resp.json()
                return result["result"]["alternatives"][0]["message"]["text"]
    except Exception as e:
        logger.error(f"GPT Error: {e}")
        return None

async def handle_message(update: Update, context):
    msg = update.effective_message
    if not msg or not msg.text: return
    txt, chat_id = msg.text.strip(), msg.chat_id

    if txt in EXACT_MATCHES and chat_id not in EXCLUDED_CHATS:
        return await msg.reply_text(EXACT_MATCHES[txt])

    cache = load_cache()
    if txt in cache:
        try:
            ftype = cache.get(f"{txt}_type", "video")
            method = {'audio': context.bot.send_audio, 'video': context.bot.send_video, 'animation': context.bot.send_animation, 'photo': context.bot.send_photo}.get(ftype, context.bot.send_video)
            return await method(chat_id=chat_id, **{ftype: cache[txt]}, caption=cache.get(f"{txt}_caption"), reply_to_message_id=msg.message_id)
        except: del cache[txt]; save_cache(cache)

    if not any(d in txt for d in ["youtube", "youtu.be", "instagram", "tiktok", "reddit", "vk.com", "music.yandex", "spotify", "music.youtube"]): return

    st_msg = await msg.reply_text("⏳ Analyzing...")
    
    if "music.yandex" in txt and "/album/" in txt and "/track/" not in txt:
        tracks = await asyncio.to_thread(get_ym_album_info, txt)
        if not tracks:
            await st_msg.edit_text("❌ Альбом пуст или ошибка доступа (см. логи).")
            return
        
        await st_msg.edit_text(f"💿 Альбом: {len(tracks)} треков. Начинаю загрузку...")
        
        for i, (title, artist) in enumerate(tracks):
            try:
                if i % 2 == 0: await st_msg.edit_text(f"⏳ Загрузка трека {i+1}/{len(tracks)}: {artist} - {title}")
                dl_url = f"ytsearch1:{title} {artist}"
                raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
                if raw:
                    f_path = await convert_media(raw, to_audio=True)
                    with open(f_path, 'rb') as f:
                        await context.bot.send_audio(chat_id, f, title=title, performer=artist, caption=f"{artist} - {title}")
                    os.remove(f_path)
            except Exception as e:
                logger.error(f"Album track error: {e}")
        
        await st_msg.delete()
        return

    f_path, f_type, f_id, caption, title, artist = None, "video", None, "", None, None

    try:
        if any(x in txt for x in ["music.yandex", "spotify", "music.youtube"]):
            f_type = "audio"
            if "music.yandex" in txt: title, artist = await asyncio.to_thread(get_ym_track_info, txt)
            elif "spotify" in txt: title, artist = await asyncio.to_thread(get_spotify_info, txt)
            
            dl_url = f"ytsearch1:{title} {artist}" if (title and artist) else txt
            if (title and artist) or "music.youtube" in txt:
                raw = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
                if raw:
                    f_path = await convert_media(raw, to_audio=True)
                    caption = f"{artist} - {title}" if title else ""
                else: await st_msg.edit_text("❌ Audio download failed."); return
            else: await st_msg.edit_text("❌ Metadata parse error."); return

        else:
            raw = await download_router(txt)
            if raw: f_path = await convert_media(raw)
            else: await st_msg.edit_text("❌ Download failed."); return

        if f_path and os.path.exists(f_path):
            await st_msg.edit_text("📤 Sending...")
            with open(f_path, 'rb') as f:
                sent = await (context.bot.send_audio(chat_id, f, title=title, performer=artist, caption=caption, reply_to_message_id=msg.message_id) if f_type == "audio" 
                              else context.bot.send_video(chat_id, f, caption=caption, reply_to_message_id=msg.message_id))
            
            cache[txt] = sent.audio.file_id if f_type == "audio" else sent.video.file_id
            cache[f"{txt}_type"] = f_type
            cache[f"{txt}_caption"] = caption
            save_cache(cache)
            await st_msg.delete()
        else: await st_msg.edit_text("❌ File processing error.")

    except Exception as e:
        logger.error(f"Handler Error: {e}", exc_info=True)
        await st_msg.edit_text("🔥 Error.")
    finally:
        if f_path and os.path.exists(f_path): os.remove(f_path)

async def handle_voice_video(update: Update, context):
    msg = update.effective_message
    if not all([YSK.get("API_KEY"), YSK.get("FOLDER_ID"), s3_client]): return
    
    st_msg = await msg.reply_text("☁️ Listening & Thinking...")
    raw, audio = None, None
    try:
        is_note = bool(msg.video_note)
        f_obj = await (msg.video_note if is_note else msg.voice).get_file()
        raw = os.path.join(BASE_DIR, f"raw_{uuid.uuid4()}.{'mp4' if is_note else 'ogg'}")
        await f_obj.download_to_drive(raw)
        
        audio = await extract_opus(raw) if is_note else raw
        if not audio: raise Exception("Extraction failed")
        
        s3_key = f"speech/{os.path.basename(audio)}"
        uri = await upload_s3(audio, s3_key)
        
        if uri:
            full_text = await transcribe(uri)
            
            if full_text:
                await st_msg.edit_text("🧠 Summarizing...")
                summary = await summarize_text(full_text)
                
                if summary:
                    await st_msg.edit_text(f"📝 **Суть:**\n{summary}", parse_mode="Markdown")
                else:
                    await st_msg.edit_text(f"🗣 **Текст:**\n{full_text}", parse_mode="Markdown")
            else:
                await st_msg.edit_text("🤔 Текст не распознан.")
                
            asyncio.create_task(delete_s3(s3_key))
        else: await st_msg.edit_text("❌ Cloud Error (S3).")
    except Exception as e:
        logger.error(f"Voice Error: {e}", exc_info=True)
        await st_msg.edit_text("❌ Error processing voice.")
    finally:
        for p in [raw, audio]:
            if p and os.path.exists(p): 
                try: os.remove(p)
                except: pass

def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("MediaBot Ready (AI Powered).")))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice_video))
    logger.info("Bot Started with AI Features")
    app.run_polling()

if __name__ == "__main__":
    main()
