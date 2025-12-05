#!/bin/bash

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Получаем абсолютный путь к текущей папке проекта
PROJECT_DIR=$(pwd)
USERNAME=$(whoami)

echo -e "${GREEN}🤖 Начинаю установку MediaKit Bot...${NC}"

# 1. Проверка прав (нужен root для apt и systemd)
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}Пожалуйста, запустите скрипт от имени root (sudo).${NC}"
  exit 1
fi

# 2. Установка системных зависимостей
echo -e "${YELLOW}📦 Установка системных пакетов (ffmpeg, aria2, git, python3-venv)...${NC}"
apt-get update -qq
apt-get install -y ffmpeg aria2 git python3-venv python3-pip uuid-runtime

# 3. Настройка Python окружения
echo -e "${YELLOW}🐍 Настройка виртуального окружения...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Virtual environment создан."
else
    echo "Virtual environment уже существует."
fi

# Активация не обязательна для скрипта, мы будем обращаться по полному пути,
# но активируем для pip install
source venv/bin/activate

# Создаем requirements.txt если его нет
if [ ! -f "requirements.txt" ]; then
    echo -e "${YELLOW}📄 Создаю requirements.txt...${NC}"
    cat <<EOF > requirements.txt
python-telegram-bot
asyncpraw
yt-dlp
requests
boto3
aiohttp
asyncpg
psycopg2-binary
EOF
fi

echo -e "${YELLOW}⬇️ Установка Python-зависимостей...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

# 4. Создание структуры папок и конфигов
echo -e "${YELLOW}⚙️ Настройка конфигурации...${NC}"
mkdir -p important

# Генерация config.json
CONFIG_PATH="important/config.json"
if [ ! -f "$CONFIG_PATH" ]; then
    echo "Создаю шаблон $CONFIG_PATH..."
    cat <<EOF > $CONFIG_PATH
{
  "BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
  "ADMIN_ID": 000000000,
  "DATABASE": {
    "USER": "db_user",
    "PASSWORD": "db_password",
    "HOST": "localhost",
    "PORT": "5432",
    "DB_NAME": "MediaKit"
  },
  "YANDEX_SPEECHKIT": {
    "API_KEY": "YOUR_API_KEY",
    "FOLDER_ID": "YOUR_FOLDER_ID",
    "S3_BUCKET_NAME": "bucket-name",
    "S3_ACCESS_KEY_ID": "aws_key_id",
    "S3_SECRET_ACCESS_KEY": "aws_secret_key"
  },
  "YANDEX_GPT": {
    "API_KEY": "YOUR_API_KEY",
    "FOLDER_ID": "YOUR_FOLDER_ID",
    "MODEL_URI": "gpt://YOUR_FOLDER_ID/yandexgpt/rc",
    "SYSTEM_PROMPT": "Ты — помощник, который кратко излагает суть."
  },
  "REDDIT": {
    "client_id": "YOUR_ID",
    "client_secret": "YOUR_SECRET",
    "user_agent": "MediaBot/1.0",
    "proxy": null
  },
  "PROXIES": {
    "yandex": null,
    "tiktok": null,
    "youtube": null
  },
  "HEADERS": {
    "yandex_auth": "Bearer YOUR_TOKEN"
  },
  "COOKIES": {
    "youtube": "important/www.youtube.com_cookies.txt",
    "reddit": "important/www.reddit.com_cookies.txt",
    "tiktok": "important/www.tiktok.com_cookies.txt"
  },
  "VK": {
    "username": "",
    "password": ""
  },
  "EXCLUDED_CHATS": []
}
EOF
else
    echo "⚠️  $CONFIG_PATH уже существует, пропускаю."
fi

# Генерация download_instagram.sh
INSTA_SCRIPT="download_instagram.sh"
echo "Обновляю скрипт $INSTA_SCRIPT..."

cat <<'EOF' > $INSTA_SCRIPT
#!/bin/bash
set -e

# --- НАСТРОЙКИ (ИЗМЕНИТЕ ЭТО) ---
PROXY_STRING="YOUR_PROXY_HERE"
COOKIE_FILE="$(dirname "$0")/important/www.instagram.com_cookies.txt"

# --- ПАРАМЕТРЫ ---
VIDEO_URL="$1"
FINAL_OUTPUT_FILE="$2"

if [ -z "$VIDEO_URL" ] || [ -z "$FINAL_OUTPUT_FILE" ]; then
  echo "Ошибка: Не передан URL или имя файла." >&2
  exit 1
fi

if [ "$PROXY_STRING" == "YOUR_PROXY_HERE" ]; then
   echo "Ошибка: ПРОКСИ НЕ НАСТРОЕНЫ в download_instagram.sh" >&2
   exit 1
fi

VIDEO_PART="temp_video_$(uuidgen).mp4"
AUDIO_PART="temp_audio_$(uuidgen).m4a"
# Важно: используем абсолютный путь к python/yt-dlp внутри venv, вычисляя его от расположения скрипта
VENV_PYTHON="$(dirname "$0")/venv/bin/yt-dlp"

# ЭТАП 1: Получение ссылок
URLS=$($VENV_PYTHON --get-url --proxy "$PROXY_STRING" --cookies "$COOKIE_FILE" "$VIDEO_URL")
VIDEO_DL_URL=$(echo "$URLS" | head -n 1)
AUDIO_DL_URL=$(echo "$URLS" | tail -n 1)

# ЭТАП 2: Скачивание (aria2c)
/usr/bin/aria2c --no-conf -x4 -s4 --http-proxy='' --https-proxy='' -o "$VIDEO_PART" "$VIDEO_DL_URL"
/usr/bin/aria2c --no-conf -x4 -s4 --http-proxy='' --https-proxy='' -o "$AUDIO_PART" "$AUDIO_DL_URL"

# ЭТАП 3: Склейка
ffmpeg -y -v quiet -i "$VIDEO_PART" -i "$AUDIO_PART" -c copy "$FINAL_OUTPUT_FILE"

# ЭТАП 4: Очистка
rm -f "$VIDEO_PART" "$AUDIO_PART"
EOF

# Права доступа
chmod +x setup.sh
chmod +x download_instagram.sh

# 5. Настройка Systemd Service (Демон)
echo -e "${YELLOW}😈 Настройка Systemd демона (mediakit.service)...${NC}"

SERVICE_FILE="/etc/systemd/system/mediakit.service"

# Генерируем сервис-файл
cat <<EOF > $SERVICE_FILE
[Unit]
Description=MediaKit Telegram Bot Service
After=network.target postgresql.service

[Service]
Type=simple
User=$USERNAME
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/MediaKit.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "Создан файл службы: $SERVICE_FILE"

# Перезагружаем демоны и включаем автозагрузку
systemctl daemon-reload
systemctl enable mediakit.service

echo -e "${GREEN}✅ Установка завершена! Сервис зарегистрирован.${NC}"
echo -e "---------------------------------------------------"
echo -e "⏭  ${YELLOW}ДАЛЬНЕЙШИЕ ДЕЙСТВИЯ:${NC}"
echo -e "1. ${GREEN}nano important/config.json${NC} (Вставьте токены)"
echo -e "2. ${GREEN}nano download_instagram.sh${NC} (Вставьте PROXY)"
echo -e "3. Загрузите файлы куки."
echo -e "4. Запустите бота командой: ${GREEN}systemctl start mediakit.service${NC}"
echo -e "5. Проверка статуса: ${GREEN}systemctl status mediakit.service${NC}"
echo -e "6. Чтение логов: ${GREEN}journalctl -u mediakit.service -f${NC}"
echo -e "---------------------------------------------------"
