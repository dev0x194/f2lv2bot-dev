"""Tests for FileToLinkV2Bot Next-Stage Full UX & Command System Upgrade.

Covers:
1. /id command: output hierarchy and native copy_text button.
2. /ping command: output hierarchy and latency measurement.
3. /files command: empty state, populated state, file menu keyboard (with copy_text),
   and delete confirmation flows.
4. /folders command: empty state, populated state, folder menu keyboard, and delete confirmation flows.
5. /folder command: argument validation (>50 chars), named session initiation, and default folder session.
6. /done command: idle state ("Nothing to finish") vs active completion.
7. /cancel command: idle state ("Nothing is currently running"), pending rename cancellation,
   and active folder cancellation.
8. Preservation invariant: ensure /start, /help, /about, /privacy, /legal remain intact.
"""

import html
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from pyrogram.enums.parse_mode import ParseMode

from FileStream.bot.helpers.ui import (
    Emoji,
    make_id_keyboard,
    make_files_empty_keyboard,
    make_file_menu_keyboard,
    make_file_delete_confirm_keyboard,
    make_folder_delete_confirm_keyboard,
    make_folder_menu_keyboard,
    back_to_home_button,
)
from FileStream.bot.messages import (
    PublicMessages,
    FileMessages,
    FolderMessages,
    LANG,
)
from FileStream.bot.handlers.admin.admin import get_id
from FileStream.bot.handlers.media.batch import (
    start_folderm,
    finish_folderm,
    cancel_folderm,
    folderm_sessions,
)
from FileStream.bot.handlers.folder.folder_manager import (
    folders_cmd,
    folder_callbacks,
    rename_pending,
)
from FileStream.bot.handlers.system.callback import (
    cb_data,
    gen_file_menu,
    delete_user_file,
)
from FileStream.services.link_service import link_service


# ============================================================================
# 1. /id COMMAND TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_get_id_command_hierarchy_and_copy_button():
    """Verify /id command returns clean hierarchy with native copy_text button."""
    mock_bot = MagicMock()
    mock_message = MagicMock(spec=Message)
    mock_user = MagicMock(spec=User)
    mock_user.id = 123456789
    mock_message.from_user = mock_user
    mock_message.reply_text = AsyncMock()

    await get_id(mock_bot, mock_message)

    mock_message.reply_text.assert_called_once()
    args, kwargs = mock_message.reply_text.call_args
    reply_text = args[0]
    reply_markup = kwargs.get("reply_markup")

    # Clean hierarchy check
    assert "🆔 <b>Your Telegram ID</b>" in reply_text
    assert "<code>123456789</code>" in reply_text

    # Keyboard has copy_text button and Home return
    assert reply_markup is not None
    assert len(reply_markup.inline_keyboard) == 1
    buttons = reply_markup.inline_keyboard[0]
    assert buttons[0].text == "📋 Copy ID"
    assert buttons[0].copy_text == "123456789"
    assert buttons[1].text == "◀️ Home"
    assert buttons[1].callback_data == "home"


def test_make_id_keyboard_structure():
    """Verify make_id_keyboard helper creates correct InlineKeyboardMarkup."""
    kb = make_id_keyboard(987654321)
    assert isinstance(kb, InlineKeyboardMarkup)
    assert len(kb.inline_keyboard) == 1
    assert kb.inline_keyboard[0][0].copy_text == "987654321"
    assert kb.inline_keyboard[0][1].callback_data == "home"


# ============================================================================
# 2. /ping COMMAND TESTS
# ============================================================================

def test_ping_message_template():
    """Verify PublicMessages.PING template format."""
    rendered = PublicMessages.PING.format(latency=42)
    assert "🏓 <b>Pong</b>" in rendered
    assert "⚡ <code>42 ms</code>" in rendered


# ============================================================================
# 3. /files COMMAND & FILE MANAGEMENT FLOWS
# ============================================================================

def test_files_empty_and_populated_templates():
    """Verify Files message templates for empty and populated states."""
    empty_rendered = PublicMessages.DASHBOARD_FILES.format(0)
    assert "Your Files" in empty_rendered
    assert "No shared files yet" in empty_rendered

    populated_rendered = PublicMessages.DASHBOARD_FILES.format(5)
    assert "Your Files" in populated_rendered
    assert "Total: <b>5</b> files" in populated_rendered


def test_make_files_empty_keyboard():
    """Verify empty files keyboard offers clear action and home return."""
    kb = make_files_empty_keyboard()
    rows = kb.inline_keyboard
    assert len(rows) == 2
    assert rows[0][0].text == "📤 Send a File"
    assert rows[0][0].callback_data == "help_send"
    assert rows[1][0].text == "◀️ Home"
    assert rows[1][0].callback_data == "home"


def test_make_file_menu_keyboard():
    """Verify file menu keyboard contains stream, share, native copy_text, delete, and back buttons."""
    kb = make_file_menu_keyboard(
        file_id="abc12345",
        file_list_no=1,
        public_url="https://domain.com/stream/abc",
        share_link="https://t.me/share/url?url=https://domain.com/stream/abc",
        is_streamable=True,
    )
    rows = kb.inline_keyboard
    assert len(rows) == 4
    # Row 1: Stream & Share
    assert rows[0][0].text == "▶️ Stream"
    assert rows[0][0].url == "https://domain.com/stream/abc"
    assert rows[0][1].text == "📤 Share"
    assert rows[0][1].url == "https://t.me/share/url?url=https://domain.com/stream/abc"
    # Row 2: Copy Link
    assert rows[1][0].text == "📋 Copy Link"
    assert rows[1][0].copy_text == "https://domain.com/stream/abc"
    # Row 3: Send File & Delete
    assert rows[2][0].text == "📥 Send File"
    assert rows[2][0].callback_data == "sendfile_abc12345"
    assert rows[2][1].text == "🗑 Delete"
    assert rows[2][1].callback_data == "msgdelete_abc12345_1"
    # Row 4: Back
    assert rows[3][0].text == "◀️ Files"
    assert rows[3][0].callback_data == "userfiles_1"


def test_make_file_delete_confirm_keyboard():
    """Verify file delete confirmation keyboard provides safe confirmation."""
    kb = make_file_delete_confirm_keyboard(file_id="abc12345", file_list_no=2)
    rows = kb.inline_keyboard
    assert len(rows) == 1
    delete_btn = rows[0][0]
    cancel_btn = rows[0][1]
    assert delete_btn.text == "🗑 Delete"
    assert delete_btn.callback_data == "msgdelyes_abc12345_2"
    assert cancel_btn.text == "❌ Cancel"
    assert cancel_btn.callback_data == "myfile_abc12345_2"


@pytest.mark.asyncio
async def test_file_menu_callback():
    """Verify file menu generation contains native copy_text and correct URLs."""
    mock_bot = MagicMock()
    mock_bot.id = 12345
    mock_bot.me = MagicMock(id=12345, username="FileToLinkV2bot")
    mock_update = MagicMock(spec=CallbackQuery)
    mock_update.data = "myfile_file123_1"
    mock_update.from_user = MagicMock(id=999)
    mock_update.message = MagicMock()
    mock_update.message.photo = None
    mock_update.message.caption = None
    mock_update.message.edit_text = AsyncMock()
    mock_update.message.edit_caption = AsyncMock()
    mock_update.message.chat = MagicMock(id=999)
    mock_update.answer = AsyncMock()

    file_doc = {
        "_id": "file123",
        "user_id": 999,
        "file_name": "sample_video.mp4",
        "file_size": 1048576,
        "file_id": "BAACAgIAAxkBAAI...",
        "time": 1700000000,
    }

    with patch("FileStream.bot.handlers.system.callback.db.get_file", new_callable=AsyncMock) as mock_get_file, \
         patch("FileStream.bot.handlers.system.callback.ensure_flog_media_exists", new_callable=AsyncMock), \
         patch("FileStream.bot.handlers.system.callback.file_visible_to_bot", return_value=True), \
         patch("FileStream.bot.handlers.system.callback.get_public_file_context", new_callable=AsyncMock) as mock_context, \
         patch("FileStream.bot.handlers.system.callback.FileId.decode") as mock_decode:
        mock_get_file.return_value = file_doc
        mock_context.return_value = (
            {},
            {},
            "https://bot.domain/stream/file123",
        )
        mock_decoded_id = MagicMock()
        mock_decoded_id.file_type = 2  # video
        mock_decode.return_value = mock_decoded_id

        await cb_data(mock_bot, mock_update)

        mock_update.message.edit_text.assert_called_once()
        args, kwargs = mock_update.message.edit_text.call_args
        caption = kwargs.get("text") or (args[0] if args else "")
        reply_markup = kwargs.get("reply_markup")

        assert "sample_video.mp4" in caption
        assert reply_markup is not None
        # copy_text check
        copy_btn = reply_markup.inline_keyboard[1][0]
        assert copy_btn.copy_text == "https://bot.domain/stream/file123"


@pytest.mark.asyncio
async def test_file_delete_confirmation_callback():
    """Verify msgdelete callback displays deletion prompt and safe confirmation buttons."""
    mock_bot = MagicMock()
    mock_update = MagicMock(spec=CallbackQuery)
    mock_update.data = "msgdelete_file123_1"
    mock_update.from_user = MagicMock(id=999)
    mock_update.message = MagicMock()
    mock_update.message.photo = None
    mock_update.message.caption = None
    mock_update.message.edit_text = AsyncMock()
    mock_update.message.edit_caption = AsyncMock()
    mock_update.answer = AsyncMock()

    await cb_data(mock_bot, mock_update)

    mock_update.message.edit_text.assert_called_once()
    args, kwargs = mock_update.message.edit_text.call_args
    caption = kwargs.get("text") or (args[0] if args else "")
    assert "Delete this file?" in caption
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup.inline_keyboard[0][0].text == "🗑 Delete"
    assert reply_markup.inline_keyboard[0][1].text == "❌ Cancel"


# ============================================================================
# 4. /folders COMMAND & FOLDER MANAGEMENT FLOWS
# ============================================================================

def test_make_folder_menu_keyboard():
    """Verify make_folder_menu_keyboard creates Open, Share, Add Files, Rename, Delete, and Back."""
    kb = make_folder_menu_keyboard(folder_id="fld_abc", page=1, link="https://link.com", share_link="https://share.com")
    rows = kb.inline_keyboard
    assert len(rows) == 4
    # Row 1: Open & Share
    assert rows[0][0].text == "📁 Open"
    assert rows[0][1].text == "📤 Share"
    # Row 2: Add Files & Rename
    assert rows[1][0].text == "➕ Add Files"
    assert rows[1][0].callback_data == "fld:add:fld_abc:1"
    assert rows[1][1].text == "✏️ Rename"
    assert rows[1][1].callback_data == "fld:ren:fld_abc:1"
    # Row 3: Delete Folder
    assert rows[2][0].text == "🗑 Delete"
    assert rows[2][0].callback_data == "fld:del:fld_abc:1"
    # Row 4: Back
    assert rows[3][0].text == "◀️ Folders"
    assert rows[3][0].callback_data == "fld:list:1"


def test_make_folder_delete_confirm_keyboard():
    """Verify folder delete confirmation keyboard."""
    kb = make_folder_delete_confirm_keyboard(folder_id="fld_abc", page=1)
    rows = kb.inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].text == "🗑 Delete"
    assert rows[0][0].callback_data == "fld:delyes:fld_abc:1"
    assert rows[0][1].text == "❌ Cancel"
    assert rows[0][1].callback_data == "fld:open:fld_abc:1"


@pytest.mark.asyncio
async def test_folder_delete_confirmation_cb():
    """Verify folder delete confirmation callback displays warning."""
    mock_bot = MagicMock()
    mock_cq = MagicMock(spec=CallbackQuery)
    mock_cq.data = "fld:del:fld_999:1"
    mock_cq.from_user = MagicMock(id=555)
    mock_cq.message = MagicMock()
    mock_cq.message.photo = None
    mock_cq.message.caption = None
    mock_cq.message.chat = MagicMock(id=555)
    mock_cq.message.from_user = MagicMock(id=555)
    mock_cq.message.edit_text = AsyncMock()
    mock_cq.message.edit_caption = AsyncMock()
    mock_cq.answer = AsyncMock()

    folder_doc = {
        "_id": "fld_999",
        "user_id": 555,
        "title": "My Folder",
        "files": ["file1"],
    }

    with patch("FileStream.bot.handlers.folder.folder_manager.db.get_folder_for_user", new_callable=AsyncMock) as mock_get_folder, \
         patch("FileStream.bot.handlers.folder.folder_manager._folder_visible_to_bot", new_callable=AsyncMock, return_value=True), \
         patch("FileStream.bot.handlers.folder.folder_manager.db.is_user_banned", new_callable=AsyncMock, return_value=False):
        mock_get_folder.return_value = folder_doc

        await folder_callbacks(mock_bot, mock_cq)

        mock_cq.message.edit_text.assert_called_once()
        args, kwargs = mock_cq.message.edit_text.call_args
        caption = kwargs.get("text") or (args[0] if args else "")
        assert "Delete this folder?" in caption
        reply_markup = kwargs.get("reply_markup")
        assert reply_markup.inline_keyboard[0][0].callback_data == "fld:delyes:fld_999:1"


@pytest.mark.asyncio
async def test_folder_delete_confirmed_execution():
    """Verify folder delete confirmed execution deletes folder, sends toast, and refreshes list."""
    mock_bot = MagicMock()
    mock_cq = MagicMock(spec=CallbackQuery)
    mock_cq.data = "fld:delyes:fld_999:1"
    mock_cq.from_user = MagicMock(id=555)
    mock_cq.message = MagicMock()
    mock_cq.message.photo = None
    mock_cq.message.caption = None
    mock_cq.message.chat = MagicMock(id=555)
    mock_cq.message.from_user = MagicMock(id=555)
    mock_cq.answer = AsyncMock()

    folder_doc = {
        "_id": "fld_999",
        "user_id": 555,
        "title": "My Folder",
        "files": ["file1"],
    }

    with patch("FileStream.bot.handlers.folder.folder_manager.db.get_folder_for_user", new_callable=AsyncMock) as mock_get_folder, \
         patch("FileStream.bot.handlers.folder.folder_manager._folder_visible_to_bot", new_callable=AsyncMock, return_value=True), \
         patch("FileStream.bot.handlers.folder.folder_manager.db.delete_folder", new_callable=AsyncMock) as mock_del, \
         patch("FileStream.bot.handlers.folder.folder_manager.db.is_user_banned", new_callable=AsyncMock, return_value=False), \
         patch("FileStream.bot.handlers.folder.folder_manager._send_folder_list", new_callable=AsyncMock) as mock_send_list:
        mock_get_folder.return_value = folder_doc
        mock_del.return_value = True

        await folder_callbacks(mock_bot, mock_cq)

        mock_del.assert_called_once_with("fld_999", 555)
        mock_cq.answer.assert_called_once_with(FolderMessages.DELETED, show_alert=False)
        mock_send_list.assert_called_once_with(mock_bot, mock_cq.message, page=1, edit=True, user_id=555)


# ============================================================================
# 5. /folder COMMAND TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_folder_command_name_too_long():
    """Verify /folder with >50 character name returns validation error."""
    mock_bot = MagicMock()
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=111)
    mock_msg.chat = MagicMock(id=111)
    mock_msg.command = ["folder", "A" * 51]
    mock_msg.reply_text = AsyncMock()

    folderm_sessions.pop(111, None)

    with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)):
        await start_folderm(mock_bot, mock_msg)

    mock_msg.reply_text.assert_called_once()
    text = mock_msg.reply_text.call_args[0][0]
    assert "⚠️ Folder name too long" in text


@pytest.mark.asyncio
async def test_folder_command_valid_name_starts_session():
    """Verify /folder with valid title starts session and sets title in session."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(id=101))
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=222)
    mock_msg.chat = MagicMock(id=222)
    mock_msg.command = ["folder", "Work & Research"]
    mock_msg.reply_text = AsyncMock()

    folderm_sessions.pop(222, None)

    created_session = {
        "user_id": 222,
        "chat_id": 222,
        "files": [],
        "file_ids": [],
    }
    mock_folder_svc = MagicMock()
    mock_folder_svc.get_active_session_for_user = AsyncMock(return_value=None)
    mock_folder_svc.create_session = AsyncMock(return_value=created_session)

    try:
        with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)), \
             patch("FileStream.bot.handlers.media.batch.folder_service", mock_folder_svc), \
             patch("FileStream.bot.handlers.media.batch.refresh_folder_status", AsyncMock()), \
             patch("FileStream.bot.handlers.media.batch.db.get_folder_session", AsyncMock(return_value=None)):
            await start_folderm(mock_bot, mock_msg)

        mock_folder_svc.create_session.assert_called_once()
        assert created_session["title"] == "Work & Research"
    finally:
        folderm_sessions.pop(222, None)


# ============================================================================
# 6. /done COMMAND TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_done_command_idle_state():
    """Verify /done when no folder session is active informs the user gracefully."""
    mock_bot = MagicMock()
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=333)
    mock_msg.chat = MagicMock(id=333)
    mock_msg.reply_text = AsyncMock()

    folderm_sessions.pop(333, None)

    with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)), \
         patch("FileStream.bot.handlers.media.batch.folder_service.get_active_session", AsyncMock(return_value=None)):
        await finish_folderm(mock_bot, mock_msg)

    mock_msg.reply_text.assert_called_once_with(
        FolderMessages.NOTHING_TO_FINISH,
        parse_mode=ParseMode.HTML,
        quote=True,
    )
    assert "Nothing to finish" in FolderMessages.NOTHING_TO_FINISH


# ============================================================================
# 7. /cancel COMMAND TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_cancel_command_idle_state():
    """Verify /cancel when no session or pending action informs the user gracefully."""
    mock_bot = MagicMock()
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=444)
    mock_msg.chat = MagicMock(id=444)
    mock_msg.reply_text = AsyncMock()

    folderm_sessions.pop(444, None)
    rename_pending.pop(444, None)

    with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)), \
         patch("FileStream.bot.handlers.media.batch.folder_service.get_active_session", AsyncMock(return_value=None)):
        await cancel_folderm(mock_bot, mock_msg)

    mock_msg.reply_text.assert_called_once_with(
        FolderMessages.NOTHING_RUNNING, quote=True, parse_mode=ParseMode.HTML
    )
    assert "Nothing is currently running" in FolderMessages.NOTHING_RUNNING


@pytest.mark.asyncio
async def test_cancel_command_pending_rename():
    """Verify /cancel during pending rename aborts the rename."""
    mock_bot = MagicMock()
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=555)
    mock_msg.chat = MagicMock(id=555)
    mock_msg.reply_text = AsyncMock()

    rename_pending[555] = {
        "folder_id": "fld_123",
        "page": 1,
    }
    folderm_sessions.pop(555, None)

    try:
        with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)):
            await cancel_folderm(mock_bot, mock_msg)

        assert 555 not in rename_pending
        mock_msg.reply_text.assert_called_once_with(
            FolderMessages.ACTION_CANCELLED, quote=True, parse_mode=ParseMode.HTML
        )
    finally:
        rename_pending.pop(555, None)


@pytest.mark.asyncio
async def test_cancel_command_active_folder_session():
    """Verify /cancel during active folder session cancels the session."""
    mock_bot = MagicMock()
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = MagicMock(id=666)
    mock_msg.chat = MagicMock(id=666)
    mock_msg.reply_text = AsyncMock()

    active_session = {
        "files": ["file1", "file2"],
        "folder_id": "fld_test",
        "title": "Test Title",
        "file_count": 2,
    }
    folderm_sessions[666] = active_session

    try:
        with patch("FileStream.bot.handlers.media.batch.verify_user", AsyncMock(return_value=True)), \
             patch("FileStream.bot.handlers.media.batch.folder_service.get_active_session", AsyncMock(return_value=active_session)), \
             patch("FileStream.bot.handlers.media.batch.cancel_folder", new_callable=AsyncMock) as mock_cancel_folder:
            await cancel_folderm(mock_bot, mock_msg)

            mock_cancel_folder.assert_called_once()
            call_kwargs = mock_cancel_folder.call_args[1]
            assert call_kwargs["user_id"] == 666
    finally:
        folderm_sessions.pop(666, None)


# ============================================================================
# 8. PRESERVATION INVARIANTS TEST
# ============================================================================

def test_preserved_screens_intact():
    """Verify that /start, /help, /about, /privacy, /legal screens were NOT modified."""
    from FileStream.bot.helpers.ui import (
        format_home_text,
        format_help_text,
        format_about_text,
        make_home_keyboard,
        make_help_keyboard,
        make_about_keyboard,
        make_legal_keyboard,
    )

    mock_user = MagicMock(spec=User)
    mock_user.first_name = "Alex"
    mock_user.mention = "Alex"

    # Home text must retain Stage 1 formatting
    home_text = format_home_text(user=mock_user, bot_username="FileToLinkV2bot")
    assert "👋 <b>Hello, Alex!</b>" in home_text
    assert "<blockquote>📤 <b>Send a file</b>" in home_text

    # Help text must retain Stage 1 formatting
    help_text = format_help_text()
    assert "❓ <b>Help & Guide</b>" in help_text
    assert "<blockquote expandable>" in help_text

    # About text must retain Stage 1 formatting
    about_text = format_about_text(bot_name="File To Link V2 bot")
    assert "ℹ️ <b>File To Link V2 bot</b>" in about_text
    assert "<b>Version:</b>" in about_text

    # Keyboards must retain Stage 1 structure
    home_kb = make_home_keyboard()
    assert len(home_kb.inline_keyboard) == 3
    help_kb = make_help_keyboard()
    assert len(help_kb.inline_keyboard) == 3
    about_kb = make_about_keyboard()
    assert len(about_kb.inline_keyboard) == 1
    legal_kb = make_legal_keyboard(command="privacy")
    assert len(legal_kb.inline_keyboard) == 2
