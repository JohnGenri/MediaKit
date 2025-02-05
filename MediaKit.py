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
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from telegram import Update, InlineQueryResultCachedVideo, InlineQueryResultCachedAudio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, InlineQueryHandler, filters
from playwright.async_api import async_playwright

# Пути к важным файлам
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # Папка MediaKit
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')    # Папка important
CACHE_FILE = os.path.join(IMPORTANT_DIR, 'cache.json')         # Кэш
SENT_FILE = os.path.join(IMPORTANT_DIR, 'sent_videos.json')    # Список отправленных
COOKIES_YOUTUBE = os.path.join(IMPORTANT_DIR, 'www.youtube.com_cookies.txt')  # Куки YouTube
COOKIES_REDDIT = os.path.join(IMPORTANT_DIR, 'www.reddit.com_cookies.txt')    # Куки Reddit
COOKIES_INSTAGRAM = os.path.join(IMPORTANT_DIR, 'www.reddit.com_cookies.txt')    # Куки Reddit
INSTAGRAM_FOLDER = os.path.join(IMPORTANT_DIR, 'instagram_video')  # Папка Инстаграм
CACHE = CACHE_FILE
SENT = SENT_FILE
COOKIES_YOUTUBE_PATH = COOKIES_YOUTUBE
COOKIES_REDDIT_PATH = COOKIES_REDDIT


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING
)
logger = logging.getLogger(__name__)

# Загрузка конфигурации
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH, 'r') as config_file:
    config = json.load(config_file)

BOT_TOKEN = config["BOT_TOKEN"]

reddit = asyncpraw.Reddit(
    client_id=config["REDDIT"]["client_id"],
    client_secret=config["REDDIT"]["client_secret"],
    user_agent=config["REDDIT"]["user_agent"]
)

YANDEX_PROXIES = config["PROXIES"]["yandex"]
SPOTIFY_PROXIES = config["PROXIES"]["spotify"]
TIKTOK_PROXIES = config["PROXIES"]["tiktok"]

YANDEX_HEADERS = {
    "Authorization": config["HEADERS"]["yandex_auth"]
}

COOKIES_YOUTUBE_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"]["youtube"])
COOKIES_REDDIT_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"]["reddit"])
COOKIES_INSTAGRAM_PATH = os.path.join(os.path.dirname(__file__), config["COOKIES"]["instagram"])



SUPPORTED_SERVICES = [
    "music.youtube.com",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "reddit.com",
    "vk.com",
    "vk.ru",
    "music.yandex.ru",
    "open.spotify.com"
]

def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, 'r') as f:
            return json.load(f)
    else:
        return {}

def save_cache(cache_data):
    with open(CACHE, 'w') as f:
        json.dump(cache_data, f)

def cleanup_folder(interval=200, target_extensions=('.mp3', '.mp4', '.avi', '.part', '.webm')):
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


#Ссылки на все сервисы музыки
def generate_service_links(url):
    try:
        # Определение сервиса и получение метаданных
        if "music.yandex.ru" in url:
            logger.info("Обработка ссылки Яндекс.Музыка")
            title, artist = get_track_info(url)
        elif "open.spotify.com" in url:
            logger.info("Обработка ссылки Spotify")
            title, artist = get_track_info_with_proxy(url)
        elif "music.youtube.com" in url:
            logger.info("Обработка ссылки YouTube Music")
            title, artist = download_youtube_music_audio(url)[1:3]
        else:
            raise ValueError("Ссылка не относится к поддерживаемым сервисам.")

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


def download_tiktok_video_with_proxy(url):
    proxy = config["PROXIES"]["tiktok"]
    unique_filename = f"tiktok_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = {
        'proxy': proxy,
        'format': 'best',
        'outtmpl': unique_filename,
        'quiet': True,
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return unique_filename
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка загрузки видео с TikTok: {e}")
        return None

def clean_youtube_music_url(url):
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)
    query_params.pop('si', None)
    netloc = parsed_url.netloc.replace('music.youtube.com', 'www.youtube.com')
    new_query = urlencode(query_params, doseq=True)
    cleaned_url = urlunparse((
        parsed_url.scheme,
        netloc,
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
            'logger': logger,
            'nocheckcertificate': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(clean_url, download=True)
            title = info_dict.get('title', 'Unknown Title')
            artist = info_dict.get('artist') or info_dict.get('uploader', 'Unknown Artist')
            logger.info(f"Название трека: {title}, Исполнитель: {artist}")

        audio_filename = base_filename + ".mp3"
        if os.path.exists(audio_filename):
            if os.path.exists(base_filename):
                os.remove(base_filename)
            return audio_filename, title, artist
        else:
            logger.error(f"Файл {audio_filename} не найден после загрузки.")
            return None, None, None

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
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(video_filename):
            logger.info(f"Видео успешно загружено: {video_filename}")
            return video_filename
        else:
            return None

    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки YouTube: {e}")
        return None


async def download_instagram_video(url):
    try:
        logger.info(f"Обработка ссылки Instagram: {url}")
        os.makedirs(INSTAGRAM_FOLDER, exist_ok=True)
        video_filename = os.path.join(INSTAGRAM_FOLDER, f"instagram_video_{uuid.uuid4().hex}.mp4")

        ydl_opts = {
            'format': 'best',
			'cookiefile': COOKIES_INSTAGRAM_PATH,
            'outtmpl': video_filename,
            'quiet': True,
            'max_filesize': 50 * 1024 * 1024,  # Ограничение размера файла
            'nocheckcertificate': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        if os.path.exists(video_filename):
            logger.info(f"Видео успешно загружено: {video_filename}")
            return video_filename
        else:
            return None

    except Exception as e:
        logger.error(f"Ошибка загрузки видео с Instagram: {e}")
        return None


async def download_reddit_video(url):
    try:
        logger.info(f"Обработка ссылки Reddit: {url}")

        video_filename = f"reddit_video_{uuid.uuid4().hex}.mp4"
        audio_filename = f"reddit_audio_{uuid.uuid4().hex}.m4a"
        combined_filename = f"reddit_combined_{uuid.uuid4().hex}.mp4"

        ydl_opts_video = {
            'format': 'bestvideo',
            'cookiefile': COOKIES_REDDIT_PATH,
            'outtmpl': video_filename,
            'quiet': True,
        }

        ydl_opts_audio = {
            'format': 'bestaudio',
            'cookiefile': COOKIES_REDDIT_PATH,
            'outtmpl': audio_filename,
            'quiet': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts_video) as ydl:
            ydl.download([url])

        with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
            ydl.download([url])

        ffmpeg_command = [
            "ffmpeg",
            "-i", video_filename,
            "-i", audio_filename,
            "-c:v", "copy",
            "-c:a", "aac",
            "-loglevel", "quiet",
            combined_filename
        ]
        subprocess.run(ffmpeg_command, check=True)

        os.remove(video_filename)
        os.remove(audio_filename)

        return combined_filename
    except Exception as e:
        logger.error(f"Ошибка при обработке ссылки Reddit: {e}")
        return None

def generate_random_filename(extension):
    random_name = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"{random_name}.{extension}"


def get_track_info(yandex_url):
    try:
        # Используем прокси из config.json
        proxies = {
            'http': config["PROXIES"]["yandex"],
            'https': config["PROXIES"]["yandex"]
        }
        track_id = yandex_url.split('/')[-1].split('?')[0]
        api_url = f"https://api.music.yandex.net/tracks/{track_id}"
        
        # Используем заголовок авторизации из config.json
        headers = {
            'Authorization': config["HEADERS"]["yandex_auth"]
        }

        response = requests.get(api_url, headers=headers, proxies=proxies)
        response.raise_for_status()
        data = response.json()
        track = data['result'][0]
        title = track['title']
        artist = track['artists'][0]['name']
        logger.info(f"Название трека: {title}, Исполнитель: {artist}")
        return title, artist

    except Exception as e:
        logger.error(f"Ошибка при получении информации о треке: {e}")
        return None, None

def get_track_info_with_proxy(spotify_url):
    proxy = {
        "http": "socks5h://127.0.0.1:9050",
        "https": "socks5h://127.0.0.1:9050"
    }
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(spotify_url, headers=headers, proxies=proxy, timeout=10)
    if response.status_code != 200:
        raise Exception(f"Ошибка доступа к странице Spotify: {response.status_code}")

    title_match = re.search(r'<meta property="og:title" content="(.*?)"', response.text)
    description_match = re.search(r'<meta property="og:description" content="(.*?)"', response.text)

    if not title_match or not description_match:
        raise Exception("Не удалось найти данные о треке.")

    track_name = title_match.group(1)
    artist_name = description_match.group(1)
    logger.info(f"Название трека: {track_name}, Исполнитель: {artist_name}")
    return track_name, artist_name

def search_and_download_from_youtube(title, artist):
    try:
        search_query = f"ytsearch1:{title} {artist}"
        random_filename = generate_random_filename("webm")
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'outtmpl': random_filename,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(search_query, download=True)
            logger.info(f"Трек скачан: {random_filename}")
            return random_filename

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

def download_vk_video(url, username, password):
    unique_filename = f"vk_video_{uuid.uuid4().hex}.mp4"
    ydl_opts = {
        'format': 'best',
        'outtmpl': unique_filename,
        'quiet': True,
        'username': username,
        'password': password,
        'max_filesize': 50 * 1024 * 1024
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return unique_filename
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Ошибка загрузки видео с ВКонтакте: {e}")
        return None


# Основная обработка сообщений
async def handle_message(update: Update, context):
    if not update.message or not update.message.text:
        logger.warning("Получено обновление без текстового сообщения. Пропускаем.")
        return

 ##   logger.info(
 ##       f"Получено сообщение: {update.message.text} от {update.effective_user.id}"
 ##   )
    message = update.message.text.strip()

    # Загрузка кэша
    cache_data = load_cache()

    # Проверка наличия ссылки в кэше
    if message in cache_data:
        file_id = cache_data[message]
        logger.info(f"Ссылка найдена в кэше для {message}, отправляем кэшированный файл.")
        if "music.yandex.ru" in message or "open.spotify.com" in message:
            await context.bot.send_audio(chat_id=update.message.chat_id, audio=file_id)
        else:
            await context.bot.send_video(chat_id=update.message.chat_id, video=file_id)
        return

    # Обработка ссылок YouTube Music
    if "music.youtube.com" in message:
        status_message = await update.message.reply_text("Обнаружена ссылка на YouTube Music! Обрабатываю...")
        try:
            audio_file, title, artist = await download_youtube_music_audio(message)
            if audio_file and os.path.exists(audio_file):
                await status_message.edit_text("Аудио загружено! Отправляю...")
                with open(audio_file, 'rb') as audio:
                    sent_message = await context.bot.send_audio(
                        chat_id=update.message.chat_id,
                        audio=audio,
                        title=title,
                        performer=artist,
                    )
                # Сохранение в кэш
                file_id = sent_message.audio.file_id
                cache_data[message] = file_id
                cache_data[f"{message}_caption"] = f"{artist} - {title}"
                save_cache(cache_data)
                os.remove(audio_file)
                await status_message.delete()
            else:
                await status_message.edit_text("Не удалось скачать аудио с YouTube Music.")
        except Exception as e:
            logger.error(f"Ошибка при обработке ссылки YouTube Music: {e}")
            await status_message.edit_text(f"Произошла ошибка: {e}")

# Обработка ссылки на Яндекс.Музыку
    elif "music.yandex.ru" in message:
        status_message = await update.message.reply_text(
        "Обнаружена ссылка на Яндекс.Музыку! Обрабатываю..."
        )
        try:
            title, artist = get_track_info(message)
            if title and artist:
                await status_message.edit_text(
                "Получена информация о треке! Ищу его"
                )
                downloaded_file = search_and_download_from_youtube(title, artist)
                if downloaded_file:
                    await status_message.edit_text(
                    "Трек найден и скачан! Конвертирую в MP3..."
                    )
                    mp3_file = convert_to_mp3(downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        formatted_filename = f"{title} - {artist}.mp3".replace("/", "_").replace("\\", "_")
                        os.rename(mp3_file, formatted_filename)
                        await status_message.edit_text(
                        "Трек успешно конвертирован в MP3! Отправляю..."
                        )
                        with open(formatted_filename, "rb") as audio:
                            sent_message = await context.bot.send_audio(
                                chat_id=update.message.chat_id,
                                audio=audio,
                                title=title,
                                performer=artist,
                            )
                    # Сохранение в кэш
                        file_id = sent_message.audio.file_id
                        cache_data[message] = file_id
                        cache_data[f"{message}_caption"] = f"{artist} - {title}"
                        save_cache(cache_data)

                    # Генерация ссылок на другие сервисы
                        links = generate_service_links(message)  # Передаём ссылку из сообщения
                        links_message = (
                        f"Ищу трек на других сервисах:\n"
                        f"🎵 [YouTube Music]({links['youtube']})\n"
                        f"🎧 [Spotify]({links['spotify']})\n"
                        f"🎼 [Яндекс.Музыка]({links['yandex']})"
                        )
                        await context.bot.send_message(
                            chat_id=update.message.chat_id,
                            text=links_message,
                            parse_mode="Markdown",
                        )

                        os.remove(formatted_filename)
                        await status_message.delete()
                    else:
                        await status_message.edit_text(
                        "Не удалось конвертировать трек в MP3."
                        )
                else:
                    await status_message.edit_text(
                    "Не удалось скачать трек с YouTube."
                    )
            else:
                await status_message.edit_text(
                "Не удалось получить информацию о треке с Яндекс.Музыки."
                )
        except Exception as e:
            logger.error(f"Ошибка при обработке ссылки Яндекс.Музыки: {e}")
            await status_message.edit_text(f"Произошла ошибка: {e}")


    # Обработка ссылок Spotify
    elif "open.spotify.com" in message:
        status_message = await update.message.reply_text(
            "Обнаружена ссылка на Spotify! Обрабатываю..."
        )
        try:
            title, artist = get_track_info_with_proxy(message)
            if title and artist:
                await status_message.edit_text(
                    "Получена информация о треке! Ищу"
                )
                downloaded_file = search_and_download_from_youtube(title, artist)
                if downloaded_file:
                    await status_message.edit_text(
                        "Трек найден и скачан! Конвертирую в MP3..."
                    )
                    mp3_file = convert_to_mp3(downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        formatted_filename = f"{title} - {artist}.mp3".replace("/", "_").replace("\\", "_")
                        os.rename(mp3_file, formatted_filename)
                        await status_message.edit_text(
                            "Трек успешно конвертирован в MP3! Отправляю..."
                        )
                        with open(formatted_filename, "rb") as audio:
                            sent_message = await context.bot.send_audio(
                                chat_id=update.message.chat_id,
                                audio=audio,
                                title=title,
                                performer=artist,
                            )
                        # Сохранение в кэш
                        file_id = sent_message.audio.file_id
                        cache_data[message] = file_id
                        cache_data[f"{message}_caption"] = f"{artist} - {title}"
                        save_cache(cache_data)
                        links = generate_service_links(message)  # Передаём ссылку из сообщения
                        links_message = (
                            f"Ищу трек на других сервисах:\n"
                            f"🎵 [YouTube Music]({links['youtube']})\n"
                            f"🎧 [Spotify]({links['spotify']})\n"
                            f"🎼 [Яндекс.Музыка]({links['yandex']})"
                        )
                        await context.bot.send_message(
                            chat_id=update.message.chat_id,
                            text=links_message,
                            parse_mode="Markdown",
                        )
                        os.remove(formatted_filename)
                        await status_message.delete()
                    else:
                        await status_message.edit_text(
                            "Не удалось конвертировать трек в MP3."
                        )
                else:
                    await status_message.edit_text(
                        "Не удалось скачать трек с YouTube."
                    )
            else:
                await status_message.edit_text(
                    "Не удалось получить информацию о треке с Spotify."
                )
        except Exception as e:
            logger.error(f"Ошибка при обработке ссылки Spotify: {e}")
            await status_message.edit_text(f"Произошла ошибка: {e}")

    elif any(service in message for service in SUPPORTED_SERVICES):
        status_message = await update.message.reply_text("Обнаружена ссылка! Начинаю обработку...")
        video_file = None
        service_detected = None

        try:
            if "youtube.com" in message or "youtu.be" in message:
                service_detected = "YouTube"
                await status_message.edit_text(f"Обнаружена ссылка на {service_detected}!")
                video_file = await download_youtube_video(message)
            elif "instagram.com" in message:
                service_detected = "Instagram"
                await status_message.edit_text(f"Обнаружена ссылка на {service_detected}!")
                video_file = await download_instagram_video(message)
            elif "tiktok.com" in message:
                service_detected = "TikTok"
                await status_message.edit_text(f"Обнаружена ссылка на {service_detected}!")
                video_file = download_tiktok_video_with_proxy(message)
            elif "reddit.com" in message:
                service_detected = "Reddit"
                await status_message.edit_text(f"Обнаружена ссылка на {service_detected}!")
                video_file = await download_reddit_video(message)
            elif "vk.com" in message or "vk.ru" in message:
                service_detected = "ВКонтакте"
                await status_message.edit_text(f"Обнаружена ссылка на {service_detected}!")
                video_file = download_vk_video(message, "YOUR_VK_USERNAME", "YOUR_VK_PASSWORD")

            if video_file and os.path.exists(video_file):
                await status_message.edit_text(f"Видео с {service_detected} загружено!...")
                with open(video_file, 'rb') as video:
                    sent_message = await context.bot.send_video(chat_id=update.message.chat_id, video=video)
                # Сохранение в кэш
                file_id = sent_message.video.file_id
                cache_data[message] = file_id
                save_cache(cache_data)
                await status_message.delete()
                os.remove(video_file)
            else:
                await status_message.edit_text(f"Не удалось скачать видео с {service_detected}.")
        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения: {e}")
            await status_message.edit_text(f"Произошла ошибка: {e}")
        finally:
            if video_file and isinstance(video_file, str) and os.path.exists(video_file):
                os.remove(video_file)
    # Бот остается молчаливым для других сообщений

# Обработка инлайн-запросов
async def handle_inline_query(update: Update, context):
    query = update.inline_query.query.strip()
    logger.info(f"Получен инлайн-запрос: {query}")

    if not query:
        logger.info("Инлайн-запрос пустой, игнорируем")
        await update.inline_query.answer([], cache_time=1)
        return

    if not any(service in query for service in SUPPORTED_SERVICES):
        logger.info("Неподдерживаемый сервис в инлайн-запросе")
        await update.inline_query.answer([], cache_time=1)
        return

    # Загрузка кэша
    cache_data = load_cache()

    # Проверка наличия ссылки в кэше
    if query in cache_data:
        file_id = cache_data[query]
        logger.info(f"Ссылка найдена в кэше для {query}, отправляем кэшированный файл.")
        if "music.yandex.ru" in query or "music.youtube.com" in query or "open.spotify.com" in query:
            caption_text = cache_data.get(f"{query}_caption", "")
            result = InlineQueryResultCachedAudio(
                id=uuid.uuid4().hex,
                audio_file_id=file_id,
                caption=caption_text,
            )
        else:
            result = InlineQueryResultCachedVideo(
                id=uuid.uuid4().hex,
                video_file_id=file_id,
                title="Тыкни сюда для отправки"
            )
        await update.inline_query.answer([result], cache_time=1)
        return

    try:
        # Обработка ссылок YouTube Music
        if "music.youtube.com" in query:
            logger.info("Обнаружена ссылка на YouTube Music в инлайн-запросе")
            audio_file, title, artist = await download_youtube_music_audio(query)
            if audio_file and os.path.exists(audio_file):
                logger.info("Аудио загружено и конвертировано в MP3")
                with open(audio_file, 'rb') as audio:
                    sent_message = await context.bot.send_audio(
                        chat_id=update.inline_query.from_user.id,
                        audio=audio,
                        title=title,
                        performer=artist,
                    )
                # Сохранение в кэше
                file_id = sent_message.audio.file_id
                caption_text = f"{artist} - {title}"
                cache_data[query] = file_id
                cache_data[f"{query}_caption"] = caption_text
                save_cache(cache_data)
                unique_id = uuid.uuid4().hex
                result = InlineQueryResultCachedAudio(
                    id=unique_id,
                    audio_file_id=file_id,
                    caption=caption_text,
                )
                os.remove(audio_file)
                await update.inline_query.answer([result], cache_time=1)
            else:
                logger.error("Не удалось скачать аудио с YouTube Music")
                await update.inline_query.answer([], cache_time=1)

        # Обработка ссылок на Яндекс.Музыку
        elif "music.yandex.ru" in query:
            logger.info("Обнаружена ссылка на Яндекс.Музыку в инлайн-запросе")
            title, artist = get_track_info(query)
            if title and artist:
                logger.info(f"Трек: {title}, Исполнитель: {artist}. Ищем на YouTube...")
                downloaded_file = search_and_download_from_youtube(title, artist)
                if downloaded_file:
                    mp3_file = convert_to_mp3(downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        logger.info("Трек найден, скачан и конвертирован в MP3")
                        with open(mp3_file, "rb") as audio:
                            sent_message = await context.bot.send_audio(
                                chat_id=update.inline_query.from_user.id,
                                audio=audio,
                                title=title,
                                performer=artist,
                            )
                        # Сохранение в кэше
                        file_id = sent_message.audio.file_id
                        caption_text = f"{artist} - {title}"
                        cache_data[query] = file_id
                        cache_data[f"{query}_caption"] = caption_text
                        save_cache(cache_data)
                        unique_id = uuid.uuid4().hex
                        result = InlineQueryResultCachedAudio(
                            id=unique_id,
                            audio_file_id=file_id,
                            caption=caption_text,
                        )
                        os.remove(mp3_file)
                        await update.inline_query.answer([result], cache_time=1)
                    else:
                        logger.error("Не удалось конвертировать трек в MP3")
                        await update.inline_query.answer([], cache_time=1)
                else:
                    logger.error("Не удалось скачать трек с YouTube")
                    await update.inline_query.answer([], cache_time=1)
            else:
                logger.error("Не удалось получить информацию о треке с Яндекс.Музыки")
                await update.inline_query.answer([], cache_time=1)

        # Обработка ссылок на Spotify
        elif "open.spotify.com" in query:
            logger.info("Обнаружена ссылка на Spotify в инлайн-запросе")
            title, artist = get_track_info_with_proxy(query)
            if title and artist:
                logger.info(f"Трек: {title}, Исполнитель: {artist}. Ищем на YouTube...")
                downloaded_file = search_and_download_from_youtube(title, artist)
                if downloaded_file:
                    mp3_file = convert_to_mp3(downloaded_file)
                    if mp3_file and os.path.exists(mp3_file):
                        logger.info("Трек найден, скачан и конвертирован")
                        with open(mp3_file, "rb") as audio:
                            sent_message = await context.bot.send_audio(
                                chat_id=update.inline_query.from_user.id,
                                audio=audio,
                                title=title,
                                performer=artist,
                            )
                        # Сохранение в кэше
                        file_id = sent_message.audio.file_id
                        caption_text = f"{artist} - {title}"
                        cache_data[query] = file_id
                        cache_data[f"{query}_caption"] = caption_text
                        save_cache(cache_data)
                        unique_id = uuid.uuid4().hex
                        result = InlineQueryResultCachedAudio(
                            id=unique_id,
                            audio_file_id=file_id,
                            caption=caption_text,
                        )
                        os.remove(mp3_file)
                        await update.inline_query.answer([result], cache_time=1)
                    else:
                        logger.error("Не удалось конвертировать трек в MP3")
                        await update.inline_query.answer([], cache_time=1)
                else:
                    logger.error("Не удалось скачать трек с YouTube")
                    await update.inline_query.answer([], cache_time=1)
            else:
                logger.error("Не удалось получить информацию о треке с Spotify")
                await update.inline_query.answer([], cache_time=1)

        else:
            logger.info("Обрабатываем ссылки других сервисов")
            video_file = None
            if "youtube.com" in query or "youtu.be" in query:
                video_file = await download_youtube_video(query)
            elif "instagram.com" in query:
                video_file = await download_instagram_video(query)
            elif "tiktok.com" in query:
                video_file = download_tiktok_video_with_proxy(query)
            elif "reddit.com" in query:
                video_file = await download_reddit_video(query)
            elif "vk.com" in query or "vk.ru" in query:
                video_file = download_vk_video(query, "YOUR_VK_USERNAME", "YOUR_VK_PASSWORD")

            if video_file and os.path.exists(video_file):
                logger.info(f"Видео скачано: {video_file}")
                with open(video_file, 'rb') as video:
                    sent_message = await context.bot.send_video(
                        chat_id=update.inline_query.from_user.id,
                        video=video,
                        caption="Ваше видео готово!"
                    )
                # Сохранение в кэше
                file_id = sent_message.video.file_id
                cache_data[query] = file_id
                save_cache(cache_data)
                unique_id = uuid.uuid4().hex
                result = InlineQueryResultCachedVideo(
                    id=unique_id,
                    video_file_id=file_id,
                    title="Скачанное видео"
                )
                await update.inline_query.answer([result], cache_time=1)
                os.remove(video_file)
            else:
                logger.error("Видео не удалось скачать")
                await update.inline_query.answer([], cache_time=1)

    except Exception as e:
        logger.error(f"Ошибка при обработке инлайн-запроса: {e}")
        await update.inline_query.answer([], cache_time=1)

# Добавляем обработчик ошибок
async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

# Основная функция запуска бота
def main():
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
