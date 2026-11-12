import os
import re
from os import environ as env
from urllib.parse import urlparse
from typing import Any
from dotenv import load_dotenv

# Ensure .env is loaded before reading environment variables
_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_path_override = os.environ.get("ENV_FILE")
if _env_path_override and os.path.isfile(_env_path_override):
    load_dotenv(dotenv_path=_env_path_override)
elif _env_path_override and os.path.isfile(os.path.join(_root_dir, _env_path_override)):
    load_dotenv(dotenv_path=os.path.join(_root_dir, _env_path_override))
elif os.path.isfile(os.path.join(_root_dir, ".env")):
    load_dotenv(dotenv_path=os.path.join(_root_dir, ".env"))
elif os.path.isfile(".env"):
    load_dotenv(dotenv_path=".env")



def _float_env(key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except Exception:
        return float(default)


def _int_or_none(key: str):
    raw = env.get(key, None)
    try:
        return int(raw) if raw not in (None, "") else None
    except Exception:
        return None


def _int_env(key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except Exception:
        return int(default)


def _bool_env(key: str, default: bool = False) -> bool:
    return str(env.get(key, str(default))).lower() in ("1", "true", "t", "yes", "y")


def _str_env(key: str, default: str = "") -> str:
    value = env.get(key, default)
    return str(value).strip() if value is not None else ""


_BOT_MEDIA_KEYS = {"FILE_PIC", "START_PIC", "ABOUT_PIC", "HELP_PIC", "VERIFY_PIC", "LEGAL_PIC", "PRIVACY_PIC", "FOLDERS_PIC"}
_BOT_MEDIA_OVERRIDES: dict[int, dict[str, str]] = {}
_BOT_WEB_OVERRIDES: dict[int, dict[str, str]] = {}
_BOT_WEB_AD_OVERRIDES: dict[int, dict[str, str]] = {}
for _env_key, _env_value in env.items():
    _match = re.fullmatch(r"MULTI_TOKEN(\d+)_(FILE_PIC|START_PIC|ABOUT_PIC|HELP_PIC|VERIFY_PIC|LEGAL_PIC|PRIVACY_PIC|FOLDERS_PIC)", _env_key)
    if _match:
        _client_index = int(_match.group(1))
        _media_key = _match.group(2)
        _BOT_MEDIA_OVERRIDES.setdefault(_client_index, {})[_media_key] = str(_env_value or "").strip()
        continue
    _match = re.fullmatch(r"MULTI_TOKEN(\d+)_(FQDN|PORT)", _env_key)
    if _match:
        _client_index = int(_match.group(1))
        _web_key = _match.group(2)
        _BOT_WEB_OVERRIDES.setdefault(_client_index, {})[_web_key] = str(_env_value or "").strip()
        continue
    _match = re.fullmatch(r"MULTI_TOKEN(\d+)_(WEB_ADS_[A-Z0-9_]+)", _env_key)
    if _match:
        _client_index = int(_match.group(1))
        _ads_key = _match.group(2)
        _BOT_WEB_AD_OVERRIDES.setdefault(_client_index, {})[_ads_key] = str(_env_value or "").strip()


def normalize_fqdn(raw_fqdn: str) -> str:
    raw = str(raw_fqdn or "").strip()
    if not raw:
        return "127.0.0.1"
    # Strip any repeated schemes and slashes (e.g. https://https://, //)
    clean = re.sub(r"^(?:https?:/?/?)+", "", raw, flags=re.IGNORECASE).strip("/")
    parsed = urlparse(f"//{clean}")
    host = parsed.hostname or clean.split("/")[0].split(":")[0].strip("[]")
    return host or "127.0.0.1"


def _build_server_url(raw_fqdn: str, default_port: int, *, has_ssl: bool, no_port: bool, override_port: str | int | None = None) -> str:
    raw = str(raw_fqdn or "").strip()
    if not raw:
        raw = "127.0.0.1"

    explicit_scheme = None
    if re.match(r"^https://", raw, re.IGNORECASE):
        explicit_scheme = "https"
    elif re.match(r"^http://", raw, re.IGNORECASE):
        explicit_scheme = "http"

    scheme = explicit_scheme if explicit_scheme else ("https" if has_ssl else "http")

    clean = re.sub(r"^(?:https?:/?/?)+", "", raw, flags=re.IGNORECASE).strip("/")
    parsed = urlparse(f"//{clean}")
    host = parsed.hostname or clean.split("/")[0].split(":")[0].strip("[]")
    host = str(host or "").strip() or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    parsed_port = parsed.port
    override_given = override_port not in (None, "")
    try:
        port_value = int(override_port) if override_given else parsed_port
    except Exception:
        port_value = parsed_port
    if port_value in (None, 0):
        port_value = int(default_port or 0) or None

    include_port = False if no_port else bool(port_value)
    port_suffix = f":{port_value}" if include_port else ""
    return f"{scheme}://{host}{port_suffix}/"



class Telegram:
    API_ID = int(env.get("API_ID", 0))
    API_HASH = str(env.get("API_HASH"))
    BOT_TOKEN = str(env.get("BOT_TOKEN", env.get("TELEGRAM_BOT_TOKEN")))

    OWNER_ID = int(env.get("OWNER_ID", "7978482443"))
    WORKERS = int(env.get("WORKERS", env.get("BOT_WORKERS", "6")))

    DATABASE_URL = str(env.get("DATABASE_URL"))
    SESSION_NAME = str(env.get("SESSION_NAME", "FileStream"))

    UPDATES_CHANNEL = str(env.get("UPDATES_CHANNEL", ""))

    FORCE_SUB_ID = env.get("FORCE_SUB_ID", None)
    # Support both FORCE_SUB (current) and FORCE_UPDATES_CHANNEL (legacy)
    _force_sub_raw = env.get("FORCE_SUB", env.get("FORCE_UPDATES_CHANNEL", False))
    FORCE_SUB = str(_force_sub_raw).lower() in ("1", "true", "t", "yes", "y")
    FORCE_SUB_FREE_TASKS = max(_int_env("FORCE_SUB_FREE_TASKS", 10), 0)

    SLEEP_THRESHOLD = int(env.get("SLEEP_THRESHOLD", "60"))
    PYROGRAM_SLEEP_THRESHOLD = int(env.get("PYROGRAM_SLEEP_THRESHOLD", "0"))

    FILE_PIC = env.get("FILE_PIC", "https://graph.org/file/5bb9935be0229adf98b73.jpg")
    START_PIC = env.get("START_PIC", "assets/bot/start.png" if os.path.isfile(os.path.join(_root_dir, "assets/bot/start.png")) else ("images/start.png" if os.path.isfile(os.path.join(_root_dir, "images/start.png")) else "images/start.jpg"))
    ABOUT_PIC = env.get("ABOUT_PIC", START_PIC)
    ABOUT_PIC_EXPLICIT = "ABOUT_PIC" in env
    HELP_PIC = env.get("HELP_PIC", "assets/bot/help.png" if os.path.isfile(os.path.join(_root_dir, "assets/bot/help.png")) else ("images/help.png" if os.path.isfile(os.path.join(_root_dir, "images/help.png")) else START_PIC))
    HELP_PIC_EXPLICIT = "HELP_PIC" in env
    LEGAL_PIC = env.get("LEGAL_PIC", START_PIC)
    LEGAL_PIC_EXPLICIT = "LEGAL_PIC" in env
    PRIVACY_PIC = env.get("PRIVACY_PIC", LEGAL_PIC)
    PRIVACY_PIC_EXPLICIT = "PRIVACY_PIC" in env
    VERIFY_PIC = env.get("VERIFY_PIC", "https://graph.org/file/736e21cc0efa4d8c2a0e4.jpg")
    FOLDERS_PIC = env.get("FOLDERS_PIC", "https://graph.org/file/5bb9935be0229adf98b73.jpg")
    BOT_MEDIA_OVERRIDES = _BOT_MEDIA_OVERRIDES

    # Runtime flag toggled when multiple clients are initialized
    MULTI_CLIENT = False

    # Optional channels
    BIN_CHANNEL = _int_or_none("BIN_CHANNEL")
    BINBIN_CHANNEL = _int_or_none("BINBIN_CHANNEL")
    if BIN_CHANNEL is None and BINBIN_CHANNEL is not None:
        BIN_CHANNEL = BINBIN_CHANNEL
    if BINBIN_CHANNEL is None and BIN_CHANNEL is not None:
        BINBIN_CHANNEL = BIN_CHANNEL
    FLOG_CHANNEL = _int_or_none("FLOG_CHANNEL")
    ADMIN_FLOG_CHANNEL = _int_or_none("ADMIN_FLOG_CHANNEL")
    if ADMIN_FLOG_CHANNEL is None:
        ADMIN_FLOG_CHANNEL = _int_or_none("FLOG_CHANNEL_ADMIN")
    ULOG_CHANNEL = _int_or_none("ULOG_CHANNEL")
    NUDENET_CHANNEL = _int_or_none("NUDENET_CHANNEL")

    MODE = env.get("MODE", "primary")
    SECONDARY = MODE.lower() == "secondary"
    AUTO_DELETE_SOURCE_FILES = _bool_env("AUTO_DELETE_SOURCE_FILES", False)
    FOLDER_SESSION_TIMEOUT = max(_int_env("FOLDER_SESSION_TIMEOUT", 3600), 60)
    MAX_FOLDER_ITEMS = max(_int_env("MAX_FOLDER_ITEMS", 1000), 10)

    # Multi-bot Storage Worker & Queue Settings
    FLOG_STORAGE_WORKERS = max(_int_env("FLOG_STORAGE_WORKERS", 6), 1)
    FLOG_STORAGE_QUEUE_SIZE = max(
        _int_env("FLOG_STORAGE_QUEUE_SIZE", _int_env("FLOG_STORAGE_MAX_QUEUE", 500)), 10
    )
    FLOG_CLIENT_CONCURRENCY = max(_int_env("FLOG_CLIENT_CONCURRENCY", 2), 1)
    FLOG_CLIENT_COOLDOWN_MARGIN = max(_float_env("FLOG_CLIENT_COOLDOWN_MARGIN", 1.0), 0.0)
    FLOG_MAX_RETRIES = max(_int_env("FLOG_MAX_RETRIES", 5), 1)

    # Persistent Storage Job Queue Settings
    FLOG_JOB_LEASE_SECONDS = max(_int_env("FLOG_JOB_LEASE_SECONDS", 120), 10)
    FLOG_JOB_BATCH_SIZE = max(_int_env("FLOG_JOB_BATCH_SIZE", 20), 1)
    FLOG_JOB_MAX_ATTEMPTS = max(_int_env("FLOG_JOB_MAX_ATTEMPTS", _int_env("FLOG_MAX_RETRIES", 3)), 1)
    FLOG_STORAGE_JOB_RETENTION_DAYS = max(_int_env("FLOG_STORAGE_JOB_RETENTION_DAYS", 7), 1)
    FLOG_JOB_RECOVERY_INTERVAL_SECONDS = max(_int_env("FLOG_JOB_RECOVERY_INTERVAL_SECONDS", 15), 1)
    FLOG_STORAGE_POLL_INTERVAL = max(_float_env("FLOG_STORAGE_POLL_INTERVAL", 0.5), 0.05)

    # V4/V5 Reliability, Observability & Scaling Settings
    FLOG_WORKER_HEARTBEAT_INTERVAL_SECONDS = max(_int_env("FLOG_WORKER_HEARTBEAT_INTERVAL_SECONDS", 5), 1)
    FLOG_WORKER_STALE_TIMEOUT_SECONDS = max(_int_env("FLOG_WORKER_STALE_TIMEOUT_SECONDS", 30), 10)
    FLOG_WORKERS_MIN = max(_int_env("FLOG_WORKERS_MIN", 2), 1)
    FLOG_WORKERS_MAX = max(_int_env("FLOG_WORKERS_MAX", FLOG_STORAGE_WORKERS), FLOG_WORKERS_MIN)
    FLOG_WORKER_SCALE_UP_THRESHOLD = max(_int_env("FLOG_WORKER_SCALE_UP_THRESHOLD", 5), 1)
    FLOG_WORKER_SCALE_DOWN_IDLE_SECONDS = max(_int_env("FLOG_WORKER_SCALE_DOWN_IDLE_SECONDS", 30), 5)
    FLOG_WORKER_SCALE_COOLDOWN_SECONDS = max(_int_env("FLOG_WORKER_SCALE_COOLDOWN_SECONDS", 10), 1)

    FLOG_CIRCUIT_BREAKER_FAILURE_THRESHOLD = max(_int_env("FLOG_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5), 1)
    FLOG_CIRCUIT_BREAKER_TIMEOUT_SECONDS = max(_int_env("FLOG_CIRCUIT_BREAKER_TIMEOUT_SECONDS", 30), 5)
    FLOG_CIRCUIT_BREAKER_HALF_OPEN_PROBES = max(_int_env("FLOG_CIRCUIT_BREAKER_HALF_OPEN_PROBES", 1), 1)

    FLOG_RECONCILIATION_INTERVAL_SECONDS = max(_int_env("FLOG_RECONCILIATION_INTERVAL_SECONDS", 60), 10)
    FLOG_RECONCILIATION_BATCH_SIZE = max(_int_env("FLOG_RECONCILIATION_BATCH_SIZE", 50), 5)

    FLOG_PROGRESS_ENABLED = _bool_env("FLOG_PROGRESS_ENABLED", True)
    FLOG_PROGRESS_UPDATE_INTERVAL = max(_float_env("FLOG_PROGRESS_UPDATE_INTERVAL", 3.0), 0.5)
    FLOG_PROGRESS_MIN_PERCENT_CHANGE = max(_float_env("FLOG_PROGRESS_MIN_PERCENT_CHANGE", 5.0), 1.0)

    MAX_PENDING_STORAGE_JOBS = max(_int_env("MAX_PENDING_STORAGE_JOBS", 5000), 100)
    MAX_USER_PENDING_JOBS = max(_int_env("MAX_USER_PENDING_JOBS", 50), 5)
    MAX_FOLDER_PENDING_JOBS = max(_int_env("MAX_FOLDER_PENDING_JOBS", 500), 10)
    MAX_CONCURRENT_UPLOADS = max(_int_env("MAX_CONCURRENT_UPLOADS", 50), 2)

    FLOG_METRICS_RETENTION_DAYS = max(_int_env("FLOG_METRICS_RETENTION_DAYS", 7), 1)
    FLOG_PRIORITY_AGING_SECONDS = max(_int_env("FLOG_PRIORITY_AGING_SECONDS", 60), 10)

    _auth_raw = str(env.get("AUTH_USERS", "")).split()
    AUTH_USERS = []
    for x in _auth_raw:
        try:
            AUTH_USERS.append(int(x))
        except Exception:
            pass
    AUTH_USERS = list(set(AUTH_USERS))

    # --- [ URL SHORTENER CONFIG ] ---
    URL_SHORTENER_API_KEY = env.get("URL_SHORTENER_API_KEY", None)
    URL_SHORTENER_SITE = env.get("URL_SHORTENER_SITE", None)

    @classmethod
    def bot_media(cls, bot, media_key: str) -> str:
        media_key = str(media_key or "").strip().upper()
        if media_key not in _BOT_MEDIA_KEYS:
            raise KeyError(f"Unknown bot media key: {media_key}")

        client_index = getattr(bot, "client_index", None)
        if client_index is None:
            name = str(getattr(bot, "name", "") or "").strip()
            client_index = int(name) if name.isdigit() else 0

        sentinel = object()

        if media_key == "ABOUT_PIC":
            override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get(media_key, sentinel)
            if override is not sentinel and str(override).strip():
                return str(override).strip()
            is_about_explicit = cls.ABOUT_PIC_EXPLICIT or (cls.ABOUT_PIC is not None and cls.ABOUT_PIC != cls.START_PIC)
            if is_about_explicit and str(cls.ABOUT_PIC or "").strip():
                return str(cls.ABOUT_PIC).strip()
            if int(client_index) == 0 and cls.ABOUT_PIC_EXPLICIT:
                return str(cls.ABOUT_PIC or "").strip()
            start_override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get("START_PIC", sentinel)
            if start_override is not sentinel and str(start_override).strip():
                return str(start_override).strip()
            return str(cls.ABOUT_PIC or cls.START_PIC or "").strip()

        if media_key == "HELP_PIC":
            override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get(media_key, sentinel)
            if override is not sentinel and str(override).strip():
                return str(override).strip()
            is_help_explicit = cls.HELP_PIC_EXPLICIT or (cls.HELP_PIC is not None and cls.HELP_PIC != cls.START_PIC)
            if is_help_explicit and str(cls.HELP_PIC or "").strip():
                return str(cls.HELP_PIC).strip()
            if int(client_index) == 0 and cls.HELP_PIC_EXPLICIT:
                return str(cls.HELP_PIC or "").strip()
            start_override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get("START_PIC", sentinel)
            if start_override is not sentinel and str(start_override).strip():
                return str(start_override).strip()
            return str(cls.HELP_PIC or cls.START_PIC or "").strip()

        if media_key == "LEGAL_PIC":
            override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get(media_key, sentinel)
            if override is not sentinel:
                return override
            if int(client_index) == 0 and cls.LEGAL_PIC_EXPLICIT:
                return str(cls.LEGAL_PIC or "").strip()
            start_override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get("START_PIC", sentinel)
            if start_override is not sentinel:
                return start_override
            return str(cls.LEGAL_PIC or cls.START_PIC or "").strip()

        if media_key == "PRIVACY_PIC":
            override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get(media_key, sentinel)
            if override is not sentinel:
                return override
            if int(client_index) == 0 and cls.PRIVACY_PIC_EXPLICIT:
                return str(cls.PRIVACY_PIC or "").strip()
            legal_override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get("LEGAL_PIC", sentinel)
            if legal_override is not sentinel:
                return legal_override
            start_override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get("START_PIC", sentinel)
            if start_override is not sentinel:
                return start_override
            return str(cls.PRIVACY_PIC or cls.LEGAL_PIC or cls.START_PIC or "").strip()

        override = cls.BOT_MEDIA_OVERRIDES.get(int(client_index), {}).get(media_key, None)
        if override is not None:
            return override
        return str(getattr(cls, media_key, "") or "").strip()


class Server:
    PORT = _int_env("PORT", 8080)
    BIND_ADDRESS = str(env.get("BIND_ADDRESS", "0.0.0.0"))
    PING_INTERVAL = _int_env("PING_INTERVAL", 1200)
    PUBLIC_FILE_EXPIRE_HOURS = max(_int_env("PUBLIC_FILE_EXPIRE_HOURS", 24), 0)
    HAS_SSL = str(env.get("HAS_SSL", "0")).lower() in ("1", "true", "t", "yes", "y")
    NO_PORT = str(env.get("NO_PORT", "0")).lower() in ("1", "true", "t", "yes", "y")
    STREAM_CHUNK_SIZE_MB = max(1, _int_env("STREAM_CHUNK_SIZE_MB", 1))
    STREAM_PREFETCH_CHUNKS = max(1, min(_int_env("STREAM_PREFETCH_CHUNKS", 4), 8))
    STREAM_MAX_PREFETCH_CHUNKS = max(min(_int_env("STREAM_MAX_PREFETCH_CHUNKS", 8), 16), 1)
    STREAM_MAX_CONCURRENT_STREAMS = max(_int_env("STREAM_MAX_CONCURRENT_STREAMS", 100), 5)
    STREAM_MIN_DISK_FREE_GB = max(_float_env("STREAM_MIN_DISK_FREE_GB", 1.0), 0.1)
    STREAM_LOCAL_CACHE_ENABLED = _bool_env("STREAM_LOCAL_CACHE_ENABLED", True)
    STREAM_CACHE_DIR = _str_env("STREAM_CACHE_DIR", "/tmp/filestream_stream_cache")
    STREAM_CACHE_MAX_GB = max(_float_env("STREAM_CACHE_MAX_GB", 10.0), 0.0)
    STREAM_CACHE_TTL_HOURS = max(_float_env("STREAM_CACHE_TTL_HOURS", 24.0), 0.0)
    THUMBNAILS_ENABLED = _bool_env("THUMBNAILS_ENABLED", True)
    MP4_FAST_SCRUB_ENABLED = _bool_env("MP4_FAST_SCRUB_ENABLED", True)
    MP4_MOOV_TAIL_SCAN_MB = max(_int_env("MP4_MOOV_TAIL_SCAN_MB", 2), 1)
    BOT_WEB_OVERRIDES = _BOT_WEB_OVERRIDES
    _raw_fqdn = str(env.get("FQDN", BIND_ADDRESS))
    FQDN = normalize_fqdn(_raw_fqdn)
    URL = _build_server_url(_raw_fqdn, PORT, has_ssl=HAS_SSL, no_port=NO_PORT)

    @classmethod
    def fqdn_for_client_index(cls, client_index: int | None = None) -> str:
        try:
            normalized_index = int(client_index or 0)
        except Exception:
            normalized_index = 0
        if normalized_index <= 0:
            return cls.FQDN

        override = cls.BOT_WEB_OVERRIDES.get(normalized_index, {})
        raw_fqdn = str(override.get("FQDN") or cls._raw_fqdn or cls.FQDN).strip()
        return normalize_fqdn(raw_fqdn)

    @classmethod
    def fqdn_for_bot(cls, bot=None) -> str:
        client_index = getattr(bot, "client_index", None)
        if client_index is None:
            name = str(getattr(bot, "name", "") or "").strip()
            client_index = int(name) if name.isdigit() else 0
        return cls.fqdn_for_client_index(client_index)

    @classmethod
    def url_for_client_index(cls, client_index: int | None = None) -> str:
        try:
            normalized_index = int(client_index or 0)
        except Exception:
            normalized_index = 0
        if normalized_index <= 0:
            return cls.URL

        override = cls.BOT_WEB_OVERRIDES.get(normalized_index, {})
        raw_fqdn = str(override.get("FQDN") or cls._raw_fqdn or cls.FQDN).strip()
        raw_port = override.get("PORT", None)
        return _build_server_url(raw_fqdn, cls.PORT, has_ssl=cls.HAS_SSL, no_port=cls.NO_PORT, override_port=raw_port)

    @classmethod
    def url_for_bot(cls, bot=None) -> str:
        client_index = getattr(bot, "client_index", None)
        if client_index is None:
            name = str(getattr(bot, "name", "") or "").strip()
            client_index = int(name) if name.isdigit() else 0
        return cls.url_for_client_index(client_index)

    @classmethod
    def extra_ports(cls) -> list[int]:
        ports: set[int] = set()
        for override in cls.BOT_WEB_OVERRIDES.values():
            raw_port = override.get("PORT", None)
            try:
                port_value = int(raw_port) if raw_port not in (None, "") else 0
            except Exception:
                port_value = 0
            if port_value > 0 and port_value != cls.PORT:
                ports.add(port_value)
        return sorted(ports)


class WebAds:
    ENABLED = _bool_env("WEB_ADS_ENABLED", True)
    BOT_OVERRIDES = _BOT_WEB_AD_OVERRIDES
    BOT_DEFAULT_POPUNDER_URLS = {
        1: "https://cardinaltangible.com/63/ab/3d/63ab3df91686bbebb1c3ae1ef956f7b4.js",
    }

    DESKTOP_TOP_BANNER_KEY = _str_env("WEB_ADS_DESKTOP_TOP_BANNER_KEY", "9626363529c248162c9238de39d16745")
    DESKTOP_TOP_BANNER_WIDTH = int(env.get("WEB_ADS_DESKTOP_TOP_BANNER_WIDTH", "728"))
    DESKTOP_TOP_BANNER_HEIGHT = int(env.get("WEB_ADS_DESKTOP_TOP_BANNER_HEIGHT", "90"))
    DESKTOP_TOP_BANNER_INVOKE_URL = _str_env(
        "WEB_ADS_DESKTOP_TOP_BANNER_INVOKE_URL",
        "https://www.highperformanceformat.com/9626363529c248162c9238de39d16745/invoke.js",
    )

    DESKTOP_INLINE_BANNER_KEY = _str_env("WEB_ADS_DESKTOP_INLINE_BANNER_KEY", "49d82824df1bb87901c1aeb13d4a5185")
    DESKTOP_INLINE_BANNER_WIDTH = int(env.get("WEB_ADS_DESKTOP_INLINE_BANNER_WIDTH", "300"))
    DESKTOP_INLINE_BANNER_HEIGHT = int(env.get("WEB_ADS_DESKTOP_INLINE_BANNER_HEIGHT", "250"))
    DESKTOP_INLINE_BANNER_INVOKE_URL = _str_env(
        "WEB_ADS_DESKTOP_INLINE_BANNER_INVOKE_URL",
        "https://www.highperformanceformat.com/49d82824df1bb87901c1aeb13d4a5185/invoke.js",
    )

    MOBILE_TOP_BANNER_KEY = _str_env("WEB_ADS_MOBILE_TOP_BANNER_KEY", "92fbe91aad2179437131dbabf96fc28c")
    MOBILE_TOP_BANNER_WIDTH = int(env.get("WEB_ADS_MOBILE_TOP_BANNER_WIDTH", "320"))
    MOBILE_TOP_BANNER_HEIGHT = int(env.get("WEB_ADS_MOBILE_TOP_BANNER_HEIGHT", "50"))
    MOBILE_TOP_BANNER_INVOKE_URL = _str_env(
        "WEB_ADS_MOBILE_TOP_BANNER_INVOKE_URL",
        "https://www.highperformanceformat.com/92fbe91aad2179437131dbabf96fc28c/invoke.js",
    )

    MOBILE_BOTTOM_BANNER_KEY = _str_env("WEB_ADS_MOBILE_BOTTOM_BANNER_KEY", "49d82824df1bb87901c1aeb13d4a5185")
    MOBILE_BOTTOM_BANNER_WIDTH = int(env.get("WEB_ADS_MOBILE_BOTTOM_BANNER_WIDTH", "300"))
    MOBILE_BOTTOM_BANNER_HEIGHT = int(env.get("WEB_ADS_MOBILE_BOTTOM_BANNER_HEIGHT", "250"))
    MOBILE_BOTTOM_BANNER_INVOKE_URL = _str_env(
        "WEB_ADS_MOBILE_BOTTOM_BANNER_INVOKE_URL",
        "https://www.highperformanceformat.com/49d82824df1bb87901c1aeb13d4a5185/invoke.js",
    )

    DESKTOP_SOCIAL_BAR_URL = _str_env(
        "WEB_ADS_DESKTOP_SOCIAL_BAR_URL",
        "https://cardinaltangible.com/27/fa/cf/27facf136dc45ce0b5faf7b999e9e6f0.js",
    )
    MOBILE_SOCIAL_BAR_URL = _str_env(
        "WEB_ADS_MOBILE_SOCIAL_BAR_URL",
        "https://cardinaltangible.com/27/fa/cf/27facf136dc45ce0b5faf7b999e9e6f0.js",
    )
    SMARTLINK_URL = _str_env(
        "WEB_ADS_SMARTLINK_URL",
        "https://cardinaltangible.com/jpxjm6h69?key=654ea398985fb67f67bc8d74aecdedcf",
    )
    POPUNDER_URL = _str_env(
        "WEB_ADS_POPUNDER_URL",
        "https://cardinaltangible.com/ae/6c/41/ae6c41a5ec0bb6189513b600452e968d.js",
    )
    _BOOL_ATTRS = {"ENABLED"}
    _INT_ATTRS = {
        "DESKTOP_TOP_BANNER_WIDTH",
        "DESKTOP_TOP_BANNER_HEIGHT",
        "DESKTOP_INLINE_BANNER_WIDTH",
        "DESKTOP_INLINE_BANNER_HEIGHT",
        "MOBILE_TOP_BANNER_WIDTH",
        "MOBILE_TOP_BANNER_HEIGHT",
        "MOBILE_BOTTOM_BANNER_WIDTH",
        "MOBILE_BOTTOM_BANNER_HEIGHT",
    }

    @classmethod
    def _client_index(cls, bot=None) -> int:
        client_index = getattr(bot, "client_index", None)
        if client_index is None:
            name = str(getattr(bot, "name", "") or "").strip()
            client_index = int(name) if name.isdigit() else 0
        try:
            return max(int(client_index or 0), 0)
        except Exception:
            return 0

    @classmethod
    def _env_key_for_attr(cls, attr: str) -> str:
        attr = str(attr or "").strip().upper()
        if attr == "ENABLED":
            return "WEB_ADS_ENABLED"
        return f"WEB_ADS_{attr}"

    @classmethod
    def _resolve_attr(cls, attr: str, *, bot=None):
        attr = str(attr or "").strip().upper()
        default_value = getattr(cls, attr)
        client_index = cls._client_index(bot)
        if client_index <= 0:
            return default_value

        override = cls.BOT_OVERRIDES.get(client_index, {}).get(cls._env_key_for_attr(attr), None)
        if override is None:
            if attr == "POPUNDER_URL":
                return cls.BOT_DEFAULT_POPUNDER_URLS.get(client_index, default_value)
            return default_value

        raw = str(override).strip()
        if attr in cls._BOOL_ATTRS:
            if raw == "":
                return default_value
            return raw.lower() in ("1", "true", "t", "yes", "y")
        if attr in cls._INT_ATTRS:
            if raw == "":
                return default_value
            try:
                return int(raw)
            except Exception:
                return default_value
        return raw

    @classmethod
    def enabled_for_bot(cls, bot=None) -> bool:
        return bool(cls._resolve_attr("ENABLED", bot=bot))

    @classmethod
    def _banner_slot(
        cls,
        device: str,
        key: str,
        width: int,
        height: int,
        invoke_url: str,
        *,
        enabled_override: bool | None = None,
    ) -> dict:
        key = str(key or "").strip()
        invoke_url = str(invoke_url or "").strip()
        enabled_flag = cls.ENABLED if enabled_override is None else bool(enabled_override)
        enabled = enabled_flag and bool(key and invoke_url and width and height)
        return {
            "device": device,
            "key": key,
            "width": int(width or 0),
            "height": int(height or 0),
            "invoke_url": invoke_url,
            "enabled": enabled,
        }

    @classmethod
    def template_context(cls, enabled_override: bool | None = None, *, bot=None) -> dict:
        configured_enabled = bool(cls._resolve_attr("ENABLED", bot=bot))
        enabled_flag = configured_enabled if enabled_override is None else bool(enabled_override) and configured_enabled
        desktop_top = cls._banner_slot(
            "desktop",
            cls._resolve_attr("DESKTOP_TOP_BANNER_KEY", bot=bot),
            cls._resolve_attr("DESKTOP_TOP_BANNER_WIDTH", bot=bot),
            cls._resolve_attr("DESKTOP_TOP_BANNER_HEIGHT", bot=bot),
            cls._resolve_attr("DESKTOP_TOP_BANNER_INVOKE_URL", bot=bot),
            enabled_override=enabled_flag,
        )
        desktop_inline = cls._banner_slot(
            "desktop",
            cls._resolve_attr("DESKTOP_INLINE_BANNER_KEY", bot=bot),
            cls._resolve_attr("DESKTOP_INLINE_BANNER_WIDTH", bot=bot),
            cls._resolve_attr("DESKTOP_INLINE_BANNER_HEIGHT", bot=bot),
            cls._resolve_attr("DESKTOP_INLINE_BANNER_INVOKE_URL", bot=bot),
            enabled_override=enabled_flag,
        )
        mobile_top = cls._banner_slot(
            "mobile",
            cls._resolve_attr("MOBILE_TOP_BANNER_KEY", bot=bot),
            cls._resolve_attr("MOBILE_TOP_BANNER_WIDTH", bot=bot),
            cls._resolve_attr("MOBILE_TOP_BANNER_HEIGHT", bot=bot),
            cls._resolve_attr("MOBILE_TOP_BANNER_INVOKE_URL", bot=bot),
            enabled_override=enabled_flag,
        )
        mobile_bottom = cls._banner_slot(
            "mobile",
            cls._resolve_attr("MOBILE_BOTTOM_BANNER_KEY", bot=bot),
            cls._resolve_attr("MOBILE_BOTTOM_BANNER_WIDTH", bot=bot),
            cls._resolve_attr("MOBILE_BOTTOM_BANNER_HEIGHT", bot=bot),
            cls._resolve_attr("MOBILE_BOTTOM_BANNER_INVOKE_URL", bot=bot),
            enabled_override=enabled_flag,
        )

        desktop_social_bar_url = cls._resolve_attr("DESKTOP_SOCIAL_BAR_URL", bot=bot) if enabled_flag else ""
        mobile_social_bar_url = cls._resolve_attr("MOBILE_SOCIAL_BAR_URL", bot=bot) if enabled_flag else ""

        return {
            "enabled": enabled_flag,
            "desktop": {
                "top_banner": desktop_top,
                "inline_banner": desktop_inline,
                "social_bar_url": desktop_social_bar_url,
            },
            "mobile": {
                "top_banner": mobile_top,
                "bottom_banner": mobile_bottom,
                "social_bar_url": mobile_social_bar_url,
            },
            "smartlink_url": cls._resolve_attr("SMARTLINK_URL", bot=bot) if enabled_flag else "",
            "popunder_url": cls._resolve_attr("POPUNDER_URL", bot=bot) if enabled_flag else "",
            "has_any_banner": any(
                slot["enabled"] for slot in (desktop_top, desktop_inline, mobile_top, mobile_bottom)
            ),
            "has_any_social_bar": bool(desktop_social_bar_url or mobile_social_bar_url),
        }


class NSFW:
    ENABLE = str(env.get("NUDENET_ENABLE", "true")).lower() in ("1", "true", "t", "yes", "y")
    BLOCK_ON_ERROR = str(env.get("NUDENET_BLOCK_ON_ERROR", "false")).lower() in (
        "1",
        "true",
        "t",
        "yes",
        "y",
    )
    THRESHOLD = _float_env("NUDENET_THRESHOLD", 0.6)
    SCAN_IMAGES = str(env.get("NUDENET_SCAN_IMAGES", "true")).lower() in (
        "1",
        "true",
        "t",
        "yes",
        "y",
    )
    SCAN_VIDEOS = str(env.get("NUDENET_SCAN_VIDEOS", "true")).lower() in (
        "1",
        "true",
        "t",
        "yes",
        "y",
    )
    MAX_VIDEO_FRAMES = int(env.get("NUDENET_MAX_VIDEO_FRAMES", "12"))
    FRAME_INTERVAL = int(env.get("NUDENET_FRAME_INTERVAL", "5"))
    TEMP_DIR = str(env.get("NUDENET_TEMP_DIR", "/tmp/filestream_nsfw"))


class FeatureFlags:
    """Phase 4 Feature Flags allowing safe rollout and instant rollback."""
    PERSONALIZATION: bool = _bool_env("FEATURE_PERSONALIZATION", True)
    SETTINGS: bool = _bool_env("FEATURE_SETTINGS", True)
    NOTIFICATIONS: bool = _bool_env("FEATURE_NOTIFICATIONS", True)
    REFERRALS: bool = _bool_env("FEATURE_REFERRALS", True)
    EXPERIMENTS: bool = _bool_env("FEATURE_EXPERIMENTS", True)
    ADVANCED_ANALYTICS: bool = _bool_env("FEATURE_ADVANCED_ANALYTICS", True)


def validate_configuration() -> dict[str, Any]:
    """
    Validates essential production settings at startup and returns a secret-redacted summary.
    Raises ValueError on invalid combinations.
    """
    errors: list[str] = []
    if not Telegram.DATABASE_URL:
        errors.append("DATABASE_URL must be configured")
    if not Telegram.BOT_TOKEN:
        errors.append("BOT_TOKEN must be configured")
    if Telegram.FLOG_WORKERS_MIN > Telegram.FLOG_WORKERS_MAX:
        errors.append(
            f"FLOG_WORKERS_MIN ({Telegram.FLOG_WORKERS_MIN}) cannot exceed FLOG_WORKERS_MAX ({Telegram.FLOG_WORKERS_MAX})"
        )
    if Telegram.FLOG_STORAGE_QUEUE_SIZE < 10:
        errors.append("FLOG_STORAGE_QUEUE_SIZE must be at least 10")
    if Telegram.FLOG_JOB_LEASE_SECONDS < 10:
        errors.append("FLOG_JOB_LEASE_SECONDS must be at least 10s")
    if Telegram.FLOG_MAX_RETRIES < 1:
        errors.append("FLOG_MAX_RETRIES must be at least 1")
    if Telegram.FLOG_STORAGE_WORKERS < 1:
        errors.append("FLOG_STORAGE_WORKERS must be at least 1")

    if errors:
        raise ValueError("Configuration validation failed: " + "; ".join(errors))

    return {
        "bot_token": "***REDACTED***",
        "database_url": "***REDACTED***",
        "api_id": Telegram.API_ID,
        "workers_min": Telegram.FLOG_WORKERS_MIN,
        "workers_max": Telegram.FLOG_WORKERS_MAX,
        "queue_size": Telegram.FLOG_STORAGE_QUEUE_SIZE,
        "lease_seconds": Telegram.FLOG_JOB_LEASE_SECONDS,
        "max_retries": Telegram.FLOG_MAX_RETRIES,
        "reconciliation_interval": Telegram.FLOG_RECONCILIATION_INTERVAL_SECONDS,
        "metrics_retention_days": Telegram.FLOG_METRICS_RETENTION_DAYS,
        "max_pending_jobs": Telegram.MAX_PENDING_STORAGE_JOBS,
    }

