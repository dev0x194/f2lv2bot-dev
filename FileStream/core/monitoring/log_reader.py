import asyncio
import html
import logging
import os
import re
from typing import NamedTuple
from urllib.parse import unquote

logger = logging.getLogger(__name__)

# Default log file path relative to repo root
DEFAULT_LOG_PATH = "streambot.log"

LOG_CATEGORY_TITLES = {
    "bot": "🤖 Bot Logs",
    "web": "🌐 Web Logs",
    "error": "❌ Error Logs",
    "storage": "💾 Storage & Folders",
    "admin": "👤 Admin Action Logs",
    "security": "🔐 Security Logs",
    "performance": "⚡ Performance Logs",
}

# Regex to identify web scanner probes and sensitive-path enumeration
RE_SCANNER_PROBE = re.compile(
    r"(?i)(?:"
    r"/\.(?:env|docker|aws|git|ssh|kube|config|npmrc)\b|"
    r"/backend/\.env|"
    r"/(?:actuator|php_info|phpinfo|wp-login|wp-admin|xmlrpc\.php|setup\.php|pma|phpmyadmin|install\.php)\b|"
    r"\.(?:php|asp|aspx|jsp|cgi)(?:[\s\?&'\"]|$)"
    r")"
)

# Regex to match aiohttp access log line: <ip> - - [<date>] "<method> <path> <proto>" <status> <bytes>
RE_AIOHTTP_ACCESS = re.compile(
    r'^(?P<ip>[0-9a-fA-F\.:]+)'
    r'(?:\s+-\s+-\s*|\s+-\s*|-|\s+)'
    r'\[.*?\]\s+'
    r'"(?P<req>[^"]+)"\s+'
    r'(?P<status>\d+)'
)

# Regex to identify explicit performance / latency metrics
RE_PERF_METRICS = re.compile(
    r"(?i)\b(?:took\s+\d+(?:\.\d+)?\s*(?:ms|s)|duration=\d+(?:\.\d+)?\s*(?:ms|s)|latency=\d+(?:\.\d+)?\s*(?:ms|s)|queue\s+depth=\d+)"
)

# Regex to match log header: [%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s
RE_LOG_HEADER = re.compile(
    r"^\[(?P<asctime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(?P<levelname>[A-Z]+)\] \[(?P<name>[^\]]+)\]: (?P<message>.*)$"
)

# Regex patterns for sensitive secret redaction
RE_BOT_TOKEN = re.compile(r"\b\d{8,11}:[A-Za-z0-9_-]{35}\b")
RE_MONGO_CREDENTIALS = re.compile(r"(mongodb(?:\+srv)?:\/\/)[^\s@]+@")
RE_SESSION_STRING = re.compile(r"\b[A-Za-z0-9+/=]{50,}\b")
RE_API_HASH = re.compile(r"(?i)(api_hash\s*[:=]\s*['\"]?)[a-fA-F0-9]{32}(['\"]?)")
RE_GENERIC_SECRET = re.compile(r"(?i)(['\"]?[a-z0-9_]*(?:secret|password|api_key)['\"]?\s*[:=]\s*['\"]?)[^'\"\s]{8,}(['\"]?)")


def redact_secrets(text: str) -> str:
    """
    Redact sensitive tokens, database credentials, API hashes, and session strings.
    """
    if not text:
        return text

    # 1. Telegram Bot Tokens
    text = RE_BOT_TOKEN.sub("[BOT_TOKEN_REDACTED]", text)

    # 2. MongoDB Credentials
    text = RE_MONGO_CREDENTIALS.sub(r"\1[REDACTED_CREDENTIALS]@", text)

    # 3. API Hashes
    text = RE_API_HASH.sub(r"\1[REDACTED]\2", text)

    # 4. Generic secrets / passwords
    text = RE_GENERIC_SECRET.sub(r"\1[REDACTED]\2", text)

    # 5. Telegram MTProto session strings (50+ base64 chars)
    text = RE_SESSION_STRING.sub("[SESSION_REDACTED]", text)

    # 6. Direct Config value redaction
    try:
        from FileStream.config import Telegram, Server

        bot_token = getattr(Telegram, "BOT_TOKEN", None)
        if bot_token and len(str(bot_token)) >= 10:
            text = text.replace(str(bot_token), "[BOT_TOKEN_REDACTED]")

        api_hash = getattr(Telegram, "API_HASH", None)
        if api_hash and len(str(api_hash)) >= 10:
            text = text.replace(str(api_hash), "[API_HASH_REDACTED]")

        session_name = getattr(Telegram, "SESSION_NAME", None)
        if session_name and len(str(session_name)) >= 20:
            text = text.replace(str(session_name), "[SESSION_REDACTED]")

        multi_session = getattr(Telegram, "MULTI_CLIENT_SESSION", None)
        if multi_session and len(str(multi_session)) >= 20:
            text = text.replace(str(multi_session), "[SESSION_REDACTED]")

        db_url = getattr(Server, "DATABASE_URL", None)
        if db_url and "@" in str(db_url):
            cred_match = re.search(r"//([^:]+:[^@]+)@", str(db_url))
            if cred_match:
                text = text.replace(cred_match.group(1), "[REDACTED_CREDENTIALS]")
    except Exception:
        pass

    return text


class ParsedLogRecord(NamedTuple):
    asctime: str
    levelname: str
    name: str
    message: str
    lines: list[str]

    def full_text(self) -> str:
        return "\n".join(self.lines)


def _format_security_probe(rec: ParsedLogRecord) -> str:
    """
    Formats a crawler / scanner probe log record cleanly for Telegram display.
    """
    m = RE_AIOHTTP_ACCESS.search(rec.message)
    if m:
        ip = m.group("ip")
        req = m.group("req")
        status = m.group("status")
        return f"[{rec.asctime}] [INFO] [FileStream.security]: [SECURITY] Sensitive-path probe: {req} from {ip} (HTTP {status})"
    return f"[{rec.asctime}] [INFO] [FileStream.security]: [SECURITY] Sensitive-path probe: {rec.message}"


def _matches_category(rec: ParsedLogRecord, category: str) -> bool:
    cat = (category or "").lower().strip()
    if cat == "all":
        return True

    name = rec.name
    full = rec.full_text()
    full_lower = full.lower()
    unquoted = unquote(full)

    if cat == "bot":
        is_bot_logger = (
            name.startswith("FileStream.bot")
            or name.startswith("FileStream.utils")
            or name.startswith("FileStream.config")
            or name.startswith("pyrogram")
            or name in ("FileStream", "__main__")
        )
        is_server_or_storage = (
            name.startswith("FileStream.server")
            or name.startswith("FileStream.services.storage_service")
            or name.startswith("FileStream.services.folder_service")
            or name == "FileStream.admin"
        )
        return is_bot_logger and not is_server_or_storage

    if cat == "web":
        # Exclude scanner/crawler probes from legitimate web logs
        if RE_SCANNER_PROBE.search(unquoted):
            return False
        return (
            name.startswith("aiohttp")
            or name.startswith("FileStream.server")
            or name.startswith("FileStream.services.stream_cache")
            or name.startswith("FileStream.utils.custom_dl")
            or "server" in name.lower()
        )

    if cat == "error":
        return (
            rec.levelname in ("ERROR", "CRITICAL")
            or "Traceback (most recent call last):" in full
            or "Exception" in full
            or (rec.levelname == "WARNING" and "error" in full_lower)
        )

    if cat == "storage":
        return (
            name.startswith("FileStream.services.storage_service")
            or name.startswith("FileStream.services.folder_service")
            or name.startswith("FileStream.utils.flog")
            or "[FLOG]" in full
            or "storage_service" in name
            or "folder_service" in name
            or "flog_storage" in full
        )

    if cat == "admin":
        return (
            name == "FileStream.admin"
            or "[ADMIN_ACTION]" in full
            or name.startswith("FileStream.bot.plugins.admin")
            or name.startswith("FileStream.bot.handlers.admin")
        )

    if cat == "security":
        if RE_SCANNER_PROBE.search(unquoted):
            return True
        if name == "FileStream.admin" and (
            "UNAUTHORIZED" in full
            or "ACCESS_DENIED" in full
        ):
            return True
        if "[security]" in full_lower or "[security_alert]" in full_lower:
            return True
        return False

    if cat == "performance":
        if "floodwait" in full_lower or "flood wait" in full_lower or "startup floodwait" in full_lower:
            return True
        if "speedtest" in full_lower or name.startswith("FileStream.utils.speedtest"):
            return True
        if "cooldown" in full_lower or "rotation delay" in full_lower or "rate limit" in full_lower:
            return True
        if "queue depth" in full_lower or "workload" in full_lower or "benchmark" in full_lower:
            return True
        if RE_PERF_METRICS.search(full):
            return True
        return False

    return False


def tail_log_file(
    filepath: str | None = None,
    category: str = "bot",
    max_entries: int = 15,
    max_bytes_to_read: int = 512 * 1024,
) -> list[str]:
    """
    Tails streambot.log from SEEK_END without loading the full file into memory.
    Returns matching log records newest-first with secret redaction applied.
    """
    path = filepath or os.environ.get("STREAMBOT_LOG_PATH", DEFAULT_LOG_PATH)
    results: list[str] = []

    # If admin or security category, also fetch relevant in-memory audit actions
    in_memory_audit_entries: list[str] = []
    if category in ("admin", "security"):
        try:
            from FileStream.core.monitoring.admin_audit import get_recent_admin_actions
            recent = get_recent_admin_actions(limit=max_entries)
            if category == "admin":
                in_memory_audit_entries = recent
            elif category == "security":
                in_memory_audit_entries = [
                    e for e in recent
                    if any(k in e for k in ("UNAUTHORIZED", "ACCESS_DENIED", "SECURITY", "BANNED", "banned"))
                ]
        except Exception:
            pass

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        if category in ("admin", "security") and in_memory_audit_entries:
            return [redact_secrets(e) for e in in_memory_audit_entries[:max_entries]]
        return []

    try:
        file_size = os.path.getsize(path)
        bytes_to_read = min(file_size, max_bytes_to_read)
        with open(path, "rb") as f:
            if file_size > bytes_to_read:
                f.seek(file_size - bytes_to_read)
            raw_data = f.read(bytes_to_read)

        text = raw_data.decode("utf-8", errors="replace")
        # If we didn't read from byte 0, strip partial first line
        if file_size > bytes_to_read:
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]

        lines = text.splitlines()
        records: list[ParsedLogRecord] = []
        current_header: re.Match | None = None
        current_lines: list[str] = []

        for line in lines:
            m = RE_LOG_HEADER.match(line)
            if m:
                if current_header is not None:
                    records.append(
                        ParsedLogRecord(
                            asctime=current_header.group("asctime"),
                            levelname=current_header.group("levelname"),
                            name=current_header.group("name"),
                            message=current_header.group("message"),
                            lines=current_lines,
                        )
                    )
                current_header = m
                current_lines = [line]
            else:
                if current_header is not None:
                    current_lines.append(line)

        if current_header is not None:
            records.append(
                ParsedLogRecord(
                    asctime=current_header.group("asctime"),
                    levelname=current_header.group("levelname"),
                    name=current_header.group("name"),
                    message=current_header.group("message"),
                    lines=current_lines,
                )
            )

        # Reverse to get newest first
        records.reverse()

        for rec in records:
            if _matches_category(rec, category):
                if category == "security" and RE_SCANNER_PROBE.search(unquote(rec.full_text())):
                    results.append(_format_security_probe(rec))
                else:
                    results.append(rec.full_text())
                if len(results) >= max_entries:
                    break

    except Exception as exc:
        logger.warning("Error reading log file %s: %s", path, exc)

    # For admin & security categories, merge in-memory entries if available
    if category in ("admin", "security") and in_memory_audit_entries:
        merged = []
        seen = set()
        for item in in_memory_audit_entries + results:
            cleaned = item.strip()
            if cleaned not in seen:
                seen.add(cleaned)
                merged.append(cleaned)
            if len(merged) >= max_entries:
                break
        results = merged

    return [redact_secrets(r) for r in results]


def _safe_truncate_record(raw: str, max_escaped_len: int) -> str:
    """
    Truncates raw text BEFORE HTML-escaping to prevent splitting HTML entities.
    Guarantees the escaped result + truncation suffix stays within max_escaped_len.
    """
    suffix = "\n[...truncated...]"
    budget = max_escaped_len - len(suffix)
    if budget <= 0:
        return suffix

    chunk = raw[:budget]
    low, high = 0, len(chunk)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = html.escape(chunk[:mid])
        if len(candidate) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best + suffix


def format_records_for_telegram(
    records: list[str],
    max_chars: int = 3200,
    max_entries: int = 15,
) -> tuple[str, int]:
    """
    Format and HTML-escape log records for preformatted Telegram display.
    Guarantees output stays under max_chars and never splits HTML entities.
    Returns (formatted_string, count).
    """
    if not records:
        return "", 0

    selected: list[str] = []
    total_len = 0
    count = 0

    for rec in records[:max_entries]:
        escaped = html.escape(rec)
        item_len = len(escaped) + (2 if selected else 0)  # newline buffer between entries
        if total_len + item_len > max_chars:
            if not selected:
                # Single record is very long, safely truncate raw text before escaping
                truncated = _safe_truncate_record(rec, max_chars)
                selected.append(truncated)
                count += 1
            else:
                selected.append("[...older entries omitted for length...]")
            break
        selected.append(escaped)
        total_len += item_len
        count += 1

    formatted_text = "\n\n".join(selected)
    return formatted_text, count


async def get_logs_for_category(
    category: str,
    max_entries: int = 15,
    filepath: str | None = None,
) -> tuple[str, int]:
    """
    Non-blocking async tail and format for a specific log category.
    Returns (formatted_html_pre_text, entry_count).
    """
    records = await asyncio.to_thread(tail_log_file, filepath, category, max_entries)
    return format_records_for_telegram(records, max_chars=3200, max_entries=max_entries)
