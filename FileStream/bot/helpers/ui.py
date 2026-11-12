"""Centralized UI and UX Design System for FileToLinkV2bot.

Provides a unified semantic emoji vocabulary, capability detection for
installed Pyrofork/Pyrogram versions, keyboard builders with strict visual hierarchy,
safe text formatting, and clean fallbacks for unsupported Telegram Bot API features.
"""

from __future__ import annotations

import html
import inspect
from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from FileStream import __version__


class Emoji:
    """Semantic icon and emoji vocabulary.
    
    Each emoji represents a distinct, unambiguous user intent or domain concept.
    Avoids emoji clutter, noise, and decorative visual pollution.
    """
    SEND = "📤"
    LINK = "🔗"
    STREAM = "▶️"
    DOWNLOAD = "📥"
    FOLDER = "📁"
    FILE = "📄"
    HELP = "❓"
    ABOUT = "ℹ️"
    PRIVACY = "🔒"
    TERMS = "⚖️"
    SUPPORT = "⭐"
    SETTINGS = "⚙️"
    STATS = "📊"
    BACK = "◀️"
    HOME = "🏠"
    SUCCESS = "✅"
    CLOSE = "❌"
    CANCEL = "✕"
    DELETE = "🗑"
    RENAME = "✏️"
    ADD = "➕"
    COPY = "📋"
    PING = "🏓"
    ID = "🆔"
    FAST = "⚡"
    WARN = "⚠️"
    TIME = "⏳"


class TelegramCapabilities:
    """Runtime capability detection and fallback mappings for Telegram Bot API features.
    
    Detects whether the installed Pyrofork / Pyrogram runtime supports modern Telegram
    features from the Bot API changelog (Bot API 7.0 - 10.3), and defines clean fallbacks
    when a capability is not natively supported.
    """
    
    @staticmethod
    def supports_copy_text_button() -> bool:
        """Bot API 7.11: CopyTextButton for copying arbitrary text to clipboard."""
        try:
            return "copy_text" in inspect.signature(InlineKeyboardButton.__init__).parameters
        except Exception:
            return False

    @staticmethod
    def supports_expandable_blockquote() -> bool:
        """Bot API 7.4: Expandable collapsible block quotations (<blockquote expandable>)."""
        return True  # Verified natively parsed by Pyrofork 2.3.69 HTML parser

    @staticmethod
    def supports_button_styles() -> bool:
        """Bot API 9.4: Semantic button colors/styles (Primary, Success, Danger).
        
        Not present in installed Pyrofork 2.3.69 InlineKeyboardButton.
        Fallback: Semantic emoji hierarchy, button placement, and clear wording.
        """
        try:
            return "style" in inspect.signature(InlineKeyboardButton.__init__).parameters
        except Exception:
            return False

    @staticmethod
    def supports_message_drafts() -> bool:
        """Bot API 9.3: Real-time message drafts and AI streaming.
        
        Not present in installed Pyrofork 2.3.69 Client.
        Fallback: Standard progress updates for genuine long-running async tasks only;
        no simulated delays or fake typing animations.
        """
        return False

    @staticmethod
    def supports_ephemeral_messages() -> bool:
        """Bot API 10.2: Native ephemeral messages.
        
        Not present in installed Pyrofork 2.3.69 Client send methods.
        Fallback: Immediate non-intrusive callback query notifications (answer(..., show_alert=False))
        and automatic cleanup of transient messages to keep the chat clean.
        """
        return False


def safe_escape(value: Any) -> str:
    """HTML-escape any user-supplied or dynamic value."""
    if value is None:
        return ""
    return html.escape(str(value))


def safe_user_name(user: Any) -> str:
    """Safely extract and HTML-escape a user's display name."""
    if not user:
        return ""
    if isinstance(user, str):
        s = user.strip()
        if "<a href=" in s:
            return s
        return html.escape(s)
    
    first_name = getattr(user, "first_name", None)
    if first_name:
        return html.escape(str(first_name).strip())
    
    mention = getattr(user, "mention", None)
    if mention:
        return str(mention)
        
    return ""


def back_to_home_button() -> InlineKeyboardButton:
    """Canonical back-to-home navigation button."""
    return InlineKeyboardButton(f"{Emoji.BACK} Home", callback_data="home")


def make_home_keyboard(bot: Any = None) -> InlineKeyboardMarkup:
    """Construct the minimalist primary navigation keyboard for /start.
    
    Fewer buttons, clear visual hierarchy:
    Row 1: Primary organizational & file management actions (Folders, Files)
    Row 2: Secondary informational guidance (Help, About)
    Row 3: Voluntary support (Stars Donation)
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.FOLDER} Folders", callback_data="my_folders"),
                InlineKeyboardButton(f"{Emoji.FILE} Files", callback_data="my_files"),
            ],
            [
                InlineKeyboardButton(f"{Emoji.HELP} Help", callback_data="help"),
                InlineKeyboardButton(f"{Emoji.ABOUT} About", callback_data="about"),
            ],
            [
                InlineKeyboardButton(f"{Emoji.SUPPORT} Support", callback_data="support"),
            ],
        ]
    )


def make_help_keyboard(bot: Any = None) -> InlineKeyboardMarkup:
    """Construct compact navigation keyboard for /help.
    
    Provides quick access to Files, Folders, Privacy, Terms, and Home return.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.FOLDER} Folders", callback_data="my_folders"),
                InlineKeyboardButton(f"{Emoji.FILE} Files", callback_data="my_files"),
            ],
            [
                InlineKeyboardButton(f"{Emoji.PRIVACY} Privacy", callback_data="privacy"),
                InlineKeyboardButton(f"{Emoji.TERMS} Terms", callback_data="legal"),
            ],
            [
                back_to_home_button(),
            ],
        ]
    )


def make_about_keyboard(bot: Any = None) -> InlineKeyboardMarkup:
    """Construct compact navigation keyboard for /about."""
    return InlineKeyboardMarkup(
        [
            [
                back_to_home_button(),
            ],
        ]
    )


def make_legal_keyboard(bot: Any = None, command: str = "legal") -> InlineKeyboardMarkup:
    """Construct navigation keyboard for Legal / Privacy policies."""
    cmd = str(command or "legal").lower()
    from FileStream.bot.helpers.legal import build_policy_url
    
    if cmd == "privacy":
        toggle_button = InlineKeyboardButton(f"{Emoji.TERMS} Terms", callback_data="legal")
        web_button = InlineKeyboardButton("🌐 Web Policy", url=build_policy_url("privacy", bot=bot))
    else:
        toggle_button = InlineKeyboardButton(f"{Emoji.PRIVACY} Privacy", callback_data="privacy")
        web_button = InlineKeyboardButton("🌐 Web Terms", url=build_policy_url("legal", bot=bot))

    return InlineKeyboardMarkup(
        [
            [toggle_button, web_button],
            [back_to_home_button()],
        ]
    )


def make_ping_keyboard(bot: Any = None) -> InlineKeyboardMarkup:
    """Construct lightweight keyboard for /ping response."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.HELP} Help", callback_data="help"),
                InlineKeyboardButton(f"{Emoji.CLOSE} Close", callback_data="close"),
            ],
        ]
    )


def format_home_text(user: Any = None, bot_username: str | None = None) -> str:
    """Build minimalist Home screen text.
    
    Structure:
    1. Personalized greeting
    2. Short one-line explanation of FileToLinkV2Bot
    3. Dominant primary action (Send a file)
    """
    name = safe_user_name(user)
    greeting = f"👋 <b>Hello, {name}!</b>" if name else "👋 <b>Hello!</b>"
    
    return (
        f"{greeting}\n\n"
        "Fast streaming & direct download links for your files.\n\n"
        "<blockquote>📤 <b>Send a file</b>\n"
        "Forward or upload any file here to get an instant direct link.</blockquote>"
    )


def format_help_text(owner_id: int | str | None = None) -> str:
    """Build compact navigation guide for /help."""
    owner_link = f"<a href='tg://user?id={owner_id}'>the administrator</a>" if owner_id else "support"
    return (
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
        f"• <code>/ping</code> — Check bot latency\n\n"
        f"Need personal assistance? Contact {owner_link}.</blockquote>"
    )


def format_about_text(bot_name: str | None = None, version: str | None = None) -> str:
    """Build ultra-compact /about presentation."""
    clean_name = safe_escape(str(bot_name or "File To Link V2 bot").removeprefix("About ").strip())
    ver = safe_escape(version or __version__)
    return (
        f"ℹ️ <b>{clean_name}</b>\n\n"
        "Simple file sharing through Telegram.\n\n"
        "• ▶️ <b>Stream</b> — Watch videos without downloading\n"
        "• 📥 <b>Download</b> — High-speed direct downloads\n"
        "• 🔗 <b>Share</b> — Instant shareable links\n"
        "• 📁 <b>Organize</b> — Multi-file folder collections\n\n"
        f"<b>Version:</b> <code>{ver}</code>"
    )


def make_id_keyboard(user_id: int | str, bot: Any = None) -> InlineKeyboardMarkup:
    """Construct keyboard for /id command with native copy_text button."""
    row = []
    if TelegramCapabilities.supports_copy_text_button():
        row.append(InlineKeyboardButton(f"{Emoji.COPY} Copy ID", copy_text=str(user_id)))
    row.append(back_to_home_button())
    return InlineKeyboardMarkup([row])


def make_files_empty_keyboard(bot: Any = None) -> InlineKeyboardMarkup:
    """Construct keyboard for empty /files state."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{Emoji.SEND} Send a File", callback_data="help_send")],
            [back_to_home_button()],
        ]
    )


def make_file_menu_keyboard(
    file_id: str,
    file_list_no: int,
    public_url: str,
    share_link: str,
    is_streamable: bool = False,
) -> InlineKeyboardMarkup:
    """Construct compact action menu keyboard for a single file."""
    view_label = f"{Emoji.STREAM} Stream" if is_streamable else f"{Emoji.FILE} View"
    rows = [
        [
            InlineKeyboardButton(view_label, url=public_url),
            InlineKeyboardButton(f"{Emoji.SEND} Share", url=share_link),
        ],
    ]
    if TelegramCapabilities.supports_copy_text_button():
        rows.append([InlineKeyboardButton(f"{Emoji.COPY} Copy Link", copy_text=public_url)])
    rows.append(
        [
            InlineKeyboardButton(f"{Emoji.DOWNLOAD} Send File", callback_data=f"sendfile_{file_id}"),
            InlineKeyboardButton(f"{Emoji.DELETE} Delete", callback_data=f"msgdelete_{file_id}_{file_list_no}"),
        ]
    )
    rows.append([InlineKeyboardButton(f"{Emoji.BACK} Files", callback_data=f"userfiles_{file_list_no}")])
    return InlineKeyboardMarkup(rows)


def make_file_delete_confirm_keyboard(file_id: str, file_list_no: int) -> InlineKeyboardMarkup:
    """Construct confirmation keyboard for file deletion."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.DELETE} Delete", callback_data=f"msgdelyes_{file_id}_{file_list_no}"),
                InlineKeyboardButton(f"{Emoji.CLOSE} Cancel", callback_data=f"myfile_{file_id}_{file_list_no}"),
            ]
        ]
    )


def make_folder_delete_confirm_keyboard(folder_id: str, page: int = 1) -> InlineKeyboardMarkup:
    """Construct confirmation keyboard for folder deletion."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.DELETE} Delete", callback_data=f"fld:delyes:{folder_id}:{page}"),
                InlineKeyboardButton(f"{Emoji.CLOSE} Cancel", callback_data=f"fld:open:{folder_id}:{page}"),
            ]
        ]
    )


def make_folder_menu_keyboard(folder_id: str, page: int = 1, link: str = "", share_link: str = "") -> InlineKeyboardMarkup:
    """Construct compact action menu keyboard for a folder."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Emoji.FOLDER} Open", url=link),
                InlineKeyboardButton(f"{Emoji.SEND} Share", url=share_link),
            ],
            [
                InlineKeyboardButton(f"{Emoji.ADD} Add Files", callback_data=f"fld:add:{folder_id}:{page}"),
                InlineKeyboardButton(f"{Emoji.RENAME} Rename", callback_data=f"fld:ren:{folder_id}:{page}"),
            ],
            [
                InlineKeyboardButton(f"{Emoji.DELETE} Delete", callback_data=f"fld:del:{folder_id}:{page}"),
            ],
            [
                InlineKeyboardButton(f"{Emoji.BACK} Folders", callback_data=f"fld:list:{page}"),
            ],
        ]
    )

