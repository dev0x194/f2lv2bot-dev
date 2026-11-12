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
from FileStream.services.file_service import FileService
from FileStream.bot.plugins import batch
from FileStream.bot.messages import FolderMessages
from FileStream.bot.clients import setup_secondary_worker_handlers
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


class StorageTestBot(MockBot):
    def __init__(self, index=0, username="test_bot", is_bot=True):
        super().__init__()
        self.client_index = index
        self.id = 100000 + index
        self.name = str(index)
        self.username = username
        self.me = MagicMock()
        self.me.id = self.id
        self.me.username = username
        self.me.is_bot = is_bot
        self.is_connected = True
        self.send_file_mock = AsyncMock()
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


def make_mock_log_msg(msg_id: int = 12345):
    msg = MagicMock()
    msg.id = msg_id
    return msg


# ==============================================================================
# TEST A: One file, one primary client -> STORED
# ==============================================================================
@pytest.mark.asyncio
async def test_a_one_file_one_primary_client():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    log_msg = make_mock_log_msg(501)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        result = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_file_a_1",
            file_name="test_a.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.message_id == 501
    assert result.client_id == 0
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == 1
    assert metrics["failed_jobs"] == 0
    await service.shutdown()


# ==============================================================================
# TEST B: Primary client FloodWait + secondary available -> failover to secondary
# ==============================================================================
@pytest.mark.asyncio
async def test_b_primary_floodwait_secondary_available():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # Put bot0 in cooldown to simulate FloodWait on primary
    stat0 = service._ensure_client_stat(0, bot0)
    stat0.cooldown_until = time.monotonic() + 30.0

    log_msg = make_mock_log_msg(601)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        result = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_file_b_1",
            file_name="test_b.mp4",
            file_size=2048,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.client_id == 1
    assert result.message_id == 601
    await service.shutdown()


# ==============================================================================
# TEST C: Primary + secondary 1 FloodWait + secondary 2 available -> secondary 2 stores
# ==============================================================================
@pytest.mark.asyncio
async def test_c_cascading_floodwait_to_secondary_2():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    bot2 = StorageTestBot(index=2, username="worker_2")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2}, max_queue_size=50)

    # Arm cooldowns on client 0 and client 1
    now = time.monotonic()
    service._ensure_client_stat(0, bot0).cooldown_until = now + 40.0
    service._ensure_client_stat(1, bot1).cooldown_until = now + 25.0

    log_msg = make_mock_log_msg(701)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        result = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_file_c_1",
            file_name="test_c.mkv",
            file_size=4096,
            file_info={"mime_type": "video/x-matroska"},
        )

    assert result.success is True
    assert result.client_id == 2
    assert result.message_id == 701
    await service.shutdown()


# ==============================================================================
# TEST D: All clients FloodWait -> queue waits, resumes when cooldown expires
# ==============================================================================
@pytest.mark.asyncio
async def test_d_all_clients_floodwait_waits_and_resumes():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # Put both clients into very brief cooldown (0.15s)
    now = time.monotonic()
    service._ensure_client_stat(0, bot0).cooldown_until = now + 0.15
    service._ensure_client_stat(1, bot1).cooldown_until = now + 0.15

    log_msg = make_mock_log_msg(801)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        result = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_file_d_1",
            file_name="test_d.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.message_id == 801
    await service.shutdown()


# ==============================================================================
# TEST E: 50 files submitted concurrently -> bounded workers, correct stored count
# ==============================================================================
@pytest.mark.asyncio
async def test_e_fifty_files_submitted_concurrently():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    bot2 = StorageTestBot(index=2, username="worker_2")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2}, max_queue_size=100)
    service.ensure_started()

    num_files = 50
    db_ids = [str(ObjectId()) for _ in range(num_files)]

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.01)
        return make_mock_log_msg(int(db_id[:8], 16) % 100000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=db_id,
                file_id=f"file_e_{i}",
                file_name=f"file_e_{i}.mp4",
                file_size=1024 * (i + 1),
                file_info={"mime_type": "video/mp4"},
            )
            for i, db_id in enumerate(db_ids)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_files
    assert all(r.success for r in results)
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == num_files
    assert metrics["failed_jobs"] == 0
    assert metrics["storage_workers"] <= getattr(Telegram, "FLOG_STORAGE_WORKERS", 6)
    await service.shutdown()


# ==============================================================================
# TEST F: 100 concurrent users / simulated file jobs -> bounded queue, no races
# ==============================================================================
@pytest.mark.asyncio
async def test_f_one_hundred_concurrent_jobs():
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(4)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=200)
    service.ensure_started()

    num_jobs = 100
    db_ids = [str(ObjectId()) for _ in range(num_jobs)]

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(1000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=db_id,
                file_id=f"file_f_{i}",
                file_name=f"file_f_{i}.dat",
                file_size=5000,
                file_info={"mime_type": "application/octet-stream"},
            )
            for i, db_id in enumerate(db_ids)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_jobs
    assert all(r.success for r in results)
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == num_jobs
    assert metrics["failed_jobs"] == 0
    await service.shutdown()


# ==============================================================================
# TEST G: Secondary bot receives direct user message -> worker redirect notice
# ==============================================================================
@pytest.mark.asyncio
async def test_g_secondary_bot_direct_user_message():
    sec_bot = StorageTestBot(index=1, username="secondary_worker_bot")
    setup_secondary_worker_handlers(sec_bot)

    assert hasattr(sec_bot, "_secondary_message_handler")
    msg_handler = sec_bot._secondary_message_handler

    user_msg = MockMessage(message_id=1, user_id=99999, text="/start")
    user_msg.from_user = MagicMock()
    user_msg.from_user.id = 99999

    from FileStream.bot import FileStream
    with patch.object(FileStream, "username", "primary_file_bot", create=True):
        await msg_handler(sec_bot, user_msg)

    # Verify secondary bot sent worker notice without generating links or folder sessions
    assert len(user_msg.replies) == 1
    reply = user_msg.replies[0]
    assert "Storage Worker Bot" in reply["text"] or "worker node" in reply["text"].lower()


# ==============================================================================
# TEST H: Admin sends command to secondary bot -> worker node status, no mutations
# ==============================================================================
@pytest.mark.asyncio
async def test_h_admin_command_to_secondary_bot():
    sec_bot = StorageTestBot(index=1, username="secondary_worker_bot")
    setup_secondary_worker_handlers(sec_bot)

    assert hasattr(sec_bot, "_secondary_message_handler")
    msg_handler = sec_bot._secondary_message_handler

    admin_id = int(Telegram.OWNER_ID)
    admin_msg = MockMessage(message_id=2, user_id=admin_id, text="/status")
    admin_msg.from_user = MagicMock()
    admin_msg.from_user.id = admin_id

    await msg_handler(sec_bot, admin_msg)

    assert len(admin_msg.replies) == 1
    reply = admin_msg.replies[0]["text"]
    assert "Storage Worker Node" in reply
    assert "STORAGE_WORKER" in reply


# ==============================================================================
# TEST I: Folder contains stored, pending, failed, cancelled -> only stored persisted
# ==============================================================================
@pytest.mark.asyncio
async def test_i_only_stored_files_persisted_in_folder():
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    user_id = 2001

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=200)

    # 3 files: 2 stored, 1 failed
    f1_id = ObjectId()
    f2_id = ObjectId()
    f3_id = ObjectId()

    # Add 2 stored files to session
    ok1, _, _ = await folder_svc.add_file(user_id, {"_id": f1_id, "file_name": "f1.mp4", "file_size": 1000, "storage_status": "stored"})
    ok2, _, _ = await folder_svc.add_file(user_id, {"_id": f2_id, "file_name": "f2.mp4", "file_size": 2000, "storage_status": "stored"})
    # Failed file must be rejected from active folder
    ok3, _, _ = await folder_svc.add_file(user_id, {"_id": f3_id, "file_name": "f3.mp4", "file_size": 3000, "storage_status": "failed"})

    assert ok1 is True
    assert ok2 is True
    assert ok3 is False

    # Complete folder
    folder, _ = await folder_svc.finalize_folder(user_id)
    assert folder is not None
    assert len(folder["files"]) == 2
    assert str(f1_id) in folder["files"]
    assert str(f2_id) in folder["files"]
    assert str(f3_id) not in folder["files"]


# ==============================================================================
# TEST J: 50-file folder presses Done while jobs pending -> finalization blocked
# ==============================================================================
@pytest.mark.asyncio
async def test_j_done_blocked_while_jobs_pending():
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    bot0 = StorageTestBot(index=0)
    user_id = 3001

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=300)
    # Simulate 47 stored, 2 in flight, 1 queued
    session["file_ids"] = [str(ObjectId()) for _ in range(47)]
    session["in_flight"] = 2
    session["pending_messages"] = {401}

    cb_query = MagicMock()
    cb_query.data = "fld_btn:done"
    cb_query.from_user = MagicMock()
    cb_query.from_user.id = user_id
    cb_query.answer = AsyncMock()

    with patch.object(batch, "db", mock_db), \
         patch.object(batch, "folder_service", folder_svc):
        await batch.complete_folder(bot0, user_id=user_id, callback_query=cb_query)

    # Must alert user that processing is still ongoing and NOT complete session
    cb_query.answer.assert_called_once()
    assert cb_query.answer.call_args[1].get("show_alert") is True
    # Session must remain active
    assert session.get("completed") is not True


# ==============================================================================
# TEST K: Single-client mode -> no secondary required, FloodWait wait and retry
# ==============================================================================
@pytest.mark.asyncio
async def test_k_single_client_mode_wait_and_retry():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=20)

    # Single client mode: client 0 is selected
    selection = service.select_best_client()
    assert selection is not None
    assert selection[0] == 0

    # Arm cooldown on client 0
    now = time.monotonic()
    service._client_stats[0].cooldown_until = now + 0.1

    log_msg = make_mock_log_msg(901)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        result = await service.store_file(
            db_id=str(ObjectId()),
            file_id="tg_single_k",
            file_name="single.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.client_id == 0
    await service.shutdown()


# ==============================================================================
# TEST L: Five storage clients -> cascading failover 0 -> 1 -> 2 -> 3 -> 4
# ==============================================================================
@pytest.mark.asyncio
async def test_l_five_clients_cascading_failover():
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=50)

    now = time.monotonic()
    # Put clients 0, 1, 2, 3 into cooldown
    service._ensure_client_stat(0, bots[0]).cooldown_until = now + 100.0
    service._ensure_client_stat(1, bots[1]).cooldown_until = now + 100.0
    service._ensure_client_stat(2, bots[2]).cooldown_until = now + 100.0
    service._ensure_client_stat(3, bots[3]).cooldown_until = now + 100.0

    # Client 4 must be selected
    selection = service.select_best_client()
    assert selection is not None
    assert selection[0] == 4
    await service.shutdown()


# ==============================================================================
# TEST M: Concurrent workers attempt to reserve the same client -> no race
# ==============================================================================
@pytest.mark.asyncio
async def test_m_concurrent_client_reservation_no_race():
    mock_db = MockDatabase()
    bot1 = StorageTestBot(index=1, username="worker_1")
    # Concurrency limit = 2
    service = StorageService(db=mock_db, clients={1: bot1}, concurrency_per_client=2)

    # 10 concurrent reservation attempts
    results = await asyncio.gather(*[service.reserve_client() for _ in range(10)])
    successful_reservations = [r for r in results if r is not None]

    # Exactly 2 reservations must succeed
    assert len(successful_reservations) == 2
    assert service._client_stats[1].current_inflight == 2
    await service.shutdown()


# ==============================================================================
# TEST N: Duplicate storage protection -> ONE FILE -> ONE ACTIVE JOB -> ONE COMMIT
# ==============================================================================
@pytest.mark.asyncio
async def test_n_duplicate_storage_protection():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    fid = str(ObjectId())
    # Pre-populate DB with flog_msg_id already existing (simulating completed storage)
    await mock_db.file.insert_one({"_id": ObjectId(fid), "flog_msg_id": 99999})

    mock_send = AsyncMock()
    with patch("FileStream.utils.file_properties.send_file", mock_send):
        result = await service.store_file(
            db_id=fid,
            file_id="dup_test",
            file_name="dup.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )

    assert result.success is True
    assert result.message_id == 99999
    # send_file must NOT have been called again!
    mock_send.assert_not_called()
    await service.shutdown()


# ==============================================================================
# TEST O: Cancel a folder with active storage jobs -> cancelled, reservations released
# ==============================================================================
@pytest.mark.asyncio
async def test_o_cancel_folder_with_active_storage_jobs():
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    user_id = 4001
    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=400)
    session_id = session["session_id"]

    # Enqueue a storage job for this session
    job = await service.enqueue_storage(
        db_id=str(ObjectId()),
        file_id="file_o_1",
        file_name="cancel_test.mp4",
        file_size=2048,
        file_info={"mime_type": "video/mp4"},
        session_id=session_id,
        create_future=True,
    )

    # Cancel the session via service
    cancelled = service.cancel_session_jobs(session_id)
    assert cancelled == 1
    assert job.status == StorageStatus.CANCELLED
    assert job.future.cancelled()

    await service.shutdown()


# ==============================================================================
# REALISTIC LOAD TEST: 100 users × 5 files = 500 storage jobs
# Simulated under mixed conditions (success, FloodWait, slow, recovery)
# ==============================================================================
@pytest.mark.asyncio
async def test_realistic_simulated_load_500_jobs():
    mock_db = MockDatabase()
    # 4 storage worker bots (Clients 1 to 4) + Client 0
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=500, concurrency_per_client=4)
    service.ensure_started()

    total_users = 100
    files_per_user = 5
    total_jobs = total_users * files_per_user  # 500 jobs

    call_count = 0
    floodwait_count = 0

    async def dynamic_send_file(client, db_id, file_id, **kwargs):
        nonlocal call_count, floodwait_count
        call_count += 1

        # Simulate intermittent FloodWait on worker 1 every 20 calls
        if client.client_index == 1 and call_count % 20 == 0:
            floodwait_count += 1
            fw = FloodWait(0.05)
            fw.value = 0.05
            raise fw

        # Normal processing latency
        await asyncio.sleep(0.001)
        return make_mock_log_msg(20000 + (call_count % 10000))

    with patch("FileStream.utils.file_properties.send_file", side_effect=dynamic_send_file):
        job_tasks = []
        for u in range(total_users):
            user_id = 10000 + u
            for f in range(files_per_user):
                fid = str(ObjectId())
                job_tasks.append(
                    service.store_file(
                        db_id=fid,
                        file_id=f"tg_{user_id}_{f}",
                        file_name=f"file_{user_id}_{f}.mp4",
                        file_size=1024 * 1024 * (f + 1),
                        file_info={"mime_type": "video/mp4"},
                        user_id=user_id,
                    )
                )

        results = await asyncio.gather(*job_tasks)

    # Verification
    assert len(results) == total_jobs
    successful = [r for r in results if r.success]
    assert len(successful) == total_jobs

    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == total_jobs
    assert metrics["failed_jobs"] == 0
    assert metrics["active_clients"] == 5
    assert metrics["storage_workers"] <= getattr(Telegram, "FLOG_STORAGE_WORKERS", 6)
    assert metrics["storage_queue_size"] == 0
    assert metrics["storage_queue_peak"] <= 500

    await service.shutdown()
