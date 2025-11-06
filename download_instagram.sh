#!/bin/bash
# Немедленно выходить, если любая команда завершится с ошибкой
set -e

# --- "ЗАШИТЫЕ" ПАРАМЕТРЫ (не меняются) ---
PROXY_STRING="http://MynameoB7:5a11afzI2@51.77.79.248:10281"
COOKIE_FILE="/root/MediaKit/important/www.instagram.com_cookies.txt"

# --- ДИНАМИЧЕСКИЕ ПАРАМЕТРЫ (приходят из Python) ---
VIDEO_URL="$1"
FINAL_OUTPUT_FILE="$2"

# --- Проверка, что Python передал все данные ---
if [ -z "$VIDEO_URL" ] || [ -z "$FINAL_OUTPUT_FILE" ]; then
  echo "Критическая ошибка: Python-скрипт не передал URL или имя файла." >&2
  exit 1
fi

# --- Временные файлы для видео и аудио ---
VIDEO_PART="temp_video_$(uuidgen).mp4"
AUDIO_PART="temp_audio_$(uuidgen).m4a"


# --- ЭТАП 1: Получаем ссылки через прокси ---
echo "Этап 1: Получаю ссылки через прокси..."
URLS=$(/root/venv/bin/yt-dlp \
  --get-url \
  --proxy "$PROXY_STRING" \
  --cookies "$COOKIE_FILE" \
  "$VIDEO_URL")

VIDEO_DL_URL=$(echo "$URLS" | head -n 1)
AUDIO_DL_URL=$(echo "$URLS" | tail -n 1)


# --- ЭТАП 2: Качаем части напрямую (без прокси) ---
echo "Этап 2: Скачиваю части напрямую..."
/usr/bin/aria2c --no-conf -x1 --http-proxy='' --https-proxy='' -o "$VIDEO_PART" "$VIDEO_DL_URL"
/usr/bin/aria2c --no-conf -x1 --http-proxy='' --https-proxy='' -o "$AUDIO_PART" "$AUDIO_DL_URL"


# --- ЭТАП 3: Склеиваем видео и аудио ---
echo "Этап 3: Собираю финальный файл..."
ffmpeg -y -v quiet -i "$VIDEO_PART" -i "$AUDIO_PART" -c copy "$FINAL_OUTPUT_FILE"


# --- ЭТАП 4: Очистка ---
rm "$VIDEO_PART" "$AUDIO_PART"

echo "Готово! Файл сохранен как $FINAL_OUTPUT_FILE"