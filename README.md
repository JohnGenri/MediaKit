# Telegram Media Downloader Bot

Простой и мощный Telegram-бот для загрузки аудио и видео из популярных сервисов прямо в чат. Отправьте боту ссылку, и он пришлет вам готовый медиафайл.

## Ключевые Возможности

-   **Поддержка множества сервисов**: Скачивайте видео и музыку с YouTube, Instagram, TikTok и других платформ.
-   **Загрузка альбомов**: Отправьте ссылку на альбом в Яндекс.Музыке, и бот скачает и пришлет все треки по очереди.
-   **Умная обработка Instagram**: Бот использует пул аккаунтов для скачивания из Instagram, автоматически переключаясь между ними при возникновении ошибок. Это повышает стабильность и отказоустойчивость.
-   **Конвертация в GIF**: Короткие видео без звука (до 60 секунд) автоматически отправляются как GIF-анимации.
-   **Кэширование**: Повторные запросы на одну и ту же ссылку обрабатываются мгновенно, отправляя файл из кэша Telegram.
-   **Гибкая настройка**: Все ключи API, прокси и аккаунты настраиваются в одном конфигурационном файле.

## Поддерживаемые Сервисы

| Сервис | Видео | Аудио | Примечания |
| :--- | :---: | :---: | :--- |
| **YouTube** | ✅ | — | |
| **YouTube Music** | — | ✅ | Скачивает аудиодорожку из клипа. |
| **Instagram** | ✅ | — | Использует пул аккаунтов для обхода ограничений. |
| **TikTok** | ✅ | — | |
| **Reddit** | ✅ | — | |
| **VK (ВКонтакте)**| ✅ | — | Требуется логин и пароль в конфиге. |
| **Яндекс.Музыка** | — | ✅ | Ищет и скачивает трек/альбом с YouTube. |
| **Spotify** | — | ✅ | Ищет и скачивает трек с YouTube. |

## Установка и Настройка

### Шаг 1: Системные требования

-   **Python 3.9+**
-   **FFmpeg**: необходим для обработки аудио и видео. Убедитесь, что `ffmpeg` и `ffprobe` доступны из командной строки.

### Шаг 2: Клонирование репозитория

```bash
git clone https://github.com/JohnGenri/MediaKit.git
cd Mediakit
```

### Шаг 3: Установка зависимостей

Рекомендуется создать виртуальное окружение.

```bash
python -m venv venv
source venv/bin/activate  # для Linux/macOS
# venv\Scripts\activate  # для Windows
```

Создайте файл `requirements.txt` со следующим содержимым:

```txt
python-telegram-bot[ext]>=20.0
yt-dlp
asyncpraw
requests
```

И установите зависимости:
```bash
pip install -r requirements.txt
```

### Шаг 4: Настройка конфигурационного файла

Создайте файл `config.json` в корневой папке проекта и заполните его по примеру ниже.

### Шаг 5: Создание папки `important`

В корне проекта создайте папку `important`. В нее нужно будет положить файлы cookie. Эта папка добавлена в `.gitignore`.

---

## Файл Конфигурации (`config.json`)

Это сердце вашего бота. Здесь хранятся все ключи, токены и аккаунты.

**Пример `config.json`:**
```json
{
    "BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "REDDIT": {
        "client_id": "ВАШ_REDDIT_CLIENT_ID",
        "client_secret": "ВАШ_REDDIT_CLIENT_SECRET",
        "user_agent": "MyTelegramBot/1.0"
    },
    "VK": {
        "username": "ВАШ_ЛОГИН_VK",
        "password": "ВАШ_ПАРОЛЬ_VK_ИЛИ_ТОКЕН"
    },
    "PROXIES": {
        "yandex": null,
        "spotify": null,
        "tiktok": "http://user:password@host:port"
    },
    "HEADERS": {
        "yandex_auth": "OAuth ВАШ_ЯНДЕКС_ТОКЕН"
    },
    "COOKIES": {
        "youtube": "important/youtube_cookies.txt",
        "reddit": "important/reddit_cookies.txt"
    },
    "INSTAGRAM_ACCOUNTS": [
        {
            "cookie_file": "instagram_cookies_1.txt",
            "proxy": "http://user1:pass1@host1:port"
        },
        {
            "cookie_file": "instagram_cookies_2.txt",
            "proxy": "http://user2:pass2@host2:port"
        },
        {
            "cookie_file": "instagram_cookies_3.txt",
            "proxy": null
        }
    ]
}
```

**Описание полей:**

-   `BOT_TOKEN`: Токен вашего Telegram-бота, полученный от [@BotFather](https://t.me/BotFather).
-   `REDDIT`: Данные для API Reddit (если нужна загрузка с Reddit).
-   `VK`: Логин и пароль для аккаунта VK. Нужен для скачивания видео, доступных после авторизации.
-   `PROXIES`: Прокси для соответствующих сервисов. Укажите `null`, если прокси не нужен.
-   `HEADERS`: Заголовки для API. `yandex_auth` нужен для получения информации о треках с Яндекс.Музыки.
-   `COOKIES`: Пути к файлам cookie для YouTube и Reddit для доступа к приватному контенту или обхода ограничений.
-   `INSTAGRAM_ACCOUNTS`: **Ключевая настройка.** Список аккаунтов для скачивания из Instagram. Бот будет использовать их по очереди.
    -   `cookie_file`: Имя файла с cookie для этого аккаунта (должен лежать в папке `important`).
    -   `proxy`: Индивидуальный прокси для этого аккаунта. `null`, если не нужен.

## Папка `important`

Эта папка содержит чувствительные данные и не должна попасть в репозиторий.

**Содержимое:**
-   `cache.json`: Создается автоматически для кэширования ссылок.
-   `youtube_cookies.txt`: Файл cookie для YouTube (если указан в `config.json`).
-   `reddit_cookies.txt`: Файл cookie для Reddit (если указан в `config.json`).
-   `instagram_cookies_1.txt`, `instagram_cookies_2.txt` и т.д.: Файлы cookie для каждого аккаунта Instagram, перечисленного в конфиге.

## Использование

1.  Запустите бота:
    ```bash
    python main.py  # или как называется ваш главный файл
    ```
2.  Отправьте боту в личные сообщения ссылку на медиа.
3.  Бот обработает ссылку и пришлет вам файл. Если ссылка уже обрабатывалась, ответ будет мгновенным благодаря кэшу.

## Логи и Отладка

-   Бот выводит подробные логи своей работы в консоль.
-   Временные файлы (`.mp4`, `.mp3` и т.д.), создаваемые в процессе работы, автоматически удаляются фоновым процессом.

## Лицензия и Ответственность

Проект предоставляется «как есть». Автор не несет ответственности за возможные нарушения авторских прав при загрузке и распространении материалов. Используйте бота на свой страх и риск и уважайте права создателей контента.
