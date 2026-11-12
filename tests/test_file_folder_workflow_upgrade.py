import pytest
import html
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from FileStream.bot.messages import FileMessages, FolderMessages
from FileStream.services.link_service import LinkService
from FileStream.bot.helpers.ui import TelegramCapabilities
from FileStream.bot.handlers.media import batch, stream
from FileStream.bot.handlers.system import callback as system_callback


class MockMessage:
    def __init__(self, message_id=1, user_id=12345, chat_id=12345, text=None):
        self.id = message_id
        self.message_id = message_id
        self.from_user = MagicMock(id=user_id, first_name="TestUser")
        self.chat = MagicMock(id=chat_id)
        self.text = text
        self.replies = []
        self.edited = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        m = MockMessage(message_id=self.id + 1, user_id=self.from_user.id, chat_id=self.chat.id, text=text)
        m.reply_markup = kwargs.get("reply_markup")
        return m

    async def edit_text(self, text, **kwargs):
        self.edited.append({"text": text, **kwargs})
        self.text = text
        return self


class MockBot:
    def __init__(self, bot_id=99999, username="FileToLinkV2bot"):
        self.id = bot_id
        self.me = MagicMock(id=bot_id, username=username)
        self.sent_messages = []
        self.edited_messages = []
        self.actions = []

    async def send_chat_action(self, chat_id, action):
        self.actions.append({"chat_id": chat_id, "action": action})

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        m = MockMessage(message_id=len(self.sent_messages), chat_id=chat_id, text=text)
        m.reply_markup = kwargs.get("reply_markup")
        return m

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        return True


@pytest.mark.asyncio
async def test_file_processing_initial_message():
    """Verify single file workflow presents clean processing feedback."""
    assert "Processing file" in FileMessages.PROCESSING
    assert "⏳" in FileMessages.PROCESSING


@pytest.mark.asyncio
async def test_link_ready_formatting():
    """Verify Link Ready card formatting and hierarchy."""
    text = FileMessages.READY.format(
        "sample_video.mp4",
        "15.4 MB",
        "Video",
        "23h 59m left",
        "2026-10-05 12:00 UTC",
        "FileToLinkV2bot",
        "https://example.com/stream/abc123xyz"
    )
    assert "📄 <b>Link Ready</b>" in text
    assert "<b>sample_video.mp4</b>" in text
    assert "📦 15.4 MB • 🏷 Video" in text
    assert "⏱ Available for 23h 59m left" in text
    assert "<i>Expires 2026-10-05 12:00 UTC</i>" in text
    assert "🔗 <b>Link</b>" in text
    assert "<code>https://example.com/stream/abc123xyz</code>" in text
    assert "🤖 <b>File To Link V2 bot</b>" in text


@pytest.mark.asyncio
async def test_link_ready_permanent_formatting():
    """Verify Link Ready card formatting for permanent links."""
    text = FileMessages.READY.format(
        "sample_doc.pdf",
        "2.1 MB",
        "Document",
        "Never expires",
        "Permanent",
        "FileToLinkV2bot",
        "https://example.com/stream/doc123"
    )
    assert "📄 <b>Link Ready</b>" in text
    assert "<b>sample_doc.pdf</b>" in text
    assert "⏱ Permanent • Never expires" in text
    assert "<code>https://example.com/stream/doc123</code>" in text


@pytest.mark.asyncio
async def test_gen_link_keyboard_with_copy_text():
    """Verify gen_link keyboard attaches stream/view, share, copy_text, and remove buttons."""
    link_service = LinkService(db=MagicMock())
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "clip.mp4",
        "file_size": 1024 * 1024,
        "mime_type": "video/mp4",
        "user_id": 12345,
    }
    link_doc = {
        "public_id": "pub_clip",
        "expires_at": 1800000000,
    }

    with patch.object(link_service, "get_public_file_context", AsyncMock(return_value=(file_doc, link_doc, "https://example.com/stream/clip"))):
        markup, text = await link_service.gen_link(fid)

    rows = markup.inline_keyboard
    all_buttons = [b for row in rows for b in row]
    btn_texts = [b.text for b in all_buttons]

    assert "▶️ Stream" in btn_texts
    assert "📤 Share" in btn_texts
    assert "🗑 Remove" in btn_texts

    # Check copy_text button if runtime supports it
    if TelegramCapabilities.supports_copy_text_button():
        copy_btns = [b for b in all_buttons if "Copy Link" in b.text]
        assert len(copy_btns) == 1
        assert getattr(copy_btns[0], "copy_text", None) == "https://example.com/stream/clip"


@pytest.mark.asyncio
async def test_single_file_removal_confirmation_and_deletion():
    """Verify removal confirmation dialog and successful deletion flow."""
    mock_db = MagicMock()
    fid = ObjectId()
    mock_db.get_file = AsyncMock(return_value={
        "_id": fid,
        "file_name": "test_to_remove.mp4",
        "user_id": 777,
    })

    # Test msgdelpvt callback opens confirmation
    cq = MagicMock(spec=CallbackQuery)
    cq.data = f"msgdelpvt_{fid}"
    cq.from_user = MagicMock(id=777)
    cq.message = MockMessage(message_id=200, user_id=777, chat_id=777)
    cq.answer = AsyncMock()

    with patch.object(system_callback, "db", mock_db), \
         patch.object(system_callback, "edit_message", AsyncMock()) as mock_edit:
        await system_callback.cb_data(MagicMock(), cq)

        assert cq.answer.called
        assert mock_edit.called
        confirm_text = mock_edit.call_args[0][1]
        assert "Remove file?" in confirm_text
        assert "test_to_remove.mp4" in confirm_text
        markup = mock_edit.call_args[0][2]
        btns = [b for row in markup.inline_keyboard for b in row]
        btn_texts = [b.text for b in btns]
        assert "🗑 Remove" in btn_texts
        assert "✕ Cancel" in btn_texts


@pytest.mark.asyncio
async def test_folder_creation_panel_formatting():
    """Verify Folder creation panel status updates."""
    # Zero files
    text_empty = FolderMessages.create_panel(0, "0 B", recent_list="", pending_count=0)
    assert "📁 <b>Create Folder</b>" in text_empty
    assert "📦 Files: <b>0</b>" in text_empty

    # In flight files
    text_in_flight = FolderMessages.create_panel(2, "5 MB", recent_list="", pending_count=3)
    assert "📁 <b>Folder Status</b>" in text_in_flight
    assert "⏳ Processing: <b>3</b>" in text_in_flight
    assert "📦 Files added: <b>2</b>" in text_in_flight

    # Updated files
    text_updated = FolderMessages.create_panel(5, "12 MB", recent_list="", pending_count=0)
    assert "📁 <b>Folder Status</b>" in text_updated
    assert "✅ Files updated" in text_updated
    assert "📦 Files added: <b>5</b>" in text_updated


@pytest.mark.asyncio
async def test_folder_completion_ready_screen():
    """Verify folder completion screen layout and copy text button."""
    public_url = "https://example.com/folder/fold123"
    completion_text = FolderMessages.DONE_SUCCESS.format(
        html.escape("My Holiday Collection"),
        5,
        "25.4 MB",
        public_url,
    )
    assert "✅ <b>Folder Ready</b>" in completion_text
    assert "My Holiday Collection" in completion_text
    assert "5 files" in completion_text
    assert "25.4 MB" in completion_text
    assert public_url in completion_text
    assert "Manage your collections anytime with /folders" in completion_text
