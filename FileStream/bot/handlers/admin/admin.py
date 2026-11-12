import os
import time
import math
import shutil
import psutil
import string
import random
import asyncio
import aiofiles
import datetime
import html
import logging
import inspect
from typing import Any
from pyrogram import filters, Client
try:
    from pyrogram.errors import MessageNotModified, MessageIdInvalid
except ImportError:
    from pyrogram.errors import MessageNotModified
    MessageIdInvalid = None
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums.parse_mode import ParseMode

logger = logging.getLogger("FileStream.bot.plugins.admin")

import platform
import pyrogram
from FileStream import __version__ as BOT_VERSION
from FileStream.bot.helpers.broadcast_helper import send_msg
from FileStream.database.database import Database
from FileStream.bot import FileStream
from FileStream.server.exceptions import FileNotFound
from FileStream.config import Server, Telegram
from FileStream.core.runtime.human_readable import humanbytes
from FileStream.core.resilience.workload import get_workload_stats, workload_tracker
from FileStream.core.monitoring.system_info import (
    get_system_info,
    get_network_info,
    format_uptime,
    get_bot_process_resources,
)
from FileStream.core.monitoring.speedtest import (
    run_speedtest,
    format_speedtest,
    format_speedtest_detailed,
    format_speedtest_empty,
    format_speedtest_failure,
    format_speedtest_history,
    format_system_status,
    get_latest_speedtest,
    set_latest_speedtest,
    get_last_speedtest_at,
    get_speedtest_history,
    SPEEDTEST_COOLDOWN,
    MSG_SPEEDTEST_START,
    MSG_SPEEDTEST_ERROR,
)
from FileStream.core.runtime.file_cleanup import delete_file_entry
from FileStream.core.runtime.flog_channels import configured_flog_channels, resolve_active_flog_target
from FileStream.core.runtime.public_links import build_public_file_url, build_public_folder_url
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.core.monitoring.admin_audit import log_admin_action
from FileStream.core.monitoring.log_reader import get_logs_for_category, LOG_CATEGORY_TITLES
from FileStream.bot.messages import AdminMessages, ErrorMessages, FileMessages, PublicMessages, LANG
from FileStream.bot.clients import is_secondary_worker

speedtest_lock = asyncio.Lock()
_last_speedtest_at = 0
SPEEDTEST_COOLDOWN = 60

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
broadcast_ids = {}

# Record Bot Start Time
BOT_START_TIME = time.time()

def _build_admin_ids():
    seen = set()
    admin_ids = []
    for raw_id in [Telegram.OWNER_ID, *(Telegram.AUTH_USERS or [])]:
        try:
            admin_id = int(raw_id)
        except Exception:
            continue
        if admin_id in seen:
            continue
        seen.add(admin_id)
        admin_ids.append(admin_id)
    return admin_ids


ADMIN_IDS = _build_admin_ids()
OWNER_ID = int(Telegram.OWNER_ID)
OWNER_ONLY_ADMIN_ACTIONS = {
    "ban",
    "unban",
    "broadcast",
    "del",
    "linkinfo",
    "revoke_link",
    "regen_link",
    "expire_link",
    "status",
    "reconcile_flog",
    "drain_worker",
    "disable_client",
    "enable_client",
}

ADMIN_USAGE = {
    "flogstorage": "Usage: `/flogstorage <main|admin|status|list> [user_id]`",
    "rm_adult": "Usage: `/rm_adult <file_id|folder_id>`",
    "ban": "Usage: `/ban <user_id>`",
    "unban": "Usage: `/unban <user_id>`",
    "broadcast": "Usage: reply to a message with `/broadcast`",
    "del": "Usage: `/del <file_id>`",
    "linkinfo": "Usage: `/linkinfo <public_id|file:<id>|folder:<id>>`",
    "revoke_link": "Usage: `/revoke_link <public_id|file:<id>|folder:<id>>`",
    "regen_link": "Usage: `/regen_link <public_id|file:<id>|folder:<id>>`",
    "expire_link": "Usage: `/expire_link <public_id|file:<id>|folder:<id>> <now|clear|hours>`",
    "storage_jobs": "Usage: `/storage_jobs [status] [limit] [skip]`",
    "storage_job": "Usage: `/storage_job <job_id>`",
    "retry_storage_job": "Usage: `/retry_storage_job <job_id>`",
    "cancel_storage_job": "Usage: `/cancel_storage_job <job_id>`",
    "storage_queue": "Usage: `/storage_queue`",
    "reconcile_flog": "Usage: `/reconcile_flog [--dry-run]`",
    "worker_status": "Usage: `/worker_status`",
    "client_status": "Usage: `/client_status`",
    "drain_worker": "Usage: `/drain_worker <worker_id>`",
    "disable_client": "Usage: `/disable_client <client_idx>`",
    "enable_client": "Usage: `/enable_client <client_idx>`",
}


def _parse_ref(value: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    if ":" in raw:
        kind, target = raw.split(":", 1)
        kind = kind.strip().lower()
        if kind in {"file", "folder", "public"}:
            return kind, target.strip()
    return None, raw


def _public_link_url(link: dict) -> str:
    if link.get("type") == "folder":
        return build_public_folder_url(link.get("public_id"))
    return build_public_file_url(link.get("public_id"))


def _public_link_status(link: dict) -> str:
    if link.get("revoked"):
        return "revoked"
    expires_at = link.get("expires_at")
    if expires_at is not None and float(expires_at) <= time.time():
        return "expired"
    return "active"


def _default_storage_label(configured_storage: dict[str, int]) -> str:
    if configured_storage.get("main"):
        return "MAIN"
    if configured_storage.get("admin"):
        return "ADMIN (fallback)"
    return "NOT SET"


def _format_admin_storage_users(user_ids: list[int], limit: int = 10) -> str:
    if not user_ids:
        return "none"
    visible = ", ".join(str(user_id) for user_id in user_ids[:limit])
    if len(user_ids) > limit:
        return f"{visible}, +{len(user_ids) - limit} more"
    return visible


def _bot_label(bot: Client | None = None) -> str:
    username = get_bot_username(bot)
    if username:
        return f"@{username}"
    return get_bot_name(bot) or "this bot"


def _parse_storage_target(raw_value: str | None, fallback_user_id: int) -> int:
    if raw_value in (None, ""):
        return int(fallback_user_id)
    return int(str(raw_value).strip())


async def _resolve_link_reference(raw: str) -> tuple[str, str, dict]:
    kind, value = _parse_ref(raw)
    if not value:
        raise FileNotFound

    if kind == "public":
        link = await db.get_public_link(value)
        return link["type"], str(link["target_id"]), link

    if kind == "file":
        file_info = await db.get_file(value)
        link = await db.ensure_public_link_for_file(file_info)
        return "file", str(file_info["_id"]), link

    if kind == "folder":
        folder = await db.get_folder(value)
        link = await db.ensure_public_link_for_folder(folder)
        return "folder", str(folder["_id"]), link

    try:
        link = await db.get_public_link(value)
        return link["type"], str(link["target_id"]), link
    except FileNotFound:
        pass

    try:
        file_info = await db.get_file(value)
        link = await db.ensure_public_link_for_file(file_info)
        return "file", str(file_info["_id"]), link
    except FileNotFound:
        pass

    folder = await db.get_folder(value)
    link = await db.ensure_public_link_for_folder(folder)
    return "folder", str(folder["_id"]), link


async def _build_admin_panel_text(user_id: int, bot: Client | None = None) -> str:
    shortener_state = "ON" if await db.get_urlshortener_status() else "OFF"
    webads_state = "ON" if await db.get_web_ads_status(bot=bot) else "OFF"
    admin_flog_users = await db.get_admin_flog_user_ids()
    effective_storage_mode, _, _ = await resolve_active_flog_target(db, user_id=user_id)
    owner_mode = int(user_id) == OWNER_ID
    bot_label = html.escape(_bot_label(bot))

    return AdminMessages.panel_text(
        shortener_state=shortener_state,
        bot_label=bot_label,
        webads_state=webads_state,
        effective_storage_mode=effective_storage_mode,
        admin_user_count=len(admin_flog_users),
        owner_mode=owner_mode,
    )


async def _build_admin_panel_markup(user_id: int, bot: Client | None = None) -> InlineKeyboardMarkup:
    owner_mode = int(user_id) == OWNER_ID
    if owner_mode:
        rows = [
            [
                InlineKeyboardButton("⚙️ Configuration", callback_data="admin:menu:config"),
                InlineKeyboardButton("📊 Monitoring", callback_data="admin:menu:monitor"),
            ],
            [
                InlineKeyboardButton("👥 User Management", callback_data="admin:menu:users"),
                InlineKeyboardButton("🔗 Link Management", callback_data="admin:menu:links"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:main"),
                InlineKeyboardButton("📋 Logs", callback_data="admin:logs"),
            ],
            [
                InlineKeyboardButton("❌ Close", callback_data="close"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton("⚙️ Configuration", callback_data="admin:menu:config"),
                InlineKeyboardButton("📊 Monitoring", callback_data="admin:menu:monitor"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:main"),
                InlineKeyboardButton("📋 Logs", callback_data="admin:logs"),
            ],
            [
                InlineKeyboardButton("❌ Close", callback_data="close"),
            ],
        ]
    return InlineKeyboardMarkup(rows)


async def _build_config_markup(user_id: int, bot: Client | None = None) -> InlineKeyboardMarkup:
    shortener_on = bool(await db.get_urlshortener_status())
    webads_on = bool(await db.get_web_ads_status(bot=bot))
    flog_storage_mode, _, _ = await resolve_active_flog_target(db, user_id=user_id)
    is_admin_storage = flog_storage_mode.lower() == "admin"

    shortener_btn = (
        InlineKeyboardButton("🔴 Turn Off Shortener", callback_data="admin:urlshortener:off")
        if shortener_on
        else InlineKeyboardButton("🟢 Turn On Shortener", callback_data="admin:urlshortener:on")
    )
    webads_btn = (
        InlineKeyboardButton("🔴 Turn Off Web Ads", callback_data="admin:webads:off")
        if webads_on
        else InlineKeyboardButton("🟢 Turn On Web Ads", callback_data="admin:webads:on")
    )
    storage_btn = (
        InlineKeyboardButton("💾 Switch to Main Storage", callback_data="admin:flogstorage:main")
        if is_admin_storage
        else InlineKeyboardButton("🔐 Switch to Admin Storage", callback_data="admin:flogstorage:admin")
    )

    rows = [
        [shortener_btn],
        [webads_btn],
        [storage_btn],
        [InlineKeyboardButton("👥 Storage Channel Info", callback_data="admin:flogstorage:status")],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:config"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:main"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _build_monitoring_markup(user_id: int, bot: Client | None = None) -> InlineKeyboardMarkup:
    owner_mode = int(user_id) == OWNER_ID
    rows = []
    if owner_mode:
        rows.append([InlineKeyboardButton("📊 System Status", callback_data="admin:run:status")])
    rows.append([InlineKeyboardButton("🤖 Bot Status", callback_data="admin:run:botstatus")])
    rows.append([InlineKeyboardButton("🐛 Reports", callback_data="admin:help:rm_adult")])
    rows.append([
        InlineKeyboardButton("❌ Close", callback_data="close"),
        InlineKeyboardButton("◀️ Back", callback_data="admin:menu:main"),
    ])
    return InlineKeyboardMarkup(rows)


async def _build_user_mgmt_markup(user_id: int, bot: Client | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin:help:broadcast")],
        [
            InlineKeyboardButton("🚫 Restrict Access", callback_data="admin:help:ban"),
            InlineKeyboardButton("✅ Restore Access", callback_data="admin:help:unban"),
        ],
        [InlineKeyboardButton("🗑️ Delete File", callback_data="admin:help:del")],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:users"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:main"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def _build_link_mgmt_markup(user_id: int, bot: Client | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔍 Link Info", callback_data="admin:help:linkinfo"),
            InlineKeyboardButton("🔄 New Link", callback_data="admin:help:regen_link"),
        ],
        [
            InlineKeyboardButton("🗑️ Remove Link", callback_data="admin:help:revoke_link"),
            InlineKeyboardButton("⏳ Set Expiry", callback_data="admin:help:expire_link"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:links"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:main"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _build_logs_menu_markup() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🤖 Bot Logs", callback_data="admin:logs:view:bot"),
            InlineKeyboardButton("🌐 Web Logs", callback_data="admin:logs:view:web"),
        ],
        [
            InlineKeyboardButton("❌ Error Logs", callback_data="admin:logs:view:error"),
            InlineKeyboardButton("💾 Storage & Folders", callback_data="admin:logs:view:storage"),
        ],
        [
            InlineKeyboardButton("👤 Admin Actions", callback_data="admin:logs:view:admin"),
            InlineKeyboardButton("🔐 Security Logs", callback_data="admin:logs:view:security"),
        ],
        [
            InlineKeyboardButton("⚡ Performance Logs", callback_data="admin:logs:view:performance"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="admin:logs:menu"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:main"),
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _build_log_view_markup(category: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"admin:logs:refresh:{category}"),
            InlineKeyboardButton("⬅️ Categories", callback_data="admin:logs:menu"),
        ],
        [
            InlineKeyboardButton("◀️ Admin Panel", callback_data="admin:menu:main"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _command_parent_menu(command_key: str) -> str:
    if command_key in {"broadcast", "ban", "unban", "del"}:
        return "users"
    if command_key in {"linkinfo", "revoke_link", "regen_link", "expire_link"}:
        return "links"
    if command_key in {"rm_adult"}:
        return "monitor"
    return "main"


# Active admin panel message tracking: maps chat_id -> active message_id
_ACTIVE_ADMIN_MESSAGES: dict[int, int] = {}


def clear_active_admin_messages_for_testing() -> None:
    """Reset the in-memory active admin messages map for test isolation."""
    _ACTIVE_ADMIN_MESSAGES.clear()


def get_active_admin_message_id(chat_id: int) -> int | None:
    """Return the active admin message ID for a chat, if known."""
    return _ACTIVE_ADMIN_MESSAGES.get(chat_id)


def set_active_admin_message_id(chat_id: int, message_id: int) -> None:
    """Set the active admin message ID for a chat."""
    _ACTIVE_ADMIN_MESSAGES[chat_id] = message_id


def _is_stale_or_invalid_message_error(exc: Exception) -> bool:
    """
    Check if an exception indicates that the message cannot be edited due to
    being deleted, stale ID, or inaccessible on Telegram servers.
    """
    if MessageIdInvalid and isinstance(exc, MessageIdInvalid):
        return True
    err_str = str(exc)
    err_lower = err_str.lower()
    if any(
        marker in err_str
        for marker in (
            "MESSAGE_ID_INVALID",
            "MessageIdInvalid",
            "MESSAGE_CANT_BE_EDITED",
            "MESSAGE_TO_EDIT_NOT_FOUND",
            "MESSAGE_EDIT_TIME_EXPIRED",
        )
    ):
        return True
    if "message id is invalid" in err_lower or "message cannot be edited" in err_lower:
        return True
    return False


async def _safe_edit_admin_message(
    query: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode=ParseMode.HTML,
    disable_web_page_preview: bool = True,
    bot: Client | None = None,
) -> Message | None:
    """
    Safely edit an admin message during callback navigation.
    If the target message was deleted or its ID is invalid, gracefully recovers
    by editing a known active replacement message or sending a fresh admin UI message.
    Updates query.message and active message tracking to prevent duplicate panels.
    Expected Telegram stale-message conditions do not pollute the Error Logs.
    """
    target_bot = (
        bot
        or getattr(query, "_client", None)
        or getattr(getattr(query, "message", None), "_client", None)
        or FileStream
    )
    msg = getattr(query, "message", None)
    chat_id = None
    if msg and getattr(msg, "chat", None) and getattr(msg.chat, "id", None) is not None:
        chat_id = int(msg.chat.id)
    if chat_id is None and getattr(query, "from_user", None) and getattr(query.from_user, "id", None) is not None:
        chat_id = int(query.from_user.id)

    # 1. Attempt in-place edit if message object exists
    if msg:
        try:
            if getattr(msg, "photo", None) or getattr(msg, "caption", None):
                res = await msg.edit_caption(
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            else:
                res = await msg.edit_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
            if chat_id and getattr(msg, "id", None) is not None:
                _ACTIVE_ADMIN_MESSAGES[chat_id] = msg.id
            return res or msg
        except MessageNotModified:
            if chat_id and getattr(msg, "id", None) is not None:
                _ACTIVE_ADMIN_MESSAGES[chat_id] = msg.id
            return msg
        except Exception as exc:
            if not _is_stale_or_invalid_message_error(exc):
                logger.warning("Error editing admin message: %s", exc)
                return None
            logger.debug(
                "Admin message %s in chat %s cannot be edited (%s); initiating recovery",
                getattr(msg, "id", None),
                chat_id,
                exc,
            )

    # 2. Recovery: target message cannot be edited (deleted, stale ID, or missing)
    if not target_bot or chat_id is None:
        return None

    # Check if a different active message is tracked for this chat
    active_msg_id = _ACTIVE_ADMIN_MESSAGES.get(chat_id)
    failed_msg_id = getattr(msg, "id", None)

    if active_msg_id and active_msg_id != failed_msg_id:
        try:
            res = await target_bot.edit_message_text(
                chat_id=chat_id,
                message_id=active_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            query.message = res
            return res
        except MessageNotModified:
            return None
        except Exception as active_exc:
            if not _is_stale_or_invalid_message_error(active_exc):
                logger.warning(
                    "Error editing active admin message %s in chat %s: %s",
                    active_msg_id,
                    chat_id,
                    active_exc,
                )
            _ACTIVE_ADMIN_MESSAGES.pop(chat_id, None)

    # Send a fresh admin message to replace the deleted/inaccessible message
    try:
        new_msg = await target_bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        if new_msg and hasattr(new_msg, "id"):
            _ACTIVE_ADMIN_MESSAGES[chat_id] = new_msg.id
            query.message = new_msg
        return new_msg
    except Exception as send_exc:
        logger.warning("Failed to send fallback admin message to chat %s: %s", chat_id, send_exc)
        return None


async def _show_admin_panel(message: Message, user_id: int, bot: Client | None = None):
    chat_id = int(message.chat.id) if getattr(message, "chat", None) and getattr(message.chat, "id", None) is not None else int(user_id)
    old_msg_id = _ACTIVE_ADMIN_MESSAGES.get(chat_id)
    if old_msg_id:
        target_bot = bot or getattr(message, "_client", None) or FileStream
        try:
            await target_bot.delete_messages(chat_id=chat_id, message_ids=old_msg_id)
        except Exception:
            pass
    sent_msg = await message.reply_text(
        await _build_admin_panel_text(user_id, bot=bot),
        parse_mode=ParseMode.HTML,
        quote=True,
        disable_web_page_preview=True,
        reply_markup=await _build_admin_panel_markup(user_id, bot=bot),
    )
    if sent_msg and getattr(sent_msg, "id", None) is not None and chat_id:
        _ACTIVE_ADMIN_MESSAGES[chat_id] = sent_msg.id
    return sent_msg



async def _refresh_admin_panel(
    query: CallbackQuery,
    user_id: int,
    note: str | None = None,
    bot: Client | None = None,
    menu: str = "main",
):
    owner_mode = int(user_id) == OWNER_ID

    if menu == "config":
        shortener_state = "ON" if await db.get_urlshortener_status() else "OFF"
        webads_state = "ON" if await db.get_web_ads_status(bot=bot) else "OFF"
        admin_flog_users = await db.get_admin_flog_user_ids()
        effective_storage_mode, _, _ = await resolve_active_flog_target(db, user_id=user_id)
        text = AdminMessages.config_panel_text(
            shortener_state=shortener_state,
            bot_label=_bot_label(bot),
            webads_state=webads_state,
            effective_storage_mode=effective_storage_mode,
            admin_user_count=len(admin_flog_users),
        )
        markup = await _build_config_markup(user_id, bot=bot)
    elif menu == "monitor":
        text = AdminMessages.monitoring_panel_text()
        markup = await _build_monitoring_markup(user_id, bot=bot)
    elif menu == "users":
        if not owner_mode:
            text = await _build_admin_panel_text(user_id, bot=bot)
            markup = await _build_admin_panel_markup(user_id, bot=bot)
        else:
            text = AdminMessages.user_mgmt_panel_text()
            markup = await _build_user_mgmt_markup(user_id, bot=bot)
    elif menu == "links":
        if not owner_mode:
            text = await _build_admin_panel_text(user_id, bot=bot)
            markup = await _build_admin_panel_markup(user_id, bot=bot)
        else:
            text = AdminMessages.link_mgmt_panel_text()
            markup = await _build_link_mgmt_markup(user_id, bot=bot)
    elif menu == "logs":
        text = AdminMessages.logs_panel_text()
        markup = _build_logs_menu_markup()
    else:  # "main"
        text = await _build_admin_panel_text(user_id, bot=bot)
        markup = await _build_admin_panel_markup(user_id, bot=bot)

    if note:
        text = f"{text}\n\n<i>{html.escape(note)}</i>"

    await _safe_edit_admin_message(query, text=text, reply_markup=markup, bot=bot)


def _build_status_markup(has_speedtest: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Speed Test", callback_data="admin:run:speedtest")],
        [
            InlineKeyboardButton("❌ Close", callback_data="close"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:monitor"),
        ],
    ])


def _build_bot_status_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:botstatus")],
        [
            InlineKeyboardButton("❌ Close", callback_data="close"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:menu:monitor"),
        ],
    ])


def _build_speedtest_no_result_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Run Speed Test", callback_data="admin:speedtest:run")],
        [
            InlineKeyboardButton("❌ Close", callback_data="close"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:run:status"),
        ],
    ])


def _build_speedtest_success_markup(share_url: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if share_url and str(share_url).strip():
        rows.append([InlineKeyboardButton("🖼 View Speedtest Card", url=str(share_url).strip())])
    rows.append([InlineKeyboardButton("🔄 Run Speed Test Again", callback_data="admin:speedtest:run")])
    rows.append([
        InlineKeyboardButton("❌ Close", callback_data="close"),
        InlineKeyboardButton("◀️ Back", callback_data="admin:run:status"),
    ])
    return InlineKeyboardMarkup(rows)


def _build_speedtest_failure_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Retry Speed Test", callback_data="admin:speedtest:run")],
        [
            InlineKeyboardButton("❌ Close", callback_data="close"),
            InlineKeyboardButton("◀️ Back", callback_data="admin:run:status"),
        ],
    ])


def _build_speedtest_markup(share_url: str | None = None, is_error: bool = False) -> InlineKeyboardMarkup:
    if is_error:
        return _build_speedtest_failure_markup()
    return _build_speedtest_success_markup(share_url=share_url)


async def _build_status_text(force_speedtest: bool = False) -> str:
    vps_info = get_system_info()
    net_info = await get_network_info()
    return format_system_status(vps_info=vps_info, net_info=net_info)


def _format_dc(val: Any) -> str | None:
    if val is None or isinstance(val, bool) or hasattr(val, "_mock_name"):
        return None
    if isinstance(val, int):
        return f"DC{val}"
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        return f"DC{val_str}" if val_str.isdigit() else val_str
    return None


async def _get_client_dc(bot: Client | None) -> str:
    if bot is None:
        return "Unavailable"
    try:
        me = getattr(bot, "me", None)
        if me is not None:
            dc_fmt = _format_dc(getattr(me, "dc_id", None))
            if dc_fmt:
                return dc_fmt

        storage = getattr(bot, "storage", None)
        if storage is not None:
            dc_attr = getattr(storage, "dc_id", None)
            if callable(dc_attr):
                try:
                    if inspect.iscoroutinefunction(dc_attr):
                        val = await asyncio.wait_for(dc_attr(), timeout=1.0)
                    else:
                        val = dc_attr()
                    dc_fmt = _format_dc(val)
                    if dc_fmt:
                        return dc_fmt
                except Exception:
                    pass
            else:
                dc_fmt = _format_dc(dc_attr)
                if dc_fmt:
                    return dc_fmt

        session = getattr(bot, "session", None)
        if session is not None:
            dc_fmt = _format_dc(getattr(session, "dc_id", None))
            if dc_fmt:
                return dc_fmt
    except Exception:
        pass
    return "Unavailable"


async def _build_bot_status_text(bot: Client | None = None) -> str:
    uptime_sec = max(time.time() - BOT_START_TIME, 0)
    uptime_str = format_uptime(uptime_sec)

    # 1. Client Details
    bot_username = get_bot_username(bot)
    username_str = f"@{bot_username}" if bot_username else "Unavailable"
    bot_id_val = getattr(getattr(bot, "me", None), "id", None)
    id_str = str(bot_id_val) if bot_id_val else "Unavailable"
    dc_str = await _get_client_dc(bot)
    is_connected = bool(getattr(bot, "is_connected", False))
    conn_str = "Connected" if is_connected else "Disconnected"

    # 2. Runtime Details
    py_ver = platform.python_version()
    fw_ver = getattr(pyrogram, "__version__", "2.0")
    arch_str = f"{platform.system()} ({platform.machine()})"

    # 3. Database Details
    db_status = "🔴 Error"
    db_ping = "Unavailable"
    try:
        t0 = time.perf_counter()
        if hasattr(db, "ping") and callable(db.ping):
            latency_ms = round(await asyncio.wait_for(db.ping(), timeout=2.0), 1)
            db_status = "🟢 Connected"
            db_ping = f"{latency_ms} ms"
        elif hasattr(db, "client") and getattr(db.client, "admin", None):
            await asyncio.wait_for(db.client.admin.command("ping"), timeout=2.0)
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            db_status = "🟢 Connected"
            db_ping = f"{latency_ms} ms"
        else:
            await asyncio.wait_for(db.total_users_count(), timeout=2.0)
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            db_status = "🟢 Connected"
            db_ping = f"{latency_ms} ms"
    except Exception:
        db_status = "🔴 Error"
        db_ping = "Unavailable"

    # 4. Application Stats
    try:
        total_users = f"{await db.total_users_count():,}"
    except Exception:
        total_users = "0"

    try:
        total_files = f"{await db.total_files():,}"
    except Exception:
        total_files = "0"

    try:
        total_links = f"{await db.total_links_count():,}"
    except Exception:
        total_links = "0"

    try:
        total_folders = f"{await db.total_folders_count():,}"
    except Exception:
        total_folders = "0"

    # 5. Process Resources
    res = get_bot_process_resources()
    proc_cpu = res.get("cpu", "Unavailable")
    proc_ram = res.get("ram", "Unavailable")

    # 6. Workload Details
    try:
        workload_stats = get_workload_stats(bot=bot)
        active_ops = workload_stats.get("active_operations", "Unavailable")
        http_reqs = workload_stats.get("http_requests", "Unavailable")
        streams_act = workload_stats.get("streams_active", "Unavailable")
        files_proc = workload_stats.get("files_processing", "Unavailable")
        req_min = workload_stats.get("requests_per_minute", "Unavailable")
    except Exception:
        active_ops = "Unavailable"
        http_reqs = "Unavailable"
        streams_act = "Unavailable"
        files_proc = "Unavailable"
        req_min = "Unavailable"

    # 7. Persistent Storage Queue
    queue_text = "Unavailable"
    try:
        from FileStream.services.storage_service import storage_service
        q_metrics = storage_service.get_metrics()
        q_stats = await db.get_storage_jobs_stats() if hasattr(db, "get_storage_jobs_stats") else {}
        p_cnt = q_stats.get("pending", 0)
        c_cnt = q_stats.get("claimed", 0)
        r_cnt = q_stats.get("running", 0)
        rw_cnt = q_stats.get("retry_wait", 0)
        s_cnt = q_stats.get("stored", 0)
        f_cnt = q_stats.get("failed", 0)
        rec_cnt = q_stats.get("total_recoveries", 0)
        oldest = q_stats.get("oldest_pending_age_seconds")
        oldest_str = f"{int(oldest)}s" if oldest is not None else "None"
        queue_text = (
            f"Workers: <code>{q_metrics.get('storage_workers', 0)}</code> | Buffer: <code>{q_metrics.get('storage_queue_size', 0)}</code>\n"
            f"PENDING: <code>{p_cnt}</code> | CLAIMED: <code>{c_cnt}</code> | RUNNING: <code>{r_cnt}</code>\n"
            f"RETRY: <code>{rw_cnt}</code> | STORED: <code>{s_cnt}</code> | FAILED: <code>{f_cnt}</code>\n"
            f"Recoveries: <code>{rec_cnt}</code> | Oldest Pending: <code>{oldest_str}</code>"
        )
    except Exception:
        pass

    return (
        "🤖 <b>Bot Status</b>\n\n"
        "🟢 <b>Bot Health</b>\n"
        "Status: <code>Online</code>\n"
        f"Version: <code>{BOT_VERSION}</code>\n"
        f"Uptime: <code>{uptime_str}</code>\n\n"
        "👤 <b>Client</b>\n"
        f"Identity: <code>{html.escape(username_str)}</code>\n"
        f"Bot ID: <code>{html.escape(id_str)}</code>\n"
        f"DC: <code>{html.escape(dc_str)}</code>\n"
        f"Connection: <code>{html.escape(conn_str)}</code>\n\n"
        "⚙️ <b>Runtime</b>\n"
        f"Python: <code>{py_ver}</code>\n"
        f"Framework: <code>Pyrofork {fw_ver}</code>\n"
        f"Platform: <code>{arch_str}</code>\n\n"
        "🗄 <b>Database</b>\n"
        f"Status: <code>{db_status}</code>\n"
        f"Latency: <code>{db_ping}</code>\n\n"
        "📊 <b>Application Stats</b>\n"
        f"Total Users: <code>{total_users}</code>\n"
        f"Stored Files: <code>{total_files}</code>\n"
        f"Public Links: <code>{total_links}</code>\n"
        f"Total Folders: <code>{total_folders}</code>\n\n"
        "💻 <b>Process Resources</b>\n"
        f"CPU: <code>{proc_cpu}</code>\n"
        f"RAM: <code>{proc_ram}</code>\n\n"
        "📈 <b>Workload</b>\n"
        f"Active Operations: <code>{active_ops}</code>\n"
        f"HTTP Requests: <code>{http_reqs}</code>\n"
        f"Streams Active: <code>{streams_act}</code>\n"
        f"Files Processing: <code>{files_proc}</code>\n"
        f"Requests/Min: <code>{req_min}</code>\n\n"
        "📦 <b>Storage Queue (Persistent)</b>\n"
        f"{queue_text}"
    )


async def _run_speedtest_action(message: Message):
    now = time.time()
    time_since = now - get_last_speedtest_at()
    if time_since < SPEEDTEST_COOLDOWN:
        wait = int(SPEEDTEST_COOLDOWN - time_since)
        await message.reply_text(AdminMessages.SPEEDTEST_WAIT.format(f"{wait}s"), quote=True, parse_mode=ParseMode.HTML)
        return

    if speedtest_lock.locked():
        await message.reply_text(AdminMessages.SPEEDTEST_ALREADY_RUNNING, quote=True)
        return

    msg = await message.reply_text(AdminMessages.SPEEDTEST_RUNNING_IN_PLACE, quote=True, parse_mode=ParseMode.HTML, reply_markup=None)

    async with speedtest_lock:
        share_url = None
        is_error = False
        speed_res = None
        try:
            speed_res = await run_speedtest(retries=1, delay=2, timeout=45.0)
            if isinstance(speed_res, dict) and "error" in speed_res:
                is_error = True
            else:
                share_url = speed_res.get("share") if isinstance(speed_res, dict) else None
        except Exception as e:
            is_error = True
            speed_res = {"error": "failed", "message": str(e)}

        test_text = format_speedtest_detailed(speed_res)
        markup = _build_speedtest_failure_markup() if is_error else _build_speedtest_success_markup(share_url=share_url)
        await msg.edit_text(test_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup)

# ---------------------[ HELPER FUNCTIONS ]---------------------#
def get_readable_time(seconds: int) -> str:
    count = 0
    ping_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", "days"]
    while count < 4:
        count += 1
        remainder, result = divmod(seconds, 60) if count < 3 else divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)
    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    if len(time_list) == 4:
        ping_time += time_list.pop() + ", "
    time_list.reverse()
    ping_time += ":".join(time_list)
    return ping_time

# ---------------------[ CHECK YOUR ID ]---------------------#
@FileStream.on_message(filters.command("id") & filters.private)
async def get_id(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    user_id = m.from_user.id
    from FileStream.bot.helpers.ui import make_id_keyboard
    await m.reply_text(
        PublicMessages.USER_ID.format(user_id),
        parse_mode=ParseMode.HTML,
        reply_markup=make_id_keyboard(user_id, bot=c),
        quote=True
    )


@FileStream.on_message(filters.command("admin") & filters.private)
async def admin_help(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        log_admin_action(m.from_user.id, "UNAUTHORIZED_ACCESS", "command /admin rejected (ACCESS_DENIED)")
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return
    log_admin_action(m.from_user.id, "DASHBOARD_ACCESS", "command /admin")
    await _show_admin_panel(m, m.from_user.id, bot=c)


async def _handle_execute_speedtest_callback(c: Client, query: CallbackQuery):
    now = time.time()
    if speedtest_lock.locked():
        try:
            await query.answer(AdminMessages.SPEEDTEST_ALREADY_RUNNING, show_alert=True)
        except Exception:
            pass
        return

    time_since = now - get_last_speedtest_at()
    if time_since < SPEEDTEST_COOLDOWN:
        wait = int(SPEEDTEST_COOLDOWN - time_since)
        try:
            await query.answer(AdminMessages.SPEEDTEST_WAIT_ALERT.format(f"{wait}s"), show_alert=True)
        except Exception:
            pass
        return

    log_admin_action(query.from_user.id, "SPEEDTEST", "callback admin:run:speedtest")

    try:
        await query.answer(AdminMessages.SPEEDTEST_RUNNING_ALERT)
    except Exception:
        pass

    # State: SPEED TEST RUNNING -> NO buttons!
    await _safe_edit_admin_message(query, text=AdminMessages.SPEEDTEST_RUNNING_IN_PLACE, reply_markup=None)

    async with speedtest_lock:
        share_url = None
        is_error = False
        speed_res = None
        try:
            speed_res = await run_speedtest(retries=1, delay=2, timeout=45.0)
            if isinstance(speed_res, dict) and "error" in speed_res:
                is_error = True
            else:
                share_url = speed_res.get("share") if isinstance(speed_res, dict) else None
        except Exception as e:
            is_error = True
            speed_res = {"error": "failed", "message": str(e)}

        if is_error:
            reason = speed_res.get("message") or speed_res.get("error") or "Unknown error"
            has_prev = bool(get_latest_speedtest() or get_speedtest_history())
            test_text = format_speedtest_failure(reason, has_previous=has_prev)
            markup = _build_speedtest_failure_markup()
        else:
            test_text = format_speedtest_detailed(speed_res)
            markup = _build_speedtest_success_markup(share_url=share_url)

        await _safe_edit_admin_message(query, text=test_text, reply_markup=markup)


@FileStream.on_callback_query(filters.regex(r"^admin:"))
async def admin_panel_callback(c: Client, query: CallbackQuery):
    if is_secondary_worker(c):
        return
    user_id = int(query.from_user.id)
    if user_id not in ADMIN_IDS:
        log_admin_action(user_id, "UNAUTHORIZED_ACCESS", f"callback {query.data} rejected (ACCESS_DENIED)")
        try:
            await query.answer(ErrorMessages.ACCESS_DENIED, show_alert=True)
        except Exception:
            pass
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        try:
            await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        except Exception:
            pass
        return

    section = parts[1]
    owner_mode = user_id == OWNER_ID

    # Submenu navigation: admin:menu:<target>
    if section == "menu":
        target = parts[2] if len(parts) > 2 else "main"
        if target in {"users", "links"} and not owner_mode:
            try:
                await query.answer(AdminMessages.OWNER_ONLY, show_alert=True)
            except Exception:
                pass
            return
        try:
            await query.answer()
        except Exception:
            pass
        await _refresh_admin_panel(query, user_id, bot=c, menu=target)
        return

    # Refresh handling: admin:refresh or admin:refresh:<menu>
    if section == "refresh":
        target_menu = parts[2] if len(parts) > 2 else "main"
        if target_menu == "botstatus":
            try:
                await query.answer(AdminMessages.REFRESHED_ALERT)
            except Exception:
                pass
            bot_text = await _build_bot_status_text(bot=c)
            markup = _build_bot_status_markup()
            await _safe_edit_admin_message(query, text=bot_text, reply_markup=markup, bot=c)
            return
        try:
            await query.answer(AdminMessages.REFRESHED_ALERT)
        except Exception:
            pass
        await _refresh_admin_panel(query, user_id, bot=c, menu=target_menu)
        return

    # Admin Logs navigation & category views: admin:logs, admin:logs:menu, admin:logs:view:<cat>, admin:logs:refresh:<cat>
    if section == "logs":
        subaction = parts[2] if len(parts) > 2 else "menu"
        if subaction == "menu":
            try:
                await query.answer()
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, bot=c, menu="logs")
            return

        if subaction in {"view", "refresh"}:
            category = parts[3] if len(parts) > 3 else "bot"
            if subaction == "refresh":
                try:
                    await query.answer(AdminMessages.REFRESHED_ALERT)
                except Exception:
                    pass
            else:
                try:
                    await query.answer()
                except Exception:
                    pass
            category_title = LOG_CATEGORY_TITLES.get(category, category.title() + " Logs")
            formatted_content, count = await get_logs_for_category(category)
            if count == 0:
                text = AdminMessages.logs_empty_text(category_title)
            else:
                text = AdminMessages.logs_view_text(category_title, formatted_content, count)
            markup = _build_log_view_markup(category)
            await _safe_edit_admin_message(query, text=text, reply_markup=markup, bot=c)
            return

        # Fallback to logs menu
        try:
            await query.answer()
        except Exception:
            pass
        await _refresh_admin_panel(query, user_id, bot=c, menu="logs")
        return

    # URL Shortener toggle
    if section == "urlshortener":
        if len(parts) < 3:
            try:
                await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            except Exception:
                pass
            return
        action = parts[2]
        if action == "status":
            state = "ON" if await db.get_urlshortener_status() else "OFF"
            try:
                await query.answer(AdminMessages.shortener_status_text(state), show_alert=True)
            except Exception:
                pass
            return
        if action == "on":
            if await db.get_urlshortener_status():
                try:
                    await query.answer(AdminMessages.SHORTENER_ALREADY_ON_ALERT, show_alert=True)
                except Exception:
                    pass
                return
            await db.update_urlshortener_status(True)
            log_admin_action(user_id, "SHORTENER_TOGGLE", "state=ON (callback)")
            try:
                await query.answer(AdminMessages.SHORTENER_ON_ALERT)
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, "Shortener is now ON.", bot=c, menu="config")
            return
        if action == "off":
            if not await db.get_urlshortener_status():
                try:
                    await query.answer(AdminMessages.SHORTENER_ALREADY_OFF_ALERT, show_alert=True)
                except Exception:
                    pass
                return
            await db.update_urlshortener_status(False)
            log_admin_action(user_id, "SHORTENER_TOGGLE", "state=OFF (callback)")
            try:
                await query.answer(AdminMessages.SHORTENER_OFF_ALERT)
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, "Shortener is now OFF.", bot=c, menu="config")
            return
        try:
            await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        except Exception:
            pass
        return

    # Web Ads toggle
    if section == "webads":
        if len(parts) < 3:
            try:
                await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            except Exception:
                pass
            return
        action = parts[2]
        if action == "status":
            state = "ON" if await db.get_web_ads_status(bot=c) else "OFF"
            try:
                await query.answer(AdminMessages.webads_status_text(_bot_label(c), state), show_alert=True)
            except Exception:
                pass
            return
        if action == "on":
            if await db.get_web_ads_status(bot=c):
                try:
                    await query.answer(AdminMessages.webads_already_enabled_text(_bot_label(c)), show_alert=True)
                except Exception:
                    pass
                return
            await db.update_web_ads_status(True, bot=c)
            log_admin_action(user_id, "WEB_ADS_TOGGLE", f"state=ON bot={_bot_label(c)} (callback)")
            try:
                await query.answer(AdminMessages.webads_enabled_text(_bot_label(c)))
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, f"Web ads are now ON for {_bot_label(c)}.", bot=c, menu="config")
            return
        if action == "off":
            if not await db.get_web_ads_status(bot=c):
                try:
                    await query.answer(AdminMessages.webads_already_disabled_text(_bot_label(c)), show_alert=True)
                except Exception:
                    pass
                return
            await db.update_web_ads_status(False, bot=c)
            log_admin_action(user_id, "WEB_ADS_TOGGLE", f"state=OFF bot={_bot_label(c)} (callback)")
            try:
                await query.answer(AdminMessages.webads_disabled_text(_bot_label(c)))
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, f"Web ads are now OFF for {_bot_label(c)}.", bot=c, menu="config")
            return
        try:
            await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        except Exception:
            pass
        return

    # Upload storage toggle & status
    if section == "flogstorage":
        if len(parts) < 3:
            try:
                await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            except Exception:
                pass
            return
        action = parts[2].strip().lower()
        configured_storage = configured_flog_channels()
        selected_users = await db.get_admin_flog_user_ids()
        using_admin_storage = await db.is_admin_flog_user(user_id)
        current_mode, _, _ = await resolve_active_flog_target(db, user_id=user_id)
        if action == "status":
            try:
                await query.answer(
                    AdminMessages.storage_status_alert(
                        current_mode=current_mode,
                        default_label=_default_storage_label(configured_storage),
                        expire_hours=int(Server.PUBLIC_FILE_EXPIRE_HOURS or 0),
                        selected_count=len(selected_users),
                        main_channel=configured_storage.get('main'),
                        admin_channel=configured_storage.get('admin'),
                    ),
                    show_alert=True,
                )
            except Exception:
                pass
            return
        if action == "main":
            if not configured_storage.get("main"):
                try:
                    await query.answer(AdminMessages.STORAGE_NOT_CONFIGURED, show_alert=True)
                except Exception:
                    pass
                return
            if not using_admin_storage:
                try:
                    await query.answer(AdminMessages.STORAGE_MAIN_ALREADY_USING_ALERT, show_alert=True)
                except Exception:
                    pass
                return
            await db.set_admin_flog_user(user_id, False)
            log_admin_action(user_id, "STORAGE_TOGGLE", "mode=MAIN (callback)")
            try:
                await query.answer(AdminMessages.STORAGE_MAIN_NOW_USING_ALERT)
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, "Your uploads now use Main Storage.", bot=c, menu="config")
            return
        if action == "admin":
            if not configured_storage.get("admin"):
                try:
                    await query.answer(AdminMessages.STORAGE_NOT_CONFIGURED, show_alert=True)
                except Exception:
                    pass
                return
            if using_admin_storage:
                try:
                    await query.answer(AdminMessages.STORAGE_ADMIN_ALREADY_USING_ALERT, show_alert=True)
                except Exception:
                    pass
                return
            await db.set_admin_flog_user(user_id, True)
            log_admin_action(user_id, "STORAGE_TOGGLE", "mode=ADMIN (callback)")
            try:
                await query.answer(AdminMessages.STORAGE_ADMIN_NOW_USING_ALERT)
            except Exception:
                pass
            await _refresh_admin_panel(query, user_id, "Your uploads now use Admin Storage.", bot=c, menu="config")
            return
        try:
            await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        except Exception:
            pass
        return

    # Speedtest callbacks
    if section == "speedtest":
        subaction = parts[2] if len(parts) > 2 else ""
        if subaction == "history":
            try:
                await query.answer()
            except Exception:
                pass
            history_text = format_speedtest_history()
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❌ Close", callback_data="close"),
                    InlineKeyboardButton("◀️ Back", callback_data="admin:run:speedtest"),
                ]
            ])
            await _safe_edit_admin_message(query, text=history_text, reply_markup=markup)
            return

        if subaction == "run":
            await _handle_execute_speedtest_callback(c, query)
            return

    # Actions: Speed test, System status, Bot status
    if section == "run":
        if len(parts) < 3:
            try:
                await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            except Exception:
                pass
            return
        action = parts[2]
        if action == "speedtest":
            if len(parts) > 3 and parts[3] == "run":
                await _handle_execute_speedtest_callback(c, query)
                return

            try:
                await query.answer()
            except Exception:
                pass

            latest_st = get_latest_speedtest()
            if latest_st and isinstance(latest_st, dict) and "error" not in latest_st:
                share = latest_st.get("share")
                st_text = format_speedtest_detailed(latest_st)
                markup = _build_speedtest_success_markup(share_url=share)
            else:
                st_text = format_speedtest_empty()
                markup = _build_speedtest_no_result_markup()

            await _safe_edit_admin_message(query, text=st_text, reply_markup=markup)
            return

        if action == "status":
            if not owner_mode:
                try:
                    await query.answer(AdminMessages.OWNER_ONLY, show_alert=True)
                except Exception:
                    pass
                return
            try:
                await query.answer()
            except Exception:
                pass
            status_text = await _build_status_text(force_speedtest=False)
            markup = _build_status_markup()
            await _safe_edit_admin_message(query, text=status_text, reply_markup=markup)
            return

        if action == "botstatus":
            try:
                await query.answer()
            except Exception:
                pass
            bot_text = await _build_bot_status_text(bot=c)
            markup = _build_bot_status_markup()
            await _safe_edit_admin_message(query, text=bot_text, reply_markup=markup)
            return

        if action == "history":
            try:
                await query.answer()
            except Exception:
                pass
            history_text = format_speedtest_history()
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("❌ Close", callback_data="close"),
                    InlineKeyboardButton("◀️ Back", callback_data="admin:run:speedtest"),
                ]
            ])
            await _safe_edit_admin_message(query, text=history_text, reply_markup=markup)
            return

        try:
            await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
        except Exception:
            pass
        return

    # Help / Command guide for operational & dangerous tools
    if section == "help":
        if len(parts) < 3:
            try:
                await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
            except Exception:
                pass
            return
        command_key = parts[2]
        if command_key in OWNER_ONLY_ADMIN_ACTIONS and not owner_mode:
            try:
                await query.answer(AdminMessages.OWNER_ONLY, show_alert=True)
            except Exception:
                pass
            return
        guide_text = AdminMessages.get_command_guide(command_key)
        parent_menu = _command_parent_menu(command_key)
        try:
            await query.answer()
        except Exception:
            pass
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Back", callback_data=f"admin:menu:{parent_menu}")]
        ])
        await _safe_edit_admin_message(query, text=guide_text, reply_markup=markup)
        return

    try:
        await query.answer(ErrorMessages.STALE_CALLBACK, show_alert=True)
    except Exception:
        pass

# ---------------------[ URL SHORTENER TOGGLE COMMAND ]---------------------#
@FileStream.on_message(filters.command(["urlshortener", "ads"]) & filters.private)
async def urlshortener_toggle(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return

    # /urlshortener (or legacy /ads) -> status
    if len(m.command) < 2:
        status = await db.get_urlshortener_status()
        state = "ON" if status else "OFF"
        await m.reply_text(
            text=AdminMessages.shortener_status_prompt(state),
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    action = m.command[1].strip().lower()

    if action in {"status", "state"}:
        status = await db.get_urlshortener_status()
        state = "ON" if status else "OFF"
        await m.reply_text(
            text=AdminMessages.shortener_status_text(state),
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    if action == "on":
        await db.update_urlshortener_status(True)
        log_admin_action(m.from_user.id, "SHORTENER_TOGGLE", "state=ON (command)")
        await m.reply_text(
            text=AdminMessages.shortener_enabled_text(),
            parse_mode=ParseMode.HTML,
            quote=True
        )
    elif action == "off":
        await db.update_urlshortener_status(False)
        log_admin_action(m.from_user.id, "SHORTENER_TOGGLE", "state=OFF (command)")
        await m.reply_text(
            text=AdminMessages.shortener_disabled_text(),
            parse_mode=ParseMode.HTML,
            quote=True
        )
    else:
        await m.reply_text(
            text=AdminMessages.COMMAND_USAGE.format("/urlshortener <on|off>"),
            parse_mode=ParseMode.HTML,
            quote=True
        )


@FileStream.on_message(filters.command("flogstorage") & filters.private)
async def flogstorage_toggle(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return

    configured_storage = configured_flog_channels()
    admin_storage_users = await db.get_admin_flog_user_ids()

    if len(m.command) < 2:
        current_mode, _, _ = await resolve_active_flog_target(db, user_id=m.from_user.id)
        await m.reply_text(
            text=AdminMessages.storage_status_prompt(
                current_mode=current_mode,
                default_label=_default_storage_label(configured_storage),
                admin_user_count=len(admin_storage_users),
            ),
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    action = m.command[1].strip().lower()

    if action in {"status", "state"}:
        try:
            target_user_id = _parse_storage_target(m.command[2] if len(m.command) > 2 else None, m.from_user.id)
        except ValueError:
            await m.reply_text(ErrorMessages.INVALID_USER_ID, parse_mode=ParseMode.HTML, quote=True)
            return
        target_mode, _, _ = await resolve_active_flog_target(db, user_id=target_user_id)
        selected = await db.is_admin_flog_user(target_user_id)
        await m.reply_text(
            text=AdminMessages.storage_user_status(
                target_user_id=target_user_id,
                target_mode=target_mode,
                is_admin_storage=selected,
                default_label=_default_storage_label(configured_storage),
            ),
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    if action == "list":
        await m.reply_text(
            text=AdminMessages.storage_user_list(
                total=len(admin_storage_users),
                formatted_list=_format_admin_storage_users(admin_storage_users, limit=50),
            ),
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    if action == "main":
        try:
            target_user_id = _parse_storage_target(m.command[2] if len(m.command) > 2 else None, m.from_user.id)
        except ValueError:
            await m.reply_text(ErrorMessages.INVALID_USER_ID, parse_mode=ParseMode.HTML, quote=True)
            return
        if not configured_storage.get("main"):
            await m.reply_text(AdminMessages.STORAGE_NOT_CONFIGURED, parse_mode=ParseMode.HTML, quote=True)
            return
        if not await db.is_admin_flog_user(target_user_id):
            await m.reply_text(
                AdminMessages.storage_user_already_using(target_user_id, "Main"),
                parse_mode=ParseMode.HTML,
                quote=True,
            )
            return
        await db.set_admin_flog_user(target_user_id, False)
        log_admin_action(m.from_user.id, "STORAGE_TOGGLE", f"target_user={target_user_id} mode=MAIN (command)")
        await m.reply_text(
            AdminMessages.storage_user_now_using(target_user_id, "Main"),
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    if action == "admin":
        try:
            target_user_id = _parse_storage_target(m.command[2] if len(m.command) > 2 else None, m.from_user.id)
        except ValueError:
            await m.reply_text(ErrorMessages.INVALID_USER_ID, parse_mode=ParseMode.HTML, quote=True)
            return
        if not configured_storage.get("admin"):
            await m.reply_text(AdminMessages.STORAGE_NOT_CONFIGURED, parse_mode=ParseMode.HTML, quote=True)
            return
        if await db.is_admin_flog_user(target_user_id):
            await m.reply_text(
                AdminMessages.storage_user_already_using(target_user_id, "Admin"),
                parse_mode=ParseMode.HTML,
                quote=True,
            )
            return
        await db.set_admin_flog_user(target_user_id, True)
        log_admin_action(m.from_user.id, "STORAGE_TOGGLE", f"target_user={target_user_id} mode=ADMIN (command)")
        await m.reply_text(
            AdminMessages.storage_user_now_using(target_user_id, "Admin"),
            parse_mode=ParseMode.HTML,
            quote=True,
        )
        return

    await m.reply_text(
        text=AdminMessages.COMMAND_USAGE.format("/flogstorage <main|admin|status|list> [user_id]"),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


# ---------------------[ WEB ADS TOGGLE COMMAND ]---------------------#
@FileStream.on_message(filters.command("webads") & filters.private)
async def webads_toggle(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return

    bot_label = _bot_label(c)

    if len(m.command) < 2:
        status = await db.get_web_ads_status(bot=c)
        state = "ON" if status else "OFF"
        await m.reply_text(
            text=AdminMessages.webads_status_prompt(bot_label, state),
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    action = m.command[1].strip().lower()

    if action in {"status", "state"}:
        status = await db.get_web_ads_status(bot=c)
        state = "ON" if status else "OFF"
        await m.reply_text(
            text=AdminMessages.webads_status_text(bot_label, state),
            parse_mode=ParseMode.HTML,
            quote=True
        )
        return

    if action == "on":
        if await db.get_web_ads_status(bot=c):
            await m.reply_text(
                text=AdminMessages.webads_already_enabled_text(bot_label),
                parse_mode=ParseMode.HTML,
                quote=True
            )
            return
        await db.update_web_ads_status(True, bot=c)
        log_admin_action(m.from_user.id, "WEB_ADS_TOGGLE", f"state=ON bot={bot_label} (command)")
        await m.reply_text(
            text=AdminMessages.webads_enabled_text(bot_label),
            parse_mode=ParseMode.HTML,
            quote=True
        )
    elif action == "off":
        if not await db.get_web_ads_status(bot=c):
            await m.reply_text(
                text=AdminMessages.webads_already_disabled_text(bot_label),
                parse_mode=ParseMode.HTML,
                quote=True
            )
            return
        await db.update_web_ads_status(False, bot=c)
        log_admin_action(m.from_user.id, "WEB_ADS_TOGGLE", f"state=OFF bot={bot_label} (command)")
        await m.reply_text(
            text=AdminMessages.webads_disabled_text(bot_label),
            parse_mode=ParseMode.HTML,
            quote=True
        )
    else:
        await m.reply_text(
            text=AdminMessages.COMMAND_USAGE.format("/webads <on|off>"),
            parse_mode=ParseMode.HTML,
            quote=True
        )

# ---------------------[ STATUS COMMAND ]---------------------#
@FileStream.on_message(filters.command("status") & filters.private & filters.user(OWNER_ID))
async def sts(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    status_text = await _build_status_text(force_speedtest=False)
    rows = [
        [InlineKeyboardButton("⚡ Speed Test", callback_data="admin:run:speedtest")],
        [InlineKeyboardButton("❌ Close", callback_data="close")],
    ]
    await m.reply_text(
        text=status_text,
        parse_mode=ParseMode.HTML,
        quote=True,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------------------[ BOT STATUS COMMAND ]---------------------#
@FileStream.on_message(filters.command("botstatus") & filters.private)
async def botstatus_cmd(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return
    bot_text = await _build_bot_status_text(bot=c)
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh:botstatus")],
        [InlineKeyboardButton("❌ Close", callback_data="close")],
    ]
    await m.reply_text(
        text=bot_text,
        parse_mode=ParseMode.HTML,
        quote=True,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(rows),
    )

# ---------------------[ BAN USER ]---------------------#
@FileStream.on_message(filters.command("speedtest") & filters.private)
async def speedtest_cmd(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        await m.reply_text(ErrorMessages.ACCESS_DENIED, quote=True)
        return
    log_admin_action(m.from_user.id, "SPEEDTEST", "command /speedtest")
    await _run_speedtest_action(m)


@FileStream.on_message(filters.command("ban") & filters.private & filters.user(OWNER_ID))
async def ban_user(b: Client, m: Message):
    if is_secondary_worker(b):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/ban <user_id>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(m.command[1])
    except ValueError:
        await m.reply_text(ErrorMessages.INVALID_USER_ID, quote=True, parse_mode=ParseMode.HTML)
        return

    if not await db.is_user_banned(target_id):
        try:
            await db.ban_user(target_id)
            await db.delete_user(target_id)
            log_admin_action(m.from_user.id, "BAN_USER", f"target_user={target_id}")
            await m.reply_text(text=AdminMessages.user_access_updated(target_id, "restricted"), parse_mode=ParseMode.HTML, quote=True)
            if not str(target_id).startswith('-100'):
                try:
                    await b.send_message(
                        chat_id=target_id,
                        text=AdminMessages.BAN_USER_NOTICE,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass
        except Exception:
            await m.reply_text(text=ErrorMessages.GENERIC, parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(text=AdminMessages.user_already_restricted(target_id), parse_mode=ParseMode.HTML, quote=True)

# ---------------------[ UNBAN USER ]---------------------#
@FileStream.on_message(filters.command("unban") & filters.private & filters.user(OWNER_ID))
async def unban_user(b: Client, m: Message):
    if is_secondary_worker(b):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/unban <user_id>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        target_id = int(m.command[1])
    except ValueError:
        await m.reply_text(ErrorMessages.INVALID_USER_ID, quote=True, parse_mode=ParseMode.HTML)
        return

    if await db.is_user_banned(target_id):
        try:
            await db.unban_user(target_id)
            log_admin_action(m.from_user.id, "UNBAN_USER", f"target_user={target_id}")
            await m.reply_text(text=AdminMessages.user_access_updated(target_id, "active"), parse_mode=ParseMode.HTML, quote=True)
            if not str(target_id).startswith('-100'):
                try:
                    await b.send_message(
                        chat_id=target_id,
                        text=AdminMessages.UNBAN_USER_NOTICE,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass
        except Exception:
            await m.reply_text(text=ErrorMessages.GENERIC, parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(text=AdminMessages.user_not_restricted(target_id), parse_mode=ParseMode.HTML, quote=True)

# ---------------------[ BROADCAST ]---------------------#
@FileStream.on_message(filters.command("broadcast") & filters.private & filters.user(OWNER_ID) & filters.reply)
async def broadcast_(c, m):
    if is_secondary_worker(c):
        return
    log_admin_action(m.from_user.id, "BROADCAST", "initiated broadcast")
    all_users = await db.get_all_users()
    broadcast_msg = m.reply_to_message
    while True:
        broadcast_id = ''.join([random.choice(string.ascii_letters) for i in range(3)])
        if not broadcast_ids.get(broadcast_id):
            break
    out = await m.reply_text(
        text=AdminMessages.BROADCAST_STARTED
    )
    start_time = time.time()
    total_users = await db.total_users_count()
    done = 0
    failed = 0
    success = 0
    broadcast_ids[broadcast_id] = dict(
        total=total_users,
        current=done,
        failed=failed,
        success=success
    )
    log_file = f"broadcast_{broadcast_id}.txt"
    async with aiofiles.open(log_file, 'w') as broadcast_log_file:
        async for user in all_users:
            sts, msg = await send_msg(
                user_id=int(user['id']),
                message=broadcast_msg
            )
            if msg is not None:
                await broadcast_log_file.write(msg)
            if sts == 200:
                success += 1
            else:
                failed += 1
            if sts == 400:
                await db.delete_user(user['id'])
            done += 1
            if broadcast_ids.get(broadcast_id) is None:
                break
            else:
                broadcast_ids[broadcast_id].update(
                    dict(
                        current=done,
                        failed=failed,
                        success=success
                    )
                )
                try:
                    await out.edit_text(AdminMessages.BROADCAST_STATUS.format(success, failed, done, total_users), parse_mode=ParseMode.HTML)
                except Exception:
                    pass
    if broadcast_ids.get(broadcast_id):
        broadcast_ids.pop(broadcast_id)
    completed_in = datetime.timedelta(seconds=int(time.time() - start_time))
    await asyncio.sleep(3)
    try:
        await out.delete()
    except Exception:
        pass
    if failed == 0:
        await m.reply_text(
            text=AdminMessages.broadcast_complete_text(completed_in, success, failed, total_users),
            parse_mode=ParseMode.HTML,
            quote=True
        )
    else:
        await m.reply_document(
            document=log_file,
            caption=AdminMessages.broadcast_complete_caption(completed_in, success, failed, total_users),
            quote=True
        )
    if os.path.exists(log_file):
        os.remove(log_file)

# ---------------------[ DELETE FILE ]---------------------#
@FileStream.on_message(filters.command("del") & filters.private & filters.user(OWNER_ID))
async def del_file(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/del <file_id>"), quote=True, parse_mode=ParseMode.HTML)
        return

    file_id = m.command[1]
    try:
        file_info = await db.get_file(file_id)
    except FileNotFound:
        await m.reply_text(
            text=AdminMessages.DEL_FILE_NOT_FOUND,
            quote=True
        )
        return

    await delete_file_entry(db, file_info, bot=c)
    log_admin_action(m.from_user.id, "DELETE_FILE", f"file_id={file_id}")
    await m.reply_text(
        text=FileMessages.DELETED,
        quote=True
    )


@FileStream.on_message(filters.command("linkinfo") & filters.private & filters.user(OWNER_ID))
async def link_info(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/linkinfo <public_id|file:<id>|folder:<id>>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        link_type, target_id, link = await _resolve_link_reference(m.command[1])
    except FileNotFound:
        await m.reply_text(AdminMessages.LINK_NOT_FOUND, quote=True)
        return

    status = _public_link_status(link)
    expires_at = link.get("expires_at")
    expires_text = (
        datetime.datetime.utcfromtimestamp(float(expires_at)).strftime("%Y-%m-%d %H:%M:%S UTC")
        if expires_at is not None else "Never"
    )
    title = link.get("file_name") or link.get("folder_name") or "N/A"
    safe_title = html.escape(str(title))
    url = _public_link_url(link)

    await m.reply_text(
        text=AdminMessages.link_info_text(
            link_type=link_type,
            target_id=target_id,
            public_id=link.get("public_id"),
            status=status,
            clicks=int(link.get("click_count", 0)),
            expires_text=expires_text,
            safe_title=safe_title,
            safe_url=html.escape(url),
        ),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


@FileStream.on_message(filters.command("revoke_link") & filters.private & filters.user(OWNER_ID))
async def revoke_link(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/revoke_link <public_id|file:<id>|folder:<id>>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        link_type, target_id, link = await _resolve_link_reference(m.command[1])
    except FileNotFound:
        await m.reply_text(AdminMessages.LINK_NOT_FOUND, quote=True)
        return

    revoked = await db.revoke_public_link(public_id=link["public_id"])
    if not revoked:
        await m.reply_text(AdminMessages.LINK_NO_ACTIVE, quote=True)
        return

    log_admin_action(m.from_user.id, "LINK_REVOKE", f"public_id={revoked['public_id']}")
    await m.reply_text(
        text=AdminMessages.link_revoked(revoked["public_id"]),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


@FileStream.on_message(filters.command("regen_link") & filters.private & filters.user(OWNER_ID))
async def regenerate_link(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if len(m.command) < 2:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/regen_link <public_id|file:<id>|folder:<id>>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        link_type, target_id, link = await _resolve_link_reference(m.command[1])
    except FileNotFound:
        await m.reply_text(AdminMessages.LINK_NOT_FOUND, quote=True)
        return

    if link_type == "file":
        file_info = await db.get_file(target_id)
        new_link = await db.regenerate_public_link(
            "file",
            target_id,
            file_name=file_info.get("file_name") or "file",
            file_type=file_info.get("mime_type") or file_info.get("category") or "",
        )
    else:
        folder = await db.get_folder(target_id)
        title = (folder.get("title") or "").strip() or f"Folder {target_id}"
        new_link = await db.regenerate_public_link(
            "folder",
            target_id,
            folder_name=title,
        )

    log_admin_action(m.from_user.id, "LINK_REGEN", f"target={target_id} new_public_id={new_link['public_id']}")
    await m.reply_text(
        text=AdminMessages.link_regenerated(new_link["public_id"], _public_link_url(new_link)),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


@FileStream.on_message(filters.command("expire_link") & filters.private & filters.user(OWNER_ID))
async def expire_link(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if len(m.command) < 3:
        await m.reply_text(AdminMessages.COMMAND_USAGE.format("/expire_link <public_id|file:<id>|folder:<id>> <now|clear|hours>"), quote=True, parse_mode=ParseMode.HTML)
        return

    try:
        link_type, target_id, link = await _resolve_link_reference(m.command[1])
    except FileNotFound:
        await m.reply_text(AdminMessages.LINK_NOT_FOUND, quote=True)
        return

    action = m.command[2].strip().lower()
    if action == "clear":
        expires_at = None
    elif action == "now":
        expires_at = time.time() - 1
    else:
        try:
            hours = float(action)
        except ValueError:
            await m.reply_text(AdminMessages.EXPIRE_LINK_INVALID, quote=True, parse_mode=ParseMode.HTML)
            return
        expires_at = time.time() + max(hours, 0) * 3600

    await db.set_public_link_expiry(link["public_id"], expires_at)
    log_admin_action(m.from_user.id, "LINK_EXPIRE", f"public_id={link['public_id']} setting={action}")

    if expires_at is None:
        result_text = "cleared"
    elif expires_at <= time.time():
        result_text = "expired immediately"
    else:
        result_text = datetime.datetime.utcfromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S UTC")

    await m.reply_text(
        text=AdminMessages.link_expiry_updated(link["public_id"], result_text),
        parse_mode=ParseMode.HTML,
        quote=True,
    )


# =============================================================================
# STORAGE JOB & WORKER ADMINISTRATION (V4/V5 PRODUCTION UPGRADE)
# =============================================================================

@FileStream.on_message(filters.command("storage_jobs") & filters.private)
async def cmd_storage_jobs(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    status = None
    limit = 10
    skip = 0
    if len(m.command) > 1:
        status = m.command[1].strip()
    if len(m.command) > 2:
        try:
            limit = max(1, min(50, int(m.command[2])))
        except ValueError:
            pass
    if len(m.command) > 3:
        try:
            skip = max(0, int(m.command[3]))
        except ValueError:
            pass

    jobs = await db.list_storage_jobs(status=status, limit=limit, skip=skip)
    if not jobs:
        status_label = f" ({status})" if status else ""
        await m.reply_text(f"No storage jobs found{status_label}.", quote=True)
        return

    lines = [f"<b>📋 Storage Jobs ({len(jobs)})</b>\n"]
    for j in jobs:
        jid = html.escape(str(j.get("job_id", "N/A")))
        st = html.escape(str(j.get("status", "N/A")))
        attempts = j.get("attempts", 0)
        max_retries = j.get("max_retries", 3)
        cid = j.get("assigned_client_id")
        cid_str = f"Client {cid}" if cid is not None else "None"
        worker = html.escape(str(j.get("worker_id") or "None"))
        prio = j.get("priority", 0)
        lines.append(
            f"• <code>{jid}</code> | <b>{st}</b>\n"
            f"  Retries: {attempts}/{max_retries} | Client: {cid_str} | Worker: {worker} | Prio: {prio}"
        )

    lines.append(f"\n<i>Use /storage_job &lt;job_id&gt; for full inspection</i>")
    await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("storage_job") & filters.private)
async def cmd_storage_job_detail(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/storage_job &lt;job_id&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    job_id = m.command[1].strip()
    job = await db.get_storage_job(job_id)
    if not job:
        await m.reply_text(f"Storage job <code>{html.escape(job_id)}</code> not found.", parse_mode=ParseMode.HTML, quote=True)
        return

    jid = html.escape(str(job.get("job_id", "N/A")))
    db_id = html.escape(str(job.get("db_id", "N/A")))
    status = html.escape(str(job.get("status", "N/A")))
    prio = job.get("priority", 0)
    attempts = job.get("attempts", 0)
    max_retries = job.get("max_retries", 3)
    worker_id = html.escape(str(job.get("worker_id") or "None"))
    lease_until = job.get("lease_until")
    lease_str = f"{round(max(lease_until - time.time(), 0.0), 1)}s remaining" if lease_until else "None"
    client_id = job.get("assigned_client_id")
    flog_mid = job.get("flog_msg_id") or "None"
    flog_cid = job.get("flog_channel_id") or "None"
    dead_letter = job.get("dead_letter", False)
    fail_reason = html.escape(str(job.get("failure_reason") or job.get("last_error") or "None"))
    fail_type = html.escape(str(job.get("failure_type") or job.get("last_error_type") or "None"))
    created_at = datetime.datetime.utcfromtimestamp(job.get("created_at", time.time())).strftime("%Y-%m-%d %H:%M:%S UTC")

    text = (
        f"<b>🔍 Storage Job Inspection</b>\n\n"
        f"• <b>Job ID:</b> <code>{jid}</code>\n"
        f"• <b>File DB ID:</b> <code>{db_id}</code>\n"
        f"• <b>Status:</b> <b>{status}</b> (Dead Letter: {dead_letter})\n"
        f"• <b>Priority:</b> {prio}\n"
        f"• <b>Attempts:</b> {attempts}/{max_retries}\n"
        f"• <b>Worker:</b> <code>{worker_id}</code>\n"
        f"• <b>Lease:</b> {lease_str}\n"
        f"• <b>Client ID:</b> {client_id}\n"
        f"• <b>FLOG Msg ID:</b> <code>{flog_mid}</code>\n"
        f"• <b>FLOG Channel ID:</b> <code>{flog_cid}</code>\n"
        f"• <b>Created At:</b> {created_at}\n"
        f"• <b>Failure Type:</b> <code>{fail_type}</code>\n"
        f"• <b>Failure Reason:</b> <code>{fail_reason}</code>\n"
    )
    await m.reply_text(text, parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("retry_storage_job") & filters.private)
async def cmd_retry_storage_job(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/retry_storage_job &lt;job_id&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    job_id = m.command[1].strip()
    success = await db.retry_dead_letter_job(job_id)
    if success:
        log_admin_action(m.from_user.id, "RETRY_STORAGE_JOB", f"job_id={job_id}")
        await db.record_audit_event(
            action="RETRY_STORAGE_JOB",
            actor=str(m.from_user.id),
            target_id=job_id,
            details={"admin_id": m.from_user.id},
        )
        await m.reply_text(f"✅ Storage job <code>{html.escape(job_id)}</code> has been reset to PENDING for retry.", parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(f"❌ Could not retry job <code>{html.escape(job_id)}</code> (job not found or not in failed/dead-letter state).", parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("cancel_storage_job") & filters.private)
async def cmd_cancel_storage_job(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/cancel_storage_job &lt;job_id&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    job_id = m.command[1].strip()
    from FileStream.services.storage_service import storage_service
    storage_service.cancel_session_jobs(job_id)
    cancelled = await db.cancel_storage_job(job_id)
    if cancelled:
        log_admin_action(m.from_user.id, "CANCEL_STORAGE_JOB", f"job_id={job_id}")
        await db.record_audit_event(
            action="CANCEL_STORAGE_JOB",
            actor=str(m.from_user.id),
            target_id=job_id,
            details={"admin_id": m.from_user.id},
        )
        await m.reply_text(f"✅ Storage job <code>{html.escape(job_id)}</code> cancelled successfully.", parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(f"❌ Storage job <code>{html.escape(job_id)}</code> could not be cancelled (not found or already terminal).", parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("storage_queue") & filters.private)
async def cmd_storage_queue(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    from FileStream.services.storage_service import storage_service
    q_metrics = storage_service.get_metrics()
    q_stats = await db.get_storage_jobs_stats() if hasattr(db, "get_storage_jobs_stats") else {}

    p_cnt = q_stats.get("pending", 0)
    c_cnt = q_stats.get("claimed", 0)
    r_cnt = q_stats.get("running", 0)
    rw_cnt = q_stats.get("retry_wait", 0)
    s_cnt = q_stats.get("stored", 0)
    f_cnt = q_stats.get("failed", 0)
    dl_cnt = q_stats.get("dead_letter", 0)
    rec_cnt = q_stats.get("total_recoveries", 0)
    oldest = q_stats.get("oldest_pending_age_seconds")
    oldest_str = f"{int(oldest)}s" if oldest is not None else "None"

    text = (
        f"<b>📊 Storage Queue Diagnostics</b>\n\n"
        f"• <b>Workers Active:</b> <code>{q_metrics.get('storage_workers', 0)}</code>\n"
        f"• <b>Local Buffer:</b> <code>{q_metrics.get('storage_queue_size', 0)}</code> (Peak: <code>{q_metrics.get('storage_queue_peak', 0)}</code>)\n"
        f"• <b>Pending Jobs:</b> <code>{p_cnt}</code>\n"
        f"• <b>Claimed Jobs:</b> <code>{c_cnt}</code>\n"
        f"• <b>Running Jobs:</b> <code>{r_cnt}</code>\n"
        f"• <b>Retry Wait:</b> <code>{rw_cnt}</code>\n"
        f"• <b>Completed:</b> <code>{s_cnt}</code>\n"
        f"• <b>Failed:</b> <code>{f_cnt}</code>\n"
        f"• <b>Dead Letter:</b> <code>{dl_cnt}</code>\n"
        f"• <b>Expired Lease Recoveries:</b> <code>{rec_cnt}</code>\n"
        f"• <b>Oldest Pending Age:</b> <code>{oldest_str}</code>\n"
    )
    await m.reply_text(text, parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("reconcile_flog") & filters.private & filters.user(OWNER_ID))
async def cmd_reconcile_flog(c: Client, m: Message):
    if is_secondary_worker(c):
        return

    is_dry_run = "--dry-run" in (m.text or "")
    mode = "DRY_RUN" if is_dry_run else "REPAIR"
    wait_msg = await m.reply_text(f"⏳ Running FLOG reconciliation ({mode})...", quote=True)

    from FileStream.core.runtime.flog_sync import run_continuous_reconciliation
    report = await run_continuous_reconciliation(bot=c, mode=mode)

    disc = report.get("discrepancies", {})
    total_disc = report.get("total_discrepancies", 0)
    repaired = report.get("repaired", 0)

    log_admin_action(m.from_user.id, "RECONCILE_FLOG", f"mode={mode} found={total_disc} repaired={repaired}")
    await db.record_audit_event(
        action="RECONCILE_FLOG",
        actor=str(m.from_user.id),
        target_id="flog_reconciliation",
        details={"mode": mode, "found": total_disc, "repaired": repaired, "admin_id": m.from_user.id},
    )

    text = (
        f"<b>🔄 FLOG Reconciliation Report</b> ({mode})\n\n"
        f"• <b>Total Discrepancies:</b> {total_disc}\n"
        f"• <b>Total Repaired:</b> {repaired}\n\n"
        f"<b>Breakdown:</b>\n"
        f"• Missing FLOG Ref: {disc.get('MISSING_FLOG_REFERENCE', 0)}\n"
        f"• Orphan FLOG Msg: {disc.get('ORPHAN_FLOG_MESSAGE', 0)}\n"
        f"• Mismatched Ref: {disc.get('MISMATCHED_FLOG_REFERENCE', 0)}\n"
        f"• Stored Pending Job: {disc.get('STORED_FILE_PENDING_JOB', 0)}\n"
        f"• Failed with FLOG: {disc.get('FAILED_JOB_WITH_FLOG', 0)}\n"
        f"• Expired Lease: {disc.get('EXPIRED_LEASE', 0)}\n"
        f"• Stale Storage Status: {disc.get('STALE_STORAGE_STATUS', 0)}\n"
    )
    if report.get("errors"):
        text += f"\n<b>⚠️ Errors:</b>\n" + "\n".join(f"• {html.escape(str(e))}" for e in report["errors"][:5])

    await wait_msg.edit_text(text, parse_mode=ParseMode.HTML)


@FileStream.on_message(filters.command("worker_status") & filters.private)
async def cmd_worker_status(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    from FileStream.services.storage_service import storage_service
    metrics = storage_service.get_metrics()
    workers_health = await db.list_workers_health()

    lines = [
        f"<b>👷 Storage Worker Pool Status</b>\n",
        f"• <b>Active Workers:</b> {metrics.get('storage_workers', 0)}",
        f"• <b>Min/Max Allowed:</b> {getattr(Telegram, 'FLOG_WORKERS_MIN', 2)} / {getattr(Telegram, 'FLOG_WORKERS_MAX', 6)}",
        f"• <b>Queue Size:</b> {metrics.get('storage_queue_size', 0)} (Peak: {metrics.get('storage_queue_peak', 0)})\n",
    ]

    if not workers_health:
        lines.append("<i>No persisted worker health records found yet.</i>")
    else:
        now = time.time()
        for w in workers_health[:15]:
            wid = html.escape(str(w.get("worker_id", "N/A")))
            state = html.escape(str(w.get("health_state", "UNKNOWN")))
            draining = " (DRAINING)" if w.get("draining") else ""
            hb_age = round(now - w.get("last_heartbeat", now), 1)
            succ = w.get("jobs_completed", w.get("jobs_processed", 0))
            fail = w.get("jobs_failed", w.get("errors_count", 0))
            lines.append(
                f"• <code>{wid}</code>: <b>{state}</b>{draining}\n"
                f"  Heartbeat: {hb_age}s ago | Jobs: {succ} succ, {fail} err"
            )

    await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("client_status") & filters.private)
async def cmd_client_status(c: Client, m: Message):
    if is_secondary_worker(c):
        return
    if m.from_user.id not in ADMIN_IDS:
        return

    from FileStream.services.storage_service import storage_service
    from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry

    stats = storage_service.get_client_stats()
    registry = get_circuit_breaker_registry()

    lines = [f"<b>🤖 Storage Clients & Circuit Breakers</b>\n"]
    if not stats:
        lines.append("<i>No client stats available yet.</i>")
    else:
        for idx in sorted(stats.keys()):
            s = stats[idx]
            breaker = registry.get_breaker(idx)
            b_state = breaker.state.value if breaker else "UNKNOWN"
            alive = "🟢" if s.get("is_alive") else "🔴"
            disabled = " [DISABLED]" if s.get("disabled") else ""
            in_cd = "⏳ Cooldown" if s.get("in_cooldown") else "Ready"
            cd_rem = f" ({s.get('cooldown_remaining_s')}s)" if s.get("in_cooldown") else ""

            lines.append(
                f"• {alive} <b>Client {idx}</b>{disabled} | Breaker: <code>{b_state}</code>\n"
                f"  Status: {in_cd}{cd_rem} | Inflight: {s.get('current_inflight', 0)}\n"
                f"  Completed: {s.get('successful_sends', 0)} | Failed: {s.get('failed_sends', 0)} | FloodWait: {s.get('floodwait_count', 0)}"
            )

    await m.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("drain_worker") & filters.private & filters.user(OWNER_ID))
async def cmd_drain_worker(c: Client, m: Message):
    if is_secondary_worker(c):
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/drain_worker &lt;worker_id&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    worker_id = m.command[1].strip()
    from FileStream.services.storage_service import storage_service
    storage_service.drain_worker(worker_id)
    await db.set_worker_health_state(worker_id, state="DRAINING", draining=True)
    log_admin_action(m.from_user.id, "DRAIN_WORKER", f"worker_id={worker_id}")
    await db.record_audit_event(
        action="DRAIN_WORKER",
        actor=str(m.from_user.id),
        target_id=worker_id,
        details={"admin_id": m.from_user.id, "worker_id": worker_id},
    )
    await m.reply_text(f"✅ Worker <code>{html.escape(worker_id)}</code> marked for graceful draining.", parse_mode=ParseMode.HTML, quote=True)


@FileStream.on_message(filters.command("disable_client") & filters.private & filters.user(OWNER_ID))
async def cmd_disable_client(c: Client, m: Message):
    if is_secondary_worker(c):
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/disable_client &lt;client_idx&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    try:
        idx = int(m.command[1].strip())
    except ValueError:
        await m.reply_text("Client index must be an integer (e.g. 0, 1, 2).", quote=True)
        return

    from FileStream.services.storage_service import storage_service
    ok = storage_service.disable_client(idx)
    if ok:
        log_admin_action(m.from_user.id, "DISABLE_CLIENT", f"client_idx={idx}")
        await db.record_audit_event(
            action="DISABLE_CLIENT",
            actor=str(m.from_user.id),
            target_id=str(idx),
            details={"admin_id": m.from_user.id, "client_idx": idx},
        )
        await m.reply_text(f"✅ Client <code>{idx}</code> has been disabled from handling storage jobs.", parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(f"❌ Client <code>{idx}</code> not found in storage client pool.", quote=True)


@FileStream.on_message(filters.command("enable_client") & filters.private & filters.user(OWNER_ID))
async def cmd_enable_client(c: Client, m: Message):
    if is_secondary_worker(c):
        return

    if len(m.command) < 2:
        await m.reply_text("Usage: <code>/enable_client &lt;client_idx&gt;</code>", parse_mode=ParseMode.HTML, quote=True)
        return

    try:
        idx = int(m.command[1].strip())
    except ValueError:
        await m.reply_text("Client index must be an integer (e.g. 0, 1, 2).", quote=True)
        return

    from FileStream.services.storage_service import storage_service
    ok = storage_service.enable_client(idx)
    if ok:
        log_admin_action(m.from_user.id, "ENABLE_CLIENT", f"client_idx={idx}")
        await db.record_audit_event(
            action="ENABLE_CLIENT",
            actor=str(m.from_user.id),
            target_id=str(idx),
            details={"admin_id": m.from_user.id, "client_idx": idx},
        )
        await m.reply_text(f"✅ Client <code>{idx}</code> has been enabled for storage jobs.", parse_mode=ParseMode.HTML, quote=True)
    else:
        await m.reply_text(f"❌ Client <code>{idx}</code> not found in storage client pool.", quote=True)
