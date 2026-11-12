import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId

from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from FileStream.services.folder_service import FolderService, FolderState
from FileStream.services.file_service import FileService
from FileStream.services.storage_service import StorageService, StorageStatus
from FileStream.services.link_service import LinkService
from FileStream.bot.plugins.batch import handle_folder_file
from FileStream.bot.plugins.start import my_files
from FileStream.bot.messages import PublicMessages
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


# ---------------------------------------------------------------------------
# F1: Normal uploaded file appears in "/files"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F1_normal_uploaded_file_appears_in_files(mock_db, mock_bot):
    user_id = 10001
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    # User uploads a normal file (no folder session)
    msg = MockMessage(msg_id=101, text="document", chat_id=user_id, from_user_id=user_id)
    doc_mock = MagicMock()
    doc_mock.file_id = "normal_doc_id_1"
    doc_mock.file_unique_id = "uniq_norm_1"
    doc_mock.file_name = "report.pdf"
    doc_mock.file_size = 5000
    doc_mock.mime_type = "application/pdf"
    msg.document = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        inserted_id, is_new = await file_service.process_new_file(msg, user_id=user_id)

    assert inserted_id is not None
    assert is_new is True

    # Check database state
    file_doc = await mock_db.get_file(inserted_id)
    assert file_doc["user_id"] == user_id
    assert file_doc.get("is_folder_file") is not True
    assert not file_doc.get("folder_session_id")
    assert not file_doc.get("folders")

    # Verify via gen_file_list_button
    file_list, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 1
    # Check that report.pdf is listed in the buttons
    button_texts = [b.text for row in file_list for b in row]
    assert any("report.pdf" in txt for txt in button_texts)

    # Verify via actual my_files handler
    cmd_msg = MockMessage(msg_id=102, text="/files", chat_id=user_id, from_user_id=user_id)
    cmd_msg.chat.type = "private"
    with patch("FileStream.bot.plugins.start.verify_user", return_value=True), \
         patch("FileStream.bot.plugins.start.is_secondary_worker", return_value=False), \
         patch("FileStream.bot.plugins.start.gen_file_list_button", link_service.gen_file_list_button):
        await my_files(mock_bot, cmd_msg)

    # User must have received a reply containing Total links: 1
    assert len(cmd_msg.replies) > 0
    replied_text = cmd_msg.replies[0].get("text", "")
    assert "1" in replied_text


# ---------------------------------------------------------------------------
# F2: Normal forwarded file appears in "/files"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F2_normal_forwarded_file_appears_in_files(mock_db, mock_bot):
    user_id = 10002
    other_user_id = 99999
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    # User forwards a file originally authored by other_user_id
    msg = MockMessage(msg_id=201, text="document", chat_id=user_id, from_user_id=user_id)
    msg.forward_from = MagicMock()
    msg.forward_from.id = other_user_id
    msg.forward_date = time.time() - 3600
    doc_mock = MagicMock()
    doc_mock.file_id = "fwd_doc_id_2"
    doc_mock.file_unique_id = "uniq_fwd_2"
    doc_mock.file_name = "forwarded_notes.pdf"
    doc_mock.file_size = 12000
    doc_mock.mime_type = "application/pdf"
    msg.document = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        inserted_id, is_new = await file_service.process_new_file(msg, user_id=user_id)

    assert inserted_id is not None
    # Must belong to the user forwarding it, not other_user_id
    file_doc = await mock_db.get_file(inserted_id)
    assert file_doc["user_id"] == user_id

    # Appears in user_id's /files
    file_list, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 1
    button_texts = [b.text for row in file_list for b in row]
    assert any("forwarded_notes.pdf" in txt for txt in button_texts)

    # Does NOT appear in other_user_id's /files
    _, other_total = await link_service.gen_file_list_button(1, other_user_id, bot=mock_bot)
    assert other_total == 0


# ---------------------------------------------------------------------------
# F3: Folder uploaded file does NOT appear in "/files"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F3_folder_uploaded_file_does_not_appear_in_files(mock_db, mock_bot):
    user_id = 10003
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    # Start folder session
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    # User uploads a file while folder mode is active
    msg = MockMessage(msg_id=301, text="document", chat_id=user_id, from_user_id=user_id)
    doc_mock = MagicMock()
    doc_mock.file_id = "folder_doc_3"
    doc_mock.file_unique_id = "uniq_fld_3"
    doc_mock.file_name = "course_slides.pdf"
    doc_mock.file_size = 8000
    doc_mock.mime_type = "application/pdf"
    msg.document = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        inserted_id, is_new = await file_service.process_new_file(
            msg, session_id=session_id, user_id=user_id
        )

    assert inserted_id is not None
    file_doc = await mock_db.get_file(inserted_id)
    assert file_doc["folder_session_id"] == session_id
    assert file_doc["is_folder_file"] is True

    # Register in folder session
    ok, reason, _ = await folder_svc.add_file(user_id, file_doc, is_new=is_new)
    assert ok is True

    # CRITICAL: /files MUST NOT return the folder file
    file_list, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    button_texts = [b.text for row in file_list for b in row]
    assert not any("course_slides.pdf" in txt for txt in button_texts)
    assert any("📭 Empty" in txt for txt in button_texts)

    # total_files count must be 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F4: Folder forwarded file does NOT appear in "/files"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F4_folder_forwarded_file_does_not_appear_in_files(mock_db, mock_bot):
    user_id = 10004
    forward_author_id = 88888
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    # Start folder mode
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    # User forwards file inside folder mode
    msg = MockMessage(msg_id=401, text="document", chat_id=user_id, from_user_id=user_id)
    msg.forward_from = MagicMock()
    msg.forward_from.id = forward_author_id
    doc_mock = MagicMock()
    doc_mock.file_id = "fwd_folder_doc_4"
    doc_mock.file_unique_id = "uniq_fwd_fld_4"
    doc_mock.file_name = "lecture.mp4"
    doc_mock.file_size = 250000
    doc_mock.mime_type = "video/mp4"
    msg.video = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        inserted_id, is_new = await file_service.process_new_file(
            msg, session_id=session_id, user_id=user_id
        )

    file_doc = await mock_db.get_file(inserted_id)
    assert file_doc["user_id"] == user_id
    assert file_doc["is_folder_file"] is True

    await folder_svc.add_file(user_id, file_doc, is_new=is_new)

    # MUST NOT appear in user_id /files
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0

    # MUST NOT appear in author's /files
    _, author_total = await link_service.gen_file_list_button(1, forward_author_id, bot=mock_bot)
    assert author_total == 0


# ---------------------------------------------------------------------------
# F5: Multiple folder files do not appear in "/files"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F5_multiple_folder_files_do_not_appear_in_files(mock_db, mock_bot):
    user_id = 10005
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    for i in range(15):
        msg = MockMessage(msg_id=500 + i, text="document", chat_id=user_id, from_user_id=user_id)
        doc_mock = MagicMock()
        doc_mock.file_id = f"doc_{i}"
        doc_mock.file_unique_id = f"uniq_{i}"
        doc_mock.file_name = f"chapter_{i}.pdf"
        doc_mock.file_size = 1000 * (i + 1)
        doc_mock.mime_type = "application/pdf"
        msg.document = doc_mock
        msg.chat.type = "private"

        with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
            ins_id, is_new = await file_service.process_new_file(
                msg, session_id=session_id, user_id=user_id
            )
            fdoc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    # 15 files added to folder session
    sess = folder_svc.get_session(user_id)
    assert sess["file_count"] == 15

    # /files MUST be 0
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F6: Folder video/document/audio/photo all follow the same rule
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F6_all_media_types_follow_folder_rule(mock_db, mock_bot):
    user_id = 10006
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    media_types = ["document", "video", "audio", "photo"]
    for i, mtype in enumerate(media_types):
        msg = MockMessage(msg_id=600 + i, text=mtype, chat_id=user_id, from_user_id=user_id)
        m_mock = MagicMock()
        m_mock.file_id = f"media_{mtype}_{i}"
        m_mock.file_unique_id = f"uniq_{mtype}_{i}"
        m_mock.file_name = f"sample.{'mp4' if mtype == 'video' else 'mp3' if mtype == 'audio' else 'jpg' if mtype == 'photo' else 'pdf'}"
        m_mock.file_size = 10000
        m_mock.mime_type = f"application/{mtype}"
        setattr(msg, mtype, m_mock)
        msg.chat.type = "private"

        with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
            ins_id, is_new = await file_service.process_new_file(
                msg, session_id=session_id, user_id=user_id
            )
            fdoc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F7: Folder cancellation prevents "/files" leakage
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F7_folder_cancellation_prevents_files_leakage(mock_db, mock_bot):
    user_id = 10007
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    for i in range(3):
        msg = MockMessage(msg_id=700 + i, text="doc", chat_id=user_id, from_user_id=user_id)
        m_mock = MagicMock()
        m_mock.file_id = f"canc_file_{i}"
        m_mock.file_unique_id = f"uniq_canc_{i}"
        m_mock.file_name = f"doc_{i}.pdf"
        m_mock.file_size = 2000
        msg.document = m_mock
        msg.chat.type = "private"

        with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
            ins_id, is_new = await file_service.process_new_file(
                msg, session_id=session_id, user_id=user_id
            )
            fdoc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    # Cancel folder session
    cancelled = await folder_svc.cancel_session(user_id, bot=mock_bot)
    assert cancelled is True

    # /files must be completely empty
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F8: Folder storage failure does not create standalone "/files" entry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F8_folder_storage_failure_does_not_create_standalone_entry(mock_db, mock_bot):
    user_id = 10008
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    msg = MockMessage(msg_id=801, text="doc", chat_id=user_id, from_user_id=user_id)
    m_mock = MagicMock()
    m_mock.file_id = "fail_file"
    m_mock.file_unique_id = "uniq_fail_8"
    m_mock.file_name = "fail_upload.pdf"
    m_mock.file_size = 3000
    msg.document = m_mock
    msg.chat.type = "private"

    # Simulate storage failure
    with patch("FileStream.services.storage_service.storage_service.store_file", side_effect=RuntimeError("Connection timeout")):
        with pytest.raises(RuntimeError):
            await file_service.process_new_file(msg, session_id=session_id, user_id=user_id)

    # /files MUST NOT return the failed folder file
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F9: Folder finalization preserves folder association
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F9_folder_finalization_preserves_folder_association(mock_db, mock_bot):
    user_id = 10009
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    file_ids = []
    for i in range(3):
        msg = MockMessage(msg_id=900 + i, text="doc", chat_id=user_id, from_user_id=user_id)
        m_mock = MagicMock()
        m_mock.file_id = f"fin_file_{i}"
        m_mock.file_unique_id = f"uniq_fin_{i}"
        m_mock.file_name = f"doc_fin_{i}.pdf"
        m_mock.file_size = 4000
        msg.document = m_mock
        msg.chat.type = "private"

        with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
            ins_id, is_new = await file_service.process_new_file(
                msg, session_id=session_id, user_id=user_id
            )
            file_ids.append(ins_id)
            fdoc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    # Finalize folder
    folder_doc, public_url = await folder_svc.finalize_folder(user_id=user_id, bot=mock_bot, title="My Finished Folder")
    assert folder_doc is not None
    assert public_url is not None

    # Check files in database have folders and is_folder_file
    for fid in file_ids:
        fdoc = await mock_db.get_file(fid)
        assert folder_doc["_id"] in fdoc.get("folders", [])
        assert fdoc.get("is_folder_file") is True

    # /files MUST NOT show any of these files
    file_list, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F10: Rapid concurrent folder uploads preserve correct folder association
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F10_rapid_concurrent_folder_uploads(mock_db, mock_bot):
    user_id = 10010
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    async def upload_worker(idx):
        msg = MockMessage(msg_id=1000 + idx, text="doc", chat_id=user_id, from_user_id=user_id)
        m_mock = MagicMock()
        m_mock.file_id = f"conc_file_{idx}"
        m_mock.file_unique_id = f"uniq_conc_{idx}"
        m_mock.file_name = f"concurrent_{idx}.pdf"
        m_mock.file_size = 1000
        msg.document = m_mock
        msg.chat.type = "private"

        with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
            ins_id, is_new = await file_service.process_new_file(
                msg, session_id=session_id, user_id=user_id
            )
            fdoc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    # 20 concurrent uploads
    await asyncio.gather(*[upload_worker(i) for i in range(20)])

    sess = folder_svc.get_session(user_id)
    assert sess["file_count"] == 20

    # /files MUST be 0
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0


# ---------------------------------------------------------------------------
# F11: Multiple users creating folders concurrently do not cross-associate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F11_multiple_users_concurrent_isolation(mock_db, mock_bot):
    user_a = 10011
    user_b = 20011
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    sess_a = await folder_svc.create_session(user_id=user_a, chat_id=user_a)
    sess_b = await folder_svc.create_session(user_id=user_b, chat_id=user_b)

    # User A uploads into folder
    msg_a = MockMessage(msg_id=1101, text="doc", chat_id=user_a, from_user_id=user_a)
    doc_a = MagicMock()
    doc_a.file_id = "fld_a_1"
    doc_a.file_unique_id = "uniq_a_1"
    doc_a.file_name = "user_a_folder.pdf"
    doc_a.file_size = 5000
    msg_a.document = doc_a
    msg_a.chat.type = "private"

    # User B uploads into folder
    msg_b = MockMessage(msg_id=1102, text="doc", chat_id=user_b, from_user_id=user_b)
    doc_b = MagicMock()
    doc_b.file_id = "fld_b_1"
    doc_b.file_unique_id = "uniq_b_1"
    doc_b.file_name = "user_b_folder.pdf"
    doc_b.file_size = 7000
    msg_b.document = doc_b
    msg_b.chat.type = "private"

    # User A also uploads a normal standalone file!
    msg_a_norm = MockMessage(msg_id=1103, text="doc", chat_id=user_a, from_user_id=user_a)
    doc_a_norm = MagicMock()
    doc_a_norm.file_id = "norm_a_1"
    doc_a_norm.file_unique_id = "uniq_norm_a_1"
    doc_a_norm.file_name = "user_a_standalone.pdf"
    doc_a_norm.file_size = 3000
    msg_a_norm.document = doc_a_norm
    msg_a_norm.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        ins_a, is_new_a = await file_service.process_new_file(msg_a, session_id=sess_a["session_id"], user_id=user_a)
        await folder_svc.add_file(user_a, await mock_db.get_file(ins_a), is_new=is_new_a)

        ins_b, is_new_b = await file_service.process_new_file(msg_b, session_id=sess_b["session_id"], user_id=user_b)
        await folder_svc.add_file(user_b, await mock_db.get_file(ins_b), is_new=is_new_b)

        await file_service.process_new_file(msg_a_norm, user_id=user_a)

    # User A has exactly 1 standalone file
    file_list_a, total_a = await link_service.gen_file_list_button(1, user_a, bot=mock_bot)
    assert total_a == 1
    btn_a = [b.text for row in file_list_a for b in row]
    assert any("user_a_standalone.pdf" in txt for txt in btn_a)
    assert not any("user_a_folder.pdf" in txt for txt in btn_a)

    # User B has 0 standalone files
    _, total_b = await link_service.gen_file_list_button(1, user_b, bot=mock_bot)
    assert total_b == 0


# ---------------------------------------------------------------------------
# F12: Restart/recovery preserves folder association
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F12_restart_recovery_preserves_folder_association(mock_db, mock_bot):
    user_id = 10012
    folder_svc = FolderService(mock_db)
    storage_svc = StorageService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    # File 1: In processing state, has flog_msg_id
    fid1 = ObjectId()
    await mock_db.file.insert_one({
        "_id": fid1,
        "user_id": user_id,
        "file_name": "recovered1.pdf",
        "folder_session_id": session_id,
        "is_folder_file": True,
        "flog_msg_id": 12345,
        "storage_status": StorageStatus.PROCESSING,
    })

    # File 2: In processing state, missing flog_msg_id
    fid2 = ObjectId()
    await mock_db.file.insert_one({
        "_id": fid2,
        "user_id": user_id,
        "file_name": "recovered2.pdf",
        "folder_session_id": session_id,
        "is_folder_file": True,
        "storage_status": StorageStatus.PROCESSING,
    })

    # Simulate process restart recovery
    reconciled = await storage_svc.reconcile_unsettled_files()
    assert reconciled >= 1

    f1 = await mock_db.get_file(str(fid1))
    f2 = await mock_db.get_file(str(fid2))

    assert f1["storage_status"] == StorageStatus.STORED
    assert f1["is_folder_file"] is True
    assert f2["storage_status"] == StorageStatus.FAILED
    assert f2["is_folder_file"] is True

    # Neither file must appear in /files!
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F13: Same file forwarded multiple times does not create standalone entries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F13_duplicate_forward_does_not_create_standalone_entries(mock_db, mock_bot):
    user_id = 10013
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    msg = MockMessage(msg_id=1301, text="doc", chat_id=user_id, from_user_id=user_id)
    msg.forward_from = MagicMock()
    msg.forward_from.id = 55555
    doc_mock = MagicMock()
    doc_mock.file_id = "dup_file_id"
    doc_mock.file_unique_id = "same_uniq_13"
    doc_mock.file_name = "duplicate.pdf"
    doc_mock.file_size = 5000
    msg.document = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        # 1st forward
        ins_id1, is_new1 = await file_service.process_new_file(
            msg, check_duplicate=True, session_id=session_id, user_id=user_id
        )
        fdoc1 = await mock_db.get_file(ins_id1)
        ok1, reason1, _ = await folder_svc.add_file(user_id, fdoc1, is_new=is_new1)
        assert ok1 is True

        # 2nd forward (same file)
        ins_id2, is_new2 = await file_service.process_new_file(
            msg, check_duplicate=True, session_id=session_id, user_id=user_id
        )
        fdoc2 = await mock_db.get_file(ins_id2)
        ok2, reason2, _ = await folder_svc.add_file(user_id, fdoc2, is_new=is_new2)
        assert ok2 is False
        assert reason2 == "duplicate"

    # /files must be completely empty (0 entries)
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F14: 500-file folder stress test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F14_500_file_folder_stress(mock_db, mock_bot):
    user_id = 10014
    folder_svc = FolderService(mock_db)
    link_service = LinkService(mock_db)

    # User creates 10 standalone files
    for i in range(10):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "user_id": user_id,
            "file_name": f"standalone_{i}.pdf",
            "file_size": 1000,
            "time": time.time() + i,
        })

    # User creates a 500-file folder
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    folder_fids = []
    for i in range(500):
        fid = ObjectId()
        await mock_db.file.insert_one({
            "_id": fid,
            "user_id": user_id,
            "file_name": f"folder_item_{i}.pdf",
            "file_size": 500,
            "folder_session_id": session_id,
            "is_folder_file": True,
            "time": time.time() + 100 + i,
        })
        folder_fids.append(str(fid))

    # Before finalization
    cursor, total_before = await mock_db.find_files(user_id, (1, 10))
    assert total_before == 10
    assert await mock_db.total_files(user_id) == 10

    # Finalize folder with 500 items
    session["file_ids"] = folder_fids
    session["files"] = [{"file_id": fid, "file_size": 500} for fid in folder_fids]
    await folder_svc.finalize_folder(user_id, bot=mock_bot, title="500 Stress Folder")

    # After finalization: exactly the 10 standalone files must appear!
    cursor, total_after = await mock_db.find_files(user_id, (1, 10))
    assert total_after == 10
    assert await mock_db.total_files(user_id) == 10

    # Retrieve all 10 files from cursor
    listed = [d async for d in cursor]
    assert len(listed) == 10
    assert all("standalone_" in d["file_name"] for d in listed)
    assert not any("folder_item_" in d["file_name"] for d in listed)


# ---------------------------------------------------------------------------
# F15: Folder + FloodWait + retry preserves folder association
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F15_floodwait_retry_preserves_folder_association(mock_db, mock_bot):
    from pyrogram.errors import FloodWait
    user_id = 10015
    folder_svc = FolderService(mock_db)
    file_service = FileService(mock_db, {0: mock_bot})
    link_service = LinkService(mock_db)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    msg = MockMessage(msg_id=1501, text="doc", chat_id=user_id, from_user_id=user_id)
    doc_mock = MagicMock()
    doc_mock.file_id = "flood_file"
    doc_mock.file_unique_id = "uniq_flood_15"
    doc_mock.file_name = "flood_retry.pdf"
    doc_mock.file_size = 5000
    msg.document = doc_mock
    msg.chat.type = "private"

    # Simulate FloodWait on first call, success on second
    call_count = 0
    async def mock_store(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            fw = FloodWait(value=0)
            raise fw
        res = MagicMock()
        res.success = True
        res.message_id = 999
        return res

    with patch("FileStream.bot.plugins.batch.folder_service", folder_svc), \
         patch("FileStream.bot.plugins.batch.file_service", file_service), \
         patch("FileStream.bot.plugins.batch.db", mock_db), \
         patch("FileStream.services.storage_service.storage_service.store_file", side_effect=mock_store), \
         patch("asyncio.sleep", new=AsyncMock()):
        ok = await handle_folder_file(mock_bot, msg, user_id=user_id)
        assert ok is True

    # Folder file was retried and added to folder session
    sess = folder_svc.get_session(user_id)
    assert sess["file_count"] == 1

    # /files MUST be 0
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# F16: Folder + multi-worker storage preserves folder association
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_F16_multi_worker_storage_preserves_folder_association(mock_db, mock_bot):
    user_id = 10016
    folder_svc = FolderService(mock_db)
    link_service = LinkService(mock_db)

    worker1 = MockBot()
    worker1.id = 701
    worker2 = MockBot()
    worker2.id = 702
    multi_clients = {0: mock_bot, 1: worker1, 2: worker2}

    file_service = FileService(mock_db, multi_clients)

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    session_id = session["session_id"]

    msg = MockMessage(msg_id=1601, text="doc", chat_id=user_id, from_user_id=user_id)
    doc_mock = MagicMock()
    doc_mock.file_id = "multi_worker_file"
    doc_mock.file_unique_id = "uniq_multi_16"
    doc_mock.file_name = "multi_worker.mkv"
    doc_mock.file_size = 50000
    msg.document = doc_mock
    msg.chat.type = "private"

    with patch("FileStream.services.file_service.get_file_ids", new=AsyncMock()):
        ins_id, is_new = await file_service.process_new_file(
            msg, bot_id=701, session_id=session_id, user_id=user_id
        )
        fdoc = await mock_db.get_file(ins_id)
        await folder_svc.add_file(user_id, fdoc, is_new=is_new)

    # Document stored
    stored_doc = await mock_db.get_file(ins_id)
    assert stored_doc["is_folder_file"] is True
    assert stored_doc["folder_session_id"] == session_id

    # /files MUST be 0
    _, total_files = await link_service.gen_file_list_button(1, user_id, bot=mock_bot)
    assert total_files == 0
    assert await mock_db.total_files(user_id) == 0


# ---------------------------------------------------------------------------
# Pagination: 50 standalone files + 20 folder files across pages
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pagination_folder_files_do_not_leak_onto_pages(mock_db, mock_bot):
    user_id = 10020
    link_service = LinkService(mock_db)

    # 50 standalone files
    for i in range(50):
        await mock_db.file.insert_one({
            "_id": ObjectId(),
            "user_id": user_id,
            "file_name": f"standalone_file_{i:02d}.pdf",
            "time": 1000 + i,
        })

    # 20 folder files (mix of session and finalized)
    for i in range(10):
        await mock_db.file.insert_one({
            "_id": ObjectId(),
            "user_id": user_id,
            "file_name": f"session_file_{i:02d}.pdf",
            "folder_session_id": "active_sess_abc",
            "is_folder_file": True,
            "time": 2000 + i,
        })
    for i in range(10):
        await mock_db.file.insert_one({
            "_id": ObjectId(),
            "user_id": user_id,
            "file_name": f"finalized_file_{i:02d}.pdf",
            "folders": ["folder_xyz"],
            "folder_id": "folder_xyz",
            "is_folder_file": True,
            "time": 3000 + i,
        })

    # Page size is 10, total standalone files = 50 => exactly 5 pages
    for page in range(1, 6):
        file_list, total_files = await link_service.gen_file_list_button(page, user_id, bot=mock_bot)
        assert total_files == 50

        # Collect button names on this page
        names = [b.text for row in file_list for b in row if not b.text.startswith("◀️") and not b.text.startswith("·") and not b.text.startswith("Next") and not b.text.startswith("📄") and not b.text.startswith("❌")]
        assert len(names) == 10
        assert all("standalone_file_" in n for n in names)
        assert not any("session_file_" in n for n in names)
        assert not any("finalized_file_" in n for n in names)


# ---------------------------------------------------------------------------
# Search and filter base query exclusion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_and_filters_exclude_folder_files(mock_db):
    user_id = 10021

    # Standalone match
    await mock_db.file.insert_one({
        "_id": ObjectId(),
        "user_id": user_id,
        "file_name": "python_tutorial_standalone.pdf",
        "mime_type": "application/pdf",
        "time": 1000.0,
    })

    # Folder match (should be excluded even though name matches)
    await mock_db.file.insert_one({
        "_id": ObjectId(),
        "user_id": user_id,
        "file_name": "python_tutorial_folder.pdf",
        "mime_type": "application/pdf",
        "folder_id": "fld_py",
        "folders": ["fld_py"],
        "is_folder_file": True,
        "time": 1000.0,
    })

    # Search by filename
    cursor, total = await mock_db.search_files(user_id, "python", (1, 10))
    assert total == 1
    files = [f async for f in cursor]
    assert len(files) == 1
    assert files[0]["file_name"] == "python_tutorial_standalone.pdf"


# ---------------------------------------------------------------------------
# Downloads integrity: folder files remain accessible and downloadable
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_downloads_integrity_folder_files_remain_accessible(mock_db, mock_bot):
    user_id = 10022
    folder_svc = FolderService(mock_db)

    fid = ObjectId()
    await mock_db.file.insert_one({
        "_id": fid,
        "user_id": user_id,
        "file_name": "downloadable_lecture.mp4",
        "file_size": 1048576,
        "mime_type": "video/mp4",
        "file_id": "tg_file_id_download",
        "folder_session_id": "sess_dl",
        "is_folder_file": True,
    })

    # 1. get_file MUST still work
    doc = await mock_db.get_file(str(fid))
    assert doc is not None
    assert doc["file_name"] == "downloadable_lecture.mp4"

    # 2. Add to folder and finalize
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id)
    await folder_svc.add_file(user_id, doc, is_new=True)
    folder_doc, public_url = await folder_svc.finalize_folder(user_id, bot=mock_bot, title="Download Test")

    # 3. Resolve folder reference
    resolved_folder = await mock_db.get_folder(folder_doc["_id"])
    assert resolved_folder is not None
    assert str(fid) in resolved_folder["files"]
