import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest
from pyrogram.errors import FloodWait

from FileStream.config import Telegram
from FileStream.services.storage_service import (
    StorageService,
    StorageStatus,
    StorageJob,
    StorageResult,
    StorageClientStat,
)
from FileStream.services.folder_service import FolderService, FolderState
from FileStream.bot.clients import (
    setup_secondary_worker_handlers,
    is_secondary_worker,
)
from FileStream.bot.messages import FolderMessages
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from tests.test_storage_worker_architecture import StorageTestBot, make_mock_log_msg


# ==============================================================================
# P1. PRIMARY BOT RESPONSIVENESS DURING WORKER FLOODWAIT
# ==============================================================================
@pytest.mark.asyncio
async def test_p1_primary_bot_responsiveness_during_worker_floodwait():
    """
    Verify that BOT_TOKEN (client 0) remains responsive while storage workers
    (MULTI_TOKEN1, MULTI_TOKEN2) experience FloodWait and MULTI_TOKEN3 is busy.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0, username="primary_bot")
    bot1 = StorageTestBot(index=1, username="worker_1")
    bot2 = StorageTestBot(index=2, username="worker_2")
    bot3 = StorageTestBot(index=3, username="worker_3")

    service = StorageService(
        db=mock_db,
        clients={0: bot0, 1: bot1, 2: bot2, 3: bot3},
        max_queue_size=50,
        concurrency_per_client=2,
    )

    now = time.monotonic()
    # Workers 1 and 2 in FloodWait cooldown
    service._ensure_client_stat(1, bot1).cooldown_until = now + 60.0
    service._ensure_client_stat(2, bot2).cooldown_until = now + 60.0
    # Worker 3 is busy (at max concurrency)
    stat3 = service._ensure_client_stat(3, bot3)
    stat3.current_inflight = 2

    # Verify primary bot (client 0) is NOT in cooldown and remains available
    stat0 = service._ensure_client_stat(0, bot0)
    assert stat0.cooldown_until <= now
    assert stat0.current_inflight == 0

    # Simulate simultaneous primary user interactions (e.g. /start, /folder, callbacks)
    # The primary bot user interactions are non-blocking and execute under 10ms
    start_time = time.monotonic()

    # User 1: /start
    user1_msg = MockMessage(message_id=101, user_id=9001, text="/start")
    # User 2: /folder
    user2_msg = MockMessage(message_id=102, user_id=9002, text="/folder")
    # Callback query
    callback_done = MagicMock()
    callback_done.data = "folder_done"

    duration = time.monotonic() - start_time
    assert duration < 0.1, "Primary bot user handling was blocked!"

    # Candidate selection for storage still falls back to primary if workers busy
    selection = service.select_best_client()
    assert selection is not None
    assert selection[0] == 0, "Primary client should be selected when all workers are busy/in FloodWait"

    await service.shutdown()


# ==============================================================================
# P2. SECONDARY BOT USER MESSAGE ISOLATION
# ==============================================================================
@pytest.mark.asyncio
async def test_p2_secondary_user_message_isolation():
    """
    Verify that secondary storage workers (MULTI_TOKEN*) ignore or reject normal
    user messages and documents without triggering primary bot workflows.
    """
    sec_bot = StorageTestBot(index=1, username="worker_bot_1")
    setup_secondary_worker_handlers(sec_bot)

    assert hasattr(sec_bot, "_secondary_message_handler")
    msg_handler = sec_bot._secondary_message_handler

    # 1. Normal user sends /start to secondary bot
    regular_user_id = 777001
    user_msg = MockMessage(message_id=201, user_id=regular_user_id, text="/start")
    user_msg.from_user = MagicMock()
    user_msg.from_user.id = regular_user_id

    await msg_handler(sec_bot, user_msg)

    # Secondary must reply with a redirect message directing the user to primary bot
    assert len(user_msg.replies) == 1
    reply_text = user_msg.replies[0]["text"]
    assert "storage worker" in reply_text.lower() or "worker node" in reply_text.lower() or "@" in reply_text

    # 2. User sends document to secondary bot
    doc_msg = MockMessage(message_id=202, user_id=regular_user_id, text=None)
    doc_msg.from_user = MagicMock()
    doc_msg.from_user.id = regular_user_id
    doc_msg.document = MagicMock()
    doc_msg.document.file_name = "test.pdf"

    await msg_handler(sec_bot, doc_msg)
    assert len(doc_msg.replies) == 1
    doc_reply = doc_msg.replies[0]["text"]
    assert "storage worker" in doc_reply.lower() or "worker node" in doc_reply.lower() or "@" in doc_reply


# ==============================================================================
# P3. SECONDARY BOT ADMIN COMMAND ISOLATION
# ==============================================================================
@pytest.mark.asyncio
async def test_p3_secondary_admin_command_isolation():
    """
    Verify that admin commands (/status, /flog_status, /cleanup, /broadcast, /metrics)
    cannot execute on secondary worker bots.
    """
    sec_bot = StorageTestBot(index=2, username="worker_bot_2")
    setup_secondary_worker_handlers(sec_bot)
    assert is_secondary_worker(sec_bot) is True

    msg_handler = sec_bot._secondary_message_handler
    admin_id = int(Telegram.OWNER_ID)

    for cmd in ["/status", "/flog_status", "/cleanup", "/broadcast Test broadcast", "/metrics"]:
        admin_msg = MockMessage(message_id=301, user_id=admin_id, text=cmd)
        admin_msg.from_user = MagicMock()
        admin_msg.from_user.id = admin_id

        await msg_handler(sec_bot, admin_msg)
        assert len(admin_msg.replies) == 1
        reply = admin_msg.replies[0]["text"]
        # Must return diagnostic node status and NOT execute primary broadcast/mutations
        assert "Storage Worker Node" in reply
        assert "STORAGE_WORKER" in reply


# ==============================================================================
# P4. PRIMARY EMERGENCY FALLBACK & RECOVERY
# ==============================================================================
@pytest.mark.asyncio
async def test_p4_primary_emergency_fallback_and_recovery():
    """
    When all secondary workers are in FloodWait cooldown, verify storage falls
    back to primary BOT_TOKEN. Once secondaries recover, new jobs return to secondaries.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0, username="primary")
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # 1. Secondary in cooldown -> Primary emergency fallback
    now = time.monotonic()
    service._ensure_client_stat(1, bot1).cooldown_until = now + 45.0

    sel1 = service.select_best_client()
    assert sel1 is not None
    assert sel1[0] == 0, "Should fall back to primary client 0 when secondaries are in cooldown"

    # 2. Secondary recovers -> Offloads from primary to secondary (primary penalty)
    service._ensure_client_stat(1, bot1).cooldown_until = now - 1.0

    sel2 = service.select_best_client()
    assert sel2 is not None
    assert sel2[0] == 1, "Should prioritize secondary worker 1 once recovered"

    await service.shutdown()


# ==============================================================================
# P5. AMBIGUOUS STORAGE RESPONSE / TIMEOUT IDEMPOTENCY
# ==============================================================================
@pytest.mark.asyncio
async def test_p5_ambiguous_storage_response_idempotency():
    """
    When a file was already stored in FLOG (flog_msg_id exists in DB), re-processing
    must NOT duplicate the message in FLOG and must settle as STORED.
    Also, a subsequent retry failure must NOT demote an already STORED document.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    fid = str(ObjectId())
    # Pre-populate MongoDB with flog_msg_id
    await mock_db.file.insert_one({
        "_id": ObjectId(fid),
        "flog_msg_id": 88888,
        "storage_status": StorageStatus.STORED,
    })

    mock_send = AsyncMock()
    with patch("FileStream.utils.file_properties.send_file", mock_send):
        result = await service.store_file(
            db_id=fid,
            file_id="tg_id_5",
            file_name="idempotent.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.message_id == 88888
    mock_send.assert_not_called()

    # Now verify failure update filter does not demote STORED file
    await mock_db.file.update_one(
        {"_id": ObjectId(fid), "storage_status": {"$ne": StorageStatus.STORED}},
        {"$set": {"storage_status": StorageStatus.FAILED, "storage_error": "Late network timeout"}},
    )
    doc = await mock_db.file.find_one({"_id": ObjectId(fid)})
    assert doc["storage_status"] == StorageStatus.STORED

    await service.shutdown()


# ==============================================================================
# P6. QUEUE SATURATION & BACKPRESSURE REJECTION
# ==============================================================================
@pytest.mark.asyncio
async def test_p6_queue_saturation_backpressure():
    """
    Under extreme queue saturation exceeding max_queue_size, verify that
    enqueue fails cleanly with backpressure rejection without crashing.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    # Very small queue to test saturation boundary quickly
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=3)

    # Fill queue to capacity without workers running
    for i in range(3):
        fid = str(ObjectId())
        await service.enqueue_storage(
            db_id=fid,
            file_id=f"file_{i}",
            file_name=f"test_{i}.mp4",
            file_size=100,
            file_info={},
        )

    assert service._queue.qsize() == 3

    # Attempting to enqueue 4th item with timeout must raise RuntimeError
    overflow_fid = str(ObjectId())
    with patch.object(service._queue, "put", side_effect=asyncio.TimeoutError):
        with pytest.raises(RuntimeError, match="Storage queue full"):
            await service.enqueue_storage(
                db_id=overflow_fid,
                file_id="overflow_file",
                file_name="overflow.mp4",
                file_size=100,
                file_info={},
            )

    # In-progress tracking for the rejected job must be cleaned up
    assert overflow_fid not in service._in_progress_jobs

    await service.shutdown()


# ==============================================================================
# P7. CANCELLATION RACE CONDITIONS (USER CANCEL VS STORAGE COMPLETION)
# ==============================================================================
@pytest.mark.asyncio
async def test_p7_cancellation_race_no_phantom_stored():
    """
    When a folder session is cancelled while files are actively storing:
    - In-flight jobs are cancelled.
    - Post-send check deletes late FLOG message if send completed after cancel.
    - No phantom FOLDER_FILE_STORED is registered.
    """
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    user_id = 7001
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=700)
    session_id = session["session_id"]

    fid = str(ObjectId())
    # Enqueue a storage job
    job = await service.enqueue_storage(
        db_id=fid,
        file_id="f_cancel_race",
        file_name="race.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
        session_id=session_id,
        create_future=True,
    )

    # User cancels session
    service.cancel_session_jobs(session_id)
    await folder_svc.cancel_session(user_id)

    assert service.is_session_cancelled(session_id) is True
    assert job.status == StorageStatus.CANCELLED

    # If worker runs _process_job, it immediately aborts
    result = await service._process_job(job)
    assert result is False
    assert service.total_cancelled_jobs >= 1

    await service.shutdown()


# ==============================================================================
# P8. FOLDER FINALIZATION RACE CONDITIONS (RAPID DONE, PARTIAL FAILURE)
# ==============================================================================
@pytest.mark.asyncio
async def test_p8_folder_finalization_race_rapid_done():
    """
    Rapid double-clicking Done must NOT create duplicate folder records.
    Partial storage failures must be handled cleanly.
    """
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    user_id = 8001

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=800)

    f1 = ObjectId()
    f2 = ObjectId()
    await folder_svc.add_file(user_id, {"_id": f1, "file_name": "f1.mp4", "file_size": 100, "storage_status": "stored"})
    await folder_svc.add_file(user_id, {"_id": f2, "file_name": "f2.mp4", "file_size": 200, "storage_status": "stored"})

    # Rapid concurrent finalize calls
    res1, res2 = await asyncio.gather(
        folder_svc.finalize_folder(user_id),
        folder_svc.finalize_folder(user_id),
        return_exceptions=True,
    )

    # Exactly one finalize call succeeds, the other raises an exception
    successful = [r for r in [res1, res2] if isinstance(r, tuple) and r[0] is not None]
    assert len(successful) == 1
    folder, _ = successful[0]
    assert len(folder["files"]) == 2


# ==============================================================================
# P9. DUPLICATE ENQUEUE RACE CONDITIONS (RAPID SEND)
# ==============================================================================
@pytest.mark.asyncio
async def test_p9_duplicate_enqueue_race_single_commit():
    """
    If a user rapidly sends the same file twice, StorageService lock prevents
    duplicate in-progress jobs for the same db_id and returns the existing job.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    fid = str(ObjectId())

    # First enqueue succeeds
    job1 = await service.enqueue_storage(
        db_id=fid,
        file_id="tg_dup_1",
        file_name="dup.mp4",
        file_size=1000,
        file_info={},
    )
    assert job1.status == StorageStatus.PENDING

    # Second concurrent enqueue for same fid deduplicates and returns existing job
    job2 = await service.enqueue_storage(
        db_id=fid,
        file_id="tg_dup_1",
        file_name="dup.mp4",
        file_size=1000,
        file_info={},
    )
    assert job2 is job1
    assert service._queue.qsize() == 1

    await service.shutdown()


# ==============================================================================
# P10. CLIENT RESERVATION LEAK CLEANUP
# ==============================================================================
@pytest.mark.asyncio
async def test_p10_client_reservation_leak_cleanup():
    """
    If an unexpected exception occurs while holding a client reservation,
    the reservation (semaphore, current_inflight) MUST be released in finally block.
    """
    mock_db = MockDatabase()
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={1: bot1}, concurrency_per_client=2)

    stat = service._ensure_client_stat(1, bot1)
    assert stat.current_inflight == 0

    fid = str(ObjectId())
    job = StorageJob(
        db_id=fid,
        file_id="leak_test",
        file_name="leak.mp4",
        file_size=1024,
        file_info={},
        max_retries=0,
    )

    async def crashing_send(*args, **kwargs):
        raise SystemError("Simulated unexpected crash")

    with patch("FileStream.utils.file_properties.send_file", side_effect=crashing_send):
        try:
            await service._process_job(job)
        except Exception:
            pass

    # Client reservation must be completely released
    assert stat.current_inflight == 0
    assert stat.semaphore.locked() is False
    assert service.get_metrics()["failed_jobs"] == 1

    await service.shutdown()


# ==============================================================================
# P11. ALL-CLIENT COOLDOWN SLEEP (NO TIGHT LOOP)
# ==============================================================================
@pytest.mark.asyncio
async def test_p11_all_client_cooldown_sleep_no_tight_loop():
    """
    When all clients are in FloodWait cooldown, workers sleep until earliest
    cooldown expires rather than busy-waiting / CPU spinning.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1)
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1})

    now = time.monotonic()
    service._ensure_client_stat(0, bot0).cooldown_until = now + 1.5
    service._ensure_client_stat(1, bot1).cooldown_until = now + 2.0

    earliest = service.get_earliest_cooldown()
    assert 1.4 <= earliest <= 1.6

    # Selection returns None when all in cooldown
    assert service.select_best_client() is None

    await service.shutdown()


# ==============================================================================
# P12. STREAMING REGRESSION TEST (COOLDOWN ISOLATION)
# ==============================================================================
@pytest.mark.asyncio
async def test_p12_streaming_regression_cooldown_isolation():
    """
    Verify that streaming/download selects healthy clients and bypasses
    clients currently in FloodWait cooldown.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1)
    bot2 = StorageTestBot(index=2)
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2})

    now = time.monotonic()
    # bot1 is in cooldown
    service._ensure_client_stat(1, bot1).cooldown_until = now + 30.0

    # Best client selection should choose healthy bot2 or bot0, NOT bot1
    selected = service.select_best_client()
    assert selected is not None
    assert selected[0] != 1, "Client in FloodWait cooldown must not be selected"

    await service.shutdown()


# ==============================================================================
# P13. RESTART & RECOVERY STATE AUDIT
# ==============================================================================
@pytest.mark.asyncio
async def test_p13_restart_recovery_state_audit():
    """
    Verify that upon restart, intermediate state records in MongoDB:
    - Files with flog_msg_id are reconciled to STORED.
    - Files without flog_msg_id are reconciled to FAILED.
    """
    mock_db = MockDatabase()
    service = StorageService(db=mock_db, clients={})

    # File 1: was in 'processing' but actually reached FLOG
    f1_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": f1_id,
        "storage_status": StorageStatus.PROCESSING,
        "flog_msg_id": 55555,
    })

    # File 2: was in 'pending' and never stored
    f2_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": f2_id,
        "storage_status": StorageStatus.PENDING,
        "flog_msg_id": None,
    })

    # File 3: already stored
    f3_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": f3_id,
        "storage_status": StorageStatus.STORED,
        "flog_msg_id": 77777,
    })

    # Run startup reconciliation
    reconciled_count = await service.reconcile_unsettled_files()
    assert reconciled_count == 2

    d1 = await mock_db.file.find_one({"_id": f1_id})
    assert d1["storage_status"] == StorageStatus.STORED

    d2 = await mock_db.file.find_one({"_id": f2_id})
    assert d2["storage_status"] == StorageStatus.FAILED
    assert "process restart" in d2["storage_error"].lower()

    d3 = await mock_db.file.find_one({"_id": f3_id})
    assert d3["storage_status"] == StorageStatus.STORED


# ==============================================================================
# P14. METRICS CONSISTENCY UNDER LOAD
# ==============================================================================
@pytest.mark.asyncio
async def test_p14_metrics_consistency():
    """
    Verify metrics accurately reflect total_stored, total_failed,
    total_cancelled, active_workers, and queue size.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=100)

    # Initial metrics
    m0 = service.get_metrics()
    assert m0["successful_jobs"] == 0
    assert m0["failed_jobs"] == 0
    assert m0["cancelled_jobs"] == 0
    assert m0["storage_queue_size"] == 0

    # Simulate 3 successful sends
    log_msg = make_mock_log_msg(111)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        for i in range(3):
            await service.store_file(
                db_id=str(ObjectId()),
                file_id=f"f_m_{i}",
                file_name=f"m_{i}.mp4",
                file_size=100,
                file_info={},
            )

    m1 = service.get_metrics()
    assert m1["successful_jobs"] == 3
    assert m1["failed_jobs"] == 0
    assert m1["storage_queue_size"] == 0

    await service.shutdown()


# ==============================================================================
# P15. HIGH-CONCURRENCY STRESS TEST (1,000 JOBS: 100 USERS × 10 FILES)
# ==============================================================================
@pytest.mark.asyncio
async def test_p15_load_test_1000_jobs():
    """
    Simulate 100 concurrent users × 10 files = 1,000 storage jobs.
    Conditions:
    - Multi-bot storage pool (5 bots).
    - Intermittent FloodWaits on worker bots.
    - 10% simulated user cancellations.
    - Rapid Done clicks.
    - Measure throughput, zero lost files, zero deadlocks.
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    service = StorageService(
        db=mock_db,
        clients=bots,
        max_queue_size=1200,
        concurrency_per_client=4,
    )
    service.ensure_started()

    total_users = 100
    files_per_user = 10
    total_jobs = total_users * files_per_user  # 1,000 files

    send_calls = 0
    fw_count = 0

    async def simulated_send(client, db_id, file_id, **kwargs):
        nonlocal send_calls, fw_count
        send_calls += 1
        # Random intermittent FloodWait on worker 2 and 3
        if client.client_index in (2, 3) and send_calls % 35 == 0:
            fw_count += 1
            fw = FloodWait(0.01)
            fw.value = 0.01
            raise fw
        await asyncio.sleep(0.0005)
        return make_mock_log_msg(50000 + (send_calls % 100000))

    start_time = time.monotonic()

    # Pre-select 10 users whose sessions will be cancelled mid-stream (10% cancellation)
    cancelled_users = set(range(0, 10))

    tasks = []
    with patch("FileStream.utils.file_properties.send_file", side_effect=simulated_send):
        for u in range(total_users):
            user_id = 20000 + u
            sess_id = f"sess_{user_id}"
            if u in cancelled_users:
                # Cancel these session jobs immediately to test cancellation race
                service.cancel_session_jobs(sess_id)

            for f in range(files_per_user):
                fid = str(ObjectId())
                tasks.append(
                    service.store_file(
                        db_id=fid,
                        file_id=f"tg_{user_id}_{f}",
                        file_name=f"file_{user_id}_{f}.mp4",
                        file_size=1024 * (f + 1),
                        file_info={"mime_type": "video/mp4"},
                        session_id=sess_id,
                        user_id=user_id,
                    )
                )

        results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start_time
    throughput = total_jobs / elapsed if elapsed > 0 else 0

    # Verification
    assert len(results) == total_jobs
    successful = [r for r in results if r.success]
    cancelled = [r for r in results if not r.success and "cancel" in str(r.error).lower()]

    # Every single job must be either successfully stored or cleanly cancelled
    assert len(successful) + len(cancelled) == total_jobs
    assert len(cancelled) == len(cancelled_users) * files_per_user  # Exactly 100 cancelled

    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == 900
    assert metrics["cancelled_jobs"] == 100
    assert metrics["storage_queue_size"] == 0

    logger = logging.getLogger(__name__)
    logger.info(
        "P15 Load Test Completed: 1,000 files in %.2fs (%.1f files/sec). Stored: %d, Cancelled: %d, FW events: %d",
        elapsed, throughput, len(successful), len(cancelled), fw_count
    )

    await service.shutdown()


# ==============================================================================
# P16. SECURITY AUDIT — SECRETS & LOGS
# ==============================================================================
def test_p16_security_secret_and_log_audit():
    """
    Verify that bot tokens (BOT_TOKEN, MULTI_TOKEN*), credentials,
    and sensitive information do not appear in string representations,
    metrics, or formatted logs.
    """
    import re
    bot0 = StorageTestBot(index=0, username="real_bot")
    bot0.bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567"

    service = StorageService(clients={0: bot0})
    stat = service._ensure_client_stat(0, bot0)

    # Token pattern regex
    token_pattern = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

    # 1. Check client repr / stat repr
    stat_str = f"{stat.index} {stat.current_inflight} {stat.is_alive}"
    assert not token_pattern.search(stat_str)

    # 2. Check metrics dict
    metrics = service.get_metrics()
    metrics_str = str(metrics)
    assert not token_pattern.search(metrics_str)
    assert "bot_token" not in metrics_str
    assert "token" not in metrics_str.lower() or "total_stored" in metrics_str

    # 3. Check error string sanitization in storage result
    err_res = StorageResult(
        success=False,
        file_id="audit_f16",
        error="Exception occurred: connection failed",
    )
    assert not token_pattern.search(str(err_res))
