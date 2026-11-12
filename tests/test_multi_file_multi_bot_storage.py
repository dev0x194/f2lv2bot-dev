import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest
from pyrogram.enums import ChatType
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
from FileStream.services.link_service import LinkService
from FileStream.bot.plugins import batch, stream, start
from FileStream.bot.clients import setup_secondary_worker_handlers
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from tests.test_storage_worker_architecture import StorageTestBot, make_mock_log_msg

logger = logging.getLogger(__name__)


# ==============================================================================
# M1 — Multiple files create multiple jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_m1_multiple_files_create_multiple_jobs():
    """
    Send 10 simulated files.
    Assert: 10 unique storage jobs are queued and executed.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    service.ensure_started()

    num_files = 10
    db_ids = [str(ObjectId()) for _ in range(num_files)]
    file_ids = [f"tg_file_m1_{i}" for i in range(num_files)]

    queued_job_ids = set()

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(int(db_id[:8], 16) % 100000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = []
        for i in range(num_files):
            tasks.append(
                service.store_file(
                    db_id=db_ids[i],
                    file_id=file_ids[i],
                    file_name=f"photo_{i}.jpg",
                    file_size=1024 * (i + 1),
                    file_info={"mime_type": "image/jpeg"},
                    user_id=12345,
                )
            )
        results = await asyncio.gather(*tasks)

    # Assert: 10 unique storage jobs succeeded
    assert len(results) == num_files
    assert all(r.success for r in results)
    stored_db_ids = {r.db_id for r in results}
    assert len(stored_db_ids) == 10

    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == 10
    assert metrics["failed_jobs"] == 0
    await service.shutdown()


# ==============================================================================
# M2 — Jobs execute concurrently
# ==============================================================================
@pytest.mark.asyncio
async def test_m2_jobs_execute_concurrently():
    """
    10 files with multiple workers.
    Assert more than one job can be active simultaneously.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    bot2 = StorageTestBot(index=2, username="worker_2")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2}, max_queue_size=50)
    service.ensure_started()

    num_files = 10
    db_ids = [str(ObjectId()) for _ in range(num_files)]

    active_concurrent = 0
    peak_concurrent = 0
    lock = asyncio.Lock()

    async def mock_concurrent_send(client, db_id, file_id, **kwargs):
        nonlocal active_concurrent, peak_concurrent
        async with lock:
            active_concurrent += 1
            if active_concurrent > peak_concurrent:
                peak_concurrent = active_concurrent
        await asyncio.sleep(0.04)
        async with lock:
            active_concurrent -= 1
        return make_mock_log_msg(2000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_concurrent_send):
        tasks = [
            service.store_file(
                db_id=db_ids[i],
                file_id=f"tg_file_m2_{i}",
                file_name=f"photo_{i}.jpg",
                file_size=2048,
                file_info={"mime_type": "image/jpeg"},
            )
            for i in range(num_files)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_files
    assert all(r.success for r in results)
    # Peak active concurrent sends must be greater than 1
    assert peak_concurrent > 1, f"Expected concurrent execution, peak was {peak_concurrent}"
    await service.shutdown()


# ==============================================================================
# M3 — Multiple clients used
# ==============================================================================
@pytest.mark.asyncio
async def test_m3_multiple_clients_used():
    """
    Configure Client 1, Client 2, Client 3, Client 4.
    Submit enough jobs to require concurrent processing.
    Assert multiple clients are selected.
    """
    mock_db = MockDatabase()
    bots = {
        0: StorageTestBot(index=0),
        1: StorageTestBot(index=1, username="worker_1"),
        2: StorageTestBot(index=2, username="worker_2"),
        3: StorageTestBot(index=3, username="worker_3"),
        4: StorageTestBot(index=4, username="worker_4"),
    }
    service = StorageService(db=mock_db, clients=bots, max_queue_size=100)
    service.ensure_started()

    num_files = 16
    db_ids = [str(ObjectId()) for _ in range(num_files)]
    clients_used = []
    lock = asyncio.Lock()

    async def mock_send(client, db_id, file_id, **kwargs):
        c_idx = getattr(client, "client_index", getattr(client, "id", None))
        async with lock:
            clients_used.append(c_idx)
        await asyncio.sleep(0.03)
        return make_mock_log_msg(3000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=db_ids[i],
                file_id=f"tg_file_m3_{i}",
                file_name=f"file_{i}.dat",
                file_size=4096,
                file_info={"mime_type": "application/octet-stream"},
            )
            for i in range(num_files)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_files
    assert all(r.success for r in results)
    unique_clients = set(clients_used)
    # Assert multiple distinct clients were selected (at least 3 of the 4 workers)
    assert len(unique_clients) >= 3, f"Expected multiple clients, got {unique_clients}"
    # Secondary clients must be utilized
    secondary_used = {c for c in unique_clients if c != 0}
    assert len(secondary_used) >= 2, f"Expected secondary workers to be used, got {secondary_used}"
    await service.shutdown()


# ==============================================================================
# M4 — Single secondary still works
# ==============================================================================
@pytest.mark.asyncio
async def test_m4_single_secondary_still_works():
    """
    Only BOT_TOKEN and MULTI_TOKEN1 configured.
    Must work correctly and prefer secondary worker.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    service.ensure_started()

    num_files = 6
    db_ids = [str(ObjectId()) for _ in range(num_files)]
    clients_used = []
    lock = asyncio.Lock()

    async def mock_send(client, db_id, file_id, **kwargs):
        async with lock:
            clients_used.append(getattr(client, "client_index", 0))
        await asyncio.sleep(0.01)
        return make_mock_log_msg(4000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=db_ids[i],
                file_id=f"tg_file_m4_{i}",
                file_name=f"file_{i}.mp4",
                file_size=10000,
                file_info={"mime_type": "video/mp4"},
            )
            for i in range(num_files)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_files
    assert all(r.success for r in results)
    # Single secondary worker Client 1 should handle storage
    assert 1 in clients_used
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == num_files
    await service.shutdown()


# ==============================================================================
# M5 — Five clients
# ==============================================================================
@pytest.mark.asyncio
async def test_m5_five_clients_work_concurrently():
    """
    Test: BOT_TOKEN + MULTI_TOKEN1 + MULTI_TOKEN2 + MULTI_TOKEN3 + MULTI_TOKEN4.
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=100)
    service.ensure_started()

    num_files = 20
    db_ids = [str(ObjectId()) for _ in range(num_files)]
    clients_used = []
    lock = asyncio.Lock()

    async def mock_send(client, db_id, file_id, **kwargs):
        c_idx = getattr(client, "client_index", 0)
        async with lock:
            clients_used.append(c_idx)
        await asyncio.sleep(0.01)
        return make_mock_log_msg(5000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=db_ids[i],
                file_id=f"tg_file_m5_{i}",
                file_name=f"doc_{i}.pdf",
                file_size=5000,
                file_info={"mime_type": "application/pdf"},
            )
            for i in range(num_files)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == num_files
    assert all(r.success for r in results)
    unique_clients = set(clients_used)
    # Secondary clients should be well utilized across the 5 clients
    assert len(unique_clients) >= 4, f"Expected at least 4 unique clients used, got {unique_clients}"
    await service.shutdown()


# ==============================================================================
# M6 — FloodWait failover
# ==============================================================================
@pytest.mark.asyncio
async def test_m6_floodwait_failover():
    """
    One client enters cooldown on FloodWait.
    Other jobs use available clients.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    bot2 = StorageTestBot(index=2, username="worker_2")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2}, max_queue_size=50)
    service.ensure_started()

    # Client 1 will raise FloodWait once on first job
    has_flooded = False

    async def mock_send_with_flood(client, db_id, file_id, **kwargs):
        nonlocal has_flooded
        c_idx = getattr(client, "client_index", 0)
        if c_idx == 1 and not has_flooded:
            has_flooded = True
            raise FloodWait(10)
        await asyncio.sleep(0.01)
        return make_mock_log_msg(6000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send_with_flood):
        tasks = [
            service.store_file(
                db_id=str(ObjectId()),
                file_id=f"tg_file_m6_{i}",
                file_name=f"file_{i}.jpg",
                file_size=1000,
                file_info={"mime_type": "image/jpeg"},
            )
            for i in range(5)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 5
    assert all(r.success for r in results)
    # Client 1 must be in cooldown
    stat1 = service._client_stats[1]
    assert stat1.cooldown_until > time.monotonic()
    assert stat1.floodwait_count >= 1
    # Other jobs succeeded using client 2 / client 0
    await service.shutdown()


# ==============================================================================
# M7 — All clients cooling
# ==============================================================================
@pytest.mark.asyncio
async def test_m7_all_clients_cooling_resumes_after_cooldown():
    """
    All clients placed in brief cooldown.
    Jobs remain queued and resume after cooldown.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    service.ensure_started()

    # Put both into very short cooldown (0.1s)
    now = time.monotonic()
    service._ensure_client_stat(0, bot0).cooldown_until = now + 0.1
    service._ensure_client_stat(1, bot1).cooldown_until = now + 0.1

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(7000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        tasks = [
            service.store_file(
                db_id=str(ObjectId()),
                file_id=f"tg_file_m7_{i}",
                file_name=f"file_{i}.jpg",
                file_size=1000,
                file_info={"mime_type": "image/jpeg"},
            )
            for i in range(4)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == 4
    assert all(r.success for r in results)
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == 4
    await service.shutdown()


# ==============================================================================
# M8 — Folder + multiple files
# ==============================================================================
@pytest.mark.asyncio
async def test_m8_folder_plus_multiple_files():
    """
    20 files inside "/folder".
    Assert:
    20 folder files
    0 standalone /files entries
    """
    mock_db = MockDatabase()
    folder_svc = FolderService(db=mock_db)
    bot0 = StorageTestBot(index=0)
    user_id = 88001

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=88001)
    session_id = session["session_id"]

    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    file_svc = FileService(db=mock_db, multi_clients={0: bot0, 1: bot1}, storage_service=service)

    num_files = 20
    stored_fids = []

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(8000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        for i in range(num_files):
            msg = MockMessage(message_id=1000 + i, user_id=user_id, chat_id=user_id)
            doc = MagicMock()
            doc.file_id = f"fid_m8_{i}"
            doc.file_unique_id = f"uniq_m8_{i}"
            doc.file_name = f"photo_{i}.jpg"
            doc.file_size = 50000
            doc.mime_type = "image/jpeg"
            msg.media = MagicMock(value="document")
            msg.document = doc

            ins_id, is_new = await file_svc.process_new_file(
                msg,
                user_id=user_id,
                session_id=session_id,
                check_duplicate=True,
            )
            stored_fids.append(ins_id)
            file_doc = await mock_db.get_file(ins_id)
            await folder_svc.add_file(user_id=user_id, file_info=file_doc, is_new=is_new)

    # 1. Verify 20 folder files in session
    active_session = await folder_svc.get_active_session(user_id)
    assert active_session is not None
    assert len(active_session["files"]) == 20
    assert active_session["file_count"] == 20

    # 2. Verify all files have folder metadata
    for fid in stored_fids:
        doc = await mock_db.get_file(fid)
        assert doc["is_folder_file"] is True
        assert doc["folder_session_id"] == session_id

    # 3. Finalize folder
    folder_doc, f_id = await folder_svc.finalize_folder(user_id=user_id, title="Test Folder")
    assert folder_doc is not None
    assert len(folder_doc["files"]) == 20

    # 4. Verify 0 standalone files in /files listing
    link_svc = LinkService(db=mock_db)
    buttons, total_files = await link_svc.gen_file_list_button(1, user_id, bot=bot0)
    assert total_files == 0, f"Expected 0 standalone files, found {total_files}"
    await service.shutdown()


# ==============================================================================
# M9 — Standalone + multiple files
# ==============================================================================
@pytest.mark.asyncio
async def test_m9_standalone_plus_multiple_files():
    """
    20 files outside folder mode.
    Assert: 20 standalone files visible in /files.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    user_id = 88002

    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    file_svc = FileService(db=mock_db, multi_clients={0: bot0, 1: bot1}, storage_service=service)

    num_files = 20

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(9000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        for i in range(num_files):
            msg = MockMessage(message_id=2000 + i, user_id=user_id, chat_id=user_id)
            doc = MagicMock()
            doc.file_id = f"fid_m9_{i}"
            doc.file_unique_id = f"uniq_m9_{i}"
            doc.file_name = f"doc_{i}.pdf"
            doc.file_size = 20000
            doc.mime_type = "application/pdf"
            msg.media = MagicMock(value="document")
            msg.document = doc

            ins_id, is_new = await file_svc.process_new_file(msg, user_id=user_id)
            doc_in_db = await mock_db.get_file(ins_id)
            assert doc_in_db["storage_status"] == "stored"
            assert doc_in_db.get("is_folder_file") is not True

    # Check /files listing
    link_svc = LinkService(db=mock_db)
    buttons, total_files = await link_svc.gen_file_list_button(1, user_id, bot=bot0)
    assert total_files == 20
    await service.shutdown()


# ==============================================================================
# M10 — Forwarded multiple files
# ==============================================================================
@pytest.mark.asyncio
async def test_m10_forwarded_multiple_files():
    """
    User forwards 10 files.
    Verify: User ownership and independent jobs.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    user_id = 88003
    original_sender_id = 999999

    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    file_svc = FileService(db=mock_db, multi_clients={0: bot0, 1: bot1}, storage_service=service)

    num_files = 10

    async def mock_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.005)
        return make_mock_log_msg(10000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        for i in range(num_files):
            msg = MockMessage(message_id=3000 + i, user_id=user_id, chat_id=user_id)
            # Simulated forward headers
            msg.forward_from = MagicMock()
            msg.forward_from.id = original_sender_id
            msg.forward_date = 1700000000

            doc = MagicMock()
            doc.file_id = f"fid_m10_{i}"
            doc.file_unique_id = f"uniq_m10_{i}"
            doc.file_name = f"forward_{i}.mp4"
            doc.file_size = 30000
            doc.mime_type = "video/mp4"
            msg.media = MagicMock(value="video")
            msg.video = doc

            ins_id, is_new = await file_svc.process_new_file(msg, user_id=user_id)
            doc_in_db = await mock_db.get_file(ins_id)

            # Assert file belongs to current user, not original sender
            assert doc_in_db["user_id"] == user_id
            assert doc_in_db["uploader_id"] == user_id
            assert doc_in_db["storage_status"] == "stored"

    await service.shutdown()


# ==============================================================================
# M11 — Duplicate update protection
# ==============================================================================
@pytest.mark.asyncio
async def test_m11_duplicate_update_protection():
    """
    Same Telegram update delivered twice.
    Assert no duplicate storage.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    user_id = 88004

    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)
    file_svc = FileService(db=mock_db, multi_clients={0: bot0, 1: bot1}, storage_service=service)

    msg = MockMessage(message_id=4001, user_id=user_id, chat_id=user_id)
    doc = MagicMock()
    doc.file_id = "fid_m11_dup"
    doc.file_unique_id = "uniq_m11_dup"
    doc.file_name = "test.jpg"
    doc.file_size = 15000
    doc.mime_type = "image/jpeg"
    msg.media = MagicMock(value="photo")
    msg.photo = doc

    send_call_count = 0

    async def mock_send(client, db_id, file_id, **kwargs):
        nonlocal send_call_count
        send_call_count += 1
        return make_mock_log_msg(11000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send):
        ins_id1, is_new1 = await file_svc.process_new_file(msg, user_id=user_id, check_duplicate=True)
        ins_id2, is_new2 = await file_svc.process_new_file(msg, user_id=user_id, check_duplicate=True)

    assert ins_id1 == ins_id2
    assert is_new1 is True
    assert is_new2 is False
    # FLOG send must only be called once
    assert send_call_count == 1
    await service.shutdown()


# ==============================================================================
# M12 — Cancellation
# ==============================================================================
@pytest.mark.asyncio
async def test_m12_cancellation_of_queued_and_inflight_jobs():
    """
    100 folder files with some queued and some in-flight.
    Cancel session.
    Assert:
    no leaked standalone files
    queue cleaned
    in-flight cleaned
    reservations released
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(4)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=200)
    service.ensure_started()

    folder_svc = FolderService(db=mock_db, storage_service=service)
    user_id = 88005

    session = await folder_svc.create_session(user_id=user_id, chat_id=user_id, status_msg_id=88005)
    session_id = session["session_id"]

    num_files = 100
    db_ids = [str(ObjectId()) for _ in range(num_files)]

    async def mock_slow_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.08)
        return make_mock_log_msg(12000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_slow_send):
        # Enqueue 100 jobs
        jobs = []
        for i in range(num_files):
            # Pre-create DB doc as folder file
            await mock_db.file.insert_one({
                "_id": ObjectId(db_ids[i]),
                "file_id": f"fid_m12_{i}",
                "folder_session_id": session_id,
                "is_folder_file": True,
                "user_id": user_id,
                "storage_status": "pending",
            })
            job = await service.enqueue_storage(
                db_id=db_ids[i],
                file_id=f"fid_m12_{i}",
                file_name=f"file_{i}.dat",
                file_size=5000,
                file_info={"folder_session_id": session_id, "is_folder_file": True},
                session_id=session_id,
                user_id=user_id,
                create_future=True,
            )
            jobs.append(job)

        # Allow some jobs to start and become in-flight
        await asyncio.sleep(0.02)

        # Cancel the session!
        await folder_svc.cancel_session(user_id=user_id)

        # Wait a moment for workers to process cancellation
        await asyncio.sleep(0.12)

    # Assert:
    # 1. In-flight and reservations released
    metrics = service.get_metrics()
    assert metrics["in_flight_jobs"] == 0, f"Expected 0 in-flight jobs, got {metrics['in_flight_jobs']}"
    for idx, stat in service._client_stats.items():
        assert stat.current_inflight == 0, f"Client {idx} leaked inflight: {stat.current_inflight}"

    # 2. No leaked standalone files in database
    cursor = mock_db.file.find({"user_id": user_id})
    remaining_files = []
    async for f in cursor:
        remaining_files.append(f)
    # Any files tagged with this folder session were purged
    for f in remaining_files:
        assert f.get("folder_session_id") != session_id
        assert f.get("storage_status") != StorageStatus.STORED or f.get("is_folder_file") is not True

    await service.shutdown()


# ==============================================================================
# M13 — Queue saturation
# ==============================================================================
@pytest.mark.asyncio
async def test_m13_queue_saturation_backpressure():
    """
    Exceed queue capacity.
    Assert rejected jobs are correctly accounted for and no phantom success occurs.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    # Small queue capacity of 10
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=10)

    # Do not start workers, so queue fills up completely
    successful_enqueues = 0
    rejected_enqueues = 0

    for i in range(25):
        try:
            await service.enqueue_storage(
                db_id=str(ObjectId()),
                file_id=f"tg_file_m13_{i}",
                file_name=f"file_{i}.jpg",
                file_size=1024,
                file_info={"mime_type": "image/jpeg"},
                create_future=True,
            )
            successful_enqueues += 1
        except RuntimeError as e:
            if "backpressure rejection" in str(e) or "queue full" in str(e):
                rejected_enqueues += 1
            else:
                raise

    assert successful_enqueues == 10
    assert rejected_enqueues == 15
    assert service.storage_queue_size == 10
    await service.shutdown()


# ==============================================================================
# M14 — 500-file stress
# ==============================================================================
@pytest.mark.asyncio
async def test_m14_500_file_stress():
    """
    Simulate 100 users, 5 files each (500 jobs).
    Measure:
    queued, started, stored, failed, cancelled, duplicates, queue_remaining, in_flight_remaining.
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(5)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=600)
    service.ensure_started()

    total_jobs = 500
    db_ids = [str(ObjectId()) for _ in range(total_jobs)]

    async def mock_fast_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.001)
        return make_mock_log_msg(14000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_fast_send):
        tasks = []
        for i in range(total_jobs):
            user_id = 1000 + (i // 5)
            tasks.append(
                service.store_file(
                    db_id=db_ids[i],
                    file_id=f"file_m14_{i}",
                    file_name=f"file_{i}.dat",
                    file_size=1024,
                    file_info={"mime_type": "application/octet-stream"},
                    user_id=user_id,
                )
            )
        results = await asyncio.gather(*tasks)

    # Verification of metrics
    metrics = service.get_metrics()
    assert len(results) == total_jobs
    assert all(r.success for r in results)
    assert metrics["successful_jobs"] == total_jobs
    assert metrics["failed_jobs"] == 0
    assert metrics["queue_size"] == 0
    assert metrics["in_flight_jobs"] == 0
    await service.shutdown()


# ==============================================================================
# M15 — 1000-file stress
# ==============================================================================
@pytest.mark.asyncio
async def test_m15_1000_file_stress():
    """
    Run 1000 independent jobs.
    Verify:
    lost = 0
    duplicate = 0
    in_flight leak = 0
    queue leak = 0
    """
    mock_db = MockDatabase()
    bots = {i: StorageTestBot(index=i, username=f"worker_{i}") for i in range(6)}
    service = StorageService(db=mock_db, clients=bots, max_queue_size=1200)
    service.ensure_started()

    total_jobs = 1000
    db_ids = [str(ObjectId()) for _ in range(total_jobs)]

    async def mock_ultra_fast_send(client, db_id, file_id, **kwargs):
        await asyncio.sleep(0.0005)
        return make_mock_log_msg(15000)

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_ultra_fast_send):
        tasks = [
            service.store_file(
                db_id=db_ids[i],
                file_id=f"file_m15_{i}",
                file_name=f"file_{i}.dat",
                file_size=2048,
                file_info={"mime_type": "application/octet-stream"},
            )
            for i in range(total_jobs)
        ]
        results = await asyncio.gather(*tasks)

    assert len(results) == total_jobs
    assert all(r.success for r in results)

    # Verify no leaks
    metrics = service.get_metrics()
    assert metrics["successful_jobs"] == total_jobs
    assert metrics["failed_jobs"] == 0
    assert metrics["queue_size"] == 0, f"Queue leak detected: {metrics['queue_size']}"
    assert metrics["in_flight_jobs"] == 0, f"In-flight leak detected: {metrics['in_flight_jobs']}"
    for idx, stat in service._client_stats.items():
        assert stat.current_inflight == 0, f"Client {idx} leaked inflight!"

    await service.shutdown()


# ==============================================================================
# M16 — Worker isolation
# ==============================================================================
@pytest.mark.asyncio
async def test_m16_worker_isolation():
    """
    Verify no secondary worker sends automatic messages to configured channels.
    Direct user message receives worker notice without starting primary workflows.
    """
    sec_bot = StorageTestBot(index=1, username="secondary_storage_worker")
    setup_secondary_worker_handlers(sec_bot)

    # 1. Startup sends 0 messages to any channel
    assert len(sec_bot.sent_messages) == 0

    # 2. Channel message ignored
    channel_msg = MockMessage(message_id=50, chat_id=-1001111111111, text="Channel update")
    channel_msg.chat.type = ChatType.CHANNEL
    channel_msg.from_user = None

    handler = sec_bot._secondary_message_handler
    await handler(sec_bot, channel_msg)
    assert len(channel_msg.replies) == 0
    assert len(sec_bot.sent_messages) == 0

    # 3. Direct user private message gets worker notice only
    user_id = 771234
    user_msg = MockMessage(message_id=51, user_id=user_id, chat_id=user_id, text="/start")
    user_msg.chat.type = ChatType.PRIVATE
    user_msg.from_user.is_bot = False

    from FileStream.bot import FileStream
    with patch.object(FileStream, "username", "main_file_bot", create=True):
        await handler(sec_bot, user_msg)

    assert len(user_msg.replies) == 1
    reply_text = user_msg.replies[0]["text"]
    assert "Storage Worker Bot" in reply_text
    assert "@main_file_bot" in reply_text
