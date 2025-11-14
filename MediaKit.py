import asyncpraw
import yt_dlp
import string
import os
import time
import requests
import subprocess
import shutil
import uuid
import random
import logging
import threading
import asyncio
import json
import re
import aiohttp  # Добавлено для асинхронных запросов к Yandex API
import boto3    # Добавлено для Yandex Object Storage (S3)
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from collections import deque

EXACT_MATCH_REPLIES = {
    "Да": "Пизда",
    "Нет": "Пидора ответ",
    "нет": "Пидора ответ",
    "да": "Пизда",
    "300": "Отсоси у тракториста",
    "Алло": "Хуем по лбу не дало?",
    "алло": "Хуем по лбу не дало?",
    "РКН": "Пидорасы",
    "Ркн": "Пидорасы",
    "ркн": "Пидорасы",
    "Звук говно": "Пиво дорогое",
}
class FileSizeExceededError(Exception):
    pass
class InstagramAccountBannedError(Exception):
    pass
class InvalidLinkError(Exception):
    pass
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CACHE_FILE = os.path.join(IMPORTANT_DIR, 'cache.json')
INSTAGRAM_FOLDER = os.path.dirname(os.path.abspath(__file__))
CACHE = CACHE_FILE
COOKIES_YOUTUBE_PATH = None
COOKIES_REDDIT_PATH = None
COOKIES_TIKTOK_PATH = None
instagram_accounts_queue = deque()
instagram_queue_lock = asyncio.Lock()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'important', 'config.json')
try:
    with open(CONFIG_PATH, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    logger.error(f"Файл конфигурации '{CONFIG_PATH}' не найден. Убедитесь, что он существует.")
    exit(1)
BOT_TOKEN = config.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в config.json. Пожалуйста, укажите токен бота.")
    exit(1)
EXCLUDED_CHAT_IDS = config.get("EXCLUDED_CHATS", [])
if not EXCLUDED_CHAT_IDS:
    logger.warning("Список EXCLUDED_CHATS в config.json не найден или пуст. Бот будет отвечать матом во всех чатах.")
else:
    try:
        EXCLUDED_CHAT_IDS = [int(cid) for cid in EXCLUDED_CHAT_IDS]
        logger.info(f"Загружено {len(EXCLUDED_CHAT_IDS)} чатов в список исключений для матерных ответов.")
    except ValueError:
        logger.error("Ошибка в 'EXCLUDED_CHATS' в config.json! ID должны быть числами. Cписок исключений будет проигнорирован.")
        EXCLUDED_CHAT_IDS = []
reddit_client_id = config["REDDIT"].get("client_id")
reddit_client_secret = config["REDDIT"].get("client_secret")
reddit_user_agent = config["REDDIT"].get("user_agent")
if reddit_client_id and reddit_client_secret and reddit_user_agent:
    reddit = asyncpraw.Reddit(
        client_id=reddit_client_id,
        client_secret=reddit_client_secret,
        user_agent=reddit_user_agent
    )
else:
    logger.warning("Параметры Reddit API не полностью указаны. Функции Reddit могут быть недоступны.")
    reddit = None
YANDEX_PROXIES = config["PROXIES"].get("yandex")
SPOTIFY_PROXIES = config["PROXIES"].get("spotify")
TIKTOK_PROXIES = config["PROXIES"].get("tiktok")
YANDEX_HEADERS = {"Authorization": config["HEADERS"].get("yandex_auth", "")}
COOKIES_YOUTUBE_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("youtube", ''))
COOKIES_REDDIT_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("reddit", ''))
COOKIES_TIKTOK_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("tiktok", ''))

YANDEX_SPEECHKIT_CONFIG = config.get("YANDEX_SPEECHKIT", {})
YANDEX_API_KEY = YANDEX_SPEECHKIT_CONFIG.get("API_KEY")
YANDEX_FOLDER_ID = YANDEX_SPEECHKIT_CONFIG.get("FOLDER_ID")
S3_BUCKET_NAME = YANDEX_SPEECHKIT_CONFIG.get("S3_BUCKET_NAME")
S3_ACCESS_KEY_ID = YANDEX_SPEECHKIT_CONFIG.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = YANDEX_SPEECHKIT_CONFIG.get("S3_SECRET_ACCESS_KEY")

s3_client = None
if all([S3_BUCKET_NAME, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY]):
    try:
        s3_client = boto3.client(
            's3',
            endpoint_url='https://storage.yandexcloud.net',
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY
        )
        logger.info("Клиент Yandex Object Storage (S3) успешно инициализирован.")
    except Exception as e:
        logger.error(f"Не удалось инициализировать клиент S3: {e}")
else:
    logger.warning("Конфигурация Yandex Object Storage (S3) не полная. Расшифровка аудио будет недоступна.")

if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
    logger.warning("YANDEX_API_KEY или YANDEX_FOLDER_ID не найдены. Расшифровка аудио будет недоступна.")


def initialize_instagram_accounts():
    global instagram_accounts_queue
    accounts = config.get("INSTAGRAM_ACCOUNTS", [])
    if not accounts:
        logger.warning("В config.json не найдены аккаунты для Instagram (INSTAGRAM_ACCOUNTS).")
        return
    for acc in accounts:
        cookie_path = os.path.join(IMPORTANT_DIR, acc['cookie_file'])
        if os.path.exists(cookie_path):
            instagram_accounts_queue.append({"cookie_file": cookie_path, "proxy": acc['proxy']})
        else:
            logger.error(f"Файл куки для Instagram не найден: {cookie_path}. Этот аккаунт будет пропущен.")
    logger.info(f"Инициализировано {len(instagram_accounts_queue)} аккаунтов Instagram.")


def upload_to_yandex_s3(file_path, s3_key):
    """
    (Синхронно) Загружает файл в Yandex Object Storage и возвращает URI.
    Нужно вызывать через asyncio.to_thread.
    """
    if not s3_client:
        logger.error("S3 клиент не инициализирован.")
        return None
    try:
        s3_client.upload_file(file_path, S3_BUCKET_NAME, s3_key)
        logger.info(f"Файл {file_path} успешно загружен в S3 как {s3_key}")
        
        return f"https://storage.yandexcloud.net/{S3_BUCKET_NAME}/{s3_key}"

    except Exception as e:
        logger.error(f"Ошибка при загрузке файла в S3: {e}")
        return None

def delete_from_yandex_s3(s3_key):
    """
    (Синхронно) Удаляет файл из Yandex Object Storage.
    Нужно вызывать через asyncio.to_thread.
    """
    if not s3_client:
        return
    try:
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        logger.info(f"Файл {s3_key} успешно удален из S3.")
    except Exception as e:
        logger.error(f"Ошибка при удалении файла из S3: {e}")

async def extract_audio_from_video(video_path: str) -> str | None:
    """
    Извлекает аудио из видеофайла (кружочка) в формат OGG (Opus)
    для совместимости с Yandex STT.
    """
    output_audio_path = f"{os.path.splitext(video_path)[0]}_audio.ogg"
    try:
        logger.info(f"Извлекаю аудио из {video_path}...")
        command = [
            "ffmpeg",
            "-i", video_path,
            "-vn",          # Нет видео
            "-c:a", "libopus", # Кодек Opus (требуется для Yandex STT)
            "-b:a", "64k",    # Битрейт
            "-vbr", "on",     # Включить VBR
            
            "-ar", "48000",   
            
            "-compression_level", "10", # Макс. сжатие
            output_audio_path,
            "-y",
            "-loglevel", "error"
        ]
        process = await asyncio.to_thread(
            subprocess.run, command, check=True, capture_output=True, text=True
        )
        if os.path.exists(output_audio_path):
            logger.info(f"Аудио успешно извлечено: {output_audio_path}")
            return output_audio_path
        else:
            logger.error(f"FFmpeg отработал, но аудиофайл не создан. Stderr: {process.stderr}")
            return None
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при извлечении аудио: {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при извлечении аудио: {e}")
        return None

async def start_yandex_transcription(s3_uri: str) -> str | None:
    """
    (Асинхронно) Запускает асинхронную операцию расшифровки в Yandex SpeechKit.
    Возвращает ID операции.
    """
    url = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}"
    }
    body = {
        "config": {
            "specification": {
                "languageCode": "ru-RU",
                "model": "general",
                "audioEncoding": "OGG_OPUS",
                
                "profanityFilter": False, # Выключаем цензуру

                "literature_text": True,
                "audioChannelCount": 1, 
                "sampleRateHertz": "48000"
            },
            "folderId": YANDEX_FOLDER_ID
        },
        "audio": {
            "uri": s3_uri
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body, timeout=20) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    operation_id = data.get("id")
                    if operation_id:
                        logger.info(f"Запущена операция расшифровки Yandex: {operation_id}")
                        return operation_id
                    else:
                        logger.error(f"Yandex API вернул 200, но не дал ID. Ответ: {data}")
                        return None
                else:
                    error_text = await resp.text()
                    logger.error(f"Ошибка при запуске расшифровки (HTTP {resp.status}): {error_text}")
                    return None
    except asyncio.TimeoutError:
        logger.error("Таймаут при запуске операции Yandex SpeechKit.")
        return None
    except Exception as e:
        logger.error(f"Исключение при запросе к Yandex API (start_transcription): {e}")
        return None

async def poll_yandex_transcription(operation_id: str) -> str | None:
    """
    (Асинхронно) Опрашивает статус операции расшифровки до ее завершения.
    """
    url = f"https://operation.api.cloud.yandex.net/operations/{operation_id}"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}"
    }
    
    for _ in range(30): 
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        logger.error(f"Ошибка при опросе статуса (HTTP {resp.status}): {await resp.text()}")
                        await asyncio.sleep(10) # Ждем 10 сек перед новой попыткой
                        continue
                        
                    data = await resp.json()
                    
                    if data.get("done") == True:
                        if "response" in data:
                            text = " ".join(
                                chunk["alternatives"][0]["text"]
                                for chunk in data["response"]["chunks"]
                                if chunk["alternatives"]
                            )
                            logger.info(f"Расшифровка {operation_id} успешно завершена.")
                            return text.strip()
                        else:
                            error = data.get("error", {})
                            logger.error(f"Операция {operation_id} завершилась с ошибкой: {error.get('message', 'Unknown error')}")
                            return None
                    else:
                        logger.info(f"Операция {operation_id} еще в процессе...")
                        await asyncio.sleep(10) # Пауза 10 секунд перед след. опросом

        except asyncio.TimeoutError:
            logger.warning(f"Таймаут опроса Yandex API {operation_id}. Повторяю...")
            await asyncio.sleep(5) # Короткая пауза при таймауте
        except Exception as e:
            logger.error(f"Исключение при опросе Yandex API ({operation_id}): {e}")
            await asyncio.sleep(10)
            
    logger.error(f"Таймаут ожидания расшифровки (5 мин) для операции {operation_id}.")
    return None



def load_cache():
    if not os.path.exists(CACHE): return {}
    with open(CACHE, 'r') as f:
        try: return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Ошибка чтения CACHE_FILE '{CACHE}', создаю новый.")
            return {}
def save_cache(cache_data):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(cache_data, f, indent=4)
def cleanup_folder(interval=3000, target_extensions=('.mp3', '.mp4', '.part', '.webm', '.jpg', '.jpeg', '.png', '.gif', '.bin', '.ogg')):
    while True:
        try:
            logger.info("Начинаю очистку временных файлов...")
            count = 0
            for filename in os.listdir(BASE_DIR):
                if filename.endswith(target_extensions) and filename != '.gitignore':
                    try:
                        os.remove(os.path.join(BASE_DIR, filename))
                        logger.info(f"Удален файл: {filename}")
                        count += 1
                    except OSError as e:
                        logger.error(f"Не удалось удалить файл {filename}: {e}")
            logger.info(f"Очистка завершена. Удалено {count} файлов.")
        except Exception as e:
            logger.error(f"Ошибка при очистке папки: {e}")
        time.sleep(interval)
async def download_media(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False)
        file_size = info_dict.get('filesize') or info_dict.get('filesize_approx') or 0
        if file_size > 50 * 1024 * 1024:
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        await asyncio.to_thread(ydl.download, [url])
def get_base_ydl_opts(output_filename):
    return {
        'outtmpl': output_filename,
        'quiet': True,
        'nocheckcertificate': True,
        'socket_timeout': 60,
        'retries': 3,
        'fragment_retries': 3,
    }
async def download_youtube_video(url):
    filename = f"youtube_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = get_base_ydl_opts(filename)
    ydl_opts.update({
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'proxy': config["PROXIES"].get("youtube")
    })
    await download_media(url, ydl_opts) 
    return filename if os.path.exists(filename) else None
async def download_youtube_music_audio(url):
    base_filename = f"youtube_music_{uuid.uuid4().hex}"
    audio_filename = f"{base_filename}.mp3"
    ydl_opts = get_base_ydl_opts(base_filename)
    ydl_opts.update({
        'format': 'bestaudio/best',
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'logger': logger,
        'proxy': config["PROXIES"].get("youtube")
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = await asyncio.to_thread(ydl.extract_info, url, download=True)
        title = info_dict.get('title', 'Unknown Title')
        artist = info_dict.get('artist') or info_dict.get('uploader', 'Unknown Artist')
    return (audio_filename, title, artist) if os.path.exists(audio_filename) else (None, None, None)
async def download_tiktok_video_with_proxy(url):
    filename = f"tiktok_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = get_base_ydl_opts(filename)
    ydl_opts.update({
        'format': 'best',
        'proxy': TIKTOK_PROXIES,
        'cookiefile': COOKIES_TIKTOK_PATH,
    })
    await download_media(url, ydl_opts)
    return filename if os.path.exists(filename) else None
async def download_reddit_video(url):
    filename = f"reddit_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = get_base_ydl_opts(filename)
    ydl_opts.update({
        'format': 'bestvideo+bestaudio/best',
        'cookiefile': COOKIES_REDDIT_PATH,
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    })
    await download_media(url, ydl_opts)
    return filename if os.path.exists(filename) else None
async def download_vk_video(url, username, password):
    filename = f"vk_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = get_base_ydl_opts(filename)
    ydl_opts.update({
        'format': 'best',
        'username': username,
        'password': password,
    })
    await download_media(url, ydl_opts)
    return filename if os.path.exists(filename) else None
async def managed_instagram_download(url):
    logger.info("Запускаю универсальный bash-загрузчик для Instagram.")
    try:
        filename = f"instagram_video_{uuid.uuid4().hex}.mp4"
        script_path = "/root/MediaKit/download_instagram.sh"
        args = [
            script_path,
            url,
            filename
        ]
        logger.info(f"Вызываю внешний скрипт: {' '.join(args)}")
        process = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            check=False
        )
        if process.returncode != 0:
            logger.error(f"Bash-скрипт завершился с кодом {process.returncode}. Stderr: {process.stderr}")
            error_output = (process.stdout + process.stderr).lower()
            if any(s in error_output for s in ["login is required", "401", "403", "429", "challenge required"]):
                return None, "ALL_ACCOUNTS_FAILED"
            if any(s in error_output for s in ["private", "404", "no media found"]):
                return None, "INVALID_LINK"
            raise yt_dlp.utils.DownloadError(f"Bash script error: {process.stderr}")
        logger.info(f"Bash-скрипт успешно выполнен. Stdout: {process.stdout}")
        return (filename, "SUCCESS") if os.path.exists(filename) else (None, "UNKNOWN_DOWNLOAD_ERROR")
    except Exception as e:
        logger.error(f"Непредвиденная ошибка Python при вызове bash-скрипта: {e}")
        return None, "ALL_ACCOUNTS_FAILED"
def get_track_info(yandex_url):
    try:
        proxies = {'http': YANDEX_PROXIES, 'https': YANDEX_PROXIES} if YANDEX_PROXIES else None
        track_id = yandex_url.split('/')[-1].split('?')[0]
        api_url = f"https://api.music.yandex.net/tracks/{track_id}"
        response = requests.get(api_url, headers=YANDEX_HEADERS, proxies=proxies, timeout=10)
        response.raise_for_status()
        track = response.json()['result'][0]
        artist_names = [artist['name'] for artist in track.get('artists', [])]
        all_artists = ', '.join(artist_names)
        return track.get('title', 'Unknown Title'), all_artists
    except Exception as e:
        logger.error(f"Ошибка при получении информации о треке Яндекс: {e}")
        return None, None
def get_yandex_album_track_details(album_url):
    try:
        proxies = {'http': YANDEX_PROXIES, 'https': YANDEX_PROXIES} if YANDEX_PROXIES else None
        album_id_match = re.search(r'/album/(\d+)', album_url)
        if not album_id_match:
            logger.error(f"Не удалось извлечь ID альбома из URL: {album_url}")
            return []
        album_id = album_id_match.group(1)
        api_url = f"https://api.music.yandex.net/albums/{album_id}/with-tracks"
        response = requests.get(api_url, headers=YANDEX_HEADERS, proxies=proxies, timeout=15)
        response.raise_for_status()
        data = response.json()
        tracks_info = []
        if 'volumes' in data['result']:
            for volume in data['result']['volumes']:
                for track in volume:
                    title = track.get('title', 'Unknown Title')
                    artist_names = [artist['name'] for artist in track.get('artists', [])]
                    artist = ', '.join(artist_names) if artist_names else 'Unknown Artist'
                    tracks_info.append({"title": title, "artist": artist})
        logger.info(f"Найдено {len(tracks_info)} треков в альбоме {album_id}")
        return tracks_info
    except Exception as e:
        logger.error(f"Ошибка при получении информации об альбоме Яндекс.Музыки: {e}")
        return []
def get_track_info_with_proxy(spotify_url):
    headers = {"User-Agent": "Mozilla/5.0"}
    if SPOTIFY_PROXIES:
        try:
            logger.warning("Spotify: Попытка 1 (с прокси)...")
            proxy = {'http': SPOTIFY_PROXIES, 'https': SPOTIFY_PROXIES}
            response = requests.get(spotify_url, headers=headers, proxies=proxy, timeout=10)
            response.raise_for_status()
            title = re.search(r'<meta property="og:title" content="(.*?)"', response.text).group(1)
            artist = re.search(r'<meta property="og:description" content="(.*?)"', response.text).group(1)
            return title, artist.split('·')[0].strip()
        except Exception as e:
            logger.warning(f"Spotify: Ошибка при работе с прокси: {e}. Пробую без прокси...")
    try:
        logger.warning("Spotify: Попытка 2 (напрямую)...")
        response = requests.get(spotify_url, headers=headers, timeout=10)
        response.raise_for_status()
        title = re.search(r'<meta property="og:title" content="(.*?)"', response.text).group(1)
        artist = re.search(r'<meta property="og:description" content="(.*?)"', response.text).group(1)
        return title, artist.split('·')[0].strip()
    except Exception as e:
        logger.error(f"Spotify: Ошибка при получении информации (даже напрямую): {e}")
        return None, None
def search_and_download_from_youtube(title, artist):
    query = f"ytsearch1:{title} {artist}"
    filename_base = f"search_{uuid.uuid4().hex}"
    ydl_opts = get_base_ydl_opts(f"{filename_base}.%(ext)s")
    ydl_opts.update({
        'format': 'bestaudio/best', 
        'noplaylist': True, 
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'proxy': config["PROXIES"].get("youtube")
    })
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(query, download=True)
    for f in os.listdir(BASE_DIR):
        if f.startswith(filename_base):
            return os.path.join(BASE_DIR, f)
    return None
def convert_to_mp3(input_file):
    if not input_file: return None
    output_file = f"{os.path.splitext(input_file)[0]}.mp3"
    try:
        subprocess.run(
            ["ffmpeg", "-i", input_file, "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", output_file, "-loglevel", "quiet"],
            check=True,
        )
        os.remove(input_file)
        return output_file
    except Exception as e:
        logger.error(f"Ошибка при конвертации в MP3: {e}")
        if os.path.exists(input_file): os.remove(input_file)
        return None
def convert_video_for_telegram(input_path: str) -> str | None:
    if not input_path or not os.path.exists(input_path):
        logger.error(f"Файл для конвертации не найден: {input_path}")
        return None
    output_path = f"{os.path.splitext(input_path)[0]}_converted.mp4"
    logger.info(f"Начинаю конвертацию: {input_path} -> {output_path}")
    try:
        command = [
            "ffmpeg",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            output_path,
            "-y", 
            "-loglevel", "error"
        ]
        subprocess.run(command, check=True)
        os.remove(input_path)
        logger.info("Конвертация успешно завершена.")
        return output_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при конвертации файла {input_path}: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
        if os.path.exists(input_path):
            os.remove(input_path)
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при конвертации: {e}")
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
def is_gif_like(file_path: str) -> bool:
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        duration_str = info.get("format", {}).get("duration", "0")
        duration = float(duration_str)
        return not has_audio and duration < 60.0
    except Exception:
        return False
async def start(update: Update, context):
    await update.message.reply_text("Привет! Отправь мне ссылку на видео или трек.")
async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

async def handle_message(update: Update, context):
    effective_msg = update.effective_message

    if not effective_msg or not effective_msg.text:
        logger.warning("Получен апдейт 'text', но effective_message.text отсутствует. Игнорирую.")
        return

    logger.info(f"Получено сообщение в чате. ID: {effective_msg.chat_id}")
    message = effective_msg.text.strip()
    chat_id = effective_msg.chat_id
    
    if message in EXACT_MATCH_REPLIES and chat_id not in EXCLUDED_CHAT_IDS:
        response_text = EXACT_MATCH_REPLIES[message]
        await effective_msg.reply_text(response_text) # <-- ИСПРАВЛЕНО
        return
        
    supported_services = [
        "youtube.com", "youtu.be", "instagram.com", "tiktok.com", 
        "reddit.com", "vk.com", "vk.ru", "vkvideo.ru", 
        "music.yandex.ru", "open.spotify.com", "music.youtube.com"
    ]
    cache_data = load_cache()
    if message in cache_data:
        file_id = cache_data[message]
        logger.info(f"Ссылка найдена в кэше, отправляем файл {file_id}.")
        cached_file_type = cache_data.get(f"{message}_type", "video")
        caption_text = cache_data.get(f"{message}_caption", "")
        try:
            if cached_file_type == "audio":
                await context.bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption_text, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
            elif cached_file_type == "animation":
                await context.bot.send_animation(chat_id=chat_id, animation=file_id, caption=caption_text, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
            elif cached_file_type == "photo":
                await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption_text, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
            else:
                await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption_text, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
            return
        except Exception as e:
            logger.error(f"Не удалось отправить кэшированный файл {file_id}: {e}")
            if message in cache_data: del cache_data[message]; save_cache(cache_data)
            await effective_msg.reply_text("Файл из кэша недействителен. Попробую скачать заново.") # <-- ИСПРАВЛЕНО
            
    if not any(s in message for s in supported_services):
        logger.info(f"Сообщение '{message}' не содержит поддерживаемой ссылки. Игнорирую.")
        return
        
    logger.info(f"Получено сообщение с поддерживаемой ссылкой: {message} от {effective_msg.from_user.id}") # <-- ИСПРАВЛЕНО
    status_message = None
    downloaded_file = None
    file_to_send_type = "video"
    file_id_to_cache = None
    caption_to_cache = ""
    title = "Unknown Title"
    artist = "Unknown Artist"
    try:
        status_message = await effective_msg.reply_text("Получил ссылку, начинаю обработку...") # <-- ИСПРАВЛЕНО
        
        if "music.yandex.ru" in message:
            if "/album/" in message and "/track/" not in message:
                await status_message.edit_text("Обнаружен альбом Яндекс.Музыки! Получаю список треков...")
                tracks = await asyncio.to_thread(get_yandex_album_track_details, message)
                if not tracks:
                    await status_message.edit_text("Не удалось получить треки из альбома.")
                    return
                await status_message.edit_text(f"Найдено {len(tracks)} треков. Начинаю загрузку (это может занять время)...")
                for i, track in enumerate(tracks):
                    title, artist = track["title"], track["artist"]
                    track_status_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"({i+1}/{len(tracks)}) Обрабатываю: *{artist} – {title}*",
                        parse_mode="Markdown",
                        reply_to_message_id=effective_msg.message_id # <-- ИСПРАВЛЕНО
                    )
                    album_track_file = None
                    try:
                        temp_dl_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                        album_track_file = await asyncio.to_thread(convert_to_mp3, temp_dl_file)
                        if album_track_file:
                            with open(album_track_file, "rb") as f:
                                await context.bot.send_audio(chat_id=chat_id, audio=f, title=title, performer=artist, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
                        else:
                            await track_status_msg.edit_text(f"({i+1}/{len(tracks)}) Не удалось скачать: *{artist} – {title}*")
                    except Exception as e:
                        logger.error(f"Ошибка при обработке трека из альбома '{title}': {e}")
                        await track_status_msg.edit_text(f"({i+1}/{len(tracks)}) Ошибка при скачивании: *{artist} – {title}*")
                    finally:
                        if album_track_file and os.path.exists(album_track_file):
                            os.remove(album_track_file)
                        if track_status_msg:
                            await track_status_msg.delete()
                await status_message.delete()
                return
            else:
                await status_message.edit_text("Обнаружен трек Яндекс.Музыки...")
                title, artist = await asyncio.to_thread(get_track_info, message)
                if title and artist:
                    await status_message.edit_text(f"Ищу: {artist} - {title}...")
                    temp_dl_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                    downloaded_file = await asyncio.to_thread(convert_to_mp3, temp_dl_file)
                    if downloaded_file:
                        caption_to_cache = f"{artist} - {title}"
                        file_to_send_type = "audio"
        elif "open.spotify.com" in message or "music.youtube.com" in message:
            await status_message.edit_text("Обнаружена музыкальная ссылка...")
            if "open.spotify.com" in message:
                title, artist = await asyncio.to_thread(get_track_info_with_proxy, message)
                if title and artist:
                    await status_message.edit_text(f"Ищу: {artist} - {title}...")
                    temp_dl_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                    downloaded_file = await asyncio.to_thread(convert_to_mp3, temp_dl_file)
            elif "music.youtube.com" in message:
                downloaded_file, title, artist = await download_youtube_music_audio(message)
            if downloaded_file:
                caption_to_cache = f"{artist} - {title}"
                file_to_send_type = "audio"
        elif any(s in message for s in ["youtube.com", "youtu.be", "instagram.com", "tiktok.com", "reddit.com", "vk.com", "vk.ru", "vkvideo.ru"]):
            if "youtube.com" in message or "youtu.be" in message:
                await status_message.edit_text("Обрабатываю ссылку YouTube...")
                try:
                    downloaded_file = await download_youtube_video(message)
                except yt_dlp.utils.DownloadError as e:
                    logger.warning(f"Ошибка YouTube: {e}")
                    if "cookies" in str(e):
                        await status_message.edit_text("Ошибка: Куки-файлы YouTube устарели. Требуется обновление.")
                    elif "private" in str(e):
                        await status_message.edit_text("Ошибка: Это видео приватное.")
                    else:
                        await status_message.edit_text("Ошибка: Не удалось скачать видео с YouTube.")
                    downloaded_file = None
                except Exception as e:
                    raise e
                if downloaded_file:
                    await status_message.edit_text("Видео скачано! Конвертирую для лучшей совместимости...")
                    converted_file = await asyncio.to_thread(convert_video_for_telegram, downloaded_file)
                    if converted_file:
                        downloaded_file = converted_file
                    else:
                        await status_message.edit_text("Не удалось сконвертировать видео. Отправка отменена.")
                        downloaded_file = None
            elif "instagram.com" in message:
                await status_message.edit_text("Обрабатываю ссылку Instagram...")
                downloaded_file, status = await managed_instagram_download(message)
                if status == "SUCCESS" and downloaded_file:
                    await status_message.edit_text("Видео скачано! Конвертирую для лучшей совместимости...")
                    converted_file = await asyncio.to_thread(convert_video_for_telegram, downloaded_file)
                    if converted_file:
                        downloaded_file = converted_file
                    else:
                        await status_message.edit_text("Не удалось сконвертировать видео. Отправка отменена.")
                        downloaded_file = None
                        status = "CONVERSION_ERROR"
                if status != "SUCCESS":
                    error_map = {
                        "INVALID_LINK": "Ошибка: ссылка недействительна или приватна.", 
                        "FILE_TOO_LARGE": "Файл слишком большой.", 
                        "ALL_ACCOUNTS_FAILED": "Все наши аккаунты забанены, попробуйте позже",
                        "NO_ACCOUNTS": "Сервис Instagram временно недоступен (нет аккаунтов).",
                        "CONVERSION_ERROR": "Произошла ошибка при обработке видео." 
                    }
                    await status_message.edit_text(error_map.get(status, "Неизвестная ошибка Instagram."))
                    downloaded_file = None
            elif "tiktok.com" in message:
                await status_message.edit_text("Обрабатываю ссылку TikTok...")
                try:
                    downloaded_file = await download_tiktok_video_with_proxy(message)
                except yt_dlp.utils.DownloadError as e:
                    logger.warning(f"Ошибка TikTok: {e}")
                    if "private" in str(e) or "404" in str(e):
                        await status_message.edit_text("Ошибка: Видео TikTok приватное или удалено.")
                    elif "proxy" in str(e):
                        await status_message.edit_text("Ошибка: Не удалось подключиться через прокси для TikTok.")
                    else:
                        await status_message.edit_text("Ошибка: Не удалось скачать. (TikTok)")
                    downloaded_file = None
                except Exception as e:
                    raise e
            elif "reddit.com" in message:
                await status_message.edit_text("Обрабатываю ссылку Reddit...")
                try:
                    downloaded_file = await download_reddit_video(message)
                except yt_dlp.utils.DownloadError as e:
                    logger.warning(f"Ошибка Reddit: {e}")
                    if "private" in str(e) or "quarantined" in str(e):
                        await status_message.edit_text("Ошибка: Сабреддит приватный или на карантине.")
                    elif "404" in str(e):
                        await status_message.edit_text("Ошибка: Пост Reddit удален.")
                    else:
                        await status_message.edit_text("Ошибка: Не удалось скачать. (Reddit)")
                    downloaded_file = None
                except Exception as e:
                    raise e
            elif any(s in message for s in ["vk.com", "vk.ru", "vkvideo.ru"]):
                await status_message.edit_text("Обрабатываю ссылку ВКонтакте...")
                vk_username, vk_password = config["VK"].get("username"), config["VK"].get("password")
                if not vk_username or not vk_password:
                    await status_message.edit_text("Данные для ВКонтакте не настроены."); return
                try:
                    downloaded_file = await download_vk_video(message, vk_username, vk_password)
                except yt_dlp.utils.DownloadError as e:
                    logger.warning(f"Ошибка VK: {e}")
                    if "login" in str(e):
                        await status_message.edit_text("Ошибка: Неверный логин или пароль для VK.")
                    elif "private" in str(e) or "Video is private" in str(e):
                        await status_message.edit_text("Ошибка: Видео VK приватное или удалено.")
                    else:
                        await status_message.edit_text("Ошибка: Не удалось скачать. (VK)")
                    downloaded_file = None
                except Exception as e:
                    raise e
        
        if downloaded_file and os.path.exists(downloaded_file):
            await status_message.edit_text("Файл загружен! Отправляю...")
            if file_to_send_type == "audio":
                with open(downloaded_file, "rb") as f:
                    sent_message = await context.bot.send_audio(chat_id=chat_id, audio=f, title=title, performer=artist, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
                file_id_to_cache = sent_message.audio.file_id
            elif await asyncio.to_thread(is_gif_like, downloaded_file):
                with open(downloaded_file, 'rb') as f: 
                    sent_message = await context.bot.send_animation(chat_id=chat_id, animation=f, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
                file_id_to_cache, file_to_send_type = sent_message.animation.file_id, "animation"
            elif downloaded_file.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                with open(downloaded_file, 'rb') as f: 
                    sent_message = await context.bot.send_photo(chat_id=chat_id, photo=f, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
                file_id_to_cache, file_to_send_type = sent_message.photo[-1].file_id, "photo"
            else:
                with open(downloaded_file, 'rb') as f: 
                    sent_message = await context.bot.send_video(chat_id=chat_id, video=f, reply_to_message_id=effective_msg.message_id) # <-- ИСПРАВЛЕНО
                file_id_to_cache, file_to_send_type = sent_message.video.file_id, "video"
            
        elif not downloaded_file and status_message and not ("Ошибка" in status_message.text or "Сервис" in status_message.text or "Все наши аккаунты" in status_message.text):
            await status_message.edit_text("Не удалось скачать медиафайл.")
            
        if file_id_to_cache:
            logger.info(f"Сохраняю в кэш: {message} -> {file_id_to_cache} (тип: {file_to_send_type})")
            cache_data[message] = file_id_to_cache
            cache_data[f"{message}_type"] = file_to_send_type
            cache_data[f"{message}_caption"] = caption_to_cache
            save_cache(cache_data)
            
        if status_message: await status_message.delete()
        
    except FileSizeExceededError:
        error_text = "Файл слишком большой. Максимальный размер 50МБ."
        if status_message: await status_message.edit_text(error_text)
        else: await effective_msg.reply_text(error_text) # <-- ИСПРАВЛЕНО
        
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке '{message}': {e}", exc_info=True)
        error_text = f"Произошла непредвиденная ошибка. Попробуйте позже."
        if status_message and not ("Ошибка" in status_message.text):
            await status_message.edit_text(error_text)
        elif not status_message:
            await effective_msg.reply_text(error_text) # <-- ИСПРАВЛЕНО
            
    finally:
        if downloaded_file and os.path.exists(downloaded_file):
            logger.info(f"Удаляю временный файл: {downloaded_file}")
            try: os.remove(downloaded_file)
            except OSError as e: logger.warning(f"Не удалось удалить временный файл {downloaded_file}: {e}")


async def handle_voice(update: Update, context):
    if not all([YANDEX_API_KEY, YANDEX_FOLDER_ID, s3_client]):
        logger.warning("Получено голосовое, но Yandex SpeechKit не настроен (API_KEY, FOLDER_ID, S3). Игнорирую.")
        return

    message = update.effective_message
    
    if not message or not message.voice:
        logger.warning("Получен апдейт 'voice', но message.voice отсутствует (возможно, channel_post?). Игнорирую.")
        return

    file_path = None
    s3_key = None
    status_msg = None
    try:
        voice_file = await message.voice.get_file()
        
        file_name = f"voice_{voice_file.file_unique_id}.ogg"
        file_path = os.path.join(BASE_DIR, file_name)
        await voice_file.download_to_drive(file_path)

        status_msg = await message.reply_text("Получил голосовое, загружаю в облако...")
        
        s3_key = f"voice/{file_name}"
        s3_uri = await asyncio.to_thread(upload_to_yandex_s3, file_path, s3_key)
        
        if not s3_uri:
            await status_msg.edit_text("Не удалось загрузить файл в облако. Отмена.")
            return

        await status_msg.edit_text("Файл в облаке. Запускаю расшифровку (это может занять время)...")

        operation_id = await start_yandex_transcription(s3_uri)
        if not operation_id:
            await status_msg.edit_text("Не удалось запустить операцию расшифровки.")
            return
        
        text = await poll_yandex_transcription(operation_id)

        if not text:
            text = "*(не удалось распознать речь или произошла ошибка)*"
        
        await status_msg.edit_text(f"**Расшифровка:**\n\n{text}", parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Ошибка при обработке голосового: {e}", exc_info=True)
        if status_msg:
            await status_msg.edit_text("Произошла ошибка при расшифровке.")
        else:
            await message.reply_text("Произошла ошибка при расшифровке.") # Реплай
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        if s3_key:
            asyncio.create_task(asyncio.to_thread(delete_from_yandex_s3, s3_key))

async def handle_video_note(update: Update, context):
    if not all([YANDEX_API_KEY, YANDEX_FOLDER_ID, s3_client]):
        logger.warning("Получен кружочек, но Yandex SpeechKit не настроен (API_KEY, FOLDER_ID, S3). Игнорирую.")
        return
                
    message = update.effective_message

    if not message or not message.video_note:
        logger.warning("Получен апдейт 'video_note', но message.video_note отсутствует (возможно, channel_post?). Игнорирую.")
        return

    video_path = None
    audio_path = None
    s3_key = None
    status_msg = None
    try:
        video_note_file = await message.video_note.get_file()
        
        file_name = f"video_note_{video_note_file.file_unique_id}.mp4"
        video_path = os.path.join(BASE_DIR, file_name)
        await video_note_file.download_to_drive(video_path)

        status_msg = await message.reply_text("Получил кружочек, извлекаю аудио...")
        
        audio_path = await extract_audio_from_video(video_path)
        if not audio_path:
            await status_msg.edit_text("Не удалось извлечь аудио из видео.")
            return

        await status_msg.edit_text("Аудио извлечено. Загружаю в облако...")
        
        s3_key = f"video_note/{os.path.basename(audio_path)}"
        s3_uri = await asyncio.to_thread(upload_to_yandex_s3, audio_path, s3_key)

        if not s3_uri:
            await status_msg.edit_text("Не удалось загрузить файл в облако. Отмена.")
            return

        await status_msg.edit_text("Файл в облаке. Запускаю расшифровку (это может занять время)...")

        operation_id = await start_yandex_transcription(s3_uri)
        if not operation_id:
            await status_msg.edit_text("Не удалось запустить операцию расшифровки.")
            return
        
        text = await poll_yandex_transcription(operation_id)

        if not text:
            text = "*(не удалось распознать речь или произошла ошибка)*"

        await status_msg.edit_text(f"**Расшифровка (кружочек):**\n\n{text}", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Ошибка при обработке кружочка: {e}", exc_info=True)
        if status_msg:
            await status_msg.edit_text("Произошла ошибка при расшифровке.")
        else:
            await message.reply_text("Произошла ошибка при расшифровке.") # Реплай
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
        if s3_key:
            asyncio.create_task(asyncio.to_thread(delete_from_yandex_s3, s3_key))


def main():
    initialize_instagram_accounts()
    threading.Thread(target=cleanup_folder, daemon=True, name="CleanupThread").start()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE | filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST), 
        handle_message
    ))
    
    app.add_error_handler(error_handler)

    if all([YANDEX_API_KEY, YANDEX_FOLDER_ID, s3_client]):
        app.add_handler(MessageHandler(
            filters.VOICE & (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST), 
            handle_voice
        ))
        app.add_handler(MessageHandler(
            filters.VIDEO_NOTE & (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST), 
            handle_video_note
        ))
        logger.info("Обработчики Yandex SpeechKit (Voice/Video Note) активированы.")
    else:
        logger.warning("Обработчики Voice/Video Note деактивированы. Проверьте 'YANDEX_SPEECHKIT' в config.json.")

    logger.info("Бот запущен и готов к работе! будь счастлив")
    app.run_polling()

if __name__ == "__main__":
    main()
