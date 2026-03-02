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
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler, ContextTypes
from telegram.error import TimedOut

# Always disable .pyc generation regardless of launch mode.
sys.dont_write_bytecode = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORTANT_DIR = os.path.join(BASE_DIR, 'important')
CONFIG_PATH = os.path.join(IMPORTANT_DIR, 'config.json')
SSL_ROOT_CERT = os.path.join(IMPORTANT_DIR, 'root.crt')
INSTAGRAM_HELPER = os.path.join(BASE_DIR, "download_instagram.sh")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MediaBot")
# Avoid noisy per-request polling logs from dependencies.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    raw = match.group(0).rstrip(").,]}>\"'")
    return unwrap_redirect_url(raw)


def unwrap_redirect_url(url, max_hops=3):
    cur = (url or "").strip()
    if not cur:
        return cur

    for _ in range(max_hops):
        try:
            parsed = urlsplit(cur)
            host = (parsed.netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
            path = parsed.path or "/"
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))

            target = None
            if (host == "google.com" or host.endswith(".google.com")) and path == "/url":
                target = params.get("url") or params.get("q")
            elif host in {"l.facebook.com", "lm.facebook.com"} and path.startswith("/l.php"):
                target = params.get("u")
            elif host in {"duckduckgo.com", "m.duckduckgo.com"} and path.startswith("/l/"):
                target = params.get("uddg")
            elif host in {"vk.com", "m.vk.com"} and path.startswith("/away.php"):
                target = params.get("to")

            if not target:
                break

            target = unquote((target or "").strip())
            if not re.match(r"^https?://", target, flags=re.IGNORECASE):
                break
            if target == cur:
                break
            cur = target.rstrip(").,]}>\"'")
        except Exception:
            break

    return cur


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
SEND_SEMAPHORE = asyncio.Semaphore(int(cfg("limits.send_concurrency", 5)))
ALBUM_TRACK_CONCURRENCY = int(cfg("limits.album_track_concurrency", 3))
ADMIN_BUTTON_CHUNK_SIZE = int(cfg("limits.admin_button_chunk_size", 50))
CLEANUP_INTERVAL_SEC = int(cfg("limits.cleanup_interval_sec", 3600))
CLEANUP_TTL_SEC = int(cfg("limits.cleanup_ttl_sec", 3600))

# UX/messages config
ERROR_MSG_USER = cfg("messages.error_user", "Error. Try again later or check the link")
TOO_LARGE_MSG = cfg("messages.too_large", "⚠️ File is too large (>200MB).")
REDDIT_BLOCKED_MSG = cfg(
    "messages.reddit_blocked",
    "⚠️ Reddit blocked this request from our current network (HTTP 403). "
    "Please send a full Reddit post link or a direct v.redd.it link."
)
STATUS_ANALYZING = cfg("messages.status.analyzing", "⏳ Analyzing...")
STATUS_SENDING = cfg("messages.status.sending", "📤 Sending...")
STATUS_LISTENING = cfg("messages.status.listening", "☁️ Listening...")
STATUS_ALBUM = cfg("messages.status.album", "💿 Album: {count} tracks...")
START_MESSAGE = cfg("messages.start", "MediaBot Ready (DB Caching).")

USER_ERROR_MESSAGES = {
    "TOO_LARGE": TOO_LARGE_MSG,
    "REDDIT_BLOCKED": REDDIT_BLOCKED_MSG,
    "ERR_PRIVATE": "This media is private. Please make it public and try again.",
    "ERR_LOGIN_REQUIRED": "This media requires login. Please send a public link or update cookies.",
    "ERR_AGE_RESTRICTED": "This media is age-restricted and requires account access.",
    "ERR_GEO_BLOCKED": "This media is blocked in the current region.",
    "ERR_UNAVAILABLE": "This media is unavailable or has been removed.",
    "ERR_ACCESS_DENIED": "Access to this media is denied by the source.",
    "ERR_NOT_FOUND": "This link was not found (404). Please check the URL.",
    "ERR_UNSUPPORTED_LINK": "This link format is not supported.",
    "ERR_RATE_LIMIT": "The source is rate-limiting requests right now. Please try again later.",
    "ERR_PROXY": "The network proxy is unavailable right now. Please try again in a few minutes.",
    "ERR_TIMEOUT": "The source timed out while downloading. Please try again.",
    "ERR_FORMAT_NOT_AVAILABLE": "Requested media format is not available for this link.",
    "ERR_SOURCE_UNREACHABLE": "Could not reach the source page. Please try again later.",
    "ERR_CONVERSION": "The media was downloaded but could not be converted.",
    "ERR_SEND_TIMEOUT": "Upload to Telegram timed out. If media did not appear, please retry.",
    "ERR_DOWNLOAD_FAILED": "Download failed for this link. Please try again or send another URL.",
    "ERR_EMPTY_ALBUM": "No playable tracks were found in this album.",
    "ERR_UNKNOWN": ERROR_MSG_USER,
}

# Download behavior config
YTDLP_DEFAULT_FORMAT = cfg(
    "downloads.ytdlp.default_format",
    "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
)
YTDLP_SOCKET_TIMEOUT = int(cfg("downloads.ytdlp.socket_timeout_sec", 30))
YTDLP_CONCURRENT_FRAGMENT_DOWNLOADS = int(cfg("downloads.ytdlp.concurrent_fragment_downloads", 2))
YTDLP_RETRIES = int(cfg("downloads.ytdlp.retries", 3))
YTDLP_FRAGMENT_RETRIES = int(cfg("downloads.ytdlp.fragment_retries", 3))
YTDLP_FILE_ACCESS_RETRIES = int(cfg("downloads.ytdlp.file_access_retries", 2))
YTDLP_YOUTUBE_FORMAT = cfg(
    "downloads.ytdlp.youtube_format",
    "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
)
YTDLP_TIKTOK_FORMAT = cfg("downloads.ytdlp.tiktok_format", "bestvideo+bestaudio/best")
_ytdlp_js_runtimes_cfg = cfg("downloads.ytdlp.js_runtimes", {"node": {"path": "node"}})
if isinstance(_ytdlp_js_runtimes_cfg, str):
    YTDLP_JS_RUNTIMES = {_ytdlp_js_runtimes_cfg: {}}
elif isinstance(_ytdlp_js_runtimes_cfg, (list, tuple)):
    YTDLP_JS_RUNTIMES = {str(x): {} for x in _ytdlp_js_runtimes_cfg if str(x).strip()}
elif isinstance(_ytdlp_js_runtimes_cfg, dict):
    YTDLP_JS_RUNTIMES = _ytdlp_js_runtimes_cfg
else:
    YTDLP_JS_RUNTIMES = {}
FORCE_CONVERSION_SERVICES = set(cfg("features.force_conversion_services", ["Instagram", "Reddit"]))

# Feature toggles/data
EXCLUDED_CHATS = set(int(x) for x in cfg("features.excluded_chats", []))
EXACT_MATCHES = cfg("features.exact_matches", {})

# Telegram API timeouts
TG_CONNECT_TIMEOUT = int(cfg("telegram.timeouts.connect_sec", 30))
TG_READ_TIMEOUT = int(cfg("telegram.timeouts.read_sec", 600))
TG_WRITE_TIMEOUT = int(cfg("telegram.timeouts.write_sec", 600))
TG_POOL_TIMEOUT = int(cfg("telegram.timeouts.pool_sec", 30))
MAX_UPDATE_AGE_SEC = int(cfg("features.max_update_age_sec", 300))

# Proxy watchdog config
PROXY_CHECK_URL = cfg("features.proxy_watchdog.url", "https://api.ipify.org?format=json")
PROXY_CHECK_TIMEOUT_SEC = int(cfg("features.proxy_watchdog.timeout_sec", 15))
REDDIT_SHORT_RESOLVE_TIMEOUT_SEC = int(cfg("features.reddit_short_resolve_timeout_sec", 15))
PROXY6_CONFIG = cfg("integrations.proxy6", {})
PROXY6_API_KEY = os.getenv("PROXY6_API_KEY") or PROXY6_CONFIG.get("api_key")
PROXY6_API_BASE_URL = PROXY6_CONFIG.get("api_base_url", "https://px6.link/api")
PROXY6_TIMEOUT_SEC = int(PROXY6_CONFIG.get("timeout_sec", 20))
PROXY6_WARN_BEFORE_SEC = max(0, int(PROXY6_CONFIG.get("warn_before_sec", 2 * 24 * 3600)))
PROXY6_PROLONG_DAYS = max(1, int(PROXY6_CONFIG.get("prolong_days", 30)))

SUPPORTED_MEDIA_MARKERS = [
    "youtube",
    "youtu.be",
    "instagram",
    "tiktok",
    "reddit",
    "redd.it",
    "music.yandex",
    "spotify",
    "music.youtube",
    "pornhub",
    "pinterest",
    "pin.it",
]

MUSIC_MARKERS = ["music.yandex", "spotify", "music.youtube"]

reddit = asyncpraw.Reddit(**REDDIT_CONFIG) if REDDIT_CONFIG.get("client_id") else None
s3_client = None
if YSK.get("S3_ACCESS_KEY_ID"):
    try:
        s3_client = boto3.client('s3', endpoint_url='https://storage.yandexcloud.net',
                                 aws_access_key_id=YSK.get("S3_ACCESS_KEY_ID"),
                                 aws_secret_access_key=YSK.get("S3_SECRET_ACCESS_KEY"))
    except Exception as e: logger.error(f"S3 Init Error: {e}")

db_pool = None
proxy_watchdog_task = None

async def init_db(app):
    """Connect to DB and create table on startup"""
    global db_pool, proxy_watchdog_task
    if proxy_watchdog_task is None or proxy_watchdog_task.done():
        proxy_watchdog_task = asyncio.create_task(proxy_watchdog_loop(app))
        logger.info("🕛 Proxy watchdog started (daily at 00:00 server time).")
    asyncio.create_task(run_proxy_health_check(app, "startup"))
    asyncio.create_task(run_proxy_expiry_check(app, "startup"))

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


def _is_reddit_share_url(url):
    try:
        parsed = urlsplit(url or "")
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        return "reddit.com" in host and "/s/" in path
    except Exception:
        return False


def _resolve_reddit_share_url_sync(url, proxy):
    cmds = [
        ["curl", "-sS", "-m", str(REDDIT_SHORT_RESOLVE_TIMEOUT_SEC), "-I", url],
        ["curl", "-sS", "-m", str(REDDIT_SHORT_RESOLVE_TIMEOUT_SEC), "-o", "/dev/null", "-D", "-", url],
    ]
    for cmd in cmds:
        if proxy:
            cmd.extend(["--proxy", proxy])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            if line.lower().startswith("location:"):
                target = line.split(":", 1)[1].strip()
                if target:
                    return urljoin(url, target)
    return url


async def resolve_reddit_share_url(url, proxy):
    if not _is_reddit_share_url(url):
        return url
    try:
        resolved = await asyncio.to_thread(_resolve_reddit_share_url_sync, url, proxy)
        if resolved and resolved != url:
            logger.info(f"🔁 Resolved Reddit share URL: {url} -> {resolved}")
            return resolved
    except Exception as e:
        logger.warning(f"Reddit share resolve failed: {e}")
    return url


def _reddit_error_code_for_result(source_url, target_url, raw_error, default_code="ERR_DOWNLOAD_FAILED"):
    low = (raw_error or "").lower()
    if "http error 403: blocked" in low:
        return "REDDIT_BLOCKED"
    code = classify_downloader_error(low, default_code=default_code)
    if _is_reddit_share_url(source_url) and target_url == source_url and code in {
        "ERR_ACCESS_DENIED",
        "ERR_SOURCE_UNREACHABLE",
        "ERR_DOWNLOAD_FAILED",
    }:
        return "REDDIT_BLOCKED"
    return code


def _mask_proxy(proxy_url):
    try:
        parsed = urlsplit(proxy_url or "")
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme or "proxy"
        if port is not None:
            return f"{scheme}://{host}:{port}"
        return f"{scheme}://{host}"
    except Exception:
        return "proxy://masked"


def _collect_proxy_sources():
    items = []
    if isinstance(PROXIES, dict):
        for name, value in PROXIES.items():
            if isinstance(value, str) and value.strip():
                items.append((f"network.proxies.{name}", value.strip()))
    reddit_proxy = REDDIT_CONFIG.get("proxy")
    if isinstance(reddit_proxy, str) and reddit_proxy.strip():
        items.append(("integrations.reddit.proxy", reddit_proxy.strip()))
    instagram_proxy = _extract_instagram_helper_proxy()
    if isinstance(instagram_proxy, str) and instagram_proxy.strip():
        items.append(("integrations.instagram.helper_script", instagram_proxy.strip()))
    return items


class Proxy6APIError(Exception):
    def __init__(self, message, error_id=None):
        super().__init__(message)
        self.error_id = error_id


def _extract_instagram_helper_proxy():
    if not os.path.exists(INSTAGRAM_HELPER):
        return None
    try:
        with open(INSTAGRAM_HELPER, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None

    patterns = [
        r'^\s*PROXY_STRING\s*=\s*"([^"]+)"',
        r"^\s*PROXY_STRING\s*=\s*'([^']+)'",
        r'^\s*PROXY_STRING\s*=\s*([^\s#]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.MULTILINE)
        if match:
            value = (match.group(1) or "").strip()
            if value and "${" not in value and "$(" not in value:
                return value
    return None


def _normalize_proxy_url(proxy_url):
    try:
        parsed = urlsplit((proxy_url or "").strip())
        scheme = (parsed.scheme or "http").lower()
        if scheme == "socks":
            scheme = "socks5"
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if not host or port is None:
            return (proxy_url or "").strip().lower()
        user = parsed.username or ""
        password = parsed.password or ""
        if user:
            return f"{scheme}://{user}:{password}@{host}:{port}"
        return f"{scheme}://{host}:{port}"
    except Exception:
        return (proxy_url or "").strip().lower()


def _proxy6_item_as_url(item, scheme_override=None):
    host = str(item.get("host") or "").strip()
    port = str(item.get("port") or "").strip()
    if not host or not port:
        return ""

    proxy_type = str(item.get("type") or "").lower()
    if scheme_override:
        scheme = scheme_override
    elif proxy_type.startswith("sock"):
        scheme = "socks5"
    else:
        scheme = "http"

    user = str(item.get("user") or "").strip()
    password = str(item.get("pass") or "").strip()
    if user:
        return f"{scheme}://{user}:{password}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def _proxy6_call_sync(method, params=None):
    if not PROXY6_API_KEY:
        raise Proxy6APIError("Proxy6 API key is not configured.")

    url = f"{PROXY6_API_BASE_URL.rstrip('/')}/{PROXY6_API_KEY}/{method.lstrip('/')}"
    try:
        response = requests.get(url, params=params or {}, timeout=PROXY6_TIMEOUT_SEC)
    except requests.RequestException as e:
        raise Proxy6APIError(f"Proxy6 request failed: {e}") from e

    try:
        payload = response.json()
    except ValueError as e:
        raise Proxy6APIError(f"Proxy6 invalid JSON response (HTTP {response.status_code}).") from e

    if not isinstance(payload, dict):
        raise Proxy6APIError("Proxy6 response format is invalid.")

    if payload.get("status") == "no":
        error_text = str(payload.get("error") or "Unknown Proxy6 error")
        error_id = payload.get("error_id")
        if error_id is not None:
            raise Proxy6APIError(f"{error_text} (error_id={error_id})", error_id=error_id)
        raise Proxy6APIError(error_text)

    if response.status_code >= 400:
        raise Proxy6APIError(f"Proxy6 HTTP error {response.status_code}.")

    return payload


def _proxy6_extract_items(payload):
    raw = payload.get("list", {})
    if isinstance(raw, dict):
        values = raw.values()
    elif isinstance(raw, list):
        values = raw
    else:
        return []
    return [x for x in values if isinstance(x, dict)]


def _proxy6_remaining_seconds(item):
    try:
        return int(item.get("unixtime_end")) - int(time.time())
    except Exception:
        return None


def _format_remaining_seconds(remaining):
    if remaining is None:
        return "неизвестно"
    if remaining <= 0:
        return "истек"
    days, rem = divmod(int(remaining), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}д {hours}ч"
    if hours > 0:
        return f"{hours}ч {minutes}м"
    return f"{max(1, minutes)}м"


def _proxy6_state_label(item):
    remaining = _proxy6_remaining_seconds(item)
    active = str(item.get("active", "1")) == "1"
    if not active or (remaining is not None and remaining <= 0):
        return "неактивна"
    if remaining is not None and remaining < PROXY6_WARN_BEFORE_SEC:
        return "скоро истечет"
    return "активна"


def _proxy_usage_map():
    usage = {}
    for source_name, proxy_url in _collect_proxy_sources():
        key = _normalize_proxy_url(proxy_url)
        usage.setdefault(key, []).append(source_name)
    return usage


def _proxy6_item_usage(item, usage_map):
    candidates = [_normalize_proxy_url(_proxy6_item_as_url(item))]
    proxy_type = str(item.get("type") or "").lower()
    if proxy_type.startswith("sock"):
        candidates.append(_normalize_proxy_url(_proxy6_item_as_url(item, scheme_override="socks")))

    used_in = []
    for key in candidates:
        for src in usage_map.get(key, []):
            if src not in used_in:
                used_in.append(src)
    return used_in


def _format_proxy6_error(err):
    text = str(err)
    low = text.lower()
    if "balance" in low or "баланс" in low or "money" in low:
        return "Недостаточно средств на балансе Proxy6."
    return text


async def get_proxy6_proxies(state="all"):
    payload = await asyncio.to_thread(_proxy6_call_sync, "getproxy", {"state": state})
    items = _proxy6_extract_items(payload)
    try:
        return sorted(items, key=lambda x: int(x.get("id", 0)))
    except Exception:
        return items


def _find_proxy_by_id(items, proxy_id):
    for item in items:
        if str(item.get("id")) == str(proxy_id):
            return item
    return None


def _check_proxy_sync(proxy):
    cmd = [
        "curl", "-sS", "-m", str(PROXY_CHECK_TIMEOUT_SEC),
        "--proxy", proxy, "-o", "/dev/null", "-w", "%{http_code}", PROXY_CHECK_URL
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, (proc.stderr or f"curl rc={proc.returncode}").strip()
    code = (proc.stdout or "").strip()
    if code.startswith("2"):
        return True, f"http {code}"
    return False, f"http {code or '000'}"


def _run_proxy_health_checks_sync():
    grouped = {}
    for source_name, proxy in _collect_proxy_sources():
        grouped.setdefault(proxy, []).append(source_name)
    results = []
    for proxy, source_names in grouped.items():
        ok, detail = _check_proxy_sync(proxy)
        results.append({"proxy": proxy, "sources": source_names, "ok": ok, "detail": detail})
    return results


async def run_proxy_health_check(app, reason):
    results = await asyncio.to_thread(_run_proxy_health_checks_sync)
    if not results:
        logger.info("Proxy health check skipped: no proxies configured.")
        return

    failed = [x for x in results if not x["ok"]]
    if not failed:
        logger.info("✅ Proxy health check OK (%s).", reason)
        return

    logger.warning("⚠️ Proxy health check failed (%s): %s", reason, failed)
    if not ADMIN_ID:
        return

    lines = [f"⚠️ Proxy check failed ({reason})", f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    for item in failed:
        src = ", ".join(item["sources"])
        lines.append(f"- {src}: {_mask_proxy(item['proxy'])} -> {item['detail']}")
    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text="\n".join(lines))
    except Exception as e:
        logger.error(f"Failed to send proxy watchdog alert: {e}")


async def run_proxy_expiry_check(app, reason):
    if not PROXY6_API_KEY:
        logger.info("Proxy expiry check skipped: Proxy6 API key is missing.")
        return

    try:
        items = await get_proxy6_proxies("all")
    except Exception as e:
        logger.warning("Proxy expiry check failed (%s): %s", reason, e)
        return

    if not items:
        logger.info("Proxy expiry check: no Proxy6 proxies found.")
        return

    usage_map = _proxy_usage_map()
    expiring = []
    expired = []
    for item in items:
        remaining = _proxy6_remaining_seconds(item)
        if remaining is None:
            continue
        if remaining <= 0:
            expired.append((item, remaining))
        elif remaining < PROXY6_WARN_BEFORE_SEC:
            expiring.append((item, remaining))

    if not expiring and not expired:
        logger.info("✅ Proxy expiry check OK (%s).", reason)
        return

    logger.warning(
        "⚠️ Proxy expiry warning (%s): expiring=%d expired=%d",
        reason,
        len(expiring),
        len(expired),
    )

    if not ADMIN_ID:
        return

    lines = [
        f"⚠️ Proxy6 срок действия ({reason})",
        f"Порог предупреждения: < {PROXY6_WARN_BEFORE_SEC // 3600}ч",
        f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    for item, remaining in expiring + expired:
        used_in = _proxy6_item_usage(item, usage_map)
        used_text = ", ".join(used_in) if used_in else "не используется"
        lines.append(
            f"- #{item.get('id')} {_mask_proxy(_proxy6_item_as_url(item))}: "
            f"{_proxy6_state_label(item)}, осталось {_format_remaining_seconds(remaining)}, "
            f"используется: {used_text}"
        )

    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text="\n".join(lines))
    except Exception as e:
        logger.error(f"Failed to send proxy expiry alert: {e}")


def _seconds_until_next_midnight():
    now = datetime.now()
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (next_midnight - now).total_seconds())


async def proxy_watchdog_loop(app):
    while True:
        try:
            sleep_sec = _seconds_until_next_midnight()
            await asyncio.sleep(sleep_sec)
            await run_proxy_health_check(app, "midnight")
            await run_proxy_expiry_check(app, "midnight")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Proxy watchdog loop error: {e}")
            await asyncio.sleep(60)


def classify_downloader_error(raw_error, default_code="ERR_DOWNLOAD_FAILED"):
    low = (raw_error or "").lower()
    if not low:
        return default_code
    if (
        ("proxy" in low and ("failed" in low or "error" in low or "connection" in low or "tunnel" in low))
        or "sockshttpsconnection" in low
        or "sockshttpconnection" in low
        or "proxyerror" in low
        or "cannot connect to proxy" in low
        or "error connecting to socks" in low
    ):
        return "ERR_PROXY"
    if "http error 429" in low or "too many requests" in low:
        return "ERR_RATE_LIMIT"
    if "private video" in low or "private account" in low or "is private" in low or "approved followers" in low:
        return "ERR_PRIVATE"
    if "age-restricted" in low or "sign in to confirm your age" in low:
        return "ERR_AGE_RESTRICTED"
    if "login required" in low or "authentication" in low or "sign in" in low:
        return "ERR_LOGIN_REQUIRED"
    if "not available in your country" in low or "geo-restricted" in low:
        return "ERR_GEO_BLOCKED"
    if "video unavailable" in low or "has been removed" in low or "is unavailable" in low:
        return "ERR_UNAVAILABLE"
    if "http error 403" in low or "forbidden" in low:
        return "ERR_ACCESS_DENIED"
    if "http error 404" in low or "404 not found" in low:
        return "ERR_NOT_FOUND"
    if "unsupported url" in low or "no suitable extractor" in low:
        return "ERR_UNSUPPORTED_LINK"
    if "timed out" in low or "timeout" in low:
        return "ERR_TIMEOUT"
    if "requested format is not available" in low:
        return "ERR_FORMAT_NOT_AVAILABLE"
    if "unable to download webpage" in low or "failed to download" in low or "unable to extract" in low:
        return "ERR_SOURCE_UNREACHABLE"
    return default_code


async def send_sad_cat(context, chat_id):
    try:
        await context.bot.send_photo(chat_id=chat_id, photo=f"https://cataas.com/cat/sad?random={uuid.uuid4()}")
    except Exception:
        pass


async def send_user_error(context, chat_id, error_code, reply_to_id=None):
    text = USER_ERROR_MESSAGES.get(error_code, USER_ERROR_MESSAGES["ERR_UNKNOWN"])
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_id)
    except Exception:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            pass
    await send_sad_cat(context, chat_id)

async def notify_error(update: Update, context, exception_obj, context_info="Unknown"):
    """
    Sends error notification to admin and user.
    """
    logger.error(f"🔥 Error in {context_info}: {exception_obj}")
    msg = update.effective_message
    
    # Notify user
    if msg:
        code = str(exception_obj).strip()
        if code not in USER_ERROR_MESSAGES:
            code = classify_downloader_error(code, default_code="ERR_UNKNOWN")
        await send_user_error(context, msg.chat_id, code, reply_to_id=msg.message_id)
    
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
    cookie_file = COOKIES.get('reddit')
    configured_proxy = REDDIT_CONFIG.get("proxy") or PROXIES.get("reddit")
    attempt_proxies = []
    if configured_proxy:
        attempt_proxies.append(configured_proxy)
    attempt_proxies.append(None)

    last_code = "ERR_DOWNLOAD_FAILED"
    for attempt_idx, attempt_proxy in enumerate(attempt_proxies, start=1):
        target_url = await resolve_reddit_share_url(url, attempt_proxy)
        cmd = ["nice", "-n", "19", "yt-dlp", "--output", fname, "--no-warnings"]
        if attempt_proxy:
            cmd.extend(["--proxy", attempt_proxy])
        if cookie_file and os.path.exists(cookie_file):
            cmd.extend(["--cookies", cookie_file])
        cmd.append(target_url)
        logger.info(
            "🚀 Executing Reddit CMD (attempt %s/%s, proxy=%s, target=%s)",
            attempt_idx,
            len(attempt_proxies),
            "on" if attempt_proxy else "off",
            target_url,
        )
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
            if proc.returncode == 0 and os.path.exists(fname):
                return fname
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            combined = f"{stderr}\n{stdout}".strip()
            logger.error(f"❌ Reddit DL Failed. RC: {proc.returncode}\nERR: {proc.stderr}")
            last_code = _reddit_error_code_for_result(
                url,
                target_url,
                combined,
                default_code="ERR_DOWNLOAD_FAILED",
            )
        except Exception as e:
            logger.error(f"❌ Reddit Exception: {e}")
            last_code = _reddit_error_code_for_result(
                url,
                target_url,
                str(e),
                default_code="ERR_DOWNLOAD_FAILED",
            )

        for suffix in ("", ".part", ".ytdl"):
            try:
                path = f"{fname}{suffix}"
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

        if attempt_proxy:
            logger.warning("Reddit attempt via proxy failed with %s, retrying direct", last_code)
            continue
        return last_code

    return last_code

async def generic_download(url, opts_update=None):
    dl_id = f"dl_{uuid.uuid4().hex}"
    outtmpl = os.path.join(BASE_DIR, f"{dl_id}.%(ext)s")
    opts = {
        'outtmpl': outtmpl,
        'quiet': True,
        'nocheckcertificate': True,
        'socket_timeout': YTDLP_SOCKET_TIMEOUT,
        'format': YTDLP_DEFAULT_FORMAT,
        # Gentle boost for fragmented streams (HLS/DASH) without aggressive load.
        'concurrent_fragment_downloads': max(1, YTDLP_CONCURRENT_FRAGMENT_DOWNLOADS),
        'retries': max(1, YTDLP_RETRIES),
        'fragment_retries': max(1, YTDLP_FRAGMENT_RETRIES),
        'file_access_retries': max(1, YTDLP_FILE_ACCESS_RETRIES),
    }
    if YTDLP_JS_RUNTIMES:
        opts['js_runtimes'] = YTDLP_JS_RUNTIMES
    if opts_update: opts.update(opts_update)
    
    def _run_ydl():
        produced_files = []
        def _hook(d):
            fn = d.get("filename")
            if d.get("status") == "finished" and fn:
                produced_files.append(fn)

        def _attempt(run_opts):
            run_opts = dict(run_opts)
            run_opts['progress_hooks'] = [_hook]
            with yt_dlp.YoutubeDL(run_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if (info.get('filesize') or info.get('filesize_approx') or 0) > MAX_FILE_SIZE:
                    logger.warning(f"File too large (estimated): {url}")
                    return "TOO_LARGE"
                ydl.download([url])
            return "OK"

        try:
            result = _attempt(opts)
        except Exception as e:
            msg = str(e)
            if "Requested format is not available" in msg and opts.get("format") != "best":
                try:
                    fallback_opts = dict(opts)
                    fallback_opts["format"] = "best"
                    result = _attempt(fallback_opts)
                except Exception as e2:
                    logger.error(f"YDL Error (fallback): {e2}")
                    return classify_downloader_error(str(e2), default_code="ERR_DOWNLOAD_FAILED")
            else:
                logger.error(f"YDL Error: {e}")
                return classify_downloader_error(msg, default_code="ERR_DOWNLOAD_FAILED")

        if result == "TOO_LARGE":
            return "TOO_LARGE"

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
        return "ERR_DOWNLOAD_FAILED"

    return await asyncio.to_thread(_run_ydl)

async def download_pinterest(url):
    try:
        headers = {
            'x-rapidapi-key': RAPID_API_KEY,
            'x-rapidapi-host': "pinterest-video-and-image-downloader.p.rapidapi.com"
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.get("https://pinterest-video-and-image-downloader.p.rapidapi.com/pinterest", params={"url": url}, headers=headers) as resp:
                if resp.status != 200:
                    return classify_downloader_error(f"http error {resp.status}", default_code="ERR_SOURCE_UNREACHABLE")
                data = await resp.json()
        
        if not data.get('success'):
            return "ERR_UNAVAILABLE"
        
        media_data = data.get('data', {})
        target_url = media_data.get('url')
        if not target_url:
            return "ERR_UNSUPPORTED_LINK"
        
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
                return classify_downloader_error(f"http error {resp.status}", default_code="ERR_SOURCE_UNREACHABLE")
    except Exception as e:
        logger.error(f"Pinterest error: {e}")
    return classify_downloader_error(str(e), default_code="ERR_DOWNLOAD_FAILED")

async def download_router(url):
    low_url = (url or "").lower()
    if "pinterest" in low_url or "pin.it" in low_url:
        return await download_pinterest(url)
    elif "instagram.com" in low_url:
        fname = f"inst_{uuid.uuid4().hex}.mp4"
        try:
            # Use nice/ionice for the shell script too
            proc = await asyncio.to_thread(subprocess.run, ["nice", "-n", "19", INSTAGRAM_HELPER, url, fname], capture_output=True)
            if proc.returncode == 0 and os.path.exists(fname):
                if os.path.getsize(fname) > MAX_FILE_SIZE:
                    os.remove(fname)
                    return "TOO_LARGE"
                return fname
            if proc.returncode != 0:
                stderr = (proc.stderr or b"").decode(errors="ignore")
                logger.error("Instagram helper failed. RC: %s STDERR: %s", proc.returncode, stderr)
                return classify_downloader_error(stderr, default_code="ERR_DOWNLOAD_FAILED")
            return "ERR_DOWNLOAD_FAILED"
        except Exception as e:
            return classify_downloader_error(str(e), default_code="ERR_DOWNLOAD_FAILED")
    elif "reddit" in low_url or "redd.it" in low_url:
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
    if "youtube" in url or "youtu.be" in url:
        opts = {
            'cookiefile': COOKIES.get('youtube'),
            'proxy': PROXIES.get('youtube'),
            'format': YTDLP_YOUTUBE_FORMAT
        }
    elif "tiktok" in url:
        opts = {
            'proxy': PROXIES.get('tiktok'),
            'cookiefile': COOKIES.get('tiktok'),
            'format': YTDLP_TIKTOK_FORMAT
        }
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


def is_mp4_container(path):
    try:
        return os.path.splitext(path or "")[1].lower() == ".mp4"
    except Exception:
        return False

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
    source_low = (source_link or "").lower()
    cache_link = normalize_link_for_cache(source_link)

    if txt in EXACT_MATCHES and chat_id not in EXCLUDED_CHATS:
        return await msg.reply_text(EXACT_MATCHES[txt])

    # VK removed from trigger list
    if not any(d in source_low for d in SUPPORTED_MEDIA_MARKERS):
        return

    detected_service = "Unknown"
    if "youtube" in source_low or "youtu.be" in source_low: detected_service = "YouTube"
    elif "instagram" in source_low: detected_service = "Instagram"
    elif "tiktok" in source_low: detected_service = "TikTok"
    elif "reddit" in source_low or "redd.it" in source_low: detected_service = "Reddit"
    # VK removed from detection logic
    elif "music.yandex" in source_low: detected_service = "YandexMusic"
    elif "spotify" in source_low: detected_service = "Spotify"
    elif "pornhub" in source_low: detected_service = "PornHub"
    elif "pinterest" in source_low or "pin.it" in source_low: detected_service = "Pinterest"

    cached_file_id = await check_db_cache(cache_link)
    if not cached_file_id and cache_link != source_link:
        cached_file_id = await check_db_cache(source_link)
    if cached_file_id:
        try:
            try:
                async with SEND_SEMAPHORE:
                    if any(x in txt for x in MUSIC_MARKERS):
                        await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id, reply_to_message_id=msg.message_id)
                    else:
                        try:
                            await context.bot.send_video(chat_id=chat_id, video=cached_file_id, reply_to_message_id=msg.message_id)
                        except Exception:
                            await context.bot.send_document(chat_id=chat_id, document=cached_file_id, reply_to_message_id=msg.message_id)
            except Exception:
                async with SEND_SEMAPHORE:
                    if any(x in txt for x in MUSIC_MARKERS):
                        await context.bot.send_audio(chat_id=chat_id, audio=cached_file_id, reply_to_message_id=None)
                    else:
                        try:
                            await context.bot.send_video(chat_id=chat_id, video=cached_file_id, reply_to_message_id=None)
                        except Exception:
                            await context.bot.send_document(chat_id=chat_id, document=cached_file_id, reply_to_message_id=None)
            
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
    media_sent = False
    try:
        st_msg = await update_status(context, chat_id, STATUS_ANALYZING, reply_to_id=msg.message_id)

        if "music.yandex" in txt and "/album/" in txt and "/track/" not in txt:
            detected_service = "YandexAlbum"
            tracks = await asyncio.to_thread(get_ym_album_info, txt)
            if not tracks: raise Exception("ERR_EMPTY_ALBUM")
            
            st_msg = await update_status(
                context, chat_id, STATUS_ALBUM.format(count=len(tracks)), message_obj=st_msg, reply_to_id=msg.message_id
            )

            album_semaphore = asyncio.Semaphore(max(1, ALBUM_TRACK_CONCURRENCY))

            async def _send_album_track(track_title, track_artist):
                raw_track, f_path_track = None, None
                try:
                    dl_url = f"ytsearch1:{track_title} {track_artist}"
                    raw_track = await generic_download(dl_url, {'noplaylist': True, 'format': 'bestaudio/best'})
                    if not raw_track:
                        return
                    if isinstance(raw_track, str) and (raw_track == "TOO_LARGE" or raw_track.startswith("ERR_")):
                        return

                    f_path_track = await convert_media(raw_track, to_audio=True)
                    if not f_path_track or f_path_track == "TOO_LARGE" or not os.path.exists(f_path_track):
                        return

                    with open(f_path_track, 'rb') as f:
                        async with SEND_SEMAPHORE:
                            await context.bot.send_audio(chat_id, f, title=track_title, performer=track_artist)
                except Exception:
                    pass
                finally:
                    for p in [f_path_track, raw_track]:
                        if p and isinstance(p, str) and os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass

            async def _bounded_send(track_title, track_artist):
                async with album_semaphore:
                    await _send_album_track(track_title, track_artist)

            await asyncio.gather(*(_bounded_send(title, artist) for title, artist in tracks))
            
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
            if not raw: raise Exception("ERR_DOWNLOAD_FAILED")
            if raw == "TOO_LARGE": raise Exception("TOO_LARGE")
            if isinstance(raw, str) and raw.startswith("ERR_"): raise Exception(raw)
            f_path = await convert_media(raw, to_audio=True)
            if not f_path: raise Exception("ERR_CONVERSION")
            if f_path == "TOO_LARGE": raise Exception("TOO_LARGE")
            caption = f"{artist} - {title}" if title else ""
        else:
            raw = await download_router(source_link)
            if not raw: raise Exception("ERR_DOWNLOAD_FAILED")
            if raw == "TOO_LARGE": raise Exception("TOO_LARGE")
            if raw == "REDDIT_BLOCKED": raise Exception("REDDIT_BLOCKED")
            if isinstance(raw, str) and raw.startswith("ERR_"): raise Exception(raw)
            
            if raw.endswith(('.jpg', '.png', '.jpeg')):
                f_type = "image"
                f_path = raw
            elif detected_service in FORCE_CONVERSION_SERVICES:
                f_path = await convert_media(raw)
                if f_path == "TOO_LARGE": raise Exception("TOO_LARGE")
                if not f_path: raise Exception("ERR_CONVERSION")
            elif not is_mp4_container(raw):
                logger.info(f"🔁 Converting non-mp4 container for {detected_service}: {os.path.splitext(raw)[1].lower()}")
                f_path = await convert_media(raw)
                if f_path == "TOO_LARGE": raise Exception("TOO_LARGE")
                if not f_path: raise Exception("ERR_CONVERSION")
            else:
                logger.info(f"🚀 Skipping conversion for {detected_service}")
                f_path = raw

        if f_path and os.path.exists(f_path):
            st_msg = await update_status(context, chat_id, STATUS_SENDING, message_obj=st_msg, reply_to_id=msg.message_id)
            
            with open(f_path, 'rb') as f:
                sent = None
                try:
                    async with SEND_SEMAPHORE:
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
                            try:
                                sent = await context.bot.send_video(
                                    chat_id, f, caption=caption, reply_to_message_id=msg.message_id,
                                    read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                                )
                            except Exception:
                                f.seek(0)
                                sent = await context.bot.send_document(
                                    chat_id, f, caption=caption, reply_to_message_id=msg.message_id,
                                    read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                                )
                except TimedOut as e:
                    # Upload may still complete on Telegram side; avoid duplicate resend and noisy false errors.
                    raise Exception("SEND_TIMEOUT") from e
                except Exception:
                    f.seek(0) 
                    try:
                        async with SEND_SEMAPHORE:
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
                                try:
                                    sent = await context.bot.send_video(
                                        chat_id, f, caption=caption, reply_to_message_id=None,
                                        read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                                    )
                                except Exception:
                                    f.seek(0)
                                    sent = await context.bot.send_document(
                                        chat_id, f, caption=caption, reply_to_message_id=None,
                                        read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT, pool_timeout=TG_POOL_TIMEOUT
                                    )
                    except TimedOut as e:
                        raise Exception("SEND_TIMEOUT") from e

            if sent:
                media_sent = True
                file_id = None
                if f_type == "audio" and getattr(sent, "audio", None):
                    file_id = sent.audio.file_id
                elif f_type == "image" and getattr(sent, "photo", None):
                    file_id = sent.photo[-1].file_id
                elif getattr(sent, "video", None):
                    file_id = sent.video.file_id
                elif getattr(sent, "document", None):
                    file_id = sent.document.file_id

                if file_id:
                    try:
                        await save_log(user.id, user.username or "Unknown", chat_id, cache_link, detected_service, file_id)
                    except Exception as cache_err:
                        logger.warning(f"Cache save skipped after successful send: {cache_err}")
                else:
                    logger.info("Media sent but file_id unavailable for cache (service=%s, chat=%s).", detected_service, chat_id)
            
            if st_msg:
                try: await st_msg.delete()
                except: pass
        else: raise Exception("ERR_CONVERSION")

    except Exception as e:
        if st_msg: 
            try: await st_msg.delete()
            except: pass

        if media_sent:
            logger.warning(f"Post-send exception suppressed for chat {chat_id}: {e}")
            return
        
        err_code = str(e).strip()
        if err_code == "SEND_TIMEOUT":
            logger.warning(f"Media send timeout for chat {chat_id}, link: {source_link}")
            err_code = "ERR_SEND_TIMEOUT"

        if err_code in USER_ERROR_MESSAGES or err_code.startswith("ERR_"):
            await send_user_error(context, chat_id, err_code, reply_to_id=msg.message_id)
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

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Send Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📋 Show Chats", callback_data="admin_show_chats")],
        [InlineKeyboardButton("🌐 Прокси", callback_data="admin_proxy_list")],
    ])


def _proxy_status_icon(item):
    state = _proxy6_state_label(item)
    if state == "активна":
        return "✅"
    if state == "скоро истечет":
        return "⚠️"
    return "⛔"


async def _admin_edit_or_reply(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def admin_proxy_list(query, context, notice=None):
    if not PROXY6_API_KEY:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="admin_menu")]])
        await _admin_edit_or_reply(query, "Proxy6 API key не настроен.", reply_markup=kb)
        return

    try:
        items = await get_proxy6_proxies("all")
    except Exception as e:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В меню", callback_data="admin_menu")]])
        await _admin_edit_or_reply(query, f"Ошибка Proxy6: {_format_proxy6_error(e)}", reply_markup=kb)
        return

    usage_map = _proxy_usage_map()
    lines = ["🌐 Proxy6: прокси"]
    if notice:
        lines.append(notice)

    keyboard = []
    if not items:
        lines.append("Список прокси пуст.")
    else:
        for item in items:
            proxy_id = item.get("id")
            masked = _mask_proxy(_proxy6_item_as_url(item))
            status = _proxy6_state_label(item)
            remaining = _format_remaining_seconds(_proxy6_remaining_seconds(item))
            used_in = _proxy6_item_usage(item, usage_map)
            used_text = ", ".join(used_in) if used_in else "не используется"
            lines.append(f"#{proxy_id} {masked} | {status} | {remaining} | {used_text}")
            keyboard.append([
                InlineKeyboardButton(
                    f"{_proxy_status_icon(item)} #{proxy_id} {item.get('host')}:{item.get('port')}",
                    callback_data=f"admin_proxy_view_{proxy_id}",
                )
            ])

    keyboard.append([InlineKeyboardButton("⬅️ В меню", callback_data="admin_menu")])
    await _admin_edit_or_reply(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_proxy_view(query, context, proxy_id, notice=None):
    try:
        items = await get_proxy6_proxies("all")
    except Exception as e:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ К списку", callback_data="admin_proxy_list")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="admin_menu")],
        ])
        await _admin_edit_or_reply(query, f"Ошибка Proxy6: {_format_proxy6_error(e)}", reply_markup=kb)
        return

    item = _find_proxy_by_id(items, proxy_id)
    if not item:
        await admin_proxy_list(query, context, notice=f"Прокси #{proxy_id} не найдена.")
        return

    usage_map = _proxy_usage_map()
    used_in = _proxy6_item_usage(item, usage_map)
    used_text = ", ".join(used_in) if used_in else "не используется"
    remaining = _format_remaining_seconds(_proxy6_remaining_seconds(item))
    lines = [f"🌐 Proxy #{item.get('id')}"]
    if notice:
        lines.append(notice)
    lines.extend([
        f"Адрес: {_mask_proxy(_proxy6_item_as_url(item))}",
        f"Тип: {item.get('type')}",
        f"Статус: {_proxy6_state_label(item)}",
        f"Осталось: {remaining}",
        f"До: {item.get('date_end')}",
        f"Используется: {used_text}",
    ])

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"♻️ Продлить на {PROXY6_PROLONG_DAYS} дн.", callback_data=f"admin_proxy_prolong_{item.get('id')}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"admin_proxy_delete_{item.get('id')}")],
        [
            InlineKeyboardButton("⬅️ К списку", callback_data="admin_proxy_list"),
            InlineKeyboardButton("⬅️ В меню", callback_data="admin_menu"),
        ],
    ])
    await _admin_edit_or_reply(query, "\n".join(lines), reply_markup=kb)


async def admin_proxy_prolong(query, context, proxy_id):
    try:
        payload = await asyncio.to_thread(
            _proxy6_call_sync,
            "prolong",
            {"period": PROXY6_PROLONG_DAYS, "ids": str(proxy_id)},
        )
    except Exception as e:
        await admin_proxy_view(query, context, proxy_id, notice=f"❌ {_format_proxy6_error(e)}")
        return

    balance = payload.get("balance")
    notice = f"✅ Продлено на {PROXY6_PROLONG_DAYS} дней."
    if balance is not None:
        notice += f" Баланс: {balance} RUB."
    await admin_proxy_view(query, context, proxy_id, notice=notice)


async def admin_proxy_delete(query, context, proxy_id):
    try:
        await asyncio.to_thread(_proxy6_call_sync, "delete", {"ids": str(proxy_id)})
    except Exception as e:
        await admin_proxy_view(query, context, proxy_id, notice=f"❌ {_format_proxy6_error(e)}")
        return
    await admin_proxy_list(query, context, notice=f"✅ Прокси #{proxy_id} удалена.")


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    await update.message.reply_text("Admin Panel:", reply_markup=admin_main_keyboard())

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return
    await query.answer()

    if query.data == "admin_show_chats":
        await admin_show_chats(query, context)
        return ConversationHandler.END
    elif query.data == "admin_menu":
        await _admin_edit_or_reply(query, "Admin Panel:", reply_markup=admin_main_keyboard())
        return ConversationHandler.END
    elif query.data == "admin_proxy_list":
        await admin_proxy_list(query, context)
        return ConversationHandler.END
    elif query.data.startswith("admin_proxy_view_"):
        await admin_proxy_view(query, context, query.data.replace("admin_proxy_view_", "", 1))
        return ConversationHandler.END
    elif query.data.startswith("admin_proxy_prolong_"):
        await admin_proxy_prolong(query, context, query.data.replace("admin_proxy_prolong_", "", 1))
        return ConversationHandler.END
    elif query.data.startswith("admin_proxy_delete_"):
        await admin_proxy_delete(query, context, query.data.replace("admin_proxy_delete_", "", 1))
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
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern="^admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_buttons, pattern="^admin_proxy_"))

    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text(START_MESSAGE)))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.VIDEO_NOTE, handle_voice_video))
    logger.info("Bot Started with PostgreSQL Caching")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
