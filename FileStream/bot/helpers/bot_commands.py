import html
import logging

from pyrogram import Client
from pyrogram.errors import PeerIdInvalid, FloodWait
from pyrogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from FileStream.config import Telegram


# =====================================================================
# PUBLIC COMMANDS PRIORITY HIERARCHY (STRATEGIC UX)
# =====================================================================
# 1.  /donation  - Support visibility (Telegram Stars contribution)
# 2.  /ping      - Quick responsiveness & latency check
# 3.  /start     - Primary onboarding & core link-generation home
# 4.  /help      - Product education & workflow assistance
# 5.  /files     - Primary asset access & link management
# 6.  /folders   - Structured collections navigation
# 7.  /folder    - Folder/batch creation workflow
# 8.  /done      - Action completion (e.g. finalizing batch)
# 9.  /cancel    - Action cancellation & workflow reset
# 10. /about     - Bot details, version & project info
# 11. /privacy   - Privacy policy & user data handling
# 12. /legal     - Terms & legal information
# 13. /id        - Telegram user ID lookup for support
# =====================================================================

PUBLIC_COMMANDS = [
    BotCommand("donation", "Support the bot with Stars"),
    BotCommand("ping", "Check bot response"),
    BotCommand("start", "Start using File To Link V2"),
    BotCommand("help", "Learn how to use the bot"),
    BotCommand("files", "View your files"),
    BotCommand("folders", "Manage your folders"),
    BotCommand("folder", "Create a folder"),
    BotCommand("done", "Finish the current action"),
    BotCommand("cancel", "Cancel the current action"),
    BotCommand("about", "About File To Link V2"),
    BotCommand("privacy", "Privacy Policy"),
    BotCommand("legal", "Terms and Legal Information"),
    BotCommand("id", "Get your Telegram ID"),
]

# Backwards compatibility references
PRIMARY_PUBLIC_COMMANDS = PUBLIC_COMMANDS
CONTEXTUAL_COMMANDS = [cmd for cmd in PUBLIC_COMMANDS if cmd.command in ("done", "cancel")]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("admin", "Admin panel"),
    BotCommand("flogstorage", "Manage upload storage"),
    BotCommand("urlshortener", "Toggle short links"),
    BotCommand("webads", "Toggle web ads"),
    BotCommand("speedtest", "Run a speed test"),
    BotCommand("botstatus", "Check multi-bot status"),
    BotCommand("rm_adult", "Review reported content"),
    BotCommand("storage_jobs", "Inspect recent storage jobs"),
    BotCommand("storage_job", "Inspect a storage job by ID"),
    BotCommand("retry_storage_job", "Retry a failed/dead-letter storage job"),
    BotCommand("cancel_storage_job", "Cancel a storage job"),
    BotCommand("storage_queue", "Storage queue diagnostics"),
    BotCommand("worker_status", "Worker pool health status"),
    BotCommand("client_status", "Client health & circuit breakers"),
]

OWNER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand("status", "System status"),
    BotCommand("ban", "Restrict a user"),
    BotCommand("unban", "Restore a user"),
    BotCommand("broadcast", "Send broadcast"),
    BotCommand("del", "Delete a file"),
    BotCommand("linkinfo", "Inspect a public link"),
    BotCommand("revoke_link", "Revoke a public link"),
    BotCommand("regen_link", "Regenerate a public link"),
    BotCommand("expire_link", "Set or clear link expiry"),
    BotCommand("reconcile_flog", "Reconcile FLOG storage"),
    BotCommand("drain_worker", "Drain a worker gracefully"),
    BotCommand("disable_client", "Disable a client"),
    BotCommand("enable_client", "Enable a client"),
]


def _unique_admin_ids():
    seen = set()
    admin_ids = []
    for raw_id in [Telegram.OWNER_ID, *(Telegram.AUTH_USERS or [])]:
        try:
            admin_id = int(raw_id)
        except Exception:
            continue
        if admin_id <= 0 or admin_id in seen:
            continue
        seen.add(admin_id)
        admin_ids.append(admin_id)
    return admin_ids


async def register_bot_commands(bot: Client):
    if not bot:
        logging.debug("Skipping bot command registration: bot client is None")
        return
    try:
        me = getattr(bot, "me", None) or await bot.get_me()
        if not getattr(me, "is_bot", False):
            logging.debug("Skipping bot command registration for non-bot client %s", getattr(bot, "name", ""))
            return
    except Exception as exc:
        logging.warning("Could not determine bot identity for command registration: %s", exc)
        return

    # 1. Register global default scope (fallback for Telegram clients & Menu button)
    try:
        await bot.set_bot_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    except Exception as exc:
        logging.warning("Failed to register default bot commands: %s", exc)

    # 2. Register all private chats scope (explicit 1-on-1 private chat list)
    try:
        await bot.set_bot_commands(PUBLIC_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        logging.warning("Failed to register public private chat bot commands: %s", exc)

    # 3. Register scoped commands for Owner and Admins
    try:
        owner_id = int(Telegram.OWNER_ID)
    except Exception:
        owner_id = 0

    registered_admins = 0
    for admin_id in _unique_admin_ids():
        commands = OWNER_COMMANDS if admin_id == owner_id else ADMIN_COMMANDS
        try:
            await bot.set_bot_commands(commands, scope=BotCommandScopeChat(chat_id=admin_id))
            registered_admins += 1
        except PeerIdInvalid:
            logging.debug("Admin %s has not started the bot yet; skipping chat-scoped commands", admin_id)
        except FloodWait as fw:
            logging.warning("FloodWait while registering commands for admin %s: sleep %ss", admin_id, getattr(fw, "value", 0))
        except Exception as exc:
            logging.warning("Failed to register bot commands for admin %s: %s", admin_id, exc)

    logging.info(
        "Bot commands registration complete for @%s (%s public commands, %s admin scopes)",
        getattr(me, "username", "bot"),
        len(PUBLIC_COMMANDS),
        registered_admins,
    )


def build_admin_help_text() -> str:
    shared_admin = [
        ("/admin", "Show this admin command list"),
        ("/flogstorage <main|admin|status|list> [user_id]", "Route uploads between MAIN and ADMIN storage"),
        ("/urlshortener <on|off>", "Enable or disable the shortener"),
        ("/webads <on|off>", "Enable or disable web ads"),
        ("/speedtest", "Run a server speed test"),
        ("/botstatus", "Check multi-bot status"),
        ("/rm_adult <file_id|folder_id>", "Remove adult content manually"),
        ("/storage_jobs [status] [limit]", "Inspect recent storage jobs"),
        ("/storage_job <job_id>", "Inspect storage job by ID"),
        ("/retry_storage_job <job_id>", "Retry a failed/dead-letter storage job"),
        ("/cancel_storage_job <job_id>", "Cancel a storage job"),
        ("/storage_queue", "Storage queue diagnostics"),
        ("/worker_status", "Worker pool health status"),
        ("/client_status", "Client health & circuit breakers"),
    ]
    owner_only = [
        ("/status", "Show system and database stats"),
        ("/ban <user_id>", "Restrict a user"),
        ("/unban <user_id>", "Restore a user"),
        ("/broadcast", "Reply to a message and broadcast it"),
        ("/del <file_id>", "Remove a file link"),
        ("/linkinfo <public_id|file:<id>|folder:<id>>", "Inspect a public link"),
        ("/revoke_link <public_id|file:<id>|folder:<id>>", "Revoke a public link"),
        ("/regen_link <public_id|file:<id>|folder:<id>>", "Generate a new public link"),
        ("/expire_link <public_id|file:<id>|folder:<id>> <now|clear|hours>", "Set or clear link expiry"),
        ("/reconcile_flog [--dry-run]", "Reconcile FLOG storage channels"),
        ("/drain_worker <worker_id>", "Drain a worker gracefully"),
        ("/disable_client <client_idx>", "Disable a client from storage"),
        ("/enable_client <client_idx>", "Enable a client for storage"),
    ]

    def render(lines):
        return "\n".join(
            f"• <code>{html.escape(command)}</code> — {html.escape(description)}"
            for command, description in lines
        )

    return (
        "<b>🛠 Admin Commands</b>\n\n"
        "<b>Shared Admin</b>\n"
        f"{render(shared_admin)}\n\n"
        "<b>Owner Only</b>\n"
        f"{render(owner_only)}"
    )
