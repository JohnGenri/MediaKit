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
from telegram import Update, InlineQueryResultCachedVideo, InlineQueryResultCachedAudio, InlineQueryResultCachedPhoto, InlineQueryResultCachedMpeg4Gif # ДОБАВЛЕНО: InlineQueryResultCachedPhoto, InlineQueryResultCachedMpeg4Gif
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, InlineQueryHandler, filters
from playwright.async_api import async_playwright
from collections import deque

# Пользовательские исключения
class FileSizeExceededError(Exception):
    """Исключение для случаев, когда размер файла превышает допустимый."""
    pass

# НОВОЕ: Исключения для управления загрузкой из Instagram
class InstagramAccountBannedError(Exception):
    """Исключение для забаненного аккаунта."""
    pass

class InvalidLinkError(Exception):
    """Исключение для неверной или приватной ссылки."""
    pass

# Пути к важным файлам
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # Папка MediaKit
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')    # Папка important
CACHE_FILE = os.path.join(IMPORTANT_DIR, 'cache.json')      # Кэш
SENT_FILE = os.path.join(IMPORTANT_DIR, 'sent_videos.json')    # Список отправленных
INSTAGRAM_FOLDER = os.path.dirname(os.path.abspath(__file__)) # Папка Инстаграм
CACHE = CACHE_FILE
SENT = SENT_FILE

# Глобальные пути к кукам, которые будут загружены из config.json
COOKIES_YOUTUBE_PATH = None
COOKIES_REDDIT_PATH = None

# НОВОЕ: Глобальные переменные для управления аккаунтами Instagram
instagram_accounts_queue = deque()
instagram_queue_lock = asyncio.Lock() # Используем asyncio.Lock для асинхронного кода
BANNED_INSTAGRAM_ACCOUNTS_FILE = os.path.join(IMPORTANT_DIR, 'banned_instagram_accounts.log')


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Уровень INFO для лучшей отладки
)
logger = logging.getLogger(__name__)

# Загрузка конфигурации
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
try:
    with open(CONFIG_PATH, 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    logger.error(f"Файл конфигурации '{CONFIG_PATH}' не найден. Убедитесь, что он существует.")
    exit(1) # Выходим, если конфигурация не найдена

BOT_TOKEN = config.get("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в config.json. Пожалуйста, укажите токен бота.")
    exit(1)

# Инициализация Reddit клиента
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
    logger.warning("Параметры Reddit API не полностью указаны в config.json. Функции Reddit могут быть недоступны.")
    reddit = None # Устанавливаем в None, если не удалось инициализировать

YANDEX_PROXIES = config["PROXIES"].get("yandex")
SPOTIFY_PROXIES = config["PROXIES"].get("spotify")
TIKTOK_PROXIES = config["PROXIES"].get("tiktok")

YANDEX_HEADERS = {
    "Authorization": config["HEADERS"].get("yandex_auth", "")
}

# Корректные пути к куки из конфига
COOKIES_YOUTUBE_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("youtube", ''))
COOKIES_REDDIT_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"].get("reddit", ''))

# НОВОЕ: Функция для инициализации очереди аккаунтов Instagram
def initialize_instagram_accounts():
    global instagram_accounts_queue
    accounts = config.get("INSTAGRAM_ACCOUNTS", [])
    if not accounts:
        logger.warning("В config.json не найдены аккаунты для Instagram (INSTAGRAM_ACCOUNTS).")
        return

    for acc in accounts:
        cookie_path = os.path.join(IMPORTANT_DIR, acc['cookie_file'])
        if os.path.exists(cookie_path):
            instagram_accounts_queue.append({
                "cookie_file": cookie_path,
                "proxy": acc['proxy']
            })
        else:
            logger.error(f"Файл куки для Instagram не найден: {cookie_path}. Этот аккаунт будет пропущен.")
    
    logger.info(f"Инициализировано {len(instagram_accounts_queue)} аккаунтов Instagram.")


SUPPORTED_SERVICES = [
    "music.youtube.com",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "reddit.com",
    "vk.com",
    "vk.ru",
    "vkvideo.ru",
    "music.yandex.ru",
    "open.spotify.com"
]

def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Ошибка чтения CACHE_FILE '{CACHE}', создаю новый.")
                return {} # Возвращаем пустой кэш, если файл поврежден
    else:
        return {}

def save_cache(cache_data):
    # Убедимся, что директория существует перед сохранением
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, 'w') as f:
        json.dump(cache_data, f)

def cleanup_folder(interval=200, target_extensions=('.mp3', '.mp4', '.avi', '.part', '.webm', '.jpg', '.jpeg', '.png', '.gif', '.bin')): # ДОБАВЛЕНО: расширения для изображений и гифок
    while True:
        try:
            logger.info("Очистка временных файлов...")
            for filename in os.listdir(BASE_DIR):
                file_path = os.path.join(BASE_DIR, filename)

                # Если это файл и его расширение соответствует целевым
                if os.path.isfile(file_path) and file_path.endswith(target_extensions) and filename != '.gitignore':
                    os.remove(file_path)
                    logger.info(f"Удален файл: {filename}")

        except Exception as e:
            logger.error(f"Ошибка при очистке папки: {e}")

        # Ожидание перед следующим циклом
        time.sleep(interval)

async def start(update: Update, context):
    logger.info(f"Получен запрос на /start от {update.effective_user.id}")
    await update.message.reply_text("Привет! Отправь мне ссылку на видео или трек, или используй инлайн-режим для скачивания!")


# Ссылки на все сервисы музыки 
def generate_service_links(url, title=None, artist=None):
    try:
        # Если title и artist уже переданы, используем их
        if title and artist:
            logger.info(f"Генерация ссылок для '{title}' от '{artist}'")
        else:
            # Определение сервиса и получение метаданных, если не переданы
            if "music.yandex.ru/track/" in url:
                logger.info("Обработка ссылки Яндекс.Музыка для генерации ссылок")
                title, artist = get_track_info(url)
            elif "open.spotify.com" in url:
                logger.info("Обработка ссылки Spotify для генерации ссылок")
                title, artist = get_track_info_with_proxy(url)
            elif "music.youtube.com" in url:
                logger.info("Обработка ссылки YouTube Music для генерации ссылок")
                # Для YouTube Music audio (0) мы уже имеем title и artist после download_youtube_music_audio 
                # Если вызываем здесь, то нужно пересмотреть логику, чтобы не скачивать дважды.
                # Пока оставим как есть, но это может быть неэффективно.
                _, title, artist = asyncio.run(download_youtube_music_audio(url)) 
                if title is None or artist is None:
                    raise ValueError("Не удалось извлечь метаданные для генерации ссылок.")
            else:
                raise ValueError("Ссылка не относится к поддерживаемым сервисам для генерации ссылок.")

        # Проверка корректности метаданных
        if not title or not artist:
            raise ValueError("Не удалось извлечь метаданные из URL.")

        # Очистка метаданных
        def clean_metadata(text):
            # Убираем все лишние элементы, такие как альбомы, года, метки
            return re.sub(r"·.*", "", text).strip()

        title = clean_metadata(title)
        artist = clean_metadata(artist)

        # Генерация ссылок
        links = {
            "youtube": f"https://music.youtube.com/search?q={title.replace(' ', '+')}+{artist.replace(' ', '+')}",
            "spotify": f"https://open.spotify.com/search/{title} {artist}".replace(" ", "%20"),
            "yandex": f"https://music.yandex.ru/search?text={title.replace(' ', '%20')}+{artist.replace(' ', '%20')}"
        }
        logger.info(f"Сгенерированные ссылки: {links}")
        return links

    except Exception as e:
        logger.error(f"Ошибка в generate_service_links: {e}")
        return None


async def download_tiktok_video_with_proxy(url):
    proxy = TIKTOK_PROXIES
    if not proxy:
        logger.error("Прокси для TikTok не настроен.")
        return None

    unique_filename = f"tiktok_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = {
        'proxy': proxy,
        'format': 'best',
        'outtmpl': unique_filename,
        'quiet': True,
        'max_filesize': 50 * 1024 * 1024, # yt-dlp опция для ограничения
        'socket_timeout': 60, # НОВОЕ: таймаут сокета
        'retries': 5, # НОВОЕ: количество попыток
        'fragment_retries': 5, # НОВОЕ: количество попыток для фрагментов
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False) # Получаем инфо без скачивания
            
            file_size = info_dict.get('filesize')
            if file_size is None:
                file_size = info_dict.get('filesize_approx')
            if file_size is None:
                file_size = 0 # Убедитесь, что file_size - число

            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([url])
        return unique_filename
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка загрузки видео с TikTok: {e}")
        if "size" in str(e).lower() and "exceeds" in str(e).lower():
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        raise e # Перебрасываем другие ошибки загрузки
    except FileSizeExceededError:
        raise # Перебрасываем наше исключение
    except Exception as e:
        logger.error(f"Неизвестная ошибка при загрузке TikTok: {e}")
        return None

def clean_youtube_music_url(url):
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    query_params.pop('si', None)
    
    netloc_actual = parsed_url.netloc

    if "music.youtube.com" in url:
        netloc_actual = "music.youtube.com"
    elif "youtube.com" in url or "youtu.be" in url:
        netloc_actual = "www.youtube.com"
    elif "open.spotify.com" in url:
        netloc_actual = "open.spotify.com"
    
    new_query = urlencode(query_params, doseq=True)
    cleaned_url = urlunparse((
        parsed_url.scheme,
        netloc_actual,
        parsed_url.path,
        parsed_url.params,
        new_query,
        parsed_url.fragment
    ))
    return cleaned_url

async def download_youtube_music_audio(url):
    try:
        logger.info(f"Обработка ссылки YouTube Music: {url}")
        clean_url = clean_youtube_music_url(url)
        logger.info(f"Используем очищенный URL: {clean_url}")
        base_filename = f"youtube_music_{uuid.uuid4().hex}"
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': base_filename,
            'cookiefile': COOKIES_YOUTUBE_PATH,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': False,
            'verbose': True,
            'logger': logger,
            'nocheckcertificate': True,
            'max_filesize': 50 * 1024 * 1024,
            'socket_timeout': 60,
            'retries': 5,
            'fragment_retries': 5,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(clean_url, download=False)
            
            file_size = info_dict.get('filesize')
            if file_size is None:
                file_size = info_dict.get('filesize_approx')
            if file_size is None:
                file_size = 0

            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([clean_url])
            title = info_dict.get('title', 'Unknown Title')
            artist = info_dict.get('artist') or info_dict.get('uploader', 'Unknown Artist')
            logger.info(f"Название трека: {title}, Исполнитель: {artist}")

        audio_filename = base_filename + ".mp3"
        if os.path.exists(audio_filename):
            for f in os.listdir(BASE_DIR):
                if f.startswith(base_filename) and not f.endswith(".mp3"):
                    try:
                        os.remove(os.path.join(BASE_DIR, f))
                    except OSError as e:
                        logger.warning(f"Не удалось удалить временный файл {f}: {e}")
            return audio_filename, title, artist
        else:
            logger.error(f"Файл {audio_filename} не найден после загрузки.")
            return None, None, None

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка при обработке ссылки YouTube Music: {e}")
        if "size" in str(e).lower() and "exceeds" in str(e).lower():
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        raise e
    except FileSizeExceededError:
        raise
    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки YouTube Music: {e}")
        return None, None, None

async def download_youtube_video(url):
    try:
        logger.info(f"Обработка ссылки YouTube: {url}")
        video_filename = f"youtube_video_{uuid.uuid4().hex}.mp4"
        ydl_opts = {
            'format': 'best',
            'cookiefile': COOKIES_YOUTUBE_PATH,
            'outtmpl': video_filename,
            'quiet': True,
            'max_filesize': 50 * 1024 * 1024,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 5,
            'fragment_retries': 5,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            
            file_size = info_dict.get('filesize')
            if file_size is None:
                file_size = info_dict.get('filesize_approx')
            if file_size is None:
                file_size = 0

            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([url])

        if os.path.exists(video_filename):
            logger.info(f"Видео успешно загружено: {video_filename}")
            return video_filename
        else:
            return None

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка при обработке ссылки YouTube: {e}")
        if "size" in str(e).lower() and "exceeds" in str(e).lower():
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        raise e
    except FileSizeExceededError:
        raise
    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки YouTube: {e}")
        return None, None

async def download_instagram_video(url, cookie_file, proxy):
    try:
        logger.info(f"Попытка скачивания с Instagram, используя куки: {os.path.basename(cookie_file)}")
        os.makedirs(INSTAGRAM_FOLDER, exist_ok=True)
        video_filename = os.path.join(INSTAGRAM_FOLDER, f"instagram_video_{uuid.uuid4().hex}.mp4")

        ydl_opts = {
            'format': 'best',
            'cookiefile': cookie_file,
            'outtmpl': video_filename,
            'quiet': True,
            'proxy': proxy,
            'max_filesize': 50 * 1024 * 1024,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 3,
            'fragment_retries': 3,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            file_size = info_dict.get('filesize') or info_dict.get('filesize_approx') or 0
            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
            ydl.download([url])

        if os.path.exists(video_filename):
            logger.info(f"Видео успешно загружено: {video_filename}")
            return video_filename
        else:
            return None

    except yt_dlp.utils.DownloadError as e:
        error_message = str(e).lower()
        logger.warning(f"Ошибка yt-dlp при работе с аккаунтом {os.path.basename(cookie_file)}: {error_message}")

        if "login is required" in error_message or "401" in error_message or "403" in error_message or "429" in error_message or "challenge required" in error_message:
            raise InstagramAccountBannedError(f"Аккаунт {os.path.basename(cookie_file)} вероятно забанен или требует входа.")
        
        if "private" in error_message or "404" in error_message or "no media found" in error_message:
            raise InvalidLinkError("Ссылка недействительна, приватна или не содержит медиа.")

        if "size" in error_message and "exceeds" in error_message:
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        
        raise e
    
    except FileSizeExceededError:
        raise
    
    except Exception as e:
        logger.error(f"Неизвестная ошибка при загрузке с Instagram: {e}")
        raise

async def managed_instagram_download(url):
    global instagram_accounts_queue
    
    async with instagram_queue_lock:
        if not instagram_accounts_queue:
            logger.error("Очередь аккаунтов Instagram пуста. Скачивание невозможно.")
            return None, "NO_ACCOUNTS"

        attempts = len(instagram_accounts_queue)
        for i in range(attempts):
            current_account = instagram_accounts_queue.popleft()
            cookie_path = current_account["cookie_file"]
            proxy_str = current_account["proxy"]
            
            logger.info(f"Попытка {i+1}/{attempts}. Используем аккаунт: {os.path.basename(cookie_path)}")

            try:
                await asyncio.sleep(random.uniform(5, 10))
                video_file = await download_instagram_video(url, cookie_path, proxy_str)
                
                if video_file:
                    logger.info(f"Успешная загрузка с аккаунтом {os.path.basename(cookie_path)}. Возвращаем его в конец очереди.")
                    instagram_accounts_queue.append(current_account)
                    return video_file, "SUCCESS"
                else:
                    instagram_accounts_queue.appendleft(current_account)
                    return None, "UNKNOWN_DOWNLOAD_ERROR"


            except InstagramAccountBannedError as e:
                logger.warning(f"Аккаунт забанен или невалиден: {e}. Удаляем из очереди.")
                with open(BANNED_INSTAGRAM_ACCOUNTS_FILE, 'a') as f:
                    f.write(f"{time.ctime()}: {current_account}\n")
                continue
            
            except InvalidLinkError as e:
                logger.error(f"Ошибка ссылки: {e}")
                instagram_accounts_queue.appendleft(current_account)
                return None, "INVALID_LINK"
            
            except FileSizeExceededError:
                logger.warning("Файл слишком большой.")
                instagram_accounts_queue.appendleft(current_account)
                return None, "FILE_TOO_LARGE"

            except Exception as e:
                logger.error(f"Произошла непредвиденная ошибка с аккаунтом {os.path.basename(cookie_path)}: {e}")
                with open(BANNED_INSTAGRAM_ACCOUNTS_FILE, 'a') as f:
                    f.write(f"{time.ctime()}: {current_account} (Unknown Error: {e})\n")
                continue

    logger.error("Все аккаунты Instagram не смогли обработать запрос.")
    return None, "ALL_ACCOUNTS_FAILED"


# ИЗМЕНЕНО: Новая, более простая и надежная версия функции Reddit
async def download_reddit_video(url):
    try:
        logger.info(f"Обработка ссылки Reddit/RedGifs: {url}")
        
        # НОВОЕ: Проверяем, является ли URL ссылкой на медиа-перенаправление Reddit
        # или просто обычным постом/RedGifs/Gfycat
        url_to_download = url
        parsed_url = urlparse(url)

        if parsed_url.netloc == "www.reddit.com" and parsed_url.path == "/media":
            # Это ссылка-редирект от Reddit. Извлекаем реальный URL медиафайла.
            query_params = parse_qs(parsed_url.query)
            if 'url' in query_params and query_params['url']:
                original_media_url = unquote(query_params['url'][0])
                logger.info(f"Извлечен оригинальный медиа URL из Reddit редиректа: {original_media_url}")
                url_to_download = original_media_url
            else:
                logger.warning(f"Reddit медиа URL не содержит параметра 'url': {url}. Используем исходный URL.")
                # Если параметр 'url' отсутствует, возможно, это нестандартный случай,
                # и лучше попробовать исходный URL, если он все же может быть обработан.
                url_to_download = url # Оставляем исходный URL, так как не смогли извлечь
        elif "redgifs.com" in parsed_url.netloc or "gfycat.com" in parsed_url.netloc:
            # Эти домены yt-dlp обычно хорошо обрабатывает напрямую.
            url_to_download = url
        elif "reddit.com" in parsed_url.netloc and "/comments/" in parsed_url.path:
            # Это обычная ссылка на пост Reddit. yt-dlp сам найдет медиа.
            url_to_download = url
        else:
            # Возможно, это какой-то другой, неопознанный URL Reddit.
            # Оставляем как есть, чтобы yt-dlp попытался обработать.
            logger.warning(f"Неизвестный тип Reddit URL: {url}. yt-dlp попытается обработать напрямую.")
            url_to_download = url


        video_filename = f"reddit_video_{uuid.uuid4().hex}.mp4"

        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': video_filename,
            'cookiefile': COOKIES_REDDIT_PATH,
            'quiet': True,
            'max_filesize': 50 * 1024 * 1024,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 5,
            'fragment_retries': 5,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url_to_download, download=False) # Используем url_to_download
            
            file_size = info_dict.get('filesize') or info_dict.get('filesize_approx') or 0
            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([url_to_download]) # Используем url_to_download

        if os.path.exists(video_filename):
            logger.info(f"Видео с Reddit/RedGifs успешно загружено: {video_filename}")
            return video_filename
        else:
            logger.error("Файл не был создан после загрузки.")
            return None

    except FileSizeExceededError:
        raise # Перебрасываем ошибку о размере файла наверх
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка yt-dlp при обработке ссылки Reddit: {e}")
        # Проверяем на специфичную ошибку, если вдруг она снова появится
        if "requested format is not available" in str(e).lower():
            logger.error("Даже с гибкими настройками не удалось найти подходящий формат.")
        raise e # Перебрасываем ошибку наверх для общей обработки
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке ссылки Reddit: {e}")
        return None


def generate_random_filename(extension):
    random_name = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"{random_name}.{extension}"


def get_track_info(yandex_url):
    try:
        proxies = {
            'http': config["PROXIES"].get("yandex"),
            'https': config["PROXIES"].get("yandex")
        }
        proxies = {k: v for k, v in proxies.items() if v is not None}

        track_id = yandex_url.split('/')[-1].split('?')[0]
        api_url = f"https://api.music.yandex.net/tracks/{track_id}"
        
        headers = {
            'Authorization': config["HEADERS"].get("yandex_auth", "")
        }

        response = requests.get(api_url, headers=headers, proxies=proxies, timeout=10)
        response.raise_for_status()
        data = response.json()
        track = data['result'][0]
        title = track['title']
        artist = track['artists'][0]['name']
        logger.info(f"Название трека: {title}, Исполнитель: {artist}")
        return title, artist

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети или запроса при получении информации о треке с Яндекс.Музыки: {e}")
        return None, None
    except KeyError as e:
        logger.error(f"Не удалось найти данные трека в ответе Яндекс.Музыки: {e}. Ответ: {response.text}")
        return None, None
    except Exception as e:
        logger.error(f"Ошибка при получении информации о треке с Яндекс.Музыки: {e}")
        return None, None

async def get_yandex_album_track_details(album_url):
    try:
        proxies = {
            'http': config["PROXIES"].get("yandex"),
            'https': config["PROXIES"].get("yandex")
        }
        proxies = {k: v for k, v in proxies.items() if v is not None}

        album_id_match = re.search(r'/album/(\d+)', album_url)
        if not album_id_match:
            logger.error(f"Не удалось извлечь ID альбома из URL: {album_url}")
            return []
        
        album_id = album_id_match.group(1)
        api_url = f"https://api.music.yandex.net/albums/{album_id}/with-tracks"
        
        headers = {
            'Authorization': config["HEADERS"].get("yandex_auth", "")
        }

        response = await asyncio.to_thread(requests.get, api_url, headers=headers, proxies=proxies, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        tracks_info = []
        if 'result' in data and 'volumes' in data['result']:
            for volume in data['result']['volumes']:
                for track in volume:
                    title = track.get('title', 'Unknown Title')
                    artist_names = [artist['name'] for artist in track.get('artists', [])]
                    artist = ', '.join(artist_names) if artist_names else 'Unknown Artist'
                    tracks_info.append({"title": title, "artist": artist})
                    logger.info(f"Найден трек в альбоме: {title} - {artist}")
        return tracks_info

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети или запроса при получении информации об альбоме Яндекс.Музыки: {e}")
        return []
    except KeyError as e:
        logger.error(f"Не удалось найти данные альбома в ответе Яндекс.Музыки: {e}. Ответ: {response.text}")
        return []
    except Exception as e:
        logger.error(f"Ошибка при получении информации об альбоме Яндекс.Музыки: {e}")
        return []


def get_track_info_with_proxy(spotify_url):
    proxy_str = config["PROXIES"].get("spotify", "socks5h://3wNBtL:Bhtv5A@38.152.244.30:9325")
    proxy = {
        "http": proxy_str,
        "https": proxy_str
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        response = requests.get(spotify_url, headers=headers, proxies=proxy, timeout=10)
        response.raise_for_status()

        title_match = re.search(r'<meta property="og:title" content="(.*?)"', response.text)
        description_match = re.search(r'<meta property="og:description" content="(.*?)"', response.text)

        if not title_match or not description_match:
            raise Exception("Не удалось найти данные о треке в метатегах Spotify.")

        track_name = title_match.group(1).strip()
        artist_name = description_match.group(1).strip()
        logger.info(f"Название трека: {track_name}, Исполнитель: {artist_name}")
        return track_name, artist_name

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка сети или запроса при доступе к Spotify: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Ошибка при получении информации о треке Spotify: {e}")
        return None, None

def search_and_download_from_youtube(title, artist):
    try:
        search_query = f"ytsearch1:{title} {artist}"
        random_filename = generate_random_filename("webm")
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'outtmpl': random_filename + '.%(ext)s',
            'max_filesize': 50 * 1024 * 1024,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 5,
            'fragment_retries': 5,
        }

        actual_downloaded_file = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(search_query, download=False)
            
            file_size = info_dict.get('filesize')
            if file_size is None:
                file_size = info_dict.get('filesize_approx')
            if file_size is None:
                file_size = 0

            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([search_query])
            
            downloaded_files = [f for f in os.listdir(BASE_DIR) if f.startswith(random_filename)]
            if downloaded_files:
                actual_downloaded_file = os.path.join(BASE_DIR, downloaded_files[0])
                logger.info(f"Трек скачан: {actual_downloaded_file}")
                return actual_downloaded_file
            else:
                logger.error("Файл не найден после загрузки с YouTube.")
                return None

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка загрузки с YouTube: {e}")
        if "size" in str(e).lower() and "exceeds" in str(e).lower():
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        raise e
    except FileSizeExceededError:
        raise
    except Exception as e:
        logger.error(f"Ошибка загрузки с YouTube: {e}")
        return None

def convert_to_mp3(input_file):
    try:
        output_file = f"{os.path.splitext(input_file)[0]}.mp3"
        subprocess.run([
            "ffmpeg", "-i", input_file, "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", output_file, "-loglevel", "quiet"
        ], check=True)
        os.remove(input_file)
        logger.info(f"Файл конвертирован в MP3: {output_file}")
        return output_file
    except Exception as e:
        logger.error(f"Ошибка при конвертации в MP3: {e}")
        return None

async def download_vk_video(url, username, password):
    unique_filename = f"vk_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = {
        'format': 'best',
        'outtmpl': unique_filename,
        'quiet': True,
        'username': username,
        'password': password,
        'max_filesize': 50 * 1024 * 1024,
        'socket_timeout': 60,
        'retries': 5,
        'fragment_retries': 5,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            
            file_size = info_dict.get('filesize')
            if file_size is None:
                file_size = info_dict.get('filesize_approx')
            if file_size is None:
                file_size = 0

            if file_size > 50 * 1024 * 1024:
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")

            ydl.download([url])
        return unique_filename
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка загрузки видео с ВКонтакте: {e}")
        if "size" in str(e).lower() and "exceeds" in str(e).lower():
            raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED")
        raise e
    except FileSizeExceededError:
        raise
    except Exception as e:
        logger.error(f"Ошибка загрузки видео с ВКонтакте: {e}")
        return None


# --- НОВАЯ ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: is_gif_like ---
def is_gif_like(file_path: str) -> bool:
    """
    Проверяет, является ли видеофайл "GIF-подобным" (без аудиодорожки и относительно короткий).
    Использует ffprobe.
    """
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        
        has_audio = False
        duration = 0.0
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio":
                has_audio = True
            if stream.get("duration"):
                try:
                    duration = max(duration, float(stream["duration"]))
                except (ValueError, TypeError):
                    pass # Игнорируем ошибки конвертации продолжительности

        # Считаем "гифкой", если нет аудио и продолжительность до 60 секунд
        return not has_audio and duration < 60.0
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning(f"Ошибка при вызове ffprobe или ffprobe не найден: {e}. Не могу определить GIF-подобность.")
        return False # Не можем определить, считаем, что не GIF-подобное
    except json.JSONDecodeError as e:
        logger.warning(f"Ошибка декодирования JSON от ffprobe для {file_path}: {e}")
        return False
    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке GIF-подобности файла {file_path}: {e}")
        return False

# --- НОВАЯ ФУНКЦИЯ: download_raw_file ---
async def download_raw_file(url: str) -> str | None:
    """
    Попытаться скачать файл напрямую по URL, используя requests в отдельном потоке.
    Возвращает путь к скачанному файлу или None в случае ошибки.
    """
    unique_filename = f"raw_download_{uuid.uuid4().hex}"
    
    parsed_url = urlparse(url)
    path = parsed_url.path
    if '.' in path:
        ext = path.split('.')[-1]
        if ext.lower() in ['mp4', 'gif', 'jpg', 'jpeg', 'png', 'webp', 'mp3', 'webm', 'ogg', 'flac']:
            unique_filename += f".{ext.lower()}"
        else:
            unique_filename += ".bin"
    else:
        unique_filename += ".bin"

    output_path = os.path.join(BASE_DIR, unique_filename)

    try:
        # Запускаем синхронную операцию requests.get в отдельном потоке
        response = await asyncio.to_thread(requests.get, url, stream=True, timeout=30)
        response.raise_for_status()

        content_length = response.headers.get('Content-Length')
        if content_length:
            file_size = int(content_length)
            if file_size > 50 * 1024 * 1024:
                logger.warning(f"Прямая загрузка: файл {url} слишком большой ({file_size} байт).")
                response.close()
                raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED_RAW")

        downloaded_size = 0
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                downloaded_size += len(chunk)
                if downloaded_size > 50 * 1024 * 1024:
                    response.close()
                    raise FileSizeExceededError("MAX_FILE_SIZE_EXCEEDED_RAW")
                f.write(chunk)
        
        response.close()
        logger.info(f"Файл успешно загружен напрямую: {output_path}")
        return output_path
    except FileSizeExceededError:
        logger.warning(f"Файл {url} превысил допустимый размер при прямой загрузке.")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка requests при прямой загрузке файла с {url}: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при прямой загрузке файла с {url}: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return None


async def handle_message(update: Update, context):
    if not update.message or not update.message.text:
        return

    message = update.message.text.strip()
    cache_data = load_cache()

    if message in cache_data:
        file_id = cache_data[message]
        logger.info(f"Ссылка найдена в кэше для {message}, отправляем кэшированный файл.")
        # ДОБАВЛЕНО: Отправка по типу файла из кэша
        cached_file_type = cache_data.get(f"{message}_type")
        caption_text = cache_data.get(f"{message}_caption", "")

        if cached_file_type == "audio":
            await context.bot.send_audio(chat_id=update.message.chat_id, audio=file_id, caption=caption_text)
        elif cached_file_type == "animation":
            await context.bot.send_animation(chat_id=update.message.chat_id, animation=file_id, caption=caption_text)
        elif cached_file_type == "photo":
            await context.bot.send_photo(chat_id=update.message.chat_id, photo=file_id, caption=caption_text)
        else: # По умолчанию или если тип не указан, отправляем как видео
            await context.bot.send_video(chat_id=update.message.chat_id, video=file_id, caption=caption_text)
        return

    # --- ОБНОВЛЕННЫЙ БЛОК ОБРАБОТКИ ССЫЛОК ---
    status_message = None
    downloaded_file = None # Переименовал, чтобы использовать одну переменную для любого типа файла
    file_id = None
    caption_text = ""
    file_type_to_cache = "video" # По умолчанию, для кэша

    try:
        if "music.youtube.com" in message:
            status_message = await update.message.reply_text("Обнаружена ссылка на YouTube Music! Обрабатываю...")
            downloaded_file, title, artist = await download_youtube_music_audio(message)
            if downloaded_file and os.path.exists(downloaded_file):
                caption_text = f"{artist} - {title}"
                file_type_to_cache = "audio"
                await status_message.edit_text("Аудио загружено! Отправляю...")
                with open(downloaded_file, 'rb') as f:
                    sent_message = await context.bot.send_audio(
                        chat_id=update.message.chat_id, audio=f, title=title, performer=artist
                    )
                file_id = sent_message.audio.file_id
            else:
                await status_message.edit_text("Не удалось скачать аудио с YouTube Music.")

        elif "music.yandex.ru" in message:
            # УТОЧНЕННАЯ ЛОГИКА ДЛЯ ЯНДЕКС.МУЗЫКИ
            if "/album/" in message and "/track/" not in message: # Ссылка на альбом, но не на конкретный трек в альбоме
                status_message = await update.message.reply_text("Обнаружена ссылка на **альбом** Яндекс.Музыки! Получаю список треков...")
                tracks_info = await get_yandex_album_track_details(message)
                if tracks_info:
                    await status_message.edit_text(f"Найдено {len(tracks_info)} треков в альбоме. Начинаю загрузку...")
                    for i, track_data in enumerate(tracks_info):
                        title = track_data.get("title")
                        artist = track_data.get("artist")
                        if not title or not artist:
                            logger.warning(f"Пропускаю трек в альбоме из-за отсутствия метаданных: {track_data}")
                            continue

                        track_progress_message = await context.bot.send_message(
                            chat_id=update.message.chat_id, text=f"Обрабатываю трек {i+1}/{len(tracks_info)}: *{title}* от *{artist}*...", parse_mode="Markdown"
                        )
                        track_cache_key = f"yandex_album_track_{title}_{artist}" # Уникальный ключ для треков из альбомов
                        if track_cache_key in cache_data:
                            file_id_cached = cache_data[track_cache_key]
                            caption_text_cached = cache_data.get(f"{track_cache_key}_caption", f"{artist} - {title}")
                            await context.bot.send_audio(chat_id=update.message.chat_id, audio=file_id_cached, caption=caption_text_cached, title=title, performer=artist)
                            await track_progress_message.delete()
                            continue

                        temp_downloaded_file = None
                        temp_mp3_file = None
                        try:
                            temp_downloaded_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                            if temp_downloaded_file:
                                temp_mp3_file = await asyncio.to_thread(convert_to_mp3, temp_downloaded_file)
                                if temp_mp3_file and os.path.exists(temp_mp3_file):
                                    formatted_filename = f"{title} - {artist}.mp3".replace("/", "_").replace("\\", "_")
                                    target_mp3_path = os.path.join(BASE_DIR, formatted_filename)
                                    shutil.move(temp_mp3_file, target_mp3_path)
                                    with open(target_mp3_path, "rb") as audio_file:
                                        sent_message = await context.bot.send_audio(
                                            chat_id=update.message.chat_id, audio=audio_file, title=title, performer=artist
                                        )
                                    file_id_album_track = sent_message.audio.file_id
                                    cache_data[track_cache_key] = file_id_album_track
                                    cache_data[f"{track_cache_key}_caption"] = f"{artist} - {title}"
                                    cache_data[f"{track_cache_key}_type"] = "audio" # ДОБАВЛЕНО: тип для кэша
                                    save_cache(cache_data)
                                    os.remove(target_mp3_path)
                                    links = await asyncio.to_thread(generate_service_links, "", title, artist)
                                    if links:
                                        links_message = (
                                            f"Ссылки для *{title}* от *{artist}*:\n"
                                            f"🎵 [YouTube Music]({links['youtube']})\n"
                                            f"🎧 [Spotify]({links['spotify']})\n"
                                            f"🎼 [Яндекс.Музыка]({links['yandex']})"
                                        )
                                        await context.bot.send_message(
                                            chat_id=update.message.chat_id, text=links_message, parse_mode="Markdown", disable_web_page_preview=True
                                        )
                                else:
                                    await context.bot.send_message(chat_id=update.message.chat_id, text=f"Не удалось конвертировать трек *{title}* в MP3.", parse_mode="Markdown")
                            else:
                                await context.bot.send_message(chat_id=update.message.chat_id, text=f"Не удалось скачать трек *{title}* с YouTube.", parse_mode="Markdown")
                        except FileSizeExceededError:
                            await context.bot.send_message(chat_id=update.message.chat_id, text=f"Трек *{title}* слишком большой. Пропускаю.", parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Ошибка при обработке трека '{title}' из альбома: {e}")
                            await context.bot.send_message(chat_id=update.message.chat_id, text=f"Ошибка при обработке трека *{title}*: {e}", parse_mode="Markdown")
                        finally:
                            if temp_downloaded_file and os.path.exists(temp_downloaded_file):
                                try: os.remove(temp_downloaded_file)
                                except OSError as e: logger.warning(f"Не удалось удалить временный файл {temp_downloaded_file}: {e}")
                            if temp_mp3_file and os.path.exists(temp_mp3_file):
                                try: os.remove(temp_mp3_file)
                                except OSError as e: logger.warning(f"Не удалось удалить временный файл {temp_mp3_file}: {e}")
                        await track_progress_message.delete()
                    await status_message.delete()
                else:
                    await status_message.edit_text("Не удалось получить список треков из альбома Яндекс.Музыки.")
            elif "/track/" in message: # Ссылка на конкретный трек (даже если он в альбоме)
                status_message = await update.message.reply_text("Обнаружена ссылка на трек Яндекс.Музыки! Обрабатываю...")
                title, artist = get_track_info(message)
                if title and artist:
                    await status_message.edit_text("Получена информация о треке! Ищу его")
                    downloaded_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                    if downloaded_file:
                        await status_message.edit_text("Трек найден и скачан! Конвертирую в MP3...")
                        mp3_file = await asyncio.to_thread(convert_to_mp3, downloaded_file)
                        if mp3_file and os.path.exists(mp3_file):
                            formatted_filename = f"{title} - {artist}.mp3".replace("/", "_").replace("\\", "_")
                            target_mp3_path = os.path.join(BASE_DIR, formatted_filename)
                            shutil.move(mp3_file, target_mp3_path)
                            downloaded_file = target_mp3_path # Обновляем, чтобы в finally удалился mp3
                            caption_text = f"{artist} - {title}"
                            file_type_to_cache = "audio"
                            await status_message.edit_text("Трек успешно конвертирован в MP3! Отправляю...")
                            with open(downloaded_file, "rb") as f:
                                sent_message = await context.bot.send_audio(
                                    chat_id=update.message.chat_id, audio=f, title=title, performer=artist
                                )
                            file_id = sent_message.audio.file_id
                        else: await status_message.edit_text("Не удалось конвертировать трек в MP3.")
                    else: await status_message.edit_text("Не удалось скачать трек с YouTube.")
                else: await status_message.edit_text("Не удалось получить информацию о треке с Яндекс.Музыки.")
            else: # Любая другая ссылка на Яндекс.Музыку, которая не альбом и не трек (например, плейлист)
                await update.message.reply_text("Обнаружена ссылка на Яндекс.Музыку, но это не трек и не альбом. Поддерживаются только прямые ссылки на треки и альбомы.")
                return


        elif "open.spotify.com" in message:
            status_message = await update.message.reply_text("Обнаружена ссылка на Spotify! Обрабатываю...")
            title, artist = await asyncio.to_thread(get_track_info_with_proxy, message)
            if title and artist:
                await status_message.edit_text("Получена информация о треке! Ищу")
                downloaded_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                if downloaded_file:
                    await status_message.edit_text("Трек найден и скачан! Конвертирую в MP3...")
                    mp3_file = await asyncio.to_thread(convert_to_mp3, downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        formatted_filename = f"{title} - {artist}.mp3".replace("/", "_").replace("\\", "_")
                        target_mp3_path = os.path.join(BASE_DIR, formatted_filename)
                        shutil.move(mp3_file, target_mp3_path)
                        downloaded_file = target_mp3_path # Обновляем, чтобы в finally удалился mp3
                        caption_text = f"{artist} - {title}"
                        file_type_to_cache = "audio"
                        await status_message.edit_text("Трек успешно конвертирован в MP3! Отправляю...")
                        with open(downloaded_file, "rb") as f:
                            sent_message = await context.bot.send_audio(
                                chat_id=update.message.chat_id, audio=f, title=title, performer=artist
                            )
                        file_id = sent_message.audio.file_id
                    else: await status_message.edit_text("Не удалось конвертировать трек в MP3.")
                else: await status_message.edit_text("Не удалось скачать трек с YouTube.")
            else: await status_message.edit_text("Не удалось получить информацию о треке с Spotify.")

        elif "instagram.com" in message:
            status_message = await update.message.reply_text("Обнаружена ссылка на Instagram! Обрабатываю...")
            downloaded_file, status = await managed_instagram_download(message)
            
            # Логика обработки результата после managed_instagram_download
            if status == "SUCCESS" and downloaded_file and os.path.exists(downloaded_file):
                file_type_to_cache = "video" # Instagram всегда видео/гиф, не аудио
                await status_message.edit_text("Видео с Instagram загружено! Отправляю...")
                
                # Проверяем, является ли это видео GIF-подобным
                if await asyncio.to_thread(is_gif_like, downloaded_file):
                    await status_message.edit_text("Видео (GIF-подобное) с Instagram загружено! Отправляю...")
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_animation(chat_id=update.message.chat_id, animation=f)
                    file_id = sent_message.animation.file_id
                    file_type_to_cache = "animation"
                else:
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_video(chat_id=update.message.chat_id, video=f)
                    file_id = sent_message.video.file_id
                    file_type_to_cache = "video"
                
                # Сохраняем в кэш после успешной отправки
                if file_id:
                    cache_data[message] = file_id
                    cache_data[f"{message}_type"] = file_type_to_cache
                    cache_data[f"{message}_caption"] = caption_text
                    save_cache(cache_data)
                
                await status_message.delete()
                return # Успешная обработка, выходим

            else: # Если managed_instagram_download вернул ошибку или None
                error_messages = {
                    "INVALID_LINK": "Ошибка: ссылка недействительна, приватна или не содержит медиа.",
                    "FILE_TOO_LARGE": "Файл слишком большой. Максимальный размер 50МБ.",
                    "ALL_ACCOUNTS_FAILED": "К сожалению, все наши аккаунты для доступа к Instagram сейчас не работают. Попробуйте позже.",
                    "NO_ACCOUNTS": "Сервис для Instagram временно не настроен.",
                    "UNKNOWN_DOWNLOAD_ERROR": "Не удалось скачать видео с Instagram из-за неизвестной ошибки загрузки."
                }
                error_text = error_messages.get(status, f"Произошла непредвиденная ошибка: {status}. Попытка прямой загрузки...")
                await status_message.edit_text(error_text)
                
                # Попытка прямой загрузки только если не было специфических ошибок аккаунта/ссылки
                if status not in ["INVALID_LINK", "ALL_ACCOUNTS_FAILED", "NO_ACCOUNTS"]:
                    downloaded_file = await download_raw_file(message)
                    if downloaded_file and os.path.exists(downloaded_file):
                        # Логика отправки файла после прямой загрузки (как в общем блоке ниже)
                        pass # Обработается общим блоком в конце try
                    else:
                        await status_message.edit_text(f"{error_text}. Прямая загрузка также не удалась.")
                        return # Выходим, если ничего не получилось

        # Если никакой из специализированных блоков не справился, или если там была
        # FileSizeExceededError или yt_dlp.utils.DownloadError (кроме Instagram-specific)
        elif any(service in message for service in SUPPORTED_SERVICES): # Этот блок остаётся как универсальный
            service_detected = "неизвестного сервиса" # Изменил на более общий текст
            if "youtube.com" in message and "music.yandex.ru" not in message: service_detected = "YouTube"
            elif "tiktok.com" in message: service_detected = "TikTok"
            elif "reddit.com" in message: service_detected = "Reddit"
            elif "vk.com" in message or "vk.ru" in message or "vkvideo.ru" in message: service_detected = "ВКонтакте"
            
            status_message = await update.message.reply_text(f"Обнаружена ссылка на {service_detected}! Начинаю обработку...")
            
            try:
                # Попытка загрузки через yt-dlp для общих видео сервисов
                if "youtube.com" in message and "music.yandex.ru" not in message:
                    downloaded_file = await download_youtube_video(message)
                elif "tiktok.com" in message:
                    downloaded_file = await download_tiktok_video_with_proxy(message)
                elif "reddit.com" in message:
                    downloaded_file = await download_reddit_video(message)
                elif "vk.com" in message or "vk.ru" in message or "vkvideo.ru" in message:
                    vk_username = config["VK"].get("username")
                    vk_password = config["VK"].get("password")
                    if not vk_username or not vk_password:
                        await status_message.edit_text("Для скачивания из ВКонтакте необходимы логин и пароль в config.json.")
                        return
                    downloaded_file = await download_vk_video(message, vk_username, vk_password)
                
                if not downloaded_file: # Если yt-dlp не смог скачать
                    logger.warning(f"yt-dlp не смог скачать файл с {service_detected} ({message}). Попытка прямой загрузки...")
                    await status_message.edit_text(f"Не удалось скачать видео с {service_detected} напрямую. Пытаюсь загрузить как обычный файл...")
                    downloaded_file = await download_raw_file(message)

            except yt_dlp.utils.DownloadError as e:
                error_message_str = str(e).lower()
                if "unsupported url" in error_message_str or "no media found" in error_message_str:
                    logger.warning(f"yt-dlp выдал 'Unsupported URL' или 'No media found' для '{message}'. Попытка прямой загрузки...")
                    await status_message.edit_text("Произошла ошибка при обработке медиа. Пытаюсь загрузить файл напрямую...")
                    downloaded_file = await download_raw_file(message)
                else:
                    raise # Перебрасываем другие ошибки yt-dlp

            # ОБЩИЙ БЛОК ОТПРАВКИ ФАЙЛА ПОСЛЕ ЛЮБОЙ УСПЕШНОЙ ЗАГРУЗКИ (yt-dlp или прямой)
            if downloaded_file and os.path.exists(downloaded_file):
                await status_message.edit_text(f"Файл загружен! Отправляю...")
                file_ext = os.path.splitext(downloaded_file)[1].lower()
                
                if file_ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_photo(chat_id=update.message.chat_id, photo=f)
                    file_id = sent_message.photo[-1].file_id # Telegram возвращает список размеров, берем последний
                    file_type_to_cache = "photo"
                elif file_ext in ['.mp4', '.webm', '.gif'] and await asyncio.to_thread(is_gif_like, downloaded_file):
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_animation(chat_id=update.message.chat_id, animation=f)
                    file_id = sent_message.animation.file_id
                    file_type_to_cache = "animation"
                elif file_ext in ['.mp3', '.ogg', '.flac']:
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_audio(chat_id=update.message.chat_id, audio=f)
                    file_id = sent_message.audio.file_id
                    file_type_to_cache = "audio"
                else: # Для всех остальных случаев или неизвестных типов, отправляем как видео/документ
                    with open(downloaded_file, 'rb') as f:
                        sent_message = await context.bot.send_video(chat_id=update.message.chat_id, video=f)
                    file_id = sent_message.video.file_id
                    file_type_to_cache = "video" # По умолчанию
                
                # Сохраняем в кэш только если успешно получили file_id
                if file_id:
                    cache_data[message] = file_id
                    cache_data[f"{message}_type"] = file_type_to_cache # ДОБАВЛЕНО: тип файла для кэша
                    cache_data[f"{message}_caption"] = caption_text # Если есть, сохраняем подпись
                    save_cache(cache_data)
                
                await status_message.delete()
                # Файл удалится в finally
                return # Выходим после успешной отправки

            else:
                # Если дошли до сюда, значит ни yt-dlp, ни прямая загрузка не сработали
                if status_message and status_message.text:
                    await status_message.edit_text(f"Не удалось скачать файл. Ни один метод не сработал. (Ссылка на {service_detected})")
                else:
                    await update.message.reply_text("Не удалось скачать файл. Неизвестная ошибка.")

    except FileSizeExceededError:
        logger.warning(f"Файл по ссылке '{message}' слишком большой.")
        if status_message:
            await status_message.edit_text("Файл слишком большой. Максимальный размер 50МБ.")
        else:
            await update.message.reply_text("Файл слишком большой. Максимальный размер 50МБ.")
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке сообщения: {e}", exc_info=True)
        if status_message:
            await status_message.edit_text(f"Произошла критическая ошибка: {e}")
        else:
            await update.message.reply_text(f"Произошла критическая ошибка: {e}")
    finally:
        if downloaded_file and isinstance(downloaded_file, str) and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except OSError as e:
                logger.warning(f"Не удалось удалить временный файл {downloaded_file}: {e}")


async def handle_inline_query(update: Update, context):
    query = update.inline_query.query.strip()
    logger.info(f"Получен инлайн-запрос: {query}")

    if not query:
        logger.info("Инлайн-запрос пустой, игнорируем")
        await update.inline_query.answer([], cache_time=1)
        return

    if "/album/" in query or "/playlist/" in query:
        # В инлайн-режиме нельзя отправлять сообщения пользователю, кроме результатов
        # Можно отправить пустое, но лучше уведомить в личку.
        await context.bot.send_message(
            chat_id=update.inline_query.from_user.id,
            text="Обработка альбомов и плейлистов в инлайн-режиме пока не поддерживается. Пожалуйста, отправьте ссылку на альбом/плейлист напрямую в чат с ботом."
        )
        await update.inline_query.answer([], cache_time=1)
        return

    if not any(service in query for service in SUPPORTED_SERVICES):
        logger.info("Неподдерживаемый сервис в инлайн-запросе")
        # Вместо пустого ответа, можно попробовать прямую загрузку, если это прямая ссылка
        # Но для инлайн-режима лучше быть более консервативным, чтобы не вызывать задержки
        # Оставим пока так, как было, или попробуем прямую загрузку, если это не SUPPORTED_SERVICES,
        # но при этом это похоже на прямую ссылку.
        # Для простоты пока оставим этот блок без прямой загрузки, чтобы не загромождать.
        await update.inline_query.answer([], cache_time=1)
        return

    cache_data = load_cache()

    if query in cache_data:
        file_id = cache_data[query]
        logger.info(f"Ссылка найдена в кэше для {query}, отправляем кэшированный файл.")
        cached_file_type = cache_data.get(f"{query}_type")
        caption_text = cache_data.get(f"{query}_caption", "")

        result = None
        if cached_file_type == "audio":
            result = InlineQueryResultCachedAudio(id=uuid.uuid4().hex, audio_file_id=file_id, caption=caption_text)
        elif cached_file_type == "animation":
            result = InlineQueryResultCachedMpeg4Gif(id=uuid.uuid4().hex, mpeg4_gif_file_id=file_id, title="Cached GIF", caption=caption_text)
        elif cached_file_type == "photo":
            result = InlineQueryResultCachedPhoto(id=uuid.uuid4().hex, photo_file_id=file_id, title="Cached Image", caption=caption_text)
        else: # video
            result = InlineQueryResultCachedVideo(id=uuid.uuid4().hex, video_file_id=file_id, title="Cached Video", caption=caption_text)
        
        await update.inline_query.answer([result], cache_time=1)
        return

    # --- ОБНОВЛЕННЫЙ БЛОК ОБРАБОТКИ ССЫЛОК ДЛЯ ИНЛАЙН-РЕЖИМА ---
    downloaded_file = None
    file_id = None
    caption_text = ""
    file_type_to_cache = "video"

    try:
        if "music.youtube.com" in query:
            logger.info("Обнаружена ссылка на YouTube Music в инлайн-запросе")
            downloaded_file, title, artist = await download_youtube_music_audio(query)
            if downloaded_file and os.path.exists(downloaded_file):
                caption_text = f"{artist} - {title}"
                file_type_to_cache = "audio"
                # В инлайн-режиме нужно сначала отправить файл в личный чат, чтобы получить file_id
                sent_message = await context.bot.send_audio(
                    chat_id=update.inline_query.from_user.id, audio=open(downloaded_file, 'rb'), title=title, performer=artist
                )
                file_id = sent_message.audio.file_id
            else:
                logger.error("Не удалось скачать аудио с YouTube Music")

        elif "music.yandex.ru/track/" in query:
            logger.info("Обнаружена ссылка на Яндекс.Музыку (трек) в инлайн-запросе")
            title, artist = await asyncio.to_thread(get_track_info, query)
            if title and artist:
                logger.info(f"Трек: {title}, Исполнитель: {artist}. Ищем на YouTube...")
                downloaded_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                if downloaded_file:
                    mp3_file = await asyncio.to_thread(convert_to_mp3, downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        downloaded_file = mp3_file # Обновляем для удаления в finally
                        caption_text = f"{artist} - {title}"
                        file_type_to_cache = "audio"
                        sent_message = await context.bot.send_audio(
                            chat_id=update.inline_query.from_user.id, audio=open(downloaded_file, 'rb'), title=title, performer=artist
                        )
                        file_id = sent_message.audio.file_id
                    else: logger.error("Не удалось конвертировать трек в MP3")
                else: logger.error("Не удалось скачать трек с YouTube")
            else: logger.error("Не удалось получить информацию о треке с Яндекс.Музыки")

        elif "open.spotify.com" in query:
            logger.info("Обнаружена ссылка на Spotify в инлайн-запросе")
            title, artist = await asyncio.to_thread(get_track_info_with_proxy, query)
            if title and artist:
                logger.info(f"Трек: {title}, Исполнитель: {artist}. Ищем на YouTube...")
                downloaded_file = await asyncio.to_thread(search_and_download_from_youtube, title, artist)
                if downloaded_file:
                    mp3_file = await asyncio.to_thread(convert_to_mp3, downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        downloaded_file = mp3_file # Обновляем для удаления в finally
                        caption_text = f"{artist} - {title}"
                        file_type_to_cache = "audio"
                        sent_message = await context.bot.send_audio(
                            chat_id=update.inline_query.from_user.id, audio=open(downloaded_file, 'rb'), title=title, performer=artist
                        )
                        file_id = sent_message.audio.file_id
                    else: logger.error("Не удалось конвертировать трек в MP3")
                else: logger.error("Не удалось скачать трек с YouTube")
            else: logger.error("Не удалось получить информацию о треке с Spotify")

        # Универсальная обработка видео (и изображений/гифок) для инлайн-режима
        else:
            logger.info(f"Обнаружена ссылка на видео/изображение в инлайн-запросе: {query}")
            
            # Попытка загрузки через yt-dlp для основных видео сервисов
            temp_downloaded_file = None
            try:
                if "instagram.com" in query:
                    temp_downloaded_file, status = await managed_instagram_download(query)
                    if status != "SUCCESS":
                        logger.warning(f"Instagram download via accounts failed for {query} with status {status}. Attempting direct download.")
                        temp_downloaded_file = await download_raw_file(query)
                elif "youtube.com" in query or "youtu.be" in query:
                    temp_downloaded_file = await download_youtube_video(query)
                elif "tiktok.com" in query:
                    temp_downloaded_file = await download_tiktok_video_with_proxy(query)
                elif "reddit.com" in query:
                    temp_downloaded_file = await download_reddit_video(query)
                elif "vk.com" in query or "vk.ru" in query or "vkvideo.ru" in query:
                    vk_username = config["VK"].get("username")
                    vk_password = config["VK"].get("password")
                    if not vk_username or not vk_password:
                        await context.bot.send_message(
                            chat_id=update.inline_query.from_user.id,
                            text="Для скачивания из ВКонтакте необходимы логин и пароль в config.json."
                        )
                        await update.inline_query.answer([], cache_time=1)
                        return
                    temp_downloaded_file = await download_vk_video(query, vk_username, vk_password)

                if not temp_downloaded_file: # Если yt-dlp не смог скачать
                    logger.warning(f"yt-dlp не смог скачать файл для {query}. Попытка прямой загрузки...")
                    temp_downloaded_file = await download_raw_file(query)

            except FileSizeExceededError:
                raise # Позволяем внешнему блоку try/except поймать это

            except yt_dlp.utils.DownloadError as e:
                error_message_str = str(e).lower()
                if "unsupported url" in error_message_str or "no media found" in error_message_str:
                    logger.warning(f"yt-dlp выдал 'Unsupported URL' или 'No media found' для '{query}'. Попытка прямой загрузки...")
                    temp_downloaded_file = await download_raw_file(query)
                else:
                    raise # Перебрасываем другие ошибки yt-dlp

            downloaded_file = temp_downloaded_file # Присваиваем для общей обработки

        # ОБЩИЙ БЛОК ОТПРАВКИ ФАЙЛА ПОСЛЕ ЛЮБОЙ УСПЕШНОЙ ЗАГРУЗКИ (yt-dlp или прямой)
        results = []
        if downloaded_file and os.path.exists(downloaded_file):
            logger.info(f"Файл загружен для инлайн-режима: {downloaded_file}")
            file_ext = os.path.splitext(downloaded_file)[1].lower()
            
            sent_message = None
            if file_ext in ['.jpg', '.jpeg', '.png', '.webp']:
                # Для inline_query_answer, Telegram требует file_id.
                # Сначала отправляем файл в личный чат пользователя, чтобы получить file_id.
                sent_message = await context.bot.send_photo(chat_id=update.inline_query.from_user.id, photo=open(downloaded_file, 'rb'))
                file_id = sent_message.photo[-1].file_id
                results.append(InlineQueryResultCachedPhoto(id=uuid.uuid4().hex, photo_file_id=file_id, title="Скачанное изображение"))
                file_type_to_cache = "photo"
            elif file_ext in ['.mp4', '.webm', '.gif'] and await asyncio.to_thread(is_gif_like, downloaded_file):
                sent_message = await context.bot.send_animation(chat_id=update.inline_query.from_user.id, animation=open(downloaded_file, 'rb'))
                file_id = sent_message.animation.file_id
                results.append(InlineQueryResultCachedMpeg4Gif(id=uuid.uuid4().hex, mpeg4_gif_file_id=file_id, title="Скачанная GIF"))
                file_type_to_cache = "animation"
            elif file_ext in ['.mp3', '.ogg', '.flac']:
                sent_message = await context.bot.send_audio(chat_id=update.inline_query.from_user.id, audio=open(downloaded_file, 'rb'))
                file_id = sent_message.audio.file_id
                results.append(InlineQueryResultCachedAudio(id=uuid.uuid4().hex, audio_file_id=file_id, title="Скачанное аудио"))
                file_type_to_cache = "audio"
            else: # Все остальные видео, или неизвестные как документ
                sent_message = await context.bot.send_video(chat_id=update.inline_query.from_user.id, video=open(downloaded_file, 'rb'))
                file_id = sent_message.video.file_id
                results.append(InlineQueryResultCachedVideo(id=uuid.uuid4().hex, video_file_id=file_id, title="Скачанное видео"))
                file_type_to_cache = "video"

            if file_id: # Сохраняем в кэш
                cache_data[query] = file_id
                cache_data[f"{query}_type"] = file_type_to_cache
                cache_data[f"{query}_caption"] = caption_text # Если есть
                save_cache(cache_data)
            
            await update.inline_query.answer(results, cache_time=1)
            # Файл удалится в finally
            return
        else:
            logger.error(f"Медиа не удалось скачать ни одним из методов для инлайн-запроса: {query}")
            # Отправляем сообщение в личный чат, так как в инлайн-режиме не можем показать ошибку
            await context.bot.send_message(chat_id=update.inline_query.from_user.id, text="Не удалось скачать файл для вашего инлайн-запроса.")
            await update.inline_query.answer([], cache_time=1) # Отвечаем пустым списком результатов

    except FileSizeExceededError:
        logger.warning(f"Файл по ссылке '{query}' слишком большой (инлайн-запрос).")
        await context.bot.send_message(
            chat_id=update.inline_query.from_user.id,
            text="Файл слишком большой. Максимальный размер 50МБ. Пожалуйста, попробуйте другую ссылку."
        )
        await update.inline_query.answer([], cache_time=1)
    except Exception as e:
        logger.error(f"Ошибка при обработке инлайн-запроса: {e}", exc_info=True)
        await context.bot.send_message(chat_id=update.inline_query.from_user.id, text=f"Произошла ошибка при обработке вашего инлайн-запроса: {e}")
        await update.inline_query.answer([], cache_time=1)
    finally:
        if downloaded_file and isinstance(downloaded_file, str) and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except OSError as e:
                logger.warning(f"Не удалось удалить временный файл {downloaded_file}: {e}")


async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    initialize_instagram_accounts()

    threading.Thread(target=cleanup_folder, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_error_handler(error_handler)

    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()