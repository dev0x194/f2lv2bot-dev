"""Centralized Telegram Bot User-Facing Response Messages.

This module is the canonical source for all user-visible text shown to Telegram
users across commands, callbacks, alerts, prompts, notifications, and confirmations.

Rules:
- Strictly Telegram user-facing content only (no logs, internal errors, or web-page HTML).
- Keep this module lightweight with zero heavy dependencies (standard library only)
  to avoid circular imports.
"""

from __future__ import annotations
import html
from typing import Any
from FileStream import __version__


# =====================================================================
# 1. PUBLIC / GENERAL BOT MESSAGES
# =====================================================================

class _StartTemplate(str):
    """Minimalist template for START message supporting personalized format()
    and legacy direct string usage."""
    def format(self, *args, **kwargs):
        from FileStream.bot.helpers.ui import format_home_text
        user_arg = args[0] if len(args) > 0 else (kwargs.get("user") or kwargs.get("name"))
        bot_username = args[1] if len(args) > 1 else kwargs.get("bot")
        return format_home_text(user=user_arg, bot_username=bot_username)


class _AboutTemplate(str):
    """Compact template for ABOUT message supporting both format()
    and direct string usage."""
    def format(self, *args, **kwargs):
        from FileStream.bot.helpers.ui import format_about_text
        bot_name = args[0] if len(args) > 0 else (kwargs.get("bot") or "File To Link V2 bot")
        version = args[1] if len(args) > 1 else (kwargs.get("version") or __version__)
        return format_about_text(bot_name=bot_name, version=version)


class _PingTemplate(str):
    """Template for PING message supporting positional format(latency),
    keyword format(latency=...), and direct string usage."""
    def format(self, *args, **kwargs):
        latency = args[0] if len(args) > 0 else kwargs.get("latency", "0.0")
        return (
            "🏓 <b>Pong</b>\n\n"
            f"⚡ <code>{latency} ms</code>"
        )


class _FilesTemplate(str):
    """Template for /files screen supporting empty and populated states."""
    def format(self, *args, **kwargs):
        total = args[0] if len(args) > 0 else kwargs.get("total", 0)
        try:
            total_int = int(total)
        except Exception:
            total_int = 0
        if total_int == 0:
            return (
                "📄 <b>Your Files</b>\n\n"
                "No shared files yet.\n\n"
                "Send or forward a file to create your first link."
            )
        suffix = "file" if total_int == 1 else "files"
        return (
            "📄 <b>Your Files</b>\n\n"
            f"Total: <b>{total_int}</b> {suffix}\n\n"
            "Select a file to view links or manage."
        )


class PublicMessages:
    """Messages for public commands (/start, /help, /about, /ping, /id, /files)."""

    PING = _PingTemplate(
        "🏓 <b>Pong</b>\n\n"
        "⚡ <code>{latency} ms</code>"
    )

    START = _StartTemplate(
        "👋 <b>Hello!</b>\n\n"
        "Fast streaming & direct download links for your files.\n\n"
        "<blockquote>📤 <b>Send a file</b>\n"
        "Forward or upload any file here to get an instant direct link.</blockquote>"
    )

    HELP = (
        "❓ <b>Help & Guide</b>\n\n"
        "📤 <b>Send a file</b>\n"
        "Forward or upload any file to generate a direct link.\n\n"
        "🔗 <b>Direct Links</b>\n"
        "Stream videos online or download files at maximum speed.\n\n"
        "📁 <b>Folders</b>\n"
        "Organize multiple files into shareable collections.\n\n"
        "<blockquote expandable>⚙️ <b>Commands</b>\n"
        "• <code>/start</code> — Home screen\n"
        "• <code>/files</code> — Your uploaded files\n"
        "• <code>/folders</code> — Your folder collections\n"
        "• <code>/settings</code> — Preferences & notifications\n"
        "• <code>/donation</code> — Support the bot with Stars\n"
        "• <code>/ping</code> — Check bot latency\n\n"
        "Need personal assistance? Contact <a href='tg://user?id={}'>the administrator</a>.</blockquote>"
    )

    ABOUT = _AboutTemplate(
        "ℹ️ <b>File To Link V2 bot</b>\n\n"
        "Simple file sharing through Telegram.\n\n"
        "• ▶️ <b>Stream</b> — Watch videos without downloading\n"
        "• 📥 <b>Download</b> — High-speed direct downloads\n"
        "• 🔗 <b>Share</b> — Instant shareable links\n"
        "• 📁 <b>Organize</b> — Multi-file folder collections\n\n"
        f"<b>Version:</b> <code>{__version__}</code>"
    )


    DASHBOARD_FILES = _FilesTemplate(
        "📄 <b>Your Files</b>\n\n"
        "Select a file to view links or manage."
    )

    USER_ID = (
        "🆔 <b>Your Telegram ID</b>\n\n"
        "<code>{}</code>"
    )

    USER_FILES_COUNT = "Total links: {}"


# =====================================================================
# 2. FILE & STREAMING MESSAGES
# =====================================================================

class _ReadyTemplate(str):
    """Custom string template for FileMessages.READY that flexibly supports both
    legacy 4-argument calls and standard 7/8-argument calls from gen_link / gen_linkx,
    ensuring proper bot branding and preserving all dynamic file/access parameters."""

    def format(self, *args, **kwargs):
        if len(args) == 4:
            return (
                "📄 <b>Link Ready</b>\n\n"
                f"<b>{args[0]}</b>\n\n"
                f"📦 {args[1]} • 🏷 {args[2]}\n"
                f"⏱ {args[3]}\n\n"
                "🤖 <b>File To Link V2 bot</b>"
            )
        if len(args) >= 7:
            name, size, cat, countdown, absolute, bot_id1, public_url = args[:7]
            bot_handle = f"@{bot_id1}" if bot_id1 and not bot_id1.startswith("@") and bot_id1 != "File To Link V2 bot" and " " not in bot_id1 else ""
            bot_line = f"🤖 <b>File To Link V2 bot</b> ({bot_handle})" if bot_handle else "🤖 <b>File To Link V2 bot</b>"
            if str(absolute).lower() == "permanent" or str(countdown).lower() == "never expires":
                expiry_section = "⏱ Permanent • Never expires"
            else:
                expiry_section = f"⏱ Available for {countdown}\n<i>Expires {absolute}</i>"
            return (
                "📄 <b>Link Ready</b>\n\n"
                f"<b>{name}</b>\n\n"
                f"📦 {size} • 🏷 {cat}\n"
                f"{expiry_section}\n\n"
                "🔗 <b>Link</b>\n"
                f"<code>{public_url}</code>\n\n"
                f"{bot_line}"
            )
        return super().format(*args, **kwargs)


class _FileDetailsTemplate(str):
    """Template for file details."""
    def format(self, *args, **kwargs):
        if len(args) >= 3:
            name = args[0]
            size = args[1]
            ftype = args[2]
            return (
                f"📄 <b>{name}</b>\n\n"
                f"Size: <code>{size}</code> • Type: <code>{ftype}</code>\n\n"
                "🔗 Direct link ready."
            )
        return super().format(*args, **kwargs)


class FileMessages:
    """Messages for file generation, details, media captions, and deletion."""

    READY = _ReadyTemplate(
        "📄 <b>Link Ready</b>\n\n"
        "<b>{0}</b>\n\n"
        "📦 {1} • 🏷 {2}\n"
        "⏱ Available for {3}\n"
        "<i>Expires {4}</i>\n\n"
        "🔗 <b>Link</b>\n"
        "<code>{6}</code>\n\n"
        "🤖 <b>File To Link V2 bot</b>"
    )

    CAPTION = (
        "📄 <b>{}</b>\n\n"
        "⏱️ This file is temporary and will be automatically deleted from chat after 1 hour for privacy."
    )

    PROCESSING = "⏳ <b>Processing file…</b>"

    POST_SUCCESS_TIP = "💡 <b>Tip:</b> Need to share several files together? Try <code>/folder</code> to create a collection."

    SENDING = "📤 Sending file…"

    SEND_FAILED = (
        "⚠️ <b>Send Failed</b>\n\n"
        "The file may be too large or temporarily unavailable.\n"
        "Try the share page instead."
    )

    ALREADY_DELETED = "ℹ️ Already removed."

    DELETED = "✓ File removed"

    DELETE_CONFIRM = (
        "🗑️ <b>Delete this file?</b>\n\n"
        "This action cannot be undone."
    )

    REVOKE_CONFIRM = (
        "🗑️ <b>Remove file?</b>\n\n"
        "This will remove the generated link."
    )

    DETAILS = _FileDetailsTemplate(
        "📄 <b>{0}</b>\n\n"
        "Size: <code>{1}</code> • Type: <code>{2}</code>\n\n"
        "🔗 Direct link ready."
    )


class _ModeEnabledTemplate(str):
    """Template for /folder create workflow."""
    def format(self, *args, **kwargs):
        title = args[0] if len(args) > 0 else kwargs.get("title")
        if title:
            return (
                f"📁 <b>Create Folder: {html.escape(str(title))}</b>\n\n"
                "Group multiple files into one shareable link.\n\n"
                "📤 <b>Send or forward files</b> to add them.\n"
                "Tap <b>Done</b> or send <code>/done</code> when finished."
            )
        return (
            "📁 <b>Create Folder</b>\n\n"
            "Group multiple files into one shareable link.\n\n"
            "📤 <b>Send or forward files</b> to add them.\n"
            "Tap <b>Done</b> or send <code>/done</code> when finished."
        )


class _FolderDetailsTemplate(str):
    """Template for folder details."""
    def format(self, *args, **kwargs):
        if len(args) >= 3:
            title = args[0]
            count = args[1]
            size = args[2]
            return (
                f"📁 <b>{title}</b>\n\n"
                f"{count} files · <code>{size}</code>\n\n"
                "🔗 Folder link ready."
            )
        return super().format(*args, **kwargs)


# =====================================================================
# 3. FOLDER & BATCH MESSAGES
# =====================================================================

class FolderMessages:
    """Messages for folder creation, management, batching, and navigation."""

    MODE_ENABLED = _ModeEnabledTemplate(
        "📁 <b>Create Folder</b>\n\n"
        "Group multiple files into one shareable link.\n\n"
        "📤 <b>Send or forward files</b> to add them.\n"
        "Tap <b>Done</b> or send <code>/done</code> when finished."
    )

    ADD_FILES_PROMPT = (
        "📤 <b>Add Files</b>\n\n"
        "Send or forward your files now.\n\n"
        "Multiple files are supported."
    )

    EMPTY_PROMPT = (
        "⚠️ <b>No files added yet.</b>\n\n"
        "Add at least one file before creating the folder."
    )

    MODE_STATUS = (
        "📁 <b>Folder Builder</b>\n\n"
        "Send files to add them.\n\n"
        "📦 Files: <code>{}</code>"
    )

    DONE = (
        "✅ <b>Folder Ready</b>\n\n"
        "{}\n"
        "Files: <b>{}</b>\n"
        "Size: <code>{}</code>\n"
        "Access: Never expires"
    )

    DONE_SUCCESS = (
        "✅ <b>Folder Ready</b>\n\n"
        "📁 <b>{}</b>\n"
        "📦 <b>{} files</b> • 💾 <code>{}</code>\n\n"
        "🔗 <b>Link</b>\n"
        "<code>{}</code>\n\n"
        "💡 <i>Manage your collections anytime with /folders</i>"
    )

    EMPTY = (
        "⚠️ <b>No files added yet.</b>\n\n"
        "Add at least one file before creating the folder."
    )

    NOTHING_TO_FINISH = (
        "ℹ️ <b>Nothing to finish.</b>\n\n"
        "Use <code>/folder</code> to create a new folder collection."
    )

    NOTHING_RUNNING = "ℹ️ Nothing is currently running."

    ACTION_CANCELLED = "✕ Action cancelled."

    CANCELLED = (
        "✅ <b>Folder Cancelled</b>\n\n"
        "🗑 0 files cleaned up\n"
        "💾 Temporary data removed\n\n"
        "Folder Mode has been cancelled."
    )

    @staticmethod
    def cancelling_preparing(total: int) -> str:
        return (
            "🛑 <b>Cancelling Folder</b>\n\n"
            "Preparing cleanup...\n\n"
            f"📦 Files: <b>{total}</b>\n"
            f"🧹 Cleanup: <b>0/{total}</b>\n\n"
            "Please wait..."
        )

    @staticmethod
    def cancelling_progress(total: int, cleaned: int, progress_bar: str = "") -> str:
        remaining = max(0, total - cleaned)
        bar_line = f"\n\nProgress: {progress_bar}" if progress_bar else ""
        return (
            "🛑 <b>Cancelling Folder</b>\n\n"
            "🧹 Cleaning up files...\n\n"
            f"📦 Files: <b>{total}</b>\n"
            f"✅ Cleaned: <b>{cleaned}</b>\n"
            f"⏳ Remaining: <b>{remaining}</b>"
            f"{bar_line}"
        )

    @staticmethod
    def cancelled_final(cleaned: int) -> str:
        file_word = "files" if cleaned != 1 else "file"
        return (
            "✅ <b>Folder Cancelled</b>\n\n"
            f"🗑 {cleaned} {file_word} cleaned up\n"
            "💾 Temporary data removed\n\n"
            "Folder Mode has been cancelled."
        )

    UPLOAD_FAILED = (
        "⚠️ <b>Failed to add this file.</b>\n\n"
        "Please try again."
    )

    RENAME_PROMPT = (
        "✏️ <b>Rename Folder</b>\n\n"
        "Send a new name (1–50 characters).\n"
        "Send <code>/cancel</code> to abort."
    )

    RENAME_CANCELLED = "✕ Action cancelled."

    INVALID_NAME = "⚠️ Invalid name. Use 1–50 characters."

    RENAMED = "✓ Folder renamed"

    DETAILS = _FolderDetailsTemplate(
        "📁 <b>{0}</b>\n\n"
        "{1} files · <code>{2}</code>\n\n"
        "🔗 Folder link ready."
    )

    DELETE_CONFIRM = (
        "🗑️ <b>Delete this folder?</b>\n\n"
        "The folder link will stop working."
    )

    DELETED = "✓ Folder deleted"

    NOT_FOUND_ALERT = "🔍 Folder not found."

    NO_ACTIVE_FOLDER_ALERT = "📭 No active folder. Use /folder to start."

    MODE_ALREADY_ACTIVE_NOTE = "⚠️ Folder builder is already active."

    ALREADY_ADDED_WARN = "⚠️ File already added to this folder."

    @staticmethod
    def already_active(count: int, total_size: str = "") -> str:
        size_part = f"\n💾 Total: <code>{total_size}</code>" if total_size else ""
        return (
            "📁 <b>Folder Mode</b>\n\n"
            f"📦 Files added: <b>{count}</b>"
            f"{size_part}\n\n"
            "Send more files or tap <b>Done</b> when finished."
        )

    STILL_PROCESSING = "⏳ Please wait! {} file(s) are still processing."
    STILL_PROCESSING_ALERT = "⏳ Please wait! {} file(s) are still processing."

    @staticmethod
    def create_panel(count: int = 0, total_size: str = "0 B", recent_list: str = "", pending_count: int = 0) -> str:
        if count == 0 and pending_count == 0:
            return (
                "📁 <b>Create Folder</b>\n\n"
                "Start adding files to your folder.\n\n"
                "📦 Files: <b>0</b>\n"
                f"💾 Total: <code>{total_size or '0 B'}</code>\n\n"
                "Send or forward files to add them."
            )
        total_size_line = f"\n💾 Total: <code>{total_size}</code>" if total_size else ""
        if pending_count > 0:
            status_line = f"⏳ Processing: <b>{pending_count}</b>\n\n"
        else:
            status_line = "✅ Files updated\n\n"
        return (
            "📁 <b>Folder Status</b>\n\n"
            f"{status_line}"
            f"📦 Files added: <b>{count}</b>"
            f"{total_size_line}"
            f"{recent_list}"
        )

    @staticmethod
    def finalizing(count: int, total_size: str = "") -> str:
        size_line = f"\n💾 Total: <code>{total_size}</code>" if total_size else ""
        return (
            "📁 <b>Finalizing Folder…</b>\n\n"
            "Please wait while your folder is being prepared.\n\n"
            f"📦 Files: <b>{count}</b>"
            f"{size_line}"
        )

    @staticmethod
    def view_files_page(files: list[dict], page: int, total_pages: int, page_size: int = 5) -> str:
        from FileStream.core.runtime.human_readable import humanbytes
        start_idx = (page - 1) * page_size
        slice_items = files[start_idx: start_idx + page_size]
        lines = []
        for i, f in enumerate(slice_items, start=start_idx + 1):
            fname = f.get("file_name") or "file"
            if len(fname) > 40:
                fname = fname[:39] + "…"
            fsize = humanbytes(f.get("file_size") or 0)
            lines.append(f"{i}. <code>{html.escape(fname, quote=False)}</code> ({fsize})")

        file_list_text = "\n".join(lines) if lines else "<i>No files</i>"
        return (
            "📋 <b>Folder Files</b>\n\n"
            f"Page <b>{page}</b> / <b>{total_pages}</b>\n\n"
            f"{file_list_text}"
        )

    MODE_SEND_FILES_NOTE = "✅ Send or forward files now."

    MODE_FILES_ONLY_NOTE = "📁 <b>Folder mode is active.</b>\nPlease send files to add them, or tap <b>Done</b> when finished."

    RECOVERY_PROMPT = (
        "📁 <b>Unfinished Folder Session</b>\n\n"
        "You have an unfinished folder with <b>{}</b> file(s).\n\n"
        "Would you like to continue adding files or discard it?"
    )

    SESSION_DISCARDED = (
        "🗑️ <b>Folder session discarded.</b>\n\n"
        "You can start a new folder anytime with <code>/folder</code>."
    )

    SESSION_EXPIRED = (
        "📁 <b>Folder session expired.</b>\n\n"
        "The temporary files have been cleaned up."
    )

    @staticmethod
    def list_caption(total: int, has_folders: bool, summary_lines: list[str] | None = None) -> str:
        if not has_folders or not summary_lines:
            return (
                "📁 <b>Folders</b>\n\n"
                "No folders yet.\n\n"
                "Create a folder to organize your files."
            )
        return (
            "📁 <b>Folders</b>\n\n"
            "Select a folder to manage.\n\n"
            + "\n".join(summary_lines)
        )

    @staticmethod
    def limit_reached(limit: int) -> str:
        return f"⚠️ Limit reached — max <b>{limit}</b> files per folder."

    @staticmethod
    def already_added(status_name: str, count: int) -> str:
        return "⚠️ File already added to this folder."

    @staticmethod
    def item_added(status_name: str, size: str, count: int, total_size: str = "", added_list: str = "") -> str:
        total_size_line = f"\n💾 Total: <code>{total_size}</code>" if total_size else ""
        return (
            "📁 <b>Folder Status</b>\n\n"
            "✅ Files added\n\n"
            f"📦 Files added: <b>{count}</b>"
            f"{total_size_line}"
            f"{added_list}"
        )

    @staticmethod
    def render_added_list(names: list[str], limit: int = 5) -> str:
        if not names:
            return ""
        shown = names[-limit:]
        lines = "\n".join(f"• {html.escape(name, quote=False)}" for name in shown)
        extra = len(names) - len(shown)
        if extra > 0:
            lines += f"\n• +{extra} more…"
        return f"\n\nRecent files:\n{lines}"

    @staticmethod
    def share_text(folder_name: str, total_files: int, total_size: str, bot_username: str | None, share_text: str) -> str:
        normalized_bot = str(bot_username or "").strip().lstrip("@")
        lines = [
            "📦 Folder share link",
            f"📂 {folder_name}",
            f"🗂 {total_files} files • {total_size}",
            share_text,
        ]
        if normalized_bot:
            lines.append(f"🤖 Use @{normalized_bot}")
        return "\n".join(lines)


# =====================================================================
# 4A. STATS & USAGE MESSAGES
# =====================================================================

class StatsMessages:
    """User usage visibility and statistics messages."""

    CARD = (
        "📊 <b>Your Usage</b>\n\n"
        "📄 Files processed: <b>{total_files}</b>\n"
        "📁 Folders created: <b>{total_folders}</b>\n"
        "🔗 Active links: <b>{total_links}</b>\n"
        "🗓️ Member since: <b>{join_date}</b>\n"
        "{recent_activity}"
    )

    EMPTY = (
        "📊 <b>Your Usage</b>\n\n"
        "No activity yet.\n"
        "Send your first file to get started and create links!"
    )

    FAILED = (
        "⚠️ I couldn't load your statistics right now.\n"
        "Please try again in a moment."
    )


# =====================================================================
# 4B. MILESTONES & DISCOVERY MESSAGES
# =====================================================================

class MilestoneMessages:
    """Informational milestones celebrating user achievements."""

    @staticmethod
    def format(count: int) -> str:
        if count == 1:
            return "🎉 <b>First Link Created!</b> You've generated your first direct streaming link."
        elif count == 10:
            return "🎉 <b>Milestone:</b> You've converted 10 files with File To Link V2 bot! Thank you for using the service."
        elif count == 25:
            return "🎉 <b>Milestone:</b> 25 files converted! Your files are organized and ready to stream."
        elif count == 50:
            return "🎉 <b>Power User:</b> 50 files converted! Thank you for trusting File To Link V2 bot."
        elif count == 100:
            return "🏆 <b>Century Club:</b> 100 files converted! You're an incredible power user."
        return f"🎉 <b>Milestone:</b> You've processed {count} files with File To Link V2 bot!"

    @staticmethod
    def folder_format(count: int) -> str:
        if count == 1:
            return "📁 <b>First Collection!</b> You created your first multi-file folder."
        elif count == 10:
            return "🏆 <b>Collection Master:</b> 10 folders created! You're an organization pro."
        return f"📁 <b>Milestone:</b> {count} folders created!"


class DiscoveryMessages:
    """Contextual tips for feature discovery."""

    FOLDER_COMPLETED = "💡 <b>Tip:</b> View and manage all your collections anytime with <code>/folders</code>."
    FILES_HINT = "💡 <b>Tip:</b> View your generated links with <code>/files</code> or check <code>/stats</code>."
    RECOMMEND_FOLDER = "💡 <b>Tip:</b> You've converted multiple files! Try <code>/folder</code> to bundle files into a single collection."
    DISMISSED = "✅ Tip dismissed."


# =====================================================================
# 4C. PHASE 4: SETTINGS, REFERRALS & NOTIFICATIONS
# =====================================================================

class SettingsMessages:
    """Messages for user settings and preferences."""

    MAIN = (
        "⚙️ <b>Settings & Preferences</b>\n\n"
        "Manage your personal preferences and notification controls.\n\n"
        "🔔 <b>Notifications:</b> <code>{}</code>\n"
        "🧠 <b>Personalization:</b> <code>{}</code>\n"
        "🌐 <b>Language:</b> <code>{}</code>\n\n"
        "<i>Tap an option below to toggle or manage.</i>"
    )

    LANG_MENU = (
        "🌐 <b>Language Settings</b>\n\n"
        "Current Language: <b>English</b> (Default)\n\n"
        "Additional translations are currently in preparation."
    )

    PRIVACY_INFO = (
        "🔒 <b>Privacy & Data Control</b>\n\n"
        "• <b>Zero Content Profiling:</b> We do not analyze or categorize your private files.\n"
        "• <b>Data Minimization:</b> We only retain necessary link IDs to serve streams.\n"
        "• <b>Full User Agency:</b> You can disable personalization or remove your links anytime with <code>/files</code>."
    )

    RESET_CONFIRM = (
        "✅ <b>Preferences Reset</b>\n\n"
        "All preferences, recommendations, and notification settings have been restored to default values."
    )


class ReferralMessages:
    """Messages for the referral and invite system."""

    CARD = (
        "👥 <b>Invite Friends</b>\n\n"
        "Share File To Link V2 bot with friends and groups!\n\n"
        "🔗 <b>Your Personal Invite Link:</b>\n"
        "<code>{}</code>\n\n"
        "📊 <b>Your Invites:</b> <b>{}</b> friends joined\n\n"
        "<i>When friends join via your link, they'll get instant fast file streaming and download links.</i>"
    )

    WELCOME = "🎉 <b>Welcome!</b> You were invited by a friend to File To Link V2 bot."


class NotificationMessages:
    """Messages for utility and lifecycle notifications."""

    LIFECYCLE_NEW_USER = (
        "👋 <b>Need a quick streaming or download link?</b>\n\n"
        "Send or forward any video, audio, or document here whenever you're ready!\n\n"
        "<i>You can manage reminders anytime in <code>/settings</code>.</i>"
    )

    EXPIRATION_WARNING = (
        "⏱️ <b>File Expiration Reminder</b>\n\n"
        "A temporary file you created is scheduled to expire soon:\n"
        "📄 <code>{}</code>\n"
        "⏳ Time remaining: <b>{}</b>\n\n"
        "<i>You can turn off reminders anytime in <code>/settings</code>.</i>"
    )


# =====================================================================
# 4. DONATION / SUPPORT MESSAGES
# =====================================================================

class DonationMessages:
    """Messages for Telegram Stars donation flow, invoices, and history."""

    MENU = (
        "⭐ <b>Support File To Link V2 bot</b>\n\n"
        "Your support keeps downloads fast and the bot online.\n\n"
        "Choose an amount or enter a custom one."
    )

    CUSTOM_AMOUNT = (
        "✏️ <b>Custom Amount</b>\n\n"
        "Send a number from <code>{}</code> to <code>{}</code> Stars.\n\n"
        "Example: <code>500</code>"
    )

    INVALID_AMOUNT = (
        "⚠️ Invalid amount.\n\n"
        "Enter a number from <code>{}</code> to <code>{}</code>."
    )

    INVOICE_FAILED = "⚠️ Couldn't create invoice. Try again."

    INVOICE_CREATED = "✅ Invoice created. Complete the payment in Telegram."

    INVOICE_CREATED_ALERT = "✅ Invoice created."

    THANK_YOU = (
        "💛 <b>Thank you!</b>\n\n"
        "Received: <code>{} ⭐</code>\n"
        "Total: <code>{} ⭐</code>\n\n"
        "Your support keeps File To Link V2 bot running."
    )

    PRECHECKOUT_INVALID = "Invalid donation request."
    PRECHECKOUT_USER_MISMATCH = "This invoice belongs to another user."
    PRECHECKOUT_CURRENCY_UNSUPPORTED = "Unsupported currency."
    PRECHECKOUT_AMOUNT_INVALID = "Donation amount is invalid."

    @staticmethod
    def invoice_title(brand: str) -> str:
        return f"Support {brand}"

    @staticmethod
    def invoice_description(brand: str) -> str:
        return f"Help keep {brand} fast and online."

    @staticmethod
    def invoice_created_text(amount: int) -> str:
        return f"✅ Invoice created for <b>{amount} ⭐</b>.\n\nComplete the payment in Telegram."

    @staticmethod
    def invoice_created_note(amount: int | str) -> str:
        return f"✅ Invoice created for {amount} ⭐."

    @staticmethod
    def invalid_amount_alert(min_stars: int, max_stars: int) -> str:
        return f"⚠️ Enter a number from {min_stars} to {max_stars}."

    @staticmethod
    def history_text(stats: dict, donations: list[dict]) -> str:
        lines = [
            "<b>🧾 Support History</b>",
            "",
            f"<b>Donations:</b> <code>{int(stats.get('count', 0))}</code>",
            f"<b>Total:</b> <code>{int(stats.get('total_stars', 0))} ⭐</code>",
            "",
        ]

        if not donations:
            lines.append("<i>No donations yet.</i>")
            return "\n".join(lines)

        import datetime
        lines.append("<b>Recent</b>")
        for index, item in enumerate(donations, start=1):
            paid_at = item.get("paid_at")
            if isinstance(paid_at, (int, float)):
                paid_on = datetime.datetime.utcfromtimestamp(paid_at).strftime("%Y-%m-%d %H:%M UTC")
            else:
                paid_on = "Unknown"
            amount = int(item.get("amount", 0) or 0)
            lines.append(f"{index}. <code>{amount} ⭐</code> — {html.escape(paid_on)}")

        return "\n".join(lines)


# =====================================================================
# 5. SAFETY & NSFW REPORT MESSAGES
# =====================================================================

class SafetyMessages:
    """Messages for content reporting and moderation notifications."""

    REPORT_UNAVAILABLE = "⚠️ Reporting unavailable. Try again later."

    REPORT_RECEIVED = (
        "✅ <b>Report Received</b>\n\n"
        "We're reviewing the content.\n"
        "Thanks for helping keep File To Link V2 bot safe."
    )

    CONTENT_REMOVED = (
        "⚠️ <b>Content Removed</b>\n\n"
        "This content appears to violate the adult-content policy.\n"
        "Please only upload content you have the right to share."
    )

    REPORT_REMOVED = "✅ Content removed. Thanks for the report."

    REPORT_CLEAN = "ℹ️ Reviewed — no violations found. Thanks for reporting."

    RM_ADULT_USAGE = "ℹ️ <b>Usage</b>\n\n<code>/rm_adult &lt;file_id|folder_id&gt;</code>"

    @staticmethod
    def channel_report_text(report_id: str, report_by: int, uploader_ref: str, target_label: str, target_name: str, status: str = "Reviewing...") -> str:
        return (
            "<b>🚨 NSFW Report</b>\n\n"
            f"<b>Report ID:</b> <code>{report_id}</code>\n"
            f"<b>Report By:</b> <a href='tg://user?id={report_by}'>User</a>\n"
            f"<b>Report User ID:</b> <code>{report_by}</code>\n"
            f"<b>Uploader ID:</b> {uploader_ref}\n"
            f"<b>Target:</b> {target_label}\n"
            f"<b>Name:</b> <code>{target_name}</code>\n\n"
            f"<b>Status:</b> <code>{status}</code>"
        )


# =====================================================================
# 6. ADMIN & OWNER MESSAGES
# =====================================================================

class AdminMessages:
    """Messages for admin controls, speedtest, broadcast, toggles, and bans."""

    PANEL_TITLE = "🛠 Admin Panel"

    STORAGE_NOT_CONFIGURED = "⚠️ Storage channel not configured."

    SPEEDTEST_WAIT = "⏳ Speed test running. Wait about <code>{}</code>."
    SPEEDTEST_WAIT_ALERT = "⏳ Speed test cooldown. Wait {}."

    SPEEDTEST_ALREADY_RUNNING = "⏳ Speed test already running."

    SPEEDTEST_RUNNING_ALERT = "⚡ Running speed test…"

    SPEEDTEST_RUNNING_IN_PLACE = (
        "⚡ <b>Speed Test Running…</b>\n\n"
        "Testing network latency, download and upload speeds.\n"
        "Please wait a moment…"
    )

    SPEEDTEST_START = "⚡ <b>Speed Test</b>\n\nRunning…"

    SPEEDTEST_ERROR = "⚠️ Speed test failed. Try again."

    SPEEDTEST_EMPTY = (
        "⚡ <b>Speed Test</b>\n\n"
        "📊 <b>No Speed Test Results Yet</b>\n\n"
        "No completed speed test is available.\n\n"
        "Run a speed test to measure:\n"
        "• Download speed\n"
        "• Upload speed\n"
        "• Ping\n"
        "• Speedtest server\n"
        "• Network details"
    )

    SPEEDTEST_TIMEOUT = (
        "⚠️ <b>Speed Test Timeout</b>\n\n"
        "The network test did not complete within the allowed time."
    )

    SPEEDTEST_RESULT = (
        "<b>⚡ Speed Test</b>\n\n"
        "↓ <code>{download_mbps} Mbps</code>\n"
        "↑ <code>{upload_mbps} Mbps</code>\n"
        "Ping: <code>{ping} ms</code>\n"
        "Time: <code>{timestamp}</code>\n\n"
        "Server: <code>{server_name}, {server_country}</code>\n"
        "ISP: <code>{client_isp}</code>\n"
        "Data: <code>{bytes_sent} / {bytes_received}</code>"
    )

    BROADCAST_STARTED = "📢 <b>Broadcast Started</b>\n\nSending to all users…"

    BROADCAST_STATUS = "<b>📢 Broadcast</b>\n\nSent: <code>{}</code>\nFailed: <code>{}</code>\nProgress: <code>{}/{}</code>"

    LINK_NOT_FOUND = "🔍 Link not found."

    LINK_NO_ACTIVE = "ℹ️ No active link found."

    OWNER_ONLY = "🔒 Owner-only command."

    COMMAND_USAGE = "ℹ️ <b>Usage</b>\n\n<code>{}</code>"

    REFRESHED_ALERT = "🔄 Refreshed."

    SHORTENER_ALREADY_ON_ALERT = "ℹ️ Shortener already on."
    SHORTENER_ALREADY_OFF_ALERT = "ℹ️ Shortener already off."
    SHORTENER_ON_ALERT = "✅ Shortener on."
    SHORTENER_OFF_ALERT = "⛔ Shortener off."

    STORAGE_MAIN_ALREADY_USING_ALERT = "ℹ️ Already using Main Storage."
    STORAGE_MAIN_NOW_USING_ALERT = "✅ Switched to Main Storage."
    STORAGE_ADMIN_ALREADY_USING_ALERT = "ℹ️ Already using Admin Storage."
    STORAGE_ADMIN_NOW_USING_ALERT = "✅ Switched to Admin Storage."

    BAN_USER_NOTICE = (
        "🚫 <b>Access Restricted</b>\n\n"
        "Your account has been restricted.\n"
        "Contact support if this is a mistake."
    )

    UNBAN_USER_NOTICE = (
        "✅ <b>Access Restored</b>\n\n"
        "You can use the bot again."
    )

    DEL_FILE_NOT_FOUND = "🔍 File not found."

    EXPIRE_LINK_INVALID = "⚠️ Use <code>now</code>, <code>clear</code>, or a number of hours."

    @staticmethod
    def user_access_updated(target_id: int | str, status: str) -> str:
        return f"✅ <b>Access Updated</b>\n\nUser: <code>{target_id}</code>\nStatus: <code>{status}</code>"

    @staticmethod
    def user_already_restricted(target_id: int | str) -> str:
        return f"ℹ️ User <code>{target_id}</code> already restricted."

    @staticmethod
    def user_not_restricted(target_id: int | str) -> str:
        return f"ℹ️ User <code>{target_id}</code> not restricted."

    @staticmethod
    def broadcast_complete_text(completed_in: Any, success: int, failed: int, total_users: int) -> str:
        return (
            "✅ <b>Broadcast Complete</b>\n\n"
            f"Time: <code>{completed_in}</code>\n"
            f"Sent: <code>{success}</code>\n"
            f"Failed: <code>{failed}</code>\n"
            f"Total: <code>{total_users}</code>"
        )

    @staticmethod
    def broadcast_complete_caption(completed_in: Any, success: int, failed: int, total_users: int) -> str:
        return f"✅ Broadcast complete\n\nTime: {completed_in}\nSent: {success}\nFailed: {failed}\nTotal: {total_users}"

    @staticmethod
    def link_revoked(public_id: str) -> str:
        return f"✅ <b>Link Removed</b>\n\nPublic ID: <code>{public_id}</code>"

    @staticmethod
    def link_regenerated(public_id: str, url: str) -> str:
        return (
            "✅ <b>New Link</b>\n\n"
            f"Public ID: <code>{public_id}</code>\n"
            f"<code>{html.escape(url)}</code>"
        )

    @staticmethod
    def link_expiry_updated(public_id: str, result_text: str) -> str:
        return (
            "✅ <b>Expiry Updated</b>\n\n"
            f"Public ID: <code>{public_id}</code>\n"
            f"Result: <code>{html.escape(str(result_text))}</code>"
        )

    @staticmethod
    def shortener_status_prompt(state: str) -> str:
        return (
            f"<b>🔗 Shortener:</b> <code>{state}</code>\n\n"
            "<code>/urlshortener on</code>\n"
            "<code>/urlshortener off</code>"
        )

    @staticmethod
    def shortener_status_text(state: str) -> str:
        return f"<b>🔗 Shortener:</b> <code>{state}</code>"

    @staticmethod
    def shortener_enabled_text() -> str:
        return "✅ Shortener: <code>ON</code>"

    @staticmethod
    def shortener_disabled_text() -> str:
        return "⛔ Shortener: <code>OFF</code>"

    @staticmethod
    def webads_status_prompt(bot_label: str, state: str) -> str:
        return (
            f"<b>📣 Web Ads ({html.escape(bot_label)}):</b> <code>{state}</code>\n\n"
            "<code>/webads on</code>\n"
            "<code>/webads off</code>"
        )

    @staticmethod
    def webads_status_text(bot_label: str, state: str) -> str:
        return f"<b>📣 Web Ads ({html.escape(bot_label)}):</b> <code>{state}</code>"

    @staticmethod
    def webads_enabled_text(bot_label: str) -> str:
        return f"✅ Web Ads: <code>ON</code> ({html.escape(bot_label)})"

    @staticmethod
    def webads_disabled_text(bot_label: str) -> str:
        return f"⛔ Web Ads: <code>OFF</code> ({html.escape(bot_label)})"

    @staticmethod
    def webads_already_enabled_text(bot_label: str) -> str:
        return f"ℹ️ Web Ads already on for {html.escape(bot_label)}."

    @staticmethod
    def webads_already_disabled_text(bot_label: str) -> str:
        return f"ℹ️ Web Ads already off for {html.escape(bot_label)}."

    @staticmethod
    def storage_status_prompt(current_mode: str, default_label: str, admin_user_count: int) -> str:
        return (
            "<b>💾 Upload Storage</b>\n\n"
            f"Mode: <code>{current_mode.upper()}</code>\n"
            f"Default: <code>{default_label}</code>\n"
            f"Admin users: <code>{admin_user_count}</code>\n\n"
            "<code>/flogstorage status [user_id]</code>\n"
            "<code>/flogstorage list</code>\n"
            "<code>/flogstorage main [user_id]</code>\n"
            "<code>/flogstorage admin [user_id]</code>"
        )

    @staticmethod
    def storage_user_status(target_user_id: int | str, target_mode: str, is_admin_storage: bool, default_label: str) -> str:
        return (
            "<b>💾 Upload Storage</b>\n\n"
            f"User: <code>{target_user_id}</code>\n"
            f"Mode: <code>{target_mode.upper()}</code>\n"
            f"Admin storage: <code>{'YES' if is_admin_storage else 'NO'}</code>\n"
            f"Default: <code>{default_label}</code>"
        )

    @staticmethod
    def storage_user_list(total: int, formatted_list: str) -> str:
        return (
            "<b>🛠 Admin Storage Users</b>\n\n"
            f"Total: <code>{total}</code>\n"
            f"<code>{formatted_list}</code>"
        )

    @staticmethod
    def storage_user_already_using(target_user_id: int | str, storage_name: str) -> str:
        return f"ℹ️ User <code>{target_user_id}</code> already uses {storage_name}."

    @staticmethod
    def storage_user_now_using(target_user_id: int | str, storage_name: str) -> str:
        return f"✅ User <code>{target_user_id}</code> switched to {storage_name}."

    @staticmethod
    def storage_status_alert(current_mode: str, default_label: str, expire_hours: int, selected_count: int, main_channel: Any, admin_channel: Any) -> str:
        return (
            f"Storage: {current_mode.upper()}\n"
            f"Default: {default_label}\n"
            f"Policy: MAIN={expire_hours}h | ADMIN=permanent\n"
            f"Selected: {selected_count}\n"
            f"Main: {main_channel or 'not set'}\n"
            f"Admin: {admin_channel or 'not set'}"
        )

    @staticmethod
    def system_status(*args, **kwargs) -> str:
        if len(args) == 13:
            bot_uptime, sys_uptime, cpu_usage, ram_used, ram_total, disk_used, disk_total, disk_percent, upload, download, total_users, banned_users, total_files = args
            return (
                "<b>📊 System Status</b>\n\n"
                f"Uptime: <code>{bot_uptime}</code>\n"
                f"System: <code>{sys_uptime}</code>\n\n"
                f"CPU: <code>{cpu_usage}%</code>\n"
                f"RAM: <code>{ram_used} / {ram_total}</code>\n"
                f"Disk: <code>{disk_used} / {disk_total}</code> ({disk_percent})\n\n"
                f"↑ <code>{upload}</code> · ↓ <code>{download}</code>\n\n"
                f"Users: <code>{total_users}</code>\n"
                f"Restricted: <code>{banned_users}</code>\n"
                f"Links: <code>{total_files}</code>"
            )
        vps_info = kwargs.get("vps_info") or (args[0] if len(args) > 0 and isinstance(args[0], dict) else {})
        net_info = kwargs.get("net_info") or (args[1] if len(args) > 1 and isinstance(args[1], dict) else {})
        speed_info = kwargs.get("speed_info") or (args[2] if len(args) > 2 and isinstance(args[2], dict) else None)
        from FileStream.core.monitoring.speedtest import format_system_status
        return format_system_status(vps_info=vps_info, net_info=net_info, speed_info=speed_info)

    @staticmethod
    def panel_text(shortener_state: str, bot_label: str, webads_state: str, effective_storage_mode: str, admin_user_count: int, owner_mode: bool) -> str:
        s_ind = "🟢 ON" if str(shortener_state).upper() == "ON" else "🔴 OFF"
        w_ind = "🟢 ON" if str(webads_state).upper() == "ON" else "🔴 OFF"
        storage_mode = str(effective_storage_mode).upper()
        storage_tag = f"🔐 {storage_mode}" if storage_mode == "ADMIN" else f"💾 {storage_mode}"
        access_tag = "👑 Access: Owner" if owner_mode else "🛡️ Access: Admin"
        return (
            "🛠️ <b>Admin Panel</b>\n\n"
            f"🔗 Shortener  • {s_ind}\n"
            f"📢 Web Ads    • {w_ind}\n"
            f"💾 Storage    • {storage_tag}\n"
            f"👥 Admins     • {admin_user_count}\n\n"
            f"{access_tag}"
        )

    @staticmethod
    def config_panel_text(shortener_state: str, bot_label: str, webads_state: str, effective_storage_mode: str, admin_user_count: int) -> str:
        s_ind = "🟢 ON" if str(shortener_state).upper() == "ON" else "🔴 OFF"
        w_ind = "🟢 ON" if str(webads_state).upper() == "ON" else "🔴 OFF"
        storage_mode = str(effective_storage_mode).upper()
        storage_tag = f"🔐 {storage_mode}" if storage_mode == "ADMIN" else f"💾 {storage_mode}"
        return (
            "⚙️ <b>Configuration</b>\n\n"
            f"🔗 Shortener  • {s_ind}\n"
            f"📢 Web Ads ({html.escape(bot_label)}) • {w_ind}\n"
            f"💾 Storage    • {storage_tag}\n"
            f"👥 Admins     • {admin_user_count}\n\n"
            "<i>Select a setting below to toggle or manage.</i>"
        )

    @staticmethod
    def monitoring_panel_text() -> str:
        return (
            "📊 <b>Monitoring & Diagnostics</b>\n\n"
            "• <b>System Status:</b> Host, storage, and network diagnostics\n"
            "• <b>Bot Status:</b> Application health, runtime, and database stats\n"
            "• <b>Reports:</b> Review & remove reported content (/rm_adult)"
        )

    @staticmethod
    def user_mgmt_panel_text() -> str:
        return (
            "👥 <b>User Management</b>\n\n"
            "• <b>Broadcast:</b> Announce a message to all users\n"
            "• <b>Restrict:</b> Ban user and revoke active links (/ban)\n"
            "• <b>Restore:</b> Unban user and restore access (/unban)\n"
            "• <b>Delete File:</b> Remove file from DB & storage channel (/del)"
        )

    @staticmethod
    def link_mgmt_panel_text() -> str:
        return (
            "🔗 <b>Link Management</b>\n\n"
            "• <b>Link Info:</b> Inspect target, clicks, status, and expiry (/linkinfo)\n"
            "• <b>New Link:</b> Regenerate public link (/regen_link)\n"
            "• <b>Remove Link:</b> Revoke active link (/revoke_link)\n"
            "• <b>Set Expiry:</b> Set or clear link expiration (/expire_link)"
        )

    @staticmethod
    def logs_panel_text() -> str:
        return (
            "📋 <b>Admin Logs</b>\n\n"
            "Select a log category to view real-time diagnostic output:\n\n"
            "• 🤖 <b>Bot Logs:</b> Telegram handlers, lifecycle & polling\n"
            "• 🌐 <b>Web Logs:</b> HTTP server requests & stream routes\n"
            "• ❌ <b>Error Logs:</b> System errors, warnings & tracebacks\n"
            "• 💾 <b>Storage & Folders:</b> Storage channels, uploads & folder operations\n"
            "• 👤 <b>Admin Actions:</b> Audit trail of administrative operations\n"
            "• 🔐 <b>Security Logs:</b> Sensitive probe detection & access violations\n"
            "• ⚡ <b>Performance Logs:</b> FloodWait, latencies, speedtests & queues"
        )

    @staticmethod
    def logs_view_text(category_title: str, log_content: str, count: int = 0) -> str:
        count_label = f" (Recent {count})" if count else ""
        return (
            f"📋 <b>{category_title}</b>{count_label}\n\n"
            f"<pre><code class=\"language-log\">{log_content}</code></pre>"
        )

    @staticmethod
    def logs_empty_text(category_title: str) -> str:
        return (
            f"📋 <b>{category_title}</b>\n\n"
            "<i>No log entries found for this category.</i>"
        )


    COMMAND_GUIDES = {
        "broadcast": {
            "title": "Broadcast Message",
            "desc": "Send an announcement message to all registered bot users.",
            "usage": "Reply to any message with /broadcast",
            "note": "Messages are delivered asynchronously to all active users.",
        },
        "ban": {
            "title": "Restrict User Access",
            "desc": "Block a user from using the bot and revoke their active links.",
            "usage": "/ban <user_id>",
            "note": "Restricting will ban the user and revoke all active sessions.",
        },
        "unban": {
            "title": "Restore User Access",
            "desc": "Unban a previously restricted user and restore their bot access.",
            "usage": "/unban <user_id>",
            "note": None,
        },
        "del": {
            "title": "Delete File",
            "desc": "Permanently delete a file from the database and storage channel.",
            "usage": "/del <file_id>",
            "note": "This action cannot be undone. Always verify the file ID before deleting.",
        },
        "linkinfo": {
            "title": "Link Information",
            "desc": "Inspect target details, click counts, status, and expiration for a file or folder link.",
            "usage": "/linkinfo <public_id|file:<id>|folder:<id>>",
            "note": None,
        },
        "revoke_link": {
            "title": "Revoke Public Link",
            "desc": "Immediately invalidate and deactivate an active public stream or download link.",
            "usage": "/revoke_link <public_id|file:<id>|folder:<id>>",
            "note": "Users will no longer be able to stream or download via this public link.",
        },
        "regen_link": {
            "title": "Regenerate Public Link",
            "desc": "Generate a fresh new public link for an existing file or folder.",
            "usage": "/regen_link <public_id|file:<id>|folder:<id>>",
            "note": "A new unique public URL will be created for the content.",
        },
        "expire_link": {
            "title": "Set Link Expiration",
            "desc": "Set a custom expiration duration, expire immediately, or clear expiration.",
            "usage": "/expire_link <public_id|file:<id>|folder:<id>> <now|clear|hours>",
            "note": "Examples: 24 (24 hours), 'now' (expire now), 'clear' (permanent).",
        },
        "rm_adult": {
            "title": "Review Content Reports",
            "desc": "Review or delete adult/NSFW files and folders reported by users.",
            "usage": "/rm_adult <file_id|folder_id>",
            "note": "Deletes the reported media from both the database and storage channel.",
        },
        "flogstorage": {
            "title": "Storage Channel Routing",
            "desc": "Configure upload target channels and per-user storage routing.",
            "usage": "/flogstorage <main|admin|status|list> [user_id]",
            "note": None,
        },
    }

    @staticmethod
    def command_guide_text(title: str, description: str, usage: str, note: str | None = None) -> str:
        text = (
            f"🛠️ <b>{html.escape(title)}</b>\n\n"
            f"{html.escape(description)}\n\n"
            f"<b>Command:</b>\n<code>{html.escape(usage)}</code>"
        )
        if note:
            text += f"\n\n⚠️ <i>{html.escape(note)}</i>"
        return text

    @classmethod
    def get_command_guide(cls, command_key: str) -> str:
        guide = cls.COMMAND_GUIDES.get(command_key)
        if not guide:
            return cls.COMMAND_USAGE.format(command_key)
        return cls.command_guide_text(
            title=guide["title"],
            description=guide["desc"],
            usage=guide["usage"],
            note=guide.get("note"),
        )

    @staticmethod
    def link_info_text(link_type: str, target_id: Any, public_id: str, status: str, clicks: int, expires_text: str, safe_title: str, safe_url: str) -> str:
        return (
            "<b>🔎 Link Info</b>\n\n"
            f"<b>Type:</b> <code>{link_type}</code>\n"
            f"<b>Target:</b> <code>{target_id}</code>\n"
            f"<b>Public ID:</b> <code>{public_id}</code>\n"
            f"<b>Status:</b> <code>{status}</code>\n"
            f"<b>Clicks:</b> <code>{clicks}</code>\n"
            f"<b>Expires:</b> <code>{expires_text}</code>\n"
            f"<b>Name:</b> <code>{safe_title}</code>\n"
            f"<b>URL:</b> <code>{safe_url}</code>"
        )


# =====================================================================
# 7. FORCE SUB & SUBSCRIPTION MESSAGES
# =====================================================================

class ForceSubMessages:
    """Messages for force subscription prompts and membership checks."""

    TEXT = (
        "🔔 <b>Join to Continue</b>\n\n"
        "Join our channel to keep using the bot.\n\n"
        "After joining, tap <b>✅ I Joined</b>."
    )

    SUCCESS = (
        "✅ <b>Access Active</b>\n\n"
        "Send a file to create your next link."
    )

    SUCCESS_ALERT = "✅ Access active."

    STILL_REQUIRED = (
        "🔔 Join the channel first.\n\n"
        "After joining, tap <b>✅ I Joined</b>."
    )

    STILL_REQUIRED_ALERT = "🔔 Join the channel first, then tap ✅ I Joined."

    ERROR = (
        "⚠️ Couldn't verify your membership.\n\n"
        "Wait a moment, then tap <b>✅ I Joined</b> again."
    )

    ACCOUNT_MISMATCH_ALERT = (
        "⚠️ This button is for the user who requested it."
    )


# =====================================================================
# 8. ERROR MESSAGES
# =====================================================================

class _PrivateBotTemplate(str):
    """Template for PRIVATE_BOT supporting format(owner_id) and direct string usage."""
    def format(self, *args, **kwargs):
        owner_id = args[0] if len(args) > 0 else kwargs.get("owner_id")
        if owner_id:
            try:
                owner_id_int = int(owner_id)
                if owner_id_int > 0:
                    return (
                        "🔒 <b>Private Bot</b>\n\n"
                        "This bot is currently private and restricted to authorized users.\n"
                        f"Please <a href='tg://user?id={owner_id_int}'>contact the bot owner</a> to request access."
                    )
            except Exception:
                pass
        return (
            "🔒 <b>Private Bot</b>\n\n"
            "This bot is currently private and restricted to authorized users.\n"
            "Please contact the bot owner for access."
        )


class _AccessDeniedTemplate(str):
    """Template for ACCESS_DENIED supporting format(owner_id) and direct string usage."""
    def format(self, *args, **kwargs):
        owner_id = args[0] if len(args) > 0 else kwargs.get("owner_id")
        if owner_id:
            try:
                owner_id_int = int(owner_id)
                if owner_id_int > 0:
                    return (
                        "🔒 <b>Access Denied</b>\n\n"
                        f"Please <a href='tg://user?id={owner_id_int}'>contact the bot owner</a> for access."
                    )
            except Exception:
                pass
        return (
            "🔒 <b>Access Denied</b>\n\n"
            "Contact the bot owner for access."
        )


class ErrorMessages:
    """Standard user-facing error messages and alerts."""

    GENERIC = (
        "⚠️ Something went wrong.\n\n"
        "Try again, or contact support if it continues."
    )

    FILE_NOT_FOUND = (
        "🔍 <b>File Not Found</b>\n\n"
        "It may have expired or been removed.\n"
        "Send the file again to create a new link."
    )

    FILE_NOT_FOUND_ALERT = "🔍 File not found."

    FOLDER_NOT_FOUND = (
        "🔍 <b>Folder Not Found</b>\n\n"
        "It may have been deleted or the link expired.\n"
        "Open My Folders or create a new one."
    )

    ACCESS_DENIED = _AccessDeniedTemplate(
        "🔒 <b>Access Denied</b>\n\n"
        "Contact the bot owner for access."
    )

    PRIVATE_BOT = _PrivateBotTemplate(
        "🔒 <b>Private Bot</b>\n\n"
        "This bot is currently private and restricted to authorized users.\n"
        "Please contact the bot owner for access."
    )

    INVALID_LINK = (
        "🤔 Unrecognized link.\n\n"
        "Check the link or start from the bot."
    )

    STALE_CALLBACK = (
        "⚠️ Button expired.\n\n"
        "Reopen the menu and try again."
    )

    UNAVAILABLE = "⚠️ Unavailable right now."

    INVALID_USER_ID = "⚠️ Invalid user ID. Use numbers only."

    BAN_TEXT = (
        "🚫 <b>Access Restricted</b>\n\n"
        "Your account has been restricted.\n"
        "Contact support if this is a mistake."
    )

    UPLOAD_FAILED = (
        "❌ <b>Something went wrong while processing your file.</b>\n\n"
        "Please try again. If this keeps happening, contact support."
    )

    FLOOD_WAIT = "⏳ Telegram is temporarily rate-limiting requests. Please wait about {}s…"

    @staticmethod
    def flood_wait(seconds: int | float | str | None = None) -> str:
        if seconds is not None:
            try:
                sec = int(seconds)
                return (
                    "⏳ Telegram is temporarily rate-limiting requests.\n"
                    f"Please wait about {sec}s…"
                )
            except Exception:
                pass
        return (
            "⏳ Telegram is temporarily rate-limiting requests.\n"
            "Please wait a moment…"
        )


# =====================================================================
# 9. TELEGRAM CHANNEL LOGGING NOTIFICATIONS
# =====================================================================

class ChannelLogMessages:
    """Messages posted to Telegram log channels (ULOG, etc.) regarding bot events."""

    @staticmethod
    def upload_delayed(seconds: int | str, user_id: int | str, user_name: str) -> str:
        return (
            "⚠️ <b>Upload delayed</b>\n\n"
            f"Telegram asked the bot to wait <code>{seconds}s</code>.\n"
            f"User: <a href='tg://user?id={user_id}'>{html.escape(str(user_name))}</a>"
        )

    UPLOAD_FAILED = "⚠️ <b>Upload failed</b>\n\nThe bot couldn't process this file. Check server logs for details."

    @staticmethod
    def channel_upload_delayed(seconds: int | str, channel_title: str) -> str:
        return (
            "⚠️ <b>Channel upload delayed</b>\n\n"
            f"Telegram asked the bot to wait <code>{seconds}s</code>.\n"
            f"Channel: <code>{html.escape(str(channel_title))}</code>"
        )

    CHANNEL_UPLOAD_FAILED = "⚠️ <b>Channel upload failed</b>\n\nThe bot couldn't update this message. Check server logs for details."

    @staticmethod
    def channel_edit_permission_error(err: Any) -> str:
        return f"Can't edit channel message:\n<code>{html.escape(str(err))}</code>"

    @staticmethod
    def new_user_log(first_name: str, user_id: int | str) -> str:
        return f"<b>#NewUser</b>\n• Name: {html.escape(str(first_name))}\n• ID: <code>{user_id}</code>"

    @staticmethod
    def new_channel_log(chat_title: str, chat_id: int | str, members: Any) -> str:
        return f"<b>#NewChannel</b>\n• Name: {html.escape(str(chat_title))}\n• ID: <code>{chat_id}</code>\n• Members: <code>{members}</code>"


# =====================================================================
# 10. LEGACY LANG COMPATIBILITY CLASS
# =====================================================================

class LANG:
    """Legacy LANG interface preserved for backward-compatibility with existing callers."""

    START_TEXT = PublicMessages.START
    PING_TEXT = PublicMessages.PING
    HELP_TEXT = PublicMessages.HELP
    ABOUT_TEXT = PublicMessages.ABOUT
    STREAM_TEXT = FileMessages.READY
    STREAM_TEXT_X = FileMessages.READY

    BAN_TEXT = ErrorMessages.BAN_TEXT

    FORCE_SUB_TEXT = ForceSubMessages.TEXT
    FORCE_SUB_SUCCESS = ForceSubMessages.SUCCESS
    FORCE_SUB_STILL_REQUIRED = ForceSubMessages.STILL_REQUIRED
    FORCE_SUB_STILL_REQUIRED_ALERT = ForceSubMessages.STILL_REQUIRED_ALERT
    FORCE_SUB_ERROR = ForceSubMessages.ERROR

    FOLDER_START = FolderMessages.MODE_ENABLED
    FOLDER_ADDED = "✅ Added to folder.\n\n<code>{}</code>\n\nSend another or <code>/done</code> to finish."
    FOLDER_DONE = FolderMessages.DONE
    FOLDER_EMPTY = FolderMessages.EMPTY
    FOLDER_CANCEL = FolderMessages.CANCELLED
    FOLDER_NAME_PROMPT = FolderMessages.RENAME_PROMPT

    ERROR_GENERIC = ErrorMessages.GENERIC
    ERROR_FILE_NOT_FOUND = ErrorMessages.FILE_NOT_FOUND
    ERROR_INVALID_LINK = ErrorMessages.INVALID_LINK
    ERROR_FLOOD = ErrorMessages.FLOOD_WAIT

    SPEEDTEST_START = AdminMessages.SPEEDTEST_START
    SPEEDTEST_ERROR = AdminMessages.SPEEDTEST_ERROR
    SPEEDTEST_RESULT = AdminMessages.SPEEDTEST_RESULT
    SPEEDTEST_TIMEOUT = AdminMessages.SPEEDTEST_TIMEOUT


class WebMessages:
    SECURE_LINK_EXPIRED = "⚠️ This secure link expired.\n\nReload the share page and try again."
    DOWNLOAD_EXPIRED = "⏳ Download link expired.\n\nGo back to the share page and tap Download again."
    STREAM_EXPIRED = "⏳ Stream link expired.\n\nReload the share page to continue watching."
    DOWNLOAD_REQUEST_INVALID = "⚠️ This download request is no longer valid.\n\nReopen the share page and try again."
