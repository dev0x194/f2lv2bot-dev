import asyncio
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pyrogram.errors import MessageNotModified, MessageIdInvalid

from FileStream.config import Telegram
from FileStream.bot.messages import AdminMessages, ErrorMessages
from FileStream.bot.plugins.admin import (
    _build_admin_panel_markup,
    _build_logs_menu_markup,
    _build_log_view_markup,
    _safe_edit_admin_message,
    _show_admin_panel,
    clear_active_admin_messages_for_testing,
    get_active_admin_message_id,
    set_active_admin_message_id,
    admin_panel_callback,
    admin_help,
    OWNER_ID,
    ADMIN_IDS,
)
from FileStream.utils.admin_audit import (
    log_admin_action,
    get_recent_admin_actions,
    clear_admin_audit_log_for_testing,
)
from FileStream.utils.log_reader import (
    redact_secrets,
    tail_log_file,
    format_records_for_telegram,
    get_logs_for_category,
    LOG_CATEGORY_TITLES,
)


# ============================================================
# 1. ROOT ADMIN DASHBOARD MARKUP TESTS
# ============================================================

@pytest.mark.asyncio
async def test_owner_root_markup_has_logs_and_no_home():
    """Verify Owner root markup contains Logs button and Home button is removed."""
    owner_id = OWNER_ID
    markup = await _build_admin_panel_markup(owner_id)

    all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    all_texts = [btn.text for row in markup.inline_keyboard for btn in row]

    # Home button must NOT be present
    assert "home" not in all_callbacks, "Found 'home' callback in Owner admin panel"
    for text in all_texts:
        assert "Home" not in text, f"Found 'Home' in button text: {text}"

    # Logs button MUST be present
    assert "admin:logs" in all_callbacks, "Expected 'admin:logs' callback in Owner admin panel"
    assert any("Logs" in text for text in all_texts), "Expected 'Logs' in button text for Owner"

    # Owner-only menus must be present
    assert "admin:menu:users" in all_callbacks
    assert "admin:menu:links" in all_callbacks


@pytest.mark.asyncio
async def test_admin_root_markup_has_logs_and_no_home():
    """Verify non-owner Admin root markup contains Logs button and Home button is removed."""
    admin_id = 999999999  # Non-owner
    markup = await _build_admin_panel_markup(admin_id)

    all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    all_texts = [btn.text for row in markup.inline_keyboard for btn in row]

    # Home button must NOT be present
    assert "home" not in all_callbacks, "Found 'home' callback in Admin panel"
    for text in all_texts:
        assert "Home" not in text, f"Found 'Home' in button text: {text}"

    # Logs button MUST be present
    assert "admin:logs" in all_callbacks, "Expected 'admin:logs' callback in Admin panel"
    assert any("Logs" in text for text in all_texts), "Expected 'Logs' in button text for Admin"

    # Owner-only menus must NOT be present for non-owner admin
    assert "admin:menu:users" not in all_callbacks
    assert "admin:menu:links" not in all_callbacks


# ============================================================
# 2. LOGS MENU & VIEW MARKUP TESTS
# ============================================================

def test_logs_menu_markup_buttons():
    """Verify _build_logs_menu_markup contains all 7 categories and navigation."""
    markup = _build_logs_menu_markup()
    all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    expected_callbacks = [
        "admin:logs:view:bot",
        "admin:logs:view:web",
        "admin:logs:view:error",
        "admin:logs:view:storage",
        "admin:logs:view:admin",
        "admin:logs:view:security",
        "admin:logs:view:performance",
        "admin:logs:menu",
        "admin:menu:main",
        "close",
    ]
    for cb in expected_callbacks:
        assert cb in all_callbacks, f"Expected callback {cb} in logs menu markup"


def test_log_view_markup_buttons():
    """Verify _build_log_view_markup contains category refresh, categories, admin panel, and close."""
    category = "storage"
    markup = _build_log_view_markup(category)
    all_callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert f"admin:logs:refresh:{category}" in all_callbacks
    assert "admin:logs:menu" in all_callbacks
    assert "admin:menu:main" in all_callbacks
    assert "close" in all_callbacks


# ============================================================
# 3. PRESENTATION LAYER (AdminMessages)
# ============================================================

def test_admin_messages_logs_templates():
    """Verify AdminMessages text formatting helpers."""
    # Panel text
    panel_text = AdminMessages.logs_panel_text()
    assert "📋 <b>Admin Logs</b>" in panel_text
    assert "Bot Logs" in panel_text
    assert "Web Logs" in panel_text
    assert "Error Logs" in panel_text
    assert "Storage & Folders" in panel_text
    assert "Admin Actions" in panel_text
    assert "Security Logs" in panel_text
    assert "Performance Logs" in panel_text

    # View text
    view_text = AdminMessages.logs_view_text("🤖 Bot Logs", "Sample log output line", count=1)
    assert "🤖 Bot Logs" in view_text
    assert "(Recent 1)" in view_text
    assert '<pre><code class="language-log">Sample log output line</code></pre>' in view_text

    # Empty text
    empty_text = AdminMessages.logs_empty_text("❌ Error Logs")
    assert "❌ Error Logs" in empty_text
    assert "No log entries found" in empty_text


# ============================================================
# 4. SECRET REDACTION TESTS
# ============================================================

def test_redact_secrets_bot_token():
    raw = "Started bot with token: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789 successfully."
    redacted = redact_secrets(raw)
    assert "[BOT_TOKEN_REDACTED]" in redacted
    assert "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz123456789" not in redacted


def test_redact_secrets_mongo_uri():
    raw = "Connecting to mongodb+srv://super_admin:MySuperSecretPass!123@cluster.mongodb.net/testdb?retryWrites=true"
    redacted = redact_secrets(raw)
    assert "[REDACTED_CREDENTIALS]@" in redacted
    assert "MySuperSecretPass!123" not in redacted
    assert "super_admin" not in redacted


def test_redact_secrets_api_hash():
    raw = "Client config: api_id=12345, api_hash='abcdef0123456789abcdef0123456789', bot_token=..."
    redacted = redact_secrets(raw)
    assert "api_hash='[REDACTED]'" in redacted or "api_hash=\"[REDACTED]\"" in redacted or "[REDACTED]" in redacted
    assert "abcdef0123456789abcdef0123456789" not in redacted


def test_redact_secrets_session_string():
    session = "1BVtsOLIBu3c94r2wHk9Pq3x5y7z9a1b3c5d7e9f1g3h5i7j9k1l3m5n7o9p1q3r5s7t9u1v3w5x7y9z"
    raw = f"Loaded MTProto session: {session} successfully"
    redacted = redact_secrets(raw)
    assert "[SESSION_REDACTED]" in redacted
    assert session not in redacted


# ============================================================
# 5. NON-BLOCKING LOG TAIL & FILTERING TESTS
# ============================================================

def test_tail_log_file_missing_and_empty():
    """Verify missing and empty log files are handled without exceptions."""
    # Missing file
    res = tail_log_file("nonexistent_test_log.log", category="bot")
    assert res == []

    # Empty file
    with tempfile.NamedTemporaryFile("w+", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = tail_log_file(tmp_path, category="bot")
        assert res == []
    finally:
        os.remove(tmp_path)


def test_tail_log_file_category_filtering_and_multiline():
    """Verify log categorization, ordering (newest first), and multiline tracebacks across 7 categories."""
    sample_logs = [
        "[2026-09-19 10:00:00] [INFO] [FileStream.bot.plugins.start]: User 12345 started the bot",
        "[2026-09-19 10:01:00] [INFO] [aiohttp.access]: 127.0.0.1 \"GET /watch/abc HTTP/1.1\" 200 1024",
        "[2026-09-19 10:02:00] [INFO] [FileStream.services.storage_service]: [FLOG] Uploaded file 555 to main channel",
        "[2026-09-19 10:02:30] [INFO] [FileStream.services.folder_service]: Folder 777 created with 3 files",
        "[2026-09-19 10:03:00] [INFO] [FileStream.admin]: [ADMIN_ACTION] user=111 action=BAN_USER details=target=222",
        "[2026-09-19 10:04:00] [ERROR] [FileStream.bot.plugins.admin]: Unhandled failure in speedtest\nTraceback (most recent call last):\n  File \"admin.py\", line 100\nValueError: speedtest failed",
        "[2026-09-19 10:05:00] [INFO] [FileStream.bot.plugins.admin]: Admin panel accessed by 111",
        "[2026-09-19 10:06:00] [INFO] [aiohttp.access]: 192.168.1.100 [19/Sep/2026:10:06:00 +0000] \"GET /.env HTTP/1.1\" 404 150",
        "[2026-09-19 10:07:00] [WARNING] [FileStream]: Startup FloodWait remaining: 15s",
        "[2026-09-19 10:08:00] [INFO] [FileStream.utils.speedtest]: Speedtest completed in 5.2s",
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(sample_logs) + "\n")
        tmp_path = tmp.name

    try:
        # 1. Bot Logs
        bot_logs = tail_log_file(tmp_path, category="bot", max_entries=10)
        assert len(bot_logs) == 5
        assert any("Admin panel accessed by 111" in line for line in bot_logs)
        assert any("Unhandled failure in speedtest" in line for line in bot_logs)
        assert any("User 12345 started the bot" in line for line in bot_logs)

        # 2. Web Logs (scanner probe excluded, legitimate /watch/ retained)
        web_logs = tail_log_file(tmp_path, category="web", max_entries=10)
        assert len(web_logs) == 1
        assert "GET /watch/abc" in web_logs[0]
        assert not any(".env" in line for line in web_logs)

        # 3. Storage Logs (both storage_service and folder_service included)
        storage_logs = tail_log_file(tmp_path, category="storage", max_entries=10)
        assert len(storage_logs) == 2
        assert any("Folder 777 created" in line for line in storage_logs)
        assert any("Uploaded file 555" in line for line in storage_logs)

        # 4. Admin Logs
        admin_logs = tail_log_file(tmp_path, category="admin", max_entries=10)
        assert any("BAN_USER" in line for line in admin_logs)

        # 5. Error Logs (with multiline traceback)
        error_logs = tail_log_file(tmp_path, category="error", max_entries=10)
        assert len(error_logs) == 1
        assert "Traceback (most recent call last):" in error_logs[0]
        assert "ValueError: speedtest failed" in error_logs[0]

        # 6. Security Logs (probe detected and formatted cleanly)
        security_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(security_logs) == 1
        assert "[SECURITY] Sensitive-path probe:" in security_logs[0]
        assert "GET /.env HTTP/1.1 from 192.168.1.100 (HTTP 404)" in security_logs[0]

        # 7. Performance Logs (FloodWait delays and speedtest timing/runs)
        perf_logs = tail_log_file(tmp_path, category="performance", max_entries=10)
        assert len(perf_logs) == 3
        assert any("Speedtest completed" in line for line in perf_logs)
        assert any("FloodWait" in line for line in perf_logs)

    finally:
        os.remove(tmp_path)


def test_web_logs_excludes_scanner_probes_completely():
    """Verify that multiple automated scanner probe patterns are filtered out from web logs."""
    probes = [
        "[2026-09-19 11:00:00] [INFO] [aiohttp.access]: 10.0.0.1 - - [19/Sep/2026:11:00:00 +0000] \"GET /.env HTTP/1.1\" 404 100",
        "[2026-09-19 11:01:00] [INFO] [aiohttp.access]: 10.0.0.2 - - [19/Sep/2026:11:01:00 +0000] \"GET /.docker/config.json HTTP/1.1\" 404 100",
        "[2026-09-19 11:02:00] [INFO] [aiohttp.access]: 10.0.0.3 - - [19/Sep/2026:11:02:00 +0000] \"GET /php_info.php HTTP/1.1\" 404 100",
        "[2026-09-19 11:03:00] [INFO] [aiohttp.access]: 10.0.0.4 - - [19/Sep/2026:11:03:00 +0000] \"GET /wp-login.php HTTP/1.1\" 404 100",
        "[2026-09-19 11:04:00] [INFO] [aiohttp.access]: 10.0.0.5 - - [19/Sep/2026:11:04:00 +0000] \"GET /dl/legitimate_file_123 HTTP/1.1\" 200 5000",
        "[2026-09-19 11:05:00] [INFO] [aiohttp.access]: 10.0.0.6 - - [19/Sep/2026:11:05:00 +0000] \"GET /stream/67890 HTTP/1.1\" 200 8000",
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(probes) + "\n")
        tmp_path = tmp.name

    try:
        web_logs = tail_log_file(tmp_path, category="web", max_entries=10)
        assert len(web_logs) == 2
        assert any("legitimate_file_123" in line for line in web_logs)
        assert any("stream/67890" in line for line in web_logs)

        sec_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(sec_logs) == 4
        for log in sec_logs:
            assert "[SECURITY] Sensitive-path probe:" in log
    finally:
        os.remove(tmp_path)


def test_security_logs_merges_in_memory_unauthorized_access():
    """Verify in-memory UNAUTHORIZED_ACCESS audit entries are merged into security logs."""
    clear_admin_audit_log_for_testing()
    log_admin_action(999999, "UNAUTHORIZED_ACCESS", "command /admin rejected (ACCESS_DENIED)")
    log_admin_action(888888, "UNAUTHORIZED_ACCESS", "callback admin:menu:config rejected (ACCESS_DENIED)")

    sec_logs = tail_log_file("nonexistent.log", category="security", max_entries=5)
    assert len(sec_logs) == 2
    assert any("999999" in log and "UNAUTHORIZED_ACCESS" in log for log in sec_logs)
    assert any("888888" in log and "UNAUTHORIZED_ACCESS" in log for log in sec_logs)
    clear_admin_audit_log_for_testing()


def test_format_records_for_telegram_escaping_and_truncation():
    """Verify HTML escaping and length truncation."""
    # Test HTML escaping
    records = ["<script>alert('xss')</script> & safe"]
    formatted, count = format_records_for_telegram(records, max_chars=1000)
    assert count == 1
    assert "&lt;script&gt;" in formatted
    assert "&amp; safe" in formatted
    assert "<script>" not in formatted

    # Test truncation
    long_records = [f"[2026-09-19 10:00:0{i}] [INFO] [FileStream]: " + "X" * 100 for i in range(50)]
    formatted, count = format_records_for_telegram(long_records, max_chars=500)
    assert len(formatted) <= 500
    assert "[...older entries omitted for length...]" in formatted


# ============================================================
# 6. ADMIN ACTION AUDIT LOGGING TESTS
# ============================================================

def test_admin_audit_logger_and_ring_buffer():
    """Verify log_admin_action records events in memory and limits to maxlen."""
    clear_admin_audit_log_for_testing()

    log_admin_action(12345, "SHORTENER_TOGGLE", "state=ON")
    log_admin_action(12345, "BAN_USER", "target_user=67890")

    actions = get_recent_admin_actions(limit=10)
    assert len(actions) == 2
    # Newest first
    assert "BAN_USER" in actions[0]
    assert "SHORTENER_TOGGLE" in actions[1]

    # Test clearing
    clear_admin_audit_log_for_testing()
    assert get_recent_admin_actions() == []


# ============================================================
# 7. CALLBACK ROUTING & PERMISSION TESTS
# ============================================================

@pytest.mark.asyncio
async def test_admin_panel_callback_denies_unauthorized_user():
    """Non-admin user invoking admin:logs is denied with ACCESS_DENIED alert and logged."""
    clear_admin_audit_log_for_testing()
    unauth_user_id = 9876543210
    assert unauth_user_id not in ADMIN_IDS

    query = MagicMock()
    query.from_user.id = unauth_user_id
    query.data = "admin:logs"
    query.answer = AsyncMock()

    client = MagicMock()
    await admin_panel_callback(client, query)

    query.answer.assert_called_once_with(ErrorMessages.ACCESS_DENIED, show_alert=True)
    actions = get_recent_admin_actions(limit=5)
    assert any("UNAUTHORIZED_ACCESS" in a and str(unauth_user_id) in a for a in actions)
    clear_admin_audit_log_for_testing()


@pytest.mark.asyncio
async def test_admin_help_command_denies_and_logs_unauthorized():
    """Unauthorized user calling /admin command is rejected and logged."""
    clear_admin_audit_log_for_testing()
    unauth_user_id = 9876543210
    assert unauth_user_id not in ADMIN_IDS

    message = MagicMock()
    message.from_user.id = unauth_user_id
    message.reply_text = AsyncMock()

    client = MagicMock()
    await admin_help(client, message)

    message.reply_text.assert_called_once_with(ErrorMessages.ACCESS_DENIED, quote=True)
    actions = get_recent_admin_actions(limit=5)
    assert any("UNAUTHORIZED_ACCESS" in a and str(unauth_user_id) in a for a in actions)
    clear_admin_audit_log_for_testing()


@pytest.mark.asyncio
async def test_admin_panel_callback_routes_to_logs_menu():
    """Authorized admin invoking admin:logs opens the logs menu."""
    admin_user_id = OWNER_ID

    query = MagicMock()
    query.from_user.id = admin_user_id
    query.data = "admin:logs"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.edit_text = AsyncMock()
    query.message.photo = None
    query.message.caption = None

    client = MagicMock()
    await admin_panel_callback(client, query)

    # Must edit the message with the logs panel text
    query.message.edit_text.assert_called_once()
    args, kwargs = query.message.edit_text.call_args
    assert "📋 <b>Admin Logs</b>" in kwargs["text"]
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_admin_panel_callback_routes_to_category_view_and_refresh():
    """Authorized admin invoking admin:logs:view:bot and refresh works."""
    admin_user_id = OWNER_ID

    # 1. View Bot Logs
    query = MagicMock()
    query.from_user.id = admin_user_id
    query.data = "admin:logs:view:bot"
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.edit_text = AsyncMock()
    query.message.photo = None
    query.message.caption = None

    client = MagicMock()
    await admin_panel_callback(client, query)

    query.message.edit_text.assert_called_once()
    args, kwargs = query.message.edit_text.call_args
    assert "🤖 Bot Logs" in kwargs["text"]

    # 2. Refresh Bot Logs
    query_refresh = MagicMock()
    query_refresh.from_user.id = admin_user_id
    query_refresh.data = "admin:logs:refresh:bot"
    query_refresh.answer = AsyncMock()
    query_refresh.message = MagicMock()
    query_refresh.message.edit_text = AsyncMock()
    query_refresh.message.photo = None
    query_refresh.message.caption = None

    await admin_panel_callback(client, query_refresh)

    query_refresh.answer.assert_called_once_with(AdminMessages.REFRESHED_ALERT)
    query_refresh.message.edit_text.assert_called_once()


# ============================================================
# 8. GLOBAL PUBLIC HOME CALLBACK INTEGRITY TEST
# ============================================================

def test_global_public_home_callback_preserved():
    """Verify that the global public 'home' callback in callback.py remains intact."""
    with open("FileStream/bot/plugins/callback.py", "r", encoding="utf-8") as f:
        callback_code = f.read()

    assert 'if usr_cmd[0] == "home":' in callback_code
    assert 'from FileStream.bot.plugins.start import show_home' in callback_code


# ============================================================
# 9. PHASE 2 REGRESSION & HARDENING TESTS
# ============================================================

def test_encoded_scanner_probe_classification():
    """Verify URL-encoded scanner probes (%2F.env, %2Fbackend%2F.env) match Security and are excluded from Web."""
    logs = [
        '[2026-09-19 12:00:00] [INFO] [aiohttp.access]: 192.168.1.100 - - [19/Sep/2026:12:00:00 +0000] "GET /%2F.env HTTP/1.1" 404 150',
        '[2026-09-19 12:01:00] [INFO] [aiohttp.access]: 192.168.1.101 - - [19/Sep/2026:12:01:00 +0000] "GET /%2Fbackend%2F.env HTTP/1.1" 404 150',
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(logs) + "\n")
        tmp_path = tmp.name

    try:
        # 1. Security logs should detect both probes
        sec_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(sec_logs) == 2
        assert any("GET /%2F.env HTTP/1.1" in log for log in sec_logs)
        assert any("GET /%2Fbackend%2F.env HTTP/1.1" in log for log in sec_logs)
        for log in sec_logs:
            assert "[SECURITY] Sensitive-path probe:" in log

        # 2. Web logs should completely exclude both probes
        web_logs = tail_log_file(tmp_path, category="web", max_entries=10)
        assert len(web_logs) == 0
    finally:
        os.remove(tmp_path)


def test_npmrc_scanner_classification():
    """Verify /.npmrc scanner probes are classified under Security and excluded from Web."""
    logs = [
        '[2026-09-19 12:00:00] [INFO] [aiohttp.access]: 10.0.0.5 - - [19/Sep/2026:12:00:00 +0000] "GET /.npmrc HTTP/1.1" 404 120',
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(logs) + "\n")
        tmp_path = tmp.name

    try:
        sec_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(sec_logs) == 1
        assert "[SECURITY] Sensitive-path probe:" in sec_logs[0]
        assert "GET /.npmrc HTTP/1.1" in sec_logs[0]

        web_logs = tail_log_file(tmp_path, category="web", max_entries=10)
        assert len(web_logs) == 0
    finally:
        os.remove(tmp_path)


def test_aiohttp_proxy_double_dash_access_format():
    """Verify aiohttp proxy access log format (IP - - [date]) extracts IP, req, and status cleanly."""
    logs = [
        '[2026-09-19 12:00:00] [INFO] [aiohttp.access]: 192.168.1.50 - - [19/Sep/2026:12:00:00 +0000] "GET /.env HTTP/1.1" 404 150',
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(logs) + "\n")
        tmp_path = tmp.name

    try:
        sec_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(sec_logs) == 1
        assert "[SECURITY] Sensitive-path probe: GET /.env HTTP/1.1 from 192.168.1.50 (HTTP 404)" in sec_logs[0]
        assert sec_logs[0].startswith("[2026-09-19 12:00:00] [INFO] [FileStream.security]:")
    finally:
        os.remove(tmp_path)


def test_html_entity_truncation_safety():
    """Verify that truncation never splits HTML entities like &amp; or produces broken entities."""
    import re

    # Create a long record with repeating special HTML chars
    raw_payload = "[INFO] Special chars: " + ("& < > \" ' " * 100)
    formatted, count = format_records_for_telegram([raw_payload], max_chars=250)

    # 1. Output must not exceed max_chars
    assert len(formatted) <= 250
    assert "[...truncated...]" in formatted

    # 2. Must not contain split HTML entities (e.g. &am or &#x without closing semicolon)
    # Check that any ampersand is followed by a valid entity name/number and a semicolon
    incomplete_entities = re.findall(r"&[a-zA-Z0-9#]+(?![a-zA-Z0-9#;])", formatted)
    assert not incomplete_entities, f"Found split HTML entities: {incomplete_entities}"


def test_security_false_positive_prevention():
    """Verify that legitimate filenames or messages containing 'banned' are NOT classified as Security."""
    logs = [
        '[2026-09-19 12:00:00] [INFO] [aiohttp.access]: 10.0.0.1 - - [19/Sep/2026:12:00:00 +0000] "GET /dl/banned_film.mp4 HTTP/1.1" 200 500000',
        '[2026-09-19 12:01:00] [INFO] [FileStream.bot]: The file is banned by administrator',
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(logs) + "\n")
        tmp_path = tmp.name

    try:
        sec_logs = tail_log_file(tmp_path, category="security", max_entries=10)
        assert len(sec_logs) == 0

        web_logs = tail_log_file(tmp_path, category="web", max_entries=10)
        assert len(web_logs) == 1
        assert "GET /dl/banned_film.mp4" in web_logs[0]
    finally:
        os.remove(tmp_path)


def test_performance_false_positive_prevention():
    """Verify media duration and casual 'took' statements are not misclassified as Performance, while metrics are."""
    logs = [
        # Non-performance logs containing words that previously caused false positives
        '[2026-09-19 12:00:00] [INFO] [FileStream.bot]: Stream duration: 120s for file 123',
        '[2026-09-19 12:01:00] [INFO] [FileStream.bot]: Bot took initiative to clean cache',
        '[2026-09-19 12:02:00] [INFO] [FileStream.bot]: The file duration is 10 seconds',
        # Valid performance logs
        '[2026-09-19 12:03:00] [INFO] [FileStream.bot]: speedtest completed in 120ms',
        '[2026-09-19 12:04:00] [INFO] [FileStream.bot]: queue depth=12 latency=85ms',
        '[2026-09-19 12:05:00] [INFO] [FileStream.bot]: FloodWait 30s triggered',
        '[2026-09-19 12:06:00] [INFO] [FileStream.bot]: client cooldown 5s active',
        '[2026-09-19 12:07:00] [INFO] [FileStream.bot]: query took 150ms to finish',
    ]
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tmp:
        tmp.write("\n".join(logs) + "\n")
        tmp_path = tmp.name

    try:
        perf_logs = tail_log_file(tmp_path, category="performance", max_entries=20)
        assert len(perf_logs) == 5
        # Ensure false positives are excluded
        assert not any("Stream duration: 120s" in log for log in perf_logs)
        assert not any("Bot took initiative" in log for log in perf_logs)
        assert not any("The file duration is 10 seconds" in log for log in perf_logs)
        # Ensure valid performance logs are included
        assert any("speedtest completed" in log for log in perf_logs)
        assert any("queue depth=12" in log for log in perf_logs)
        assert any("FloodWait 30s" in log for log in perf_logs)
        assert any("client cooldown 5s" in log for log in perf_logs)
        assert any("query took 150ms" in log for log in perf_logs)
    finally:
        os.remove(tmp_path)


# ============================================================
# 10. ADMIN LOGS MESSAGE LIFECYCLE & RESILIENCE TESTS
# ============================================================

@pytest.mark.asyncio
async def test_safe_edit_admin_message_valid_edit():
    """Test 1 — Valid edit: A valid admin message is edited successfully in-place."""
    clear_active_admin_messages_for_testing()
    query = MagicMock()
    query.from_user.id = OWNER_ID
    query.message = MagicMock()
    query.message.id = 500
    query.message.chat.id = OWNER_ID
    query.message.photo = None
    query.message.caption = None
    query.message.edit_text = AsyncMock(return_value=query.message)

    res = await _safe_edit_admin_message(query, text="Test valid edit", reply_markup=None)
    assert res is not None
    query.message.edit_text.assert_called_once()
    assert get_active_admin_message_id(OWNER_ID) == 500
    clear_active_admin_messages_for_testing()


@pytest.mark.asyncio
async def test_safe_edit_admin_message_message_id_invalid_recovery(caplog):
    """Test 2 — Message ID invalid: Recovers gracefully by sending fresh UI without error log pollution."""
    clear_active_admin_messages_for_testing()
    query = MagicMock()
    query.from_user.id = OWNER_ID
    query.message = MagicMock()
    query.message.id = 404
    query.message.chat.id = OWNER_ID
    query.message.photo = None
    query.message.caption = None
    # Simulate Telegram 400 MESSAGE_ID_INVALID
    query.message.edit_text = AsyncMock(
        side_effect=MessageIdInvalid("Telegram says: [400 MESSAGE_ID_INVALID] Pyrogram 2.3.69 thinks: The message id is invalid")
    )

    client = MagicMock()
    replacement_msg = MagicMock()
    replacement_msg.id = 777
    replacement_msg.chat.id = OWNER_ID
    client.send_message = AsyncMock(return_value=replacement_msg)

    with caplog.at_level("WARNING"):
        res = await _safe_edit_admin_message(query, text="Logs content", reply_markup=None, bot=client)

    # Must recover by sending a replacement message
    assert res is replacement_msg
    assert query.message is replacement_msg
    assert query.message.id == 777
    assert get_active_admin_message_id(OWNER_ID) == 777
    client.send_message.assert_called_once()

    # Must NOT pollute the log with WARNING/ERROR containing "error" or scary text
    assert not any("Error editing admin message" in r.message for r in caplog.records)
    clear_active_admin_messages_for_testing()


@pytest.mark.asyncio
async def test_safe_edit_admin_message_deleted_message_fallback():
    """Test 3 — Message deleted: Recovers when query.message was already removed or inaccessible."""
    clear_active_admin_messages_for_testing()
    query = MagicMock()
    query.from_user.id = OWNER_ID
    query.message = None  # Message completely deleted / missing from callback

    client = MagicMock()
    replacement_msg = MagicMock()
    replacement_msg.id = 888
    client.send_message = AsyncMock(return_value=replacement_msg)

    res = await _safe_edit_admin_message(query, text="Logs menu", reply_markup=None, bot=client)
    assert res is replacement_msg
    assert query.message is replacement_msg
    assert query.message.id == 888
    client.send_message.assert_called_once()
    clear_active_admin_messages_for_testing()


@pytest.mark.asyncio
async def test_safe_edit_admin_message_message_not_modified_no_error_log(caplog):
    """Test 4 — MessageNotModified: Handled silently without generating warning or error logs."""
    clear_active_admin_messages_for_testing()
    query = MagicMock()
    query.from_user.id = OWNER_ID
    query.message = MagicMock()
    query.message.id = 501
    query.message.chat.id = OWNER_ID
    query.message.photo = None
    query.message.caption = None
    query.message.edit_text = AsyncMock(side_effect=MessageNotModified("Message is not modified"))

    client = MagicMock()
    client.send_message = AsyncMock()

    with caplog.at_level("WARNING"):
        res = await _safe_edit_admin_message(query, text="Unchanged text", reply_markup=None, bot=client)

    assert res is query.message
    client.send_message.assert_not_called()
    assert len(caplog.records) == 0
    clear_active_admin_messages_for_testing()


@pytest.mark.asyncio
async def test_admin_navigation_after_fallback():
    """Test 5 — Navigation after fallback: Subsequent callbacks navigate using the replacement message."""
    clear_active_admin_messages_for_testing()
    admin_user_id = OWNER_ID

    # Step 1: User is on a stale message that raises MessageIdInvalid when viewing Error Logs
    stale_query = MagicMock()
    stale_query.from_user.id = admin_user_id
    stale_query.data = "admin:logs:view:error"
    stale_query.answer = AsyncMock()
    stale_query.message = MagicMock()
    stale_query.message.id = 100
    stale_query.message.chat.id = admin_user_id
    stale_query.message.photo = None
    stale_query.message.caption = None
    stale_query.message.edit_text = AsyncMock(
        side_effect=MessageIdInvalid("Telegram says: [400 MESSAGE_ID_INVALID]")
    )

    client = MagicMock()
    replacement_msg = MagicMock()
    replacement_msg.id = 200
    replacement_msg.chat.id = admin_user_id
    replacement_msg.photo = None
    replacement_msg.caption = None
    replacement_msg.edit_text = AsyncMock()
    client.send_message = AsyncMock(return_value=replacement_msg)

    # Initial view triggers recovery to replacement_msg
    await admin_panel_callback(client, stale_query)
    client.send_message.assert_called_once()
    assert stale_query.message is replacement_msg
    assert get_active_admin_message_id(admin_user_id) == 200

    # Step 2: Next navigation (Refresh) on the replacement message operates on replacement_msg
    refresh_query = MagicMock()
    refresh_query.from_user.id = admin_user_id
    refresh_query.data = "admin:logs:refresh:error"
    refresh_query.answer = AsyncMock()
    refresh_query.message = replacement_msg

    await admin_panel_callback(client, refresh_query)
    # Refresh must edit replacement_msg directly, not call send_message again
    replacement_msg.edit_text.assert_called_once()
    assert client.send_message.call_count == 1

    # Step 3: Next navigation (Categories) operates on replacement_msg
    replacement_msg.edit_text.reset_mock()
    menu_query = MagicMock()
    menu_query.from_user.id = admin_user_id
    menu_query.data = "admin:logs:menu"
    menu_query.answer = AsyncMock()
    menu_query.message = replacement_msg

    await admin_panel_callback(client, menu_query)
    replacement_msg.edit_text.assert_called_once()
    assert client.send_message.call_count == 1

    # Step 4: Next navigation (Admin Panel) operates on replacement_msg
    replacement_msg.edit_text.reset_mock()
    panel_query = MagicMock()
    panel_query.from_user.id = admin_user_id
    panel_query.data = "admin:menu:main"
    panel_query.answer = AsyncMock()
    panel_query.message = replacement_msg

    with patch("FileStream.bot.plugins.admin._build_admin_panel_text", AsyncMock(return_value="Admin Dashboard")):
        await admin_panel_callback(client, panel_query)
    replacement_msg.edit_text.assert_called_once()
    assert client.send_message.call_count == 1
    clear_active_admin_messages_for_testing()



@pytest.mark.asyncio
async def test_safe_edit_admin_message_no_duplicate_messages():
    """Test 6 — No duplicate messages: If active panel exists, stale callbacks edit it rather than creating new messages."""
    clear_active_admin_messages_for_testing()
    chat_id = OWNER_ID

    # Record active admin message 300
    set_active_admin_message_id(chat_id, 300)

    client = MagicMock()
    client.edit_message_text = AsyncMock(return_value=MagicMock(id=300))
    client.send_message = AsyncMock()

    # User clicks stale callback with message ID 100
    stale_query = MagicMock()
    stale_query.from_user.id = chat_id
    stale_query.message = MagicMock()
    stale_query.message.id = 100
    stale_query.message.chat.id = chat_id
    stale_query.message.photo = None
    stale_query.message.caption = None
    stale_query.message.edit_text = AsyncMock(
        side_effect=MessageIdInvalid("Telegram says: [400 MESSAGE_ID_INVALID]")
    )

    res = await _safe_edit_admin_message(stale_query, text="Updated text", reply_markup=None, bot=client)
    assert res is not None
    # Must edit the tracked active message 300 instead of creating a new message
    client.edit_message_text.assert_called_once()
    client.send_message.assert_not_called()
    assert get_active_admin_message_id(chat_id) == 300
    clear_active_admin_messages_for_testing()


@pytest.mark.asyncio
async def test_show_admin_panel_deletes_prior_panel_to_prevent_duplicates():
    """Verify /admin deletes the prior tracked admin panel message if one existed."""
    clear_active_admin_messages_for_testing()
    chat_id = OWNER_ID
    set_active_admin_message_id(chat_id, 456)

    message = MagicMock()
    message.chat.id = chat_id
    message.from_user.id = chat_id
    sent_panel = MagicMock()
    sent_panel.id = 457
    message.reply_text = AsyncMock(return_value=sent_panel)

    client = MagicMock()
    client.delete_messages = AsyncMock()

    with patch("FileStream.bot.plugins.admin._build_admin_panel_text", AsyncMock(return_value="Admin Panel Text")), \
         patch("FileStream.bot.plugins.admin._build_admin_panel_markup", AsyncMock(return_value=MagicMock())):
        await _show_admin_panel(message, chat_id, bot=client)

    # Prior panel 456 must be deleted
    client.delete_messages.assert_called_once_with(chat_id=chat_id, message_ids=456)
    # New panel 457 must be tracked
    assert get_active_admin_message_id(chat_id) == 457
    clear_active_admin_messages_for_testing()
