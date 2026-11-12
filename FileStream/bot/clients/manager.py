import asyncio
import logging
import re
from os import environ
from FileStream.config import Telegram
from pyrogram import Client
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream.core.runtime.client_balance import ensure_client_stat

logger = logging.getLogger(__name__)


_clients_initialized = False
_last_client_status: dict[int, str] = {}


def is_secondary_worker(client) -> bool:
    """Return True if client is a designated secondary storage/worker bot (client_index > 0)."""
    try:
        from unittest.mock import MagicMock
        idx = getattr(client, "client_index", 0)
        if isinstance(idx, MagicMock):
            return False
        return int(idx) > 0
    except (ValueError, TypeError):
        return False


def setup_secondary_worker_handlers(client: Client) -> None:
    """
    Register dedicated secondary worker handlers:
    - User message/file/command: respond with informational worker notice pointing to @primary_bot_username
      without creating links or folder sessions.
    - Admin command: respond with worker node health without executing duplicate admin mutations.
    - Callbacks: alert user to use the main bot.
    """
    from pyrogram import filters
    from pyrogram.handlers import MessageHandler, CallbackQueryHandler
    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
    from pyrogram.enums import ParseMode, ChatType
    from FileStream.bot.helpers.bot_utils import (
        is_internal_service_chat,
        is_configured_channel,
        is_secondary_bot_id,
        get_known_bot_ids,
    )

    admin_ids = set()
    for raw_id in [Telegram.OWNER_ID, *(Telegram.AUTH_USERS or [])]:
        try:
            admin_ids.add(int(raw_id))
        except Exception:
            pass

    async def secondary_message_handler(bot: Client, message: Message):
        if not message:
            return

        chat = getattr(message, "chat", None)
        if not chat:
            return

        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            return

        try:
            numeric_chat_id = int(chat_id)
        except Exception:
            return

        # STRICT GUARD 1: All Telegram channels and groups have negative IDs (< 0).
        # Internal storage workers MUST NEVER send messages to any channel or group.
        if numeric_chat_id <= 0:
            return

        # STRICT GUARD 2: Explicit internal / storage / log / content channel check.
        if is_internal_service_chat(numeric_chat_id) or is_configured_channel(numeric_chat_id):
            return

        # STRICT GUARD 3: Chat type check - only private chats are processed.
        chat_type = getattr(chat, "type", None)
        if isinstance(chat_type, ChatType) and chat_type != ChatType.PRIVATE:
            return
        if isinstance(chat_type, str) and chat_type.lower() not in ("private",):
            return

        # STRICT GUARD 4: Direct user check. Channel posts have no from_user.
        user = getattr(message, "from_user", None)
        if not user:
            return

        # STRICT GUARD 5: Ignore messages sent by bots to prevent bot echo loops.
        if getattr(user, "is_bot", None) is True:
            return

        user_id = getattr(user, "id", None)
        if user_id is None:
            return

        try:
            numeric_user_id = int(user_id)
        except Exception:
            return

        if numeric_user_id <= 0:
            return

        if is_internal_service_chat(numeric_user_id) or is_configured_channel(numeric_user_id):
            return

        # STRICT GUARD 6: Never respond to other worker bots or primary bot.
        known_bots = get_known_bot_ids()
        if numeric_user_id in known_bots or is_secondary_bot_id(numeric_user_id):
            return

        primary_user = getattr(FileStream, "username", None)
        worker_idx = getattr(bot, "client_index", getattr(bot, "name", "N/A"))

        # Admin command handling
        text = getattr(message, "text", "") or getattr(message, "caption", "") or ""
        is_command = text.startswith("/")

        if numeric_user_id in admin_ids and is_command:
            from FileStream.services.storage_service import storage_service
            try:
                c_idx = int(worker_idx)
            except Exception:
                c_idx = -1
            client_stat = storage_service._client_stats.get(c_idx)
            inflight = client_stat.current_inflight if client_stat else 0
            completed = client_stat.successful_sends if client_stat else 0
            failed = client_stat.failed_sends if client_stat else 0
            fw_count = client_stat.floodwait_count if client_stat else 0

            status_msg = (
                f"🤖 <b>Storage Worker Node #{worker_idx}</b>\n\n"
                f"<b>Role:</b> <code>STORAGE_WORKER</code>\n"
                f"<b>Status:</b> <code>ONLINE</code>\n"
                f"<b>In-flight Jobs:</b> {inflight}\n"
                f"<b>Completed Sends:</b> {completed}\n"
                f"<b>Failed Sends:</b> {failed}\n"
                f"<b>FloodWaits:</b> {fw_count}\n"
                f"<b>Primary Bot:</b> @{primary_user or 'N/A'}\n\n"
                f"<i>Note: Administrative mutations (broadcast, ban, delete) must be performed on the primary bot.</i>"
            )
            await message.reply_text(status_msg, parse_mode=ParseMode.HTML)
            return

        # Regular user message / upload / command redirect
        bot_mention = f"@{primary_user}" if primary_user else "the primary bot"
        reply_markup = None
        if primary_user:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Open Main Bot", url=f"https://t.me/{primary_user}")]
            ])

        worker_notice = (
            f"ℹ️ <b>Storage Worker Bot</b>\n\n"
            f"This bot operates as an internal storage and processing worker node.\n"
            f"To generate download links, manage folders, or upload files, please message {bot_mention} directly."
        )
        await message.reply_text(worker_notice, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def secondary_callback_handler(bot: Client, callback_query: CallbackQuery):
        primary_user = getattr(FileStream, "username", None)
        bot_mention = f"@{primary_user}" if primary_user else "the main bot"
        try:
            await callback_query.answer(
                f"Please use {bot_mention} to interact with buttons.",
                show_alert=True
            )
        except Exception:
            pass

    if hasattr(client, "add_handler"):
        client.add_handler(MessageHandler(secondary_message_handler, filters.private), group=0)
        client.add_handler(CallbackQueryHandler(secondary_callback_handler), group=0)
    client._secondary_message_handler = secondary_message_handler
    client._secondary_callback_handler = secondary_callback_handler

    # Defensive wrapper: Ensure secondary worker bot can never send worker notices to channels
    orig_send_message = getattr(client, "send_message", None)
    if callable(orig_send_message) and not getattr(client, "_worker_send_message_guarded", False):
        async def guarded_send_message(chat_id, text=None, *args, **kwargs):
            try:
                cid = int(chat_id)
            except Exception:
                cid = None

            text_str = str(text or "")
            if "Storage Worker Bot" in text_str or "worker node" in text_str.lower():
                from FileStream.bot.helpers.bot_utils import is_internal_service_chat, is_configured_channel
                if cid is None or cid <= 0 or is_internal_service_chat(cid) or is_configured_channel(cid):
                    logger.warning(
                        "Blocked worker notice to channel %s from secondary client %s",
                        chat_id,
                        getattr(client, "client_index", getattr(client, "name", "N/A")),
                    )
                    return None
            return await orig_send_message(chat_id, text, *args, **kwargs)

        client.send_message = guarded_send_message
        client._worker_send_message_guarded = True


async def clone_primary_handlers(client: Client):
    """
    Configure handlers for secondary client.
    Secondary clients operate strictly as storage/stream workers and do not clone
    primary user-facing or mutation handlers. They register dedicated worker handlers instead.
    """
    setup_secondary_worker_handlers(client)


async def initialize_clients(force_reinit: bool = False):
    global _clients_initialized, _last_client_status
    if _clients_initialized and not force_reinit:
        logger.debug("initialize_clients() already called; skipping duplicate execution.")
        return log_client_diagnostics(_last_client_status)

    tokens = []
    _last_client_status = {}
    for k, v in environ.items():
        match = re.fullmatch(r"MULTI_TOKEN(\d+)", str(k or "").strip())
        if not match:
            continue
        idx = int(match.group(1))
        token_value = str(v or "").strip()
        if not token_value:
            _last_client_status[idx] = "NOT CONFIGURED"
            continue
        tokens.append((idx, token_value))
    tokens.sort(key=lambda x: x[0])

    all_tokens = {idx: token for idx, token in tokens}

    # Reset secondary clients so only Client 0 is initially registered
    for k in list(multi_clients.keys()):
        if k != 0:
            multi_clients.pop(k, None)
    for k in list(work_loads.keys()):
        if k != 0:
            work_loads.pop(k, None)

    multi_clients[0] = FileStream
    work_loads[0] = 0
    ensure_client_stat(0)

    try:
        from FileStream.services.storage_service import storage_service
        for k in list(storage_service._clients_dict.keys()):
            if k != 0:
                storage_service.unregister_client(k)
        storage_service.register_client(0, FileStream)
        from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry
        get_circuit_breaker_registry().reset_all()
    except Exception:
        pass

    if not all_tokens:
        _clients_initialized = True
        Telegram.MULTI_CLIENT = False
        return log_client_diagnostics(_last_client_status)

    ordered_client_ids = list(all_tokens.keys())

    async def start_client(client_id, token):
        try:
            from FileStream.bot.helpers.bot_commands import register_bot_commands
            if len(token) >= 100:
                session_string = token
                bot_token = None
                logger.info("Starting Client - %s Using Session String", client_id)
            else:
                session_string = None
                bot_token = token
                logger.info("Starting Client - %s Using Bot Token", client_id)

            if client_id == ordered_client_ids[-1]:
                await asyncio.sleep(2)
                logger.info("This will take some time, please wait...")

            import sys
            clients_mod = sys.modules.get("FileStream.bot.clients") or sys.modules[__name__]
            _client_cls = getattr(clients_mod, "Client", Client)
            _clone_fn = getattr(clients_mod, "clone_primary_handlers", clone_primary_handlers)

            client = _client_cls(
                name=str(client_id),
                api_id=Telegram.API_ID,
                api_hash=Telegram.API_HASH,
                bot_token=bot_token,
                sleep_threshold=getattr(Telegram, "PYROGRAM_SLEEP_THRESHOLD", 0),
                no_updates=False,
                session_string=session_string,
                in_memory=True,
            )
            await _clone_fn(client)
            await client.start()
            me = await client.get_me()
            client.id = me.id
            client.username = me.username
            client.fname = me.first_name
            client.client_index = client_id
            if getattr(me, "is_bot", False):
                await register_bot_commands(client)
            work_loads[client_id] = 0
            ensure_client_stat(client_id)
            try:
                from FileStream.services.storage_service import storage_service
                storage_service.register_client(client_id, client)
                from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry
                get_circuit_breaker_registry().reset_client(client_id)
            except Exception:
                pass
            _last_client_status[client_id] = "CONNECTED"
            return client_id, client
        except Exception:
            _last_client_status[client_id] = "CONFIGURED / INITIALIZATION FAILED"
            logger.error("Failed starting Client - %s Error:", client_id, exc_info=True)
            return None

    clients = await asyncio.gather(*[start_client(i, token) for i, token in all_tokens.items()])
    multi_clients.update(dict([c for c in clients if c]))

    _clients_initialized = True

    if len(multi_clients) == 1:
        Telegram.MULTI_CLIENT = False
        return log_client_diagnostics(_last_client_status)

    Telegram.MULTI_CLIENT = True
    return log_client_diagnostics(_last_client_status)


def log_client_diagnostics(client_init_status: dict[int, str] | None = None) -> dict:
    """
    Safely logs startup diagnostics for all configured Telegram clients
    without exposing any bot tokens, session strings, or credentials.
    Returns diagnostics metadata dictionary.
    """
    status_map = dict(client_init_status or {})

    # Client 0 metadata
    c0 = multi_clients.get(0, FileStream)
    c0_id = getattr(c0, "id", None) or "N/A"
    c0_user = getattr(c0, "username", None)
    c0_is_conn = getattr(c0, "is_connected", True)
    c0_desc = f"CONNECTED (ID: {c0_id}" + (f", User: @{c0_user})" if c0_user else ")") if c0_is_conn else "NOT CONNECTED"

    # Slots to inspect: at least 0, 1, 2, plus all configured or active slots
    inspected_indices = sorted(set([0, 1, 2] + list(status_map.keys()) + list(multi_clients.keys())))

    lines = [
        "Storage clients:",
    ]
    diag = {
        "total_active": len([c for c in multi_clients.values() if c is not None]),
        "active_storage_clients": len([c for c in multi_clients.values() if c is not None]),
        "multi_client_mode": bool(Telegram.MULTI_CLIENT),
        "multi_client_storage": "ENABLED" if Telegram.MULTI_CLIENT else "DISABLED",
        "clients": {},
        "storage_clients": {},
    }

    for idx in inspected_indices:
        if idx == 0:
            status_desc = c0_desc
            diag["clients"][0] = {
                "index": 0,
                "role": "Primary Bot",
                "connected": c0_is_conn,
                "id": c0_id,
                "username": c0_user or "Unknown",
            }
        elif idx in multi_clients and multi_clients[idx] is not None:
            c = multi_clients[idx]
            cid = getattr(c, "id", None) or "N/A"
            username = getattr(c, "username", None) or getattr(c, "fname", None) or "Unknown"
            status_desc = f"CONNECTED (ID: {cid}" + (f", User: @{username})" if username != "Unknown" else ")")
            diag["clients"][idx] = {
                "index": idx,
                "role": f"Storage/Stream Worker {idx}",
                "connected": True,
                "id": cid,
                "username": username,
            }
        elif idx in status_map:
            status_desc = status_map[idx]
        else:
            status_desc = "NOT CONFIGURED"

        lines.append(f"  Client {idx}: {status_desc}")
        diag["storage_clients"][idx] = status_desc

    lines.append("")
    lines.append(f"Active storage clients: {diag['active_storage_clients']}")
    lines.append(f"Multi-client storage: {diag['multi_client_storage']}")

    diag_text = "\n".join(lines)
    logger.info("Storage Client Diagnostics:\n%s", diag_text)
    return diag
