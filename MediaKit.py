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
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from collections import deque


# --- Словарь для мгновенных ответов на точные совпадения ---
# Бот ответит, только если сообщение пользователя В ТОЧНОСТИ совпадает с ключом.
EXACT_MATCH_REPLIES = {
    # Формат: "Сообщение от пользователя": "Ответ бота"
    # Чтобы добавить новую пару, просто скопируйте и вставьте строку.
    # Чтобы временно отключить фразу, поставьте # в начале строки.

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
    "Путин": "Величайший лидер",
    "Звук говно": "Пиво дорогое",
    # "Эта фраза закомментирована": "И бот на нее не отреагирует",
}

# --- Пользовательские исключения ---
class FileSizeExceededError(Exception):
    """Исключение для случаев, когда размер файла превышает допустимый."""
    pass

class InstagramAccountBannedError(Exception):
    """Исключение для забаненного аккаунта."""
    pass

class InvalidLinkError(Exception):
    """Исключение для неверной или приватной ссылки."""
    pass

# --- Пути к файлам и константы ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CACHE_FILE = os.path.join(IMPORTANT_DIR, 'cache.json')
INSTAGRAM_FOLDER = os.path.dirname(os.path.abspath(__file__))
CACHE = CACHE_FILE

# --- Глобальные переменные ---
COOKIES_YOUTUBE_PATH = None
COOKIES_REDDIT_PATH = None
COOKIES_TIKTOK_PATH = None
instagram_accounts_queue = deque()
instagram_queue_lock = asyncio.Lock()

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Изменено на WARNING для лучшей отладки
)
logger = logging.getLogger(__name__)

# --- Загрузка конфигурации ---
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

# <<<--- НАЧАЛО ИЗМЕНЕННОГО БЛОКА 1 ---
# Загрузка списка исключенных чатов
EXCLUDED_CHAT_IDS = config.get("EXCLUDED_CHATS", [])
if not EXCLUDED_CHAT_IDS:
    logger.warning("Список EXCLUDED_CHATS в config.json не найден или пуст. Бот будет отвечать матом во всех чатах.")
else:
    try:
        # ID чатов из Telegram API - это числа. Приведем все к int для корректного сравнения.
        EXCLUDED_CHAT_IDS = [int(cid) for cid in EXCLUDED_CHAT_IDS]
        logger.info(f"Загружено {len(EXCLUDED_CHAT_IDS)} чатов в список исключений для матерных ответов.")
    except ValueError:
        logger.error("Ошибка в 'EXCLUDED_CHATS' в config.json! ID должны быть числами. Cписок исключений будет проигнорирован.")
        EXCLUDED_CHAT_IDS = []
# <<<--- КОНЕЦ ИЗМЕНЕННОГО БЛОКА 1 ---


# --- Инициализация API клиентов ---
# Reddit
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

# Прокси и заголовки
YANDEX_PROXIES = config["PROXIES"].get("yandex")
SPOTIFY_PROXIES = config["PROXIES"].get("spotify")
TIKTOK_PROXIES = config["PROXIES"].get("tiktok")
YANDEX_HEADERS = {"Authorization": config["HEADERS"].get("yandex_auth", "")}

# Пути к куки-файлам
COOKIES_YOUTUBE_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("youtube", ''))
COOKIES_REDDIT_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("reddit", ''))
COOKIES_TIKTOK_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("tiktok", ''))

# --- Функции инициализации ---
def initialize_instagram_accounts():
    """
    Инициализирует очередь аккаунтов Instagram из файла конфигурации.
    """
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

# --- Вспомогательные функции ---
def load_cache():
    """Загружает данные кэша из файла."""
    if not os.path.exists(CACHE): return {}
    with open(CACHE, 'r') as f:
        try: return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Ошибка чтения CACHE_FILE '{CACHE}', создаю новый.")
            return {}

def save_cache(cache_data):
    """Сохраняет данные кэша в файл."""
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(cache_data, f, indent=4)

def cleanup_folder(interval=3000, target_extensions=('.mp3', '.mp4', '.part', '.webm', '.jpg', '.jpeg', '.png', '.gif', '.bin')):
    """
    Периодически очищает временные файлы из рабочей директории.
    """
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

# --- Функции загрузки ---
async def download_media(url, ydl_opts):
    """Универсальная функция-обертка для yt-dlp."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False)
        
        file_size = info_dict.get('filesize') or info_dict.get('filesize_approx') or 0
        if file_size > 50 * 1024 * 1024:
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        
        await asyncio.to_thread(ydl.download, [url])

def get_base_ydl_opts(output_filename):
    """Возвращает базовые опции для yt-dlp."""
    return {
        'outtmpl': output_filename,
        'quiet': True,
        'nocheckcertificate': True,
        'socket_timeout': 60,
        'retries': 3,
        'fragment_retries': 3,
    }

async def download_youtube_video(url):
    """Скачивает видео с YouTube."""
    filename = f"youtube_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = get_base_ydl_opts(filename)
    ydl_opts.update({
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'proxy': config["PROXIES"].get("youtube") # <-- ОСТАВЛЕНО ПО ЗАПРОСУ
    })
    # Оборачиваем вызов download_media, чтобы он соответствовал другим функциям
    await download_media(url, ydl_opts) 
    return filename if os.path.exists(filename) else None

async def download_youtube_music_audio(url):
    """Скачивает аудио с YouTube Music."""
    base_filename = f"youtube_music_{uuid.uuid4().hex}"
    audio_filename = f"{base_filename}.mp3"
    ydl_opts = get_base_ydl_opts(base_filename)
    ydl_opts.update({
        'format': 'bestaudio/best',
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'logger': logger,
        'proxy': config["PROXIES"].get("youtube") # Добавим прокси и сюда
    })
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = await asyncio.to_thread(ydl.extract_info, url, download=True)
        title = info_dict.get('title', 'Unknown Title')
        artist = info_dict.get('artist') or info_dict.get('uploader', 'Unknown Artist')
    
    return (audio_filename, title, artist) if os.path.exists(audio_filename) else (None, None, None)

async def download_tiktok_video_with_proxy(url):
    """Скачивает видео с TikTok с использованием прокси."""
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
    """Скачивает видео с Reddit."""
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
    """Скачивает видео с VK."""
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
    """
    Скачивает медиа с Instagram, вызывая внешний bash-скрипт.
    """
    logger.info("Запускаю универсальный bash-загрузчик для Instagram.")
    try:
        filename = f"instagram_video_{uuid.uuid4().hex}.mp4"
        
        # Абсолютный путь к нашему bash-скрипту
        script_path = "/root/MediaKit/download_instagram.sh"
        
        # Аргументы для передачи в скрипт: URL и имя файла
        args = [
            script_path,
            url,
            filename
        ]

        logger.info(f"Вызываю внешний скрипт: {' '.join(args)}")

        # Запускаем скрипт и ждем завершения
        process = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            check=False
        )

        # Проверяем, успешно ли отработал скрипт
        if process.returncode != 0:
            logger.error(f"Bash-скрипт завершился с кодом {process.returncode}. Stderr: {process.stderr}")
            # Анализируем вывод, чтобы дать пользователю более точный ответ
            error_output = (process.stdout + process.stderr).lower()
            if any(s in error_output for s in ["login is required", "401", "403", "429", "challenge required"]):
                return None, "ALL_ACCOUNTS_FAILED" # Используем эту ошибку как "бан"
            if any(s in error_output for s in ["private", "404", "no media found"]):
                return None, "INVALID_LINK"
            raise yt_dlp.utils.DownloadError(f"Bash script error: {process.stderr}")

        logger.info(f"Bash-скрипт успешно выполнен. Stdout: {process.stdout}")
        return (filename, "SUCCESS") if os.path.exists(filename) else (None, "UNKNOWN_DOWNLOAD_ERROR")

    except Exception as e:
        logger.error(f"Непредвиденная ошибка Python при вызове bash-скрипта: {e}")
        return None, "ALL_ACCOUNTS_FAILED"

        
# --- Функции для музыкальных сервисов ---
def get_track_info(yandex_url):
    """Получает информацию о треке Яндекс.Музыки со всеми исполнителями."""
    try:
        proxies = {'http': YANDEX_PROXIES, 'https': YANDEX_PROXIES} if YANDEX_PROXIES else None
        track_id = yandex_url.split('/')[-1].split('?')[0]
        api_url = f"https://api.music.yandex.net/tracks/{track_id}"
        response = requests.get(api_url, headers=YANDEX_HEADERS, proxies=proxies, timeout=10)
        response.raise_for_status()
        track = response.json()['result'][0]

        # Собираем имена ВСЕХ артистов из списка
        artist_names = [artist['name'] for artist in track.get('artists', [])]

        # Объединяем имена в одну строку через запятую
        all_artists = ', '.join(artist_names)

        return track.get('title', 'Unknown Title'), all_artists

    except Exception as e:
        logger.error(f"Ошибка при получении информации о треке Яндекс: {e}")
        return None, None

def get_yandex_album_track_details(album_url):
    """Получает список треков из альбома Яндекс.Музыки."""
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

# =================================================================
#
# НАЧАЛО ИЗМЕНЕННОГО БЛОКА 2: get_track_info_with_proxy (Spotify)
#
# =================================================================
def get_track_info_with_proxy(spotify_url):
    """
    Получает информацию о треке Spotify.
    Сначала пробует через прокси, потом (в случае неудачи) напрямую.
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    # Попытка 1: Через прокси (если он указан)
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

    # Попытка 2: Напрямую (если прокси не указан или не сработал)
    try:
        logger.warning("Spotify: Попытка 2 (напрямую)...")
        response = requests.get(spotify_url, headers=headers, timeout=10) # proxies=None
        response.raise_for_status()
        title = re.search(r'<meta property="og:title" content="(.*?)"', response.text).group(1)
        artist = re.search(r'<meta property="og:description" content="(.*?)"', response.text).group(1)
        return title, artist.split('·')[0].strip()
    except Exception as e:
        logger.error(f"Spotify: Ошибка при получении информации (даже напрямую): {e}")
        return None, None
# =================================================================
#
# КОНЕЦ ИЗМЕНЕННОГО БЛОКА 2
#
# =================================================================

def search_and_download_from_youtube(title, artist):
    """Ищет и скачивает аудио с YouTube по названию и исполнителю."""
    query = f"ytsearch1:{title} {artist}"
    filename_base = f"search_{uuid.uuid4().hex}"
    ydl_opts = get_base_ydl_opts(f"{filename_base}.%(ext)s")
    ydl_opts.update({
        'format': 'bestaudio/best', 
        'noplaylist': True, 
        'cookiefile': COOKIES_YOUTUBE_PATH,
        'proxy': config["PROXIES"].get("youtube") # <-- ОСТАВЛЕНО ПО ЗАПРОСУ
    })
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(query, download=True)
    
    for f in os.listdir(BASE_DIR):
        if f.startswith(filename_base):
            return os.path.join(BASE_DIR, f)
    return None

def convert_to_mp3(input_file):
    """Конвертирует файл в MP3."""
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

# --- Другие утилиты ---
def convert_video_for_telegram(input_path: str) -> str | None:
    """
    Конвертирует видео в формат, совместимый с Telegram (H.264/AAC).
    
    :param input_path: Путь к исходному видеофайлу.
    :return: Путь к сконвертированному файлу или None в случае ошибки.
    """
    if not input_path or not os.path.exists(input_path):
        logger.error(f"Файл для конвертации не найден: {input_path}")
        return None

    output_path = f"{os.path.splitext(input_path)[0]}_converted.mp4"
    logger.info(f"Начинаю конвертацию: {input_path} -> {output_path}")

    try:
        # Команда FFmpeg для максимальной совместимости
        # -c:v libx264 - видеокодек H.264
        # -preset veryfast - баланс между скоростью и качеством
        # -crf 23 - уровень качества (sane default)
        # -c:a aac - аудиокодек AAC
        # -pix_fmt yuv420p - пиксельный формат для совместимости со старыми устройствами
        command = [
            "ffmpeg",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p", # Важный флаг для совместимости
            output_path,
            "-y",  # Перезаписывать выходной файл без вопроса
            "-loglevel", "error" # Скрыть лишний вывод, показывать только ошибки
        ]
        
        subprocess.run(command, check=True)

        # Если конвертация успешна, удаляем исходный файл
        os.remove(input_path)
        logger.info("Конвертация успешно завершена.")
        return output_path

    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка FFmpeg при конвертации файла {input_path}: {e}")
        # Удаляем "битый" выходной файл, если он создался
        if os.path.exists(output_path):
            os.remove(output_path)
        # Если оригинал все еще существует, оставляем его как запасной вариант
        if os.path.exists(input_path):
             os.remove(input_path) # или можно вернуть input_path, но лучше удалить
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при конвертации: {e}")
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
        
def is_gif_like(file_path: str) -> bool:
    """Проверяет, является ли видеофайл 'GIF-подобным' (без аудио, до 60 сек)."""
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

# --- Основные обработчики Telegram ---
async def start(update: Update, context):
    """Обработчик команды /start."""
    await update.message.reply_text("Привет! Отправь мне ссылку на видео или трек.")

async def error_handler(update, context):
    """Обработчик ошибок Telegram API."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# <<<--- НАЧАЛО ИЗМЕНЕННОГО БЛОКА 2 ---
async def handle_message(update: Update, context):
    """Основной обработчик текстовых сообщений."""
    # --- ВРЕМЕННАЯ СТРОКА (теперь в логах) ---
    logger.info(f"Получено сообщение в чате. ID: {update.message.chat_id}")
    # ------------------------------------

    if not update.message or not update.message.text: return
    #...

    message = update.message.text.strip()
    # --- ИЗМЕНЕНИЕ 1: Получаем ID чата ---
    chat_id = update.message.chat_id

    # ===================================================================
    # НАЧАЛО БЛОКА ДЛЯ ОБРАБОТКИ ТОЧНЫХ СООБЩЕНИЙ
    # ===================================================================
    # Проверяем, есть ли сообщение пользователя как ключ в нашем словаре
    
    # --- ИЗМЕНЕНИЕ 2: Добавляем проверку на chat_id ---
    if message in EXACT_MATCH_REPLIES and chat_id not in EXCLUDED_CHAT_IDS:
        # Если есть, отправляем соответствующий ответ
        response_text = EXACT_MATCH_REPLIES[message]
        await update.message.reply_text(response_text)
        # И немедленно прекращаем дальнейшую обработку этого сообщения
        return
    # ===================================================================
    # КОНЕЦ БЛОКА
    # ===================================================================
    
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
                await context.bot.send_audio(chat_id=update.message.chat_id, audio=file_id, caption=caption_text)
            elif cached_file_type == "animation":
                await context.bot.send_animation(chat_id=update.message.chat_id, animation=file_id, caption=caption_text)
            elif cached_file_type == "photo":
                await context.bot.send_photo(chat_id=update.message.chat_id, photo=file_id, caption=caption_text)
            else:
                await context.bot.send_video(chat_id=update.message.chat_id, video=file_id, caption=caption_text)
            return
        except Exception as e:
            logger.error(f"Не удалось отправить кэшированный файл {file_id}: {e}")
            if message in cache_data: del cache_data[message]; save_cache(cache_data)
            await update.message.reply_text("Файл из кэша недействителен. Попробую скачать заново.")

    if not any(s in message for s in supported_services):
        logger.info(f"Сообщение '{message}' не содержит поддерживаемой ссылки. Игнорирую.")
        return

    logger.info(f"Получено сообщение с поддерживаемой ссылкой: {message} от {update.effective_user.id}")
    
    status_message = None
    downloaded_file = None
    file_to_send_type = "video"
    file_id_to_cache = None
    caption_to_cache = ""

    # Переменные для аудио (title/artist)
    title = "Unknown Title"
    artist = "Unknown Artist"

    try:
        status_message = await update.message.reply_text("Получил ссылку, начинаю обработку...")

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
                        chat_id=update.message.chat_id,
                        text=f"({i+1}/{len(tracks)}) Обрабатываю: *{artist} – {title}*",
                        parse_mode="Markdown"
                    )
                    album_track_file = None
                    try:
                        temp_dl_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                        album_track_file = await asyncio.to_thread(convert_to_mp3, temp_dl_file)
                        if album_track_file:
                            with open(album_track_file, "rb") as f:
                                await context.bot.send_audio(chat_id=update.message.chat_id, audio=f, title=title, performer=artist)
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
            # title, artist = None, None # Убрали, т.к. определены выше
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
                    raise e # Передаем FileSizeExceededError и др. выше
                
                # --- БЛОК ДЛЯ YOUTUBE ---
                if downloaded_file:
                    await status_message.edit_text("Видео скачано! Конвертирую для лучшей совместимости...")
                    converted_file = await asyncio.to_thread(convert_video_for_telegram, downloaded_file)
                    
                    if converted_file:
                        downloaded_file = converted_file # Подменяем имя файла на новое
                    else:
                        # Если конвертация не удалась, сообщаем и прерываем
                        await status_message.edit_text("Не удалось сконвертировать видео. Отправка отменена.")
                        downloaded_file = None
                # --- КОНЕЦ БЛОКА ---

            elif "instagram.com" in message:
                await status_message.edit_text("Обрабатываю ссылку Instagram...")
                # Эта функция уже имеет детальную обработку ошибок
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

# =================================================================
#
# НАЧАЛО ИЗМЕНЕННОГО БЛОКА 3: TikTok/Reddit/VK
#
# =================================================================
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
# =================================================================
#
# КОНЕЦ ИЗМЕНЕННОГО БЛОКА 3
#
# =================================================================

        if downloaded_file and os.path.exists(downloaded_file):
            await status_message.edit_text("Файл загружен! Отправляю...")
            if file_to_send_type == "audio":
                with open(downloaded_file, "rb") as f:
                    sent_message = await context.bot.send_audio(chat_id=update.message.chat_id, audio=f, title=title, performer=artist)
                file_id_to_cache = sent_message.audio.file_id
            elif await asyncio.to_thread(is_gif_like, downloaded_file):
                with open(downloaded_file, 'rb') as f: sent_message = await context.bot.send_animation(chat_id=update.message.chat_id, animation=f)
                file_id_to_cache, file_to_send_type = sent_message.animation.file_id, "animation"
            elif downloaded_file.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                with open(downloaded_file, 'rb') as f: sent_message = await context.bot.send_photo(chat_id=update.message.chat_id, photo=f)
                file_id_to_cache, file_to_send_type = sent_message.photo[-1].file_id, "photo"
            else:
                with open(downloaded_file, 'rb') as f: sent_message = await context.bot.send_video(chat_id=update.message.chat_id, video=f)
                file_id_to_cache, file_to_send_type = sent_message.video.file_id, "video"
        
        # Это условие проверяет, что файл не был скачан И при этом не было УЖЕ отправлено сообщение об ошибке
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
        else: await update.message.reply_text(error_text)
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке '{message}': {e}", exc_info=True)
        error_text = f"Произошла непредвиденная ошибка. Попробуйте позже."
        if status_message and not ("Ошибка" in status_message.text): # Не перезаписываем детальную ошибку
            await status_message.edit_text(error_text)
        elif not status_message:
            await update.message.reply_text(error_text)
    finally:
        if downloaded_file and os.path.exists(downloaded_file):
            logger.info(f"Удаляю временный файл: {downloaded_file}")
            try: os.remove(downloaded_file)
            except OSError as e: logger.warning(f"Не удалось удалить временный файл {downloaded_file}: {e}")

def main():
    """
    Основная функция для запуска Telegram-бота.
    """
    initialize_instagram_accounts()
    # Запускаем поток для очистки временных файлов
    threading.Thread(target=cleanup_folder, daemon=True, name="CleanupThread").start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен и готов к работе!")
    app.run_polling()

if __name__ == "__main__":
    main()