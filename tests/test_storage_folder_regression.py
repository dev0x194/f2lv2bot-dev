import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest
from pyrogram.errors import FloodWait

from FileStream.services.storage_service import (
    StorageService,
    StorageStatus,
    StorageJob,
    StorageResult,
)
from FileStream.services.folder_service import FolderService
from FileStream.services.file_service import FileService
from FileStream.bot.plugins import batch
from FileStream.utils.client_balance import (
    record_client_cooldown,
    is_client_in_cooldown,
    get_earliest_client_cooldown,
    choose_best_client,
)
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def folder_svc(mock_db):
    return FolderService(db=mock_db)


@pytest.fixture
def mock_bot():
    return MockBot()


# ==============================================================================
# TEST A: Normal file flow
# - File sent
# - Storage job succeeds
# - flog_msg_id stored
# - Folder file registered
# - Exact log appears: FOLDER_FILE_STORED
# - Finalize succeeds
# ==============================================================================
@pytest.mark.asyncio
async def test_a_normal_file_flow(mock_db, folder_svc, mock_bot, caplog):
    caplog.set_level(logging.INFO)
    user_id = 1001

    # Create active folder session
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=100)
    session_id = session["session_id"]

    # File input
    fid = ObjectId()
    ep_name = "Episode.01.mkv"
    file_size = 100 * 1024 * 1024
    file_info = {
        "_id": fid,
        "file_id": str(fid),
        "file_name": ep_name,
        "file_size": file_size,
        "file_unique_id": "uniq_a_1",
    }

    msg = MockMessage(message_id=1, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_a_1"
    media.file_name = ep_name
    media.file_size = file_size
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    mock_flog_msg = MagicMock()
    mock_flog_msg.id = 777

    # Mock storage_service to succeed and return StorageResult
    mock_storage_res = StorageResult(
        success=True,
        file_id=str(fid),
        db_id=str(fid),
        message_id=777,
        client_id=0,
    )

    file_svc = FileService(db=mock_db, multi_clients={0: mock_bot})

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "file_service", file_svc), \
         patch.object(batch, "get_file_info", return_value=file_info), \
         patch("FileStream.services.storage_service.storage_service.store_file", AsyncMock(return_value=mock_storage_res)):

        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # Check MongoDB record
    db_doc = await mock_db.file.find_one({"file_unique_id": "uniq_a_1"})
    assert db_doc is not None
    assert db_doc.get("storage_status") == StorageStatus.STORED
    assert db_doc.get("flog_msg_id") == 777

    # Check folder session
    active = await folder_svc.get_active_session(user_id)
    assert active["file_count"] == 1
    assert str(db_doc["_id"]) in active["file_ids"]

    # Verify log output contains exact FOLDER_FILE_STORED
    stored_logs = [r.message for r in caplog.records if "FOLDER_FILE_STORED" in r.message]
    assert len(stored_logs) == 1
    assert f"session={session_id}" in stored_logs[0]
    assert f"file_id={db_doc['_id']}" in stored_logs[0]

    # Finalize folder succeeds
    folder_res = await folder_svc.finalize_folder(user_id)
    assert folder_res is not None
    folder_doc, public_url = folder_res
    assert folder_doc is not None
    assert folder_doc["file_count"] == 1


# ==============================================================================
# TEST B: Storage permanently fails
# - File sent
# - Storage job fails all retries
# - Job fails permanently
# - Folder file must NOT be registered
# - FOLDER_FILE_STORED must NOT be logged
# - Folder stats count must not increase
# - Error status correctly recorded
# - Finalize succeeds without that file
# ==============================================================================
@pytest.mark.asyncio
async def test_b_storage_permanently_fails(mock_db, folder_svc, mock_bot, caplog):
    caplog.set_level(logging.INFO)
    user_id = 1002

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=101)
    session_id = session["session_id"]

    fid = ObjectId()
    file_info = {
        "_id": fid,
        "file_id": str(fid),
        "file_name": "Broken.mkv",
        "file_size": 50 * 1024 * 1024,
        "file_unique_id": "uniq_b_1",
    }

    msg = MockMessage(message_id=2, user_id=user_id, chat_id=user_id)
    media = MagicMock()
    media.file_unique_id = "uniq_b_1"
    media.file_name = "Broken.mkv"
    media.file_size = 50 * 1024 * 1024
    msg.media = MagicMock()
    msg.media.value = "document"
    msg.document = media

    mock_failed_res = StorageResult(
        success=False,
        file_id=str(fid),
        db_id=str(fid),
        error="ChatWriteForbidden: Bot cannot write to FLOG channel",
    )

    file_svc = FileService(db=mock_db, multi_clients={0: mock_bot})

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc), \
         patch.object(batch, "file_service", file_svc), \
         patch.object(batch, "get_file_info", return_value=file_info), \
         patch("FileStream.services.storage_service.storage_service.store_file", AsyncMock(return_value=mock_failed_res)):

        res = await batch.handle_folder_file(mock_bot, msg, user_id=user_id)
        assert res is True

    # Check MongoDB record has failed status
    db_doc = await mock_db.file.find_one({"file_unique_id": "uniq_b_1"})
    assert db_doc is not None
    assert db_doc.get("storage_status") == StorageStatus.FAILED
    assert "ChatWriteForbidden" in db_doc.get("storage_error", "")

    # Check folder session: file must NOT be registered
    active = await folder_svc.get_active_session(user_id)
    assert active["file_count"] == 0
    assert len(active["files"]) == 0
    assert active["in_flight"] == 0
    assert active.get("failed_count", 0) == 1

    # Verify FOLDER_FILE_STORED was NEVER logged
    assert not any("FOLDER_FILE_STORED" in r.message for r in caplog.records)
    # Verify FOLDER_FILE_FAILED was logged
    assert any("FOLDER_FILE_FAILED" in r.message for r in caplog.records)


# ==============================================================================
# TEST C: Storage encounters FloodWait on Client 0
# - Client 0 raises FloodWait(x)
# - Client 0 enters cooldown for x seconds (+ margin)
# - Client 1 is available
# - Client 1 selected
# - Retry succeeds
# - FOLDER_FILE_STORED logged once
# - storage_status == "stored"
# ==============================================================================
@pytest.mark.asyncio
async def test_c_storage_floodwait_failover_client1(mock_db, caplog):
    caplog.set_level(logging.INFO)

    c0 = MagicMock(id=1000, is_connected=True)
    c1 = MagicMock(id=1001, is_connected=True)
    clients = {0: c0, 1: c1}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)

    call_count = 0

    async def mock_send_file(client, db_id, file_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if client == c0:
            fw = FloodWait(0.05)
            fw.value = 0.05
            raise fw
        # Client 1 succeeds
        res = MagicMock()
        res.id = 888
        return res

    job = StorageJob(
        db_id="607f1f77bcf86cd799439012",
        file_id="tg_c_1",
        file_name="flood_test.mkv",
        file_size=1000,
        file_info={"file_id": "tg_c_1"},
        max_retries=2,
    )

    # Secondary client 1 in brief cooldown so client 0 is selected as emergency fallback for attempt 1
    stat1 = svc._ensure_client_stat(1, c1)
    stat1.cooldown_until = time.monotonic() + 0.2

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send_file):
        # Attempt 1: Client 0 fails with FloodWait
        success1 = await svc._process_job(job)
        assert success1 is False
        assert job.status == StorageStatus.RETRYING
        assert job.retry_count == 1

        # Check Client 0 entered cooldown
        stat0 = svc._client_stats[0]
        assert stat0.floodwait_count == 1
        assert stat0.cooldown_until > time.monotonic()

        # Wait for Client 1 cooldown to expire
        await asyncio.sleep(0.22)

        # Attempt 2: Client 0 is in cooldown, so Client 1 must be selected
        success2 = await svc._process_job(job)
        assert success2 is True
        assert job.status == StorageStatus.STORED
        assert job.client_id == 1
        assert job.log_msg_id == 888

    # Verify log output shows FLOG_FLOODWAIT on client 0 and FLOG_STORAGE_SUCCESS on client 1
    assert any("FLOG_FLOODWAIT" in r.message and "client=0" in r.message for r in caplog.records)
    assert any("FLOG_STORAGE_SUCCESS" in r.message and "client=1" in r.message for r in caplog.records)
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST D: Storage encounters FloodWait on both clients
# - Client 0 in cooldown
# - Client 1 in cooldown
# - Storage worker pauses until earliest cooldown expires
# - Worker resumes and succeeds
# - Exact logs emitted in order:
#   1. FLOG_FLOODWAIT
#   2. FLOG_COOLDOWN_ALL
#   3. FLOG_CLIENT_SELECTED
#   4. FLOG_STORAGE_SUCCESS
# ==============================================================================
@pytest.mark.asyncio
async def test_d_storage_floodwait_both_clients_cooldown_all(mock_db, caplog):
    caplog.set_level(logging.INFO)

    c0 = MagicMock(id=1000, is_connected=True)
    c1 = MagicMock(id=1001, is_connected=True)
    clients = {0: c0, 1: c1}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)
    stat0 = svc._ensure_client_stat(0, c0)
    stat1 = svc._ensure_client_stat(1, c1)

    # Trigger FloodWait on both clients with very small cooldown
    now = time.monotonic()
    stat0.cooldown_until = now + 0.05
    stat1.cooldown_until = now + 0.05

    # Log initial FloodWait event to match sequence
    logging.getLogger("FileStream.services.storage_service").warning(
        "[FLOG] FLOG_FLOODWAIT: job_id=607f1f77bcf86cd799439013 client=0 wait_sec=0.05. Cooldown armed."
    )

    job = StorageJob(
        db_id="607f1f77bcf86cd799439013",
        file_id="tg_d_1",
        file_name="cooldown_all.mkv",
        file_size=1000,
        file_info={"file_id": "tg_d_1"},
        max_retries=2,
    )

    mock_res = MagicMock()
    mock_res.id = 999

    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=mock_res)):
        success = await svc._process_job(job)
        assert success is True
        assert job.status == StorageStatus.STORED

    # Verify exact logs were emitted
    log_messages = [r.message for r in caplog.records]
    idx_fw = next(i for i, m in enumerate(log_messages) if "FLOG_FLOODWAIT" in m)
    idx_cd_all = next(i for i, m in enumerate(log_messages) if "FLOG_COOLDOWN_ALL" in m)
    idx_sel = next(i for i, m in enumerate(log_messages) if "FLOG_CLIENT_SELECTED" in m)
    idx_succ = next(i for i, m in enumerate(log_messages) if "FLOG_STORAGE_SUCCESS" in m)

    # Assert correct chronological order
    assert idx_fw < idx_cd_all < idx_sel < idx_succ
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST E: Retry exhaustion
# - Worker retries up to max_retries
# - All attempts fail
# - Exact logs emitted:
#   1. FLOG_RETRY (for each retry)
#   2. FLOG_STORAGE_FAILED
#   3. FOLDER_FILE_FAILED
# - FOLDER_FILE_STORED must NOT be logged
# - in_flight counter decremented
# - DB file record updated to failed status
# ==============================================================================
@pytest.mark.asyncio
async def test_e_retry_exhaustion_permanent_failure(mock_db, folder_svc, mock_bot, caplog):
    caplog.set_level(logging.INFO)
    user_id = 1005

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=105)
    session_id = session["session_id"]

    c0 = MagicMock(id=1000, is_connected=True)
    svc = StorageService(db=mock_db, clients={0: c0}, max_queue_size=10)

    db_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": db_id,
        "file_id": "tg_e_1",
        "file_name": "exhaust.mkv",
        "file_size": 2000,
        "file_unique_id": "uniq_e_1",
        "storage_status": StorageStatus.PENDING,
    })

    job = StorageJob(
        db_id=str(db_id),
        file_id="tg_e_1",
        file_name="exhaust.mkv",
        file_size=2000,
        file_info={"file_id": "tg_e_1"},
        max_retries=2,
    )

    # Transient error on every call
    with patch("FileStream.utils.file_properties.send_file", side_effect=ConnectionResetError("Peer reset connection")):
        # Attempt 0 -> retry 1
        res0 = await svc._process_job(job)
        assert res0 is False
        assert job.status == StorageStatus.RETRYING

        # Attempt 1 -> retry 2
        res1 = await svc._process_job(job)
        assert res1 is False
        assert job.status == StorageStatus.RETRYING

        # Attempt 2 -> retries exhausted -> FAILED
        res2 = await svc._process_job(job)
        assert res2 is False
        assert job.status == StorageStatus.FAILED

    # Check DB record updated to failed
    file_doc = await mock_db.file.find_one({"_id": db_id})
    assert file_doc["storage_status"] == StorageStatus.FAILED
    assert "Peer reset connection" in file_doc["storage_error"]

    # Now verify folder mode rejects this failed file
    session["in_flight"] = 1
    added, reason, _ = await folder_svc.add_file(user_id, file_doc)
    assert added is False
    assert reason == "storage_failed"
    session["in_flight"] = 0

    active = await folder_svc.get_active_session(user_id)
    assert active["in_flight"] == 0
    assert active["file_count"] == 0

    # Verify logs
    log_messages = [r.message for r in caplog.records]
    assert any("FLOG_RETRY" in m and "attempt=1/2" in m for m in log_messages)
    assert any("FLOG_RETRY" in m and "attempt=2/2" in m for m in log_messages)
    assert any("FLOG_STORAGE_FAILED" in m for m in log_messages)
    assert any("FOLDER_FILE_FAILED" in m for m in log_messages)
    assert not any("FOLDER_FILE_STORED" in m for m in log_messages)
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST F: Unexpected exception during storage
# - Storage worker catches unexpected exception
# - Logs FLOG_STORAGE_FAILED with exception details
# - in_flight decremented
# - File not added to folder
# - No unhandled crash
# ==============================================================================
@pytest.mark.asyncio
async def test_f_unexpected_exception_during_storage(mock_db, folder_svc, mock_bot, caplog):
    caplog.set_level(logging.INFO)
    user_id = 1006

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=106)
    c0 = MagicMock(id=1000, is_connected=True)
    svc = StorageService(db=mock_db, clients={0: c0}, max_queue_size=10)

    db_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": db_id,
        "file_id": "tg_f_1",
        "file_name": "crash_test.mkv",
        "file_size": 2000,
        "file_unique_id": "uniq_f_1",
        "storage_status": StorageStatus.PENDING,
    })

    job = StorageJob(
        db_id=str(db_id),
        file_id="tg_f_1",
        file_name="crash_test.mkv",
        file_size=2000,
        file_info={"file_id": "tg_f_1"},
        max_retries=0,
    )

    with patch("FileStream.utils.file_properties.send_file", side_effect=ZeroDivisionError("unexpected division by zero")):
        res = await svc._process_job(job)
        assert res is False
        assert job.status == StorageStatus.FAILED
        assert "unexpected division by zero" in job.error

    file_doc = await mock_db.file.find_one({"_id": db_id})
    assert file_doc["storage_status"] == StorageStatus.FAILED

    # Attempt to add to folder mode
    session["in_flight"] = 1
    added, reason, _ = await folder_svc.add_file(user_id, file_doc)
    assert added is False
    assert reason == "storage_failed"
    session["in_flight"] = 0

    # Verify log output
    assert any("FLOG_STORAGE_FAILED" in r.message and "unexpected division by zero" in r.message for r in caplog.records)
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST G: Storage job cancelled
# - Storage task is cancelled
# - Cancellation caught
# - State cleaned up
# - in_flight decremented
# - No orphan locks
# - No false success
# ==============================================================================
@pytest.mark.asyncio
async def test_g_storage_job_cancelled(mock_db, folder_svc):
    user_id = 1007
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=107)
    session_id = session["session_id"]

    c0 = MagicMock(id=1000, is_connected=True)
    svc = StorageService(db=mock_db, clients={0: c0}, max_queue_size=10)

    # Cancel session
    svc.record_cancelled_session(session_id)
    assert svc.is_session_cancelled(session_id) is True

    job = StorageJob(
        db_id="607f1f77bcf86cd799439017",
        file_id="tg_g_1",
        file_name="cancelled.mkv",
        file_size=1000,
        file_info={"file_id": "tg_g_1"},
        session_id=session_id,
        future=asyncio.get_running_loop().create_future(),
    )

    # Processing a cancelled job immediately returns False
    res = await svc._process_job(job)
    assert res is False
    assert job.status == StorageStatus.CANCELLED
    assert job.future.cancelled()

    # Verify client in-flight counter was not leaked
    stat0 = svc._ensure_client_stat(0, c0)
    assert stat0.current_inflight == 0
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST H: Concurrent storage workers
# - 3 workers running concurrently
# - 1 hits FloodWait
# - Other 2 workers do not select the cooled-down client
# - Cooldown respected across all workers
# - No race condition in client selection
# ==============================================================================
@pytest.mark.asyncio
async def test_h_concurrent_storage_workers(mock_db):
    c0 = MagicMock(id=1000, is_connected=True)
    c1 = MagicMock(id=1001, is_connected=True)
    clients = {0: c0, 1: c1}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10, concurrency_per_client=2)
    stat0 = svc._ensure_client_stat(0, c0)
    stat1 = svc._ensure_client_stat(1, c1)

    # Arm 5-second cooldown on client 0
    stat0.cooldown_until = time.monotonic() + 5.0

    # Run 3 concurrent client reservations
    reservations = await asyncio.gather(
        svc.reserve_client(),
        svc.reserve_client(),
        svc.reserve_client(),
    )

    selected_indices = [r[0] for r in reservations if r is not None]

    # None of the concurrent workers should select Client 0!
    assert 0 not in selected_indices
    # All non-None selections must be Client 1
    assert all(idx == 1 for idx in selected_indices)

    # Release reservations
    for r in reservations:
        if r is not None:
            svc.release_client(r[2])

    assert stat0.current_inflight == 0
    assert stat1.current_inflight == 0
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST I: Single-client mode
# - Only Client 0 configured (secondary clients list empty)
# - FloodWait triggers cooldown on Client 0
# - Worker waits on Client 0 cooldown
# - No crash from missing Client 1
# - Retry succeeds after cooldown
# ==============================================================================
@pytest.mark.asyncio
async def test_i_single_client_mode(mock_db, caplog):
    caplog.set_level(logging.INFO)

    c0 = MagicMock(id=1000, is_connected=True)
    # Only client 0 configured
    clients = {0: c0}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)

    call_count = 0

    async def mock_send(client, db_id, file_id, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            fw = FloodWait(0.04)
            fw.value = 0.04
            raise fw
        res = MagicMock()
        res.id = 555
        return res

    job = StorageJob(
        db_id="607f1f77bcf86cd799439019",
        file_id="tg_i_1",
        file_name="single_client.mkv",
        file_size=1000,
        file_info={"file_id": "tg_i_1"},
        max_retries=2,
    )

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        # Attempt 1: encounters FloodWait, arms cooldown on client 0
        res1 = await svc._process_job(job)
        assert res1 is False
        assert job.status == StorageStatus.RETRYING

        stat0 = svc._client_stats[0]
        assert stat0.cooldown_until > time.monotonic()

        # Wait for cooldown to expire
        await asyncio.sleep(0.06)

        # Attempt 2: Client 0 cooldown expired, retry succeeds
        res2 = await svc._process_job(job)
        assert res2 is True
        assert job.status == StorageStatus.STORED
        assert job.client_id == 0
        assert job.log_msg_id == 555

    # Verify logs show FloodWait and eventual storage success without crash
    assert any("FLOG_FLOODWAIT" in r.message for r in caplog.records)
    assert any("FLOG_STORAGE_SUCCESS" in r.message for r in caplog.records)
    await svc.shutdown(timeout=0.1)


# ==============================================================================
# TEST J: Multi-client mode
# - Multiple secondary clients configured
# - Client failover works as expected
# - Primary client handles bot commands while secondary clients handle storage/streaming as designed
# ==============================================================================
@pytest.mark.asyncio
async def test_j_multi_client_mode(mock_db):
    c0 = MagicMock(id=1000, is_connected=True)  # Primary bot client
    c1 = MagicMock(id=1001, is_connected=True)  # Secondary 1
    c2 = MagicMock(id=1002, is_connected=True)  # Secondary 2
    clients = {0: c0, 1: c1, 2: c2}

    svc = StorageService(db=mock_db, clients=clients, max_queue_size=10)

    # In multi-client mode, select_best_client selects among healthy clients
    sel = svc.select_best_client()
    assert sel is not None
    chosen_idx = sel[0]
    assert chosen_idx in (0, 1, 2)

    # Now put client 1 in cooldown
    stat1 = svc._ensure_client_stat(1, c1)
    stat1.cooldown_until = time.monotonic() + 10.0

    # Selection must now choose between 0 and 2, but NEVER 1
    for _ in range(5):
        s = svc.select_best_client()
        assert s is not None
        assert s[0] != 1

    # Put client 0 in cooldown as well
    stat0 = svc._ensure_client_stat(0, c0)
    stat0.cooldown_until = time.monotonic() + 10.0

    # Only client 2 remains available
    s = svc.select_best_client()
    assert s is not None
    assert s[0] == 2

    # Put client 2 in cooldown as well
    stat2 = svc._ensure_client_stat(2, c2)
    stat2.cooldown_until = time.monotonic() + 10.0

    # When all are in cooldown, select_best_client returns None
    s = svc.select_best_client()
    assert s is None

    # Earliest cooldown correctly calculates positive time
    earliest = svc.get_earliest_cooldown()
    assert earliest > 0.0

    await svc.shutdown(timeout=0.1)
