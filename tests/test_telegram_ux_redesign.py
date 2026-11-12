"""Comprehensive tests for the FileToLinkV2Bot minimalist Telegram UX redesign.

Tests cover:
1. Home screen (/start) presentation, greeting, primary action, and removal of database decoration.
2. Zero database queries and immediate callback ACK on Home.
3. Help compact navigation guide with expandable blockquote.
4. About screen compact presentation.
5. Privacy and Terms scannable structure, legal substance preservation, and navigation.
6. Navigation hierarchy consistency (◀️ Home callback convention).
7. Telegram Bot API changelog capabilities detection and fallbacks.
8. Security HTML escaping against script/tag injections.
9. Callback immediate acknowledgement performance.
10. Media churn prevention (edit_caption over edit_media when photo unchanged).
"""

import html
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Photo,
    User,
)
from pyrogram.enums.parse_mode import ParseMode

from FileStream import __version__
from FileStream.bot.helpers.ui import (
    Emoji,
    TelegramCapabilities,
    format_home_text,
    format_help_text,
    format_about_text,
    safe_escape,
    safe_user_name,
    make_home_keyboard,
    make_help_keyboard,
    make_about_keyboard,
    make_legal_keyboard,
    make_ping_keyboard,
    back_to_home_button,
)
from FileStream.bot.helpers.translation import BUTTON
from FileStream.bot.helpers.legal import build_bot_policy_text
from FileStream.bot.messages import PublicMessages, LANG
from FileStream.bot.handlers.user.start import render_home_content, show_home


# ============================================================================
# 1. HOME SCREEN REDESIGN & CLUTTER REMOVAL TESTS
# ============================================================================

def test_home_screen_clean_minimalist_structure():
    """Verify /start has a personalized greeting, minimalist explanation, and dominant primary action."""
    mock_user = MagicMock(spec=User)
    mock_user.first_name = "Alex"
    mock_user.mention = "Alex"

    text = format_home_text(user=mock_user, bot_username="FileToLinkV2bot")

    # 1. Greeting
    assert "👋 <b>Hello, Alex!</b>" in text

    # 2. What is this bot? (Under 2-3 seconds comprehension)
    assert "Fast streaming & direct download links for your files." in text

    # 3. Dominant primary action (Send a file)
    assert "<blockquote>📤 <b>Send a file</b>" in text
    assert "Forward or upload any file here to get an instant direct link.</blockquote>" in text

    # 4. Confirm old clutter is completely removed
    assert "Recent: Shared Folder" not in text
    assert "recent_info" not in text
    assert "GET STARTED" not in text
    assert "Turn your Telegram files" not in text
    assert "CREATE A LINK" not in text
    assert "Total links:" not in text


def test_home_screen_no_user_fallback():
    """Verify fallback greeting when no user object is provided."""
    text = format_home_text(user=None)
    assert "👋 <b>Hello!</b>" in text
    assert "📤 <b>Send a file</b>" in text


def test_home_screen_keyboard_hierarchy():
    """Verify home keyboard has fewer buttons, clear hierarchy, and semantic emojis."""
    kb = make_home_keyboard()
    rows = kb.inline_keyboard

    # Exactly 3 rows, 5 buttons total
    assert len(rows) == 3
    assert len(rows[0]) == 2
    assert len(rows[1]) == 2
    assert len(rows[2]) == 1

    # Row 1: Organizational actions
    assert rows[0][0].text == "📁 Folders"
    assert rows[0][0].callback_data == "my_folders"
    assert rows[0][1].text == "📄 Files"
    assert rows[0][1].callback_data == "my_files"

    # Row 2: Secondary info & guidance
    assert rows[1][0].text == "❓ Help"
    assert rows[1][0].callback_data == "help"
    assert rows[1][1].text == "ℹ️ About"
    assert rows[1][1].callback_data == "about"

    # Row 3: Support
    assert rows[2][0].text == "⭐ Support"
    assert rows[2][0].callback_data == "support"


def test_render_home_content_integration():
    """Verify render_home_content returns text, reply_markup, and photo without errors."""
    mock_bot = MagicMock()
    mock_bot.me = MagicMock(username="FileToLinkV2bot")
    mock_user = MagicMock(spec=User)
    mock_user.first_name = "Sam"

    text, reply_markup, photo = render_home_content(mock_bot, user=mock_user)

    assert "👋 <b>Hello, Sam!</b>" in text
    assert "📤 <b>Send a file</b>" in text
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert len(reply_markup.inline_keyboard) == 3


@pytest.mark.asyncio
async def test_show_home_zero_database_queries():
    """Verify show_home executes 0 database queries (no decorative DB calls)."""
    mock_bot = MagicMock()
    mock_bot.me = MagicMock(username="FileToLinkV2bot")
    mock_target = MagicMock(spec=CallbackQuery)
    mock_target.from_user = MagicMock(id=12345, first_name="Chris")
    mock_target.answer = AsyncMock()

    with patch("FileStream.bot.handlers.user.start.db") as mock_db, \
         patch("FileStream.bot.handlers.system.callback.edit_message", new=AsyncMock()) as mock_edit:
        
        await show_home(mock_bot, mock_target)

        # Confirm immediate callback answer
        mock_target.answer.assert_called_once()

        # Confirm zero database queries executed
        mock_db.total_files.assert_not_called()
        mock_db.get_user_preferences.assert_not_called()
        mock_db.folders.find_one.assert_not_called()
        mock_edit.assert_called_once()


# ============================================================================
# 2. HELP SCREEN REDESIGN TESTS
# ============================================================================

def test_help_screen_compact_guide():
    """Verify /help is a compact guide with commands collapsed in an expandable blockquote."""
    text = format_help_text(owner_id=99999)

    assert "❓ <b>Help & Guide</b>" in text
    assert "📤 <b>Send a file</b>" in text
    assert "🔗 <b>Direct Links</b>" in text
    assert "📁 <b>Folders</b>" in text
    assert "<blockquote expandable>⚙️ <b>Commands</b>" in text
    assert "• <code>/start</code> — Home screen" in text
    assert "• <code>/files</code> — Your uploaded files" in text
    assert "• <code>/folders</code> — Your folder collections" in text
    assert "tg://user?id=99999" in text


def test_help_keyboard_navigation():
    """Verify help keyboard has direct links to Files, Folders, Privacy, Terms, and Home."""
    kb = make_help_keyboard()
    rows = kb.inline_keyboard

    assert len(rows) == 3
    # Row 1: Files & Folders
    assert rows[0][0].text == "📁 Folders"
    assert rows[0][0].callback_data == "my_folders"
    assert rows[0][1].text == "📄 Files"
    assert rows[0][1].callback_data == "my_files"

    # Row 2: Privacy & Terms
    assert rows[1][0].text == "🔒 Privacy"
    assert rows[1][0].callback_data == "privacy"
    assert rows[1][1].text == "⚖️ Terms"
    assert rows[1][1].callback_data == "legal"

    # Row 3: Return Home
    assert rows[2][0].text == "◀️ Home"
    assert rows[2][0].callback_data == "home"


# ============================================================================
# 3. ABOUT SCREEN REDESIGN TESTS
# ============================================================================

def test_about_screen_compact_presentation():
    """Verify /about is extremely compact and devoid of marketing fluff."""
    text = format_about_text(bot_name="File To Link V2 bot", version=__version__)

    assert "ℹ️ <b>File To Link V2 bot</b>" in text
    assert "Simple file sharing through Telegram." in text
    assert "• ▶️ <b>Stream</b>" in text
    assert "• 📥 <b>Download</b>" in text
    assert "• 🔗 <b>Share</b>" in text
    assert "• 📁 <b>Organize</b>" in text
    assert f"<b>Version:</b> <code>{__version__}</code>" in text

    # No marketing paragraphs
    assert len(text.splitlines()) < 15


def test_about_keyboard():
    """Verify about keyboard contains only the canonical return Home button."""
    kb = make_about_keyboard()
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].text == "◀️ Home"
    assert kb.inline_keyboard[0][0].callback_data == "home"


# ============================================================================
# 4. PRIVACY & TERMS REDESIGN TESTS
# ============================================================================

def test_privacy_screen_structure_and_substance():
    """Verify Privacy screen has concise blockquote sections preserving legal meaning."""
    text = build_bot_policy_text("privacy")

    assert "🔒 <b>Privacy Policy</b>" in text
    assert "<blockquote><b>What We Store</b>" in text
    assert "• Telegram account info needed to operate the bot" in text
    assert "• File and link metadata required for sharing" in text
    assert "<blockquote><b>Files & Storage</b>" in text
    assert "• Files are handled through Telegram / FLOG storage" in text
    assert "• Temporary server caching is used solely for streaming speed" in text
    assert "<blockquote><b>Web & Security</b>" in text
    assert "• Public pages use short-lived sessions for secure access" in text
    assert "• Advertising scripts may load when enabled by admin" in text


def test_terms_screen_structure_and_substance():
    """Verify Terms screen clearly delineates Allowed vs Not Allowed content."""
    text = build_bot_policy_text("legal")

    assert "⚖️ <b>Terms of Service</b>" in text
    assert "<blockquote><b>Allowed</b>" in text
    assert "• Legal and legitimate file sharing" in text
    assert "• Personal and public file or folder links" in text
    assert "<blockquote><b>Not Allowed</b>" in text
    assert "• Illegal, pirated, or infringing content" in text
    assert "• Malware, viruses, or malicious files" in text
    assert "• Adult, pornographic, or NSFW content" in text
    assert "• Abuse of links, bandwidth, or services" in text
    assert "Links depend on backing Telegram storage" in text


def test_legal_keyboard_navigation():
    """Verify Privacy and Terms toggle smoothly and link to Web terms and Home."""
    # When viewing Privacy: toggle is Terms, web is Web Policy, back is Home
    priv_kb = make_legal_keyboard(command="privacy")
    assert priv_kb.inline_keyboard[0][0].text == "⚖️ Terms"
    assert priv_kb.inline_keyboard[0][0].callback_data == "legal"
    assert "Web Policy" in priv_kb.inline_keyboard[0][1].text
    assert priv_kb.inline_keyboard[1][0].text == "◀️ Home"
    assert priv_kb.inline_keyboard[1][0].callback_data == "home"

    # When viewing Legal: toggle is Privacy, web is Web Terms, back is Home
    legal_kb = make_legal_keyboard(command="legal")
    assert legal_kb.inline_keyboard[0][0].text == "🔒 Privacy"
    assert legal_kb.inline_keyboard[0][0].callback_data == "privacy"
    assert "Web Terms" in legal_kb.inline_keyboard[0][1].text
    assert legal_kb.inline_keyboard[1][0].text == "◀️ Home"
    assert legal_kb.inline_keyboard[1][0].callback_data == "home"


# ============================================================================
# 5. CONSISTENT NAVIGATION & EMOJI SYSTEM TESTS
# ============================================================================

def test_back_to_home_button_consistency():
    """Verify canonical Back/Home button convention across all secondary keyboards."""
    btn = back_to_home_button()
    assert btn.text == "◀️ Home"
    assert btn.callback_data == "home"

    # Verify presence in Help, About, and Legal keyboards
    help_kb = make_help_keyboard()
    assert any(b.text == "◀️ Home" and b.callback_data == "home" for row in help_kb.inline_keyboard for b in row)

    about_kb = make_about_keyboard()
    assert any(b.text == "◀️ Home" and b.callback_data == "home" for row in about_kb.inline_keyboard for b in row)

    legal_kb = make_legal_keyboard()
    assert any(b.text == "◀️ Home" and b.callback_data == "home" for row in legal_kb.inline_keyboard for b in row)


def test_emoji_vocabulary():
    """Verify semantic emoji vocabulary defines all core user interaction icons."""
    assert Emoji.SEND == "📤"
    assert Emoji.LINK == "🔗"
    assert Emoji.STREAM == "▶️"
    assert Emoji.DOWNLOAD == "📥"
    assert Emoji.FOLDER == "📁"
    assert Emoji.FILE == "📄"
    assert Emoji.HELP == "❓"
    assert Emoji.ABOUT == "ℹ️"
    assert Emoji.PRIVACY == "🔒"
    assert Emoji.TERMS == "⚖️"
    assert Emoji.SUPPORT == "⭐"
    assert Emoji.SETTINGS == "⚙️"
    assert Emoji.BACK == "◀️"
    assert Emoji.HOME == "🏠"


# ============================================================================
# 6. CAPABILITY DETECTION & FALLBACK TESTS
# ============================================================================

def test_telegram_capabilities_detection():
    """Verify runtime capability detection matches installed Pyrofork 2.3.69 realities."""
    # CopyTextButton is supported in Pyrofork 2.3.69 via copy_text parameter
    assert TelegramCapabilities.supports_copy_text_button() is True

    # Expandable blockquote is supported via HTML parser
    assert TelegramCapabilities.supports_expandable_blockquote() is True

    # Button styles (Bot API 9.4) are not in Pyrofork 2.3.69 -> clean fallback used
    assert TelegramCapabilities.supports_button_styles() is False

    # Message drafts (Bot API 9.3) are not in Pyrofork 2.3.69 -> clean fallback used
    assert TelegramCapabilities.supports_message_drafts() is False

    # Ephemeral messages (Bot API 10.2) are not in Pyrofork 2.3.69 -> clean fallback used
    assert TelegramCapabilities.supports_ephemeral_messages() is False


# ============================================================================
# 7. SECURITY & HTML ESCAPING TESTS
# ============================================================================

def test_security_html_injection_prevention():
    """Verify user-controlled inputs (names, titles) are rigorously HTML-escaped."""
    malicious_user = MagicMock(spec=User)
    malicious_user.first_name = "<script>alert('pwned')</script>"
    malicious_user.mention = None

    text = format_home_text(user=malicious_user)
    assert "<script>" not in text
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in text

    # Test safe_escape with HTML entities
    assert safe_escape("<b>Bold</b> & 'Special'") == "&lt;b&gt;Bold&lt;/b&gt; &amp; &#x27;Special&#x27;"



# ============================================================================
# 8. PERFORMANCE: IMMEDIATE CALLBACK ACK & MEDIA CHURN PREVENTION
# ============================================================================

@pytest.mark.asyncio
async def test_callback_immediate_acknowledgement():
    """Verify callback queries are acknowledged immediately on entry."""
    from FileStream.bot.handlers.system.callback import cb_data

    mock_bot = MagicMock()
    mock_bot.me = MagicMock(username="FileToLinkV2bot")
    
    mock_update = MagicMock(spec=CallbackQuery)
    mock_update.data = "help"
    mock_update.answer = AsyncMock()
    mock_update.message = MagicMock(spec=Message)
    mock_update.message.caption = None
    mock_update.message.photo = None
    mock_update.message.edit_text = AsyncMock()

    with patch("FileStream.bot.handlers.system.callback.edit_message", new=AsyncMock()):
        await cb_data(mock_bot, mock_update)
        mock_update.answer.assert_called_once()


@pytest.mark.asyncio
async def test_no_media_churn_when_photo_unchanged():
    """Verify edit_message uses edit_caption when photo file_id matches cached file_id."""
    from FileStream.bot.handlers.system.callback import edit_message

    mock_update = MagicMock(spec=CallbackQuery)
    mock_update.message = MagicMock(spec=Message)
    mock_photo = MagicMock(spec=Photo)
    mock_photo.file_id = "CACHED_PHOTO_123"
    mock_update.message.photo = mock_photo
    mock_update.message.edit_media = AsyncMock()
    mock_update.message.edit_caption = AsyncMock()

    with patch("FileStream.bot.handlers.system.callback.resolve_media_source", return_value="/path/photo.png"), \
         patch("FileStream.bot.handlers.system.callback.is_local_media_path", return_value=True), \
         patch("FileStream.bot.handlers.system.callback.get_cached_media_file_id", return_value="CACHED_PHOTO_123"):
        
        await edit_message(
            mock_update,
            text="Updated caption",
            reply_markup=None,
            photo="/path/photo.png",
        )

        # edit_caption should be called, NOT edit_media
        mock_update.message.edit_caption.assert_called_once()
        mock_update.message.edit_media.assert_not_called()
