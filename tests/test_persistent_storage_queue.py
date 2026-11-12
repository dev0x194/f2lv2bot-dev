import asyncio
import datetime
from datetime import timezone, timedelta
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
    LeaseRenewer,
)
from FileStream.services.folder_service import FolderService, FolderState
from FileStream.services.file_service import FileService
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from tests.test_storage_worker_architecture import StorageTestBot, make_mock_log_msg


# ==============================================================================
# PQ1. Single file storage creates persistent job document
# ==============================================================================
@pytest.mark.asyncio
async def test_pq1_single_file_storage_creates_persistent_job():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_file_pq1",
        file_name="pq1.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
    )

    assert job is not None
    assert job.job_id == f"{db_id}:STORE_TO_FLOG"
    assert job.status == StorageStatus.CLAIMED
    assert job.worker_id == service.worker_id

    doc = await mock_db.get_storage_job(job.job_id)
    assert doc is not None
    assert doc["file_id"] == "tg_file_pq1"
    assert doc["db_id"] == db_id
    assert doc["status"] == "CLAIMED"
    assert doc["worker_id"] == service.worker_id
    assert doc["lease_until"] is not None
    assert doc["lease_until"] > time.time()
    await service.shutdown()


# ==============================================================================
# PQ2. 10 separate files create 10 separate persistent jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq2_ten_separate_files_create_ten_separate_persistent_jobs():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    job_ids = []
    for i in range(10):
        db_id = str(ObjectId())
        job = await service.enqueue_storage(
            db_id=db_id,
            file_id=f"tg_file_pq2_{i}",
            file_name=f"file_{i}.mp4",
            file_size=1000 + i,
            file_info={"mime_type": "video/mp4"},
        )
        job_ids.append(job.job_id)

    assert len(job_ids) == 10
    assert len(set(job_ids)) == 10

    docs = list(mock_db.storage_jobs.docs.values())
    assert len(docs) == 10
    keys = [d["storage_operation_key"] for d in docs]
    assert len(set(keys)) == 10
    await service.shutdown()


# ==============================================================================
# PQ3. 100 files create 100 separate persistent jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq3_hundred_files_create_hundred_separate_persistent_jobs():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=150)

    for i in range(100):
        db_id = str(ObjectId())
        await service.enqueue_storage(
            db_id=db_id,
            file_id=f"tg_file_pq3_{i}",
            file_name=f"file_{i}.mp4",
            file_size=2000 + i,
            file_info={"mime_type": "video/mp4"},
        )

    assert len(mock_db.storage_jobs.docs) == 100
    await service.shutdown()


# ==============================================================================
# PQ4. Duplicate enqueue of same file does not duplicate storage job
# ==============================================================================
@pytest.mark.asyncio
async def test_pq4_duplicate_enqueue_same_file_does_not_duplicate_job():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job1 = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_file_pq4",
        file_name="dup.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
    )
    job2 = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_file_pq4",
        file_name="dup.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
    )

    assert job1.job_id == job2.job_id
    assert len(mock_db.storage_jobs.docs) == 1
    await service.shutdown()


# ==============================================================================
# PQ5. Concurrent enqueue of same file produces exactly one job
# ==============================================================================
@pytest.mark.asyncio
async def test_pq5_concurrent_enqueue_same_file_produces_one_job():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    tasks = [
        service.enqueue_storage(
            db_id=db_id,
            file_id="tg_file_pq5",
            file_name="concurrent.mp4",
            file_size=1024,
            file_info={"mime_type": "video/mp4"},
        )
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)
    assert len(results) == 10
    # All returned jobs refer to the same job_id
    assert all(r.job_id == results[0].job_id for r in results)
    assert len(mock_db.storage_jobs.docs) == 1
    await service.shutdown()


# ==============================================================================
# PQ6. Two workers running concurrently do not claim the same job
# ==============================================================================
@pytest.mark.asyncio
async def test_pq6_two_workers_concurrently_do_not_claim_same_job():
    mock_db = MockDatabase()

    # Pre-populate 10 PENDING jobs in database
    for i in range(10):
        await mock_db.create_storage_job(
            file_id=f"f_{i}",
            db_id=f"db_{i}",
            max_attempts=3,
        )

    # Worker 1 and Worker 2 claim concurrently
    claimed_w1 = []
    claimed_w2 = []

    async def worker_1():
        for _ in range(10):
            doc = await mock_db.claim_storage_job("worker_1", lease_seconds=60)
            if doc:
                claimed_w1.append(doc["job_id"])
            await asyncio.sleep(0.001)

    async def worker_2():
        for _ in range(10):
            doc = await mock_db.claim_storage_job("worker_2", lease_seconds=60)
            if doc:
                claimed_w2.append(doc["job_id"])
            await asyncio.sleep(0.001)

    await asyncio.gather(worker_1(), worker_2())

    total_claimed = len(claimed_w1) + len(claimed_w2)
    assert total_claimed == 10
    # Disjoint sets: no overlap!
    assert set(claimed_w1).isdisjoint(set(claimed_w2))


# ==============================================================================
# PQ7. Claiming assigns worker_id and lease_until
# ==============================================================================
@pytest.mark.asyncio
async def test_pq7_claiming_assigns_worker_id_and_lease_until():
    mock_db = MockDatabase()
    await mock_db.create_storage_job(
        file_id="f_pq7",
        db_id="db_pq7",
        max_attempts=3,
    )

    t0 = time.time()
    claim = await mock_db.claim_storage_job("filestream:host:1234:abcd", lease_seconds=120)
    assert claim is not None
    assert claim["worker_id"] == "filestream:host:1234:abcd"
    assert claim["status"] == "CLAIMED"
    assert claim["attempts"] == 1
    assert claim["lease_until"] >= t0 + 119.0


# ==============================================================================
# PQ8. Lease expires and job becomes claimable again
# ==============================================================================
@pytest.mark.asyncio
async def test_pq8_lease_expires_and_job_becomes_claimable_again():
    mock_db = MockDatabase()
    await mock_db.create_storage_job(
        file_id="f_pq8",
        db_id="db_pq8",
        max_attempts=3,
    )

    # Claim with a tiny lease
    claim1 = await mock_db.claim_storage_job("worker_dead", lease_seconds=0.05)
    assert claim1 is not None

    # Wait for lease to expire
    await asyncio.sleep(0.08)

    # Immediate second claim before recovery fails because lease has expired but status is CLAIMED
    # Run recovery to reclaim expired leases
    recovered = await mock_db.recover_expired_leases()
    assert recovered == 1

    # Now new worker can claim it
    claim2 = await mock_db.claim_storage_job("worker_alive", lease_seconds=60)
    assert claim2 is not None
    assert claim2["worker_id"] == "worker_alive"
    assert claim2["recovery_count"] >= 1


# ==============================================================================
# PQ9. Active upload renews lease; job is not reclaimed while healthy
# ==============================================================================
@pytest.mark.asyncio
async def test_pq9_active_upload_renews_lease():
    mock_db = MockDatabase()
    job_doc = await mock_db.create_storage_job(
        file_id="f_pq9",
        db_id="db_pq9",
        max_attempts=3,
    )
    job_id = job_doc["job_id"]
    await mock_db.claim_storage_job("worker_renewer", lease_seconds=0.3, job_id=job_id)

    renewer = LeaseRenewer(mock_db, job_id, "worker_renewer", lease_seconds=0.3)
    renewer.start()

    # Sleep longer than the original 0.3s lease while renewer keeps it alive
    for _ in range(4):
        await asyncio.sleep(0.12)

    # Recover expired leases should find 0 expired because renewer refreshed it
    recovered = await mock_db.recover_expired_leases()
    assert recovered == 0

    await renewer.stop()


# ==============================================================================
# PQ10. Worker crash leaves job recoverable after lease expiry
# ==============================================================================
@pytest.mark.asyncio
async def test_pq10_worker_crash_leaves_job_recoverable():
    mock_db = MockDatabase()
    job_doc = await mock_db.create_storage_job(
        file_id="f_pq10",
        db_id="db_pq10",
        max_attempts=3,
    )
    job_id = job_doc["job_id"]
    await mock_db.claim_storage_job("crashed_worker", lease_seconds=0.05, job_id=job_id)
    await mock_db.mark_job_running(job_id, "crashed_worker", lease_seconds=0.05)

    # Simulate crash (no lease renewals). Wait for expiry:
    await asyncio.sleep(0.08)

    recovered = await mock_db.recover_expired_leases()
    assert recovered == 1

    doc = await mock_db.get_storage_job(job_id)
    assert doc["status"] == "PENDING"
    assert doc["worker_id"] is None
    assert doc["recovery_count"] == 1


# ==============================================================================
# PQ11. Process restart recovers uncompleted jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq11_process_restart_recovers_uncompleted_jobs():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    # Create an expired job in DB
    job_doc = await mock_db.create_storage_job(file_id="f_pq11", db_id="db_pq11")
    await mock_db.claim_storage_job("old_proc", lease_seconds=0.01, job_id=job_doc["job_id"])
    await asyncio.sleep(0.03)

    # Run startup recovery
    recovery_stats = await service.startup_recovery()
    assert recovery_stats["expired_leases_recovered"] >= 1

    doc = await mock_db.get_storage_job(job_doc["job_id"])
    assert doc["status"] == "PENDING"
    await service.shutdown()


# ==============================================================================
# PQ12. FloodWait marks job RETRY_WAIT and does not busy-loop
# ==============================================================================
@pytest.mark.asyncio
async def test_pq12_floodwait_marks_job_retry_wait_and_does_not_busy_loop():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_fw_file",
        file_name="fw.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
    )

    fw_exc = FloodWait(value=2)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(side_effect=fw_exc)):
        await service._process_job(job)

    assert job.status in (StorageStatus.RETRYING, StorageStatus.RETRY_WAIT)
    assert job.retry_count == 1

    doc = await mock_db.get_storage_job(job.job_id)
    assert doc["status"] in ("RETRY_WAIT", "retry_wait")
    assert doc["next_retry_at"] > time.time()
    assert doc["last_error_type"] == "FloodWait"
    await service.shutdown()


# ==============================================================================
# PQ13. FloodWait preserves other client availability
# ==============================================================================
@pytest.mark.asyncio
async def test_pq13_floodwait_preserves_other_client_availability():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="bot_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # Arm cooldown on bot1
    stat1 = service._ensure_client_stat(1, bot1)
    stat1.cooldown_until = time.monotonic() + 60.0

    # Best client must be bot0 (healthy)
    selection = service.select_best_client()
    assert selection is not None
    idx, client, _ = selection
    assert idx == 0
    await service.shutdown()


# ==============================================================================
# PQ14. Max retry attempts exceeded transitions job to FAILED
# ==============================================================================
@pytest.mark.asyncio
async def test_pq14_max_retry_attempts_exceeded_transitions_to_failed():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_fail_file",
        file_name="fail.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
        max_retries=1,
    )

    err = ConnectionError("Network reset")
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(side_effect=err)):
        # Attempt 1
        await service._process_job(job)
        assert job.status in (StorageStatus.RETRYING, StorageStatus.RETRY_WAIT)
        # Attempt 2 (exceeds max_retries=1)
        await service._process_job(job)
        assert job.status == StorageStatus.FAILED

    doc = await mock_db.get_storage_job(job.job_id)
    assert doc["status"] == "FAILED"
    assert "Network reset" in str(doc["last_error"])
    await service.shutdown()


# ==============================================================================
# PQ15. Permanent error (e.g. ChatWriteForbidden) fails immediately
# ==============================================================================
@pytest.mark.asyncio
async def test_pq15_permanent_error_fails_immediately():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_perm_file",
        file_name="perm.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
        max_retries=5,
    )

    class ChatWriteForbidden(Exception):
        pass

    with patch("FileStream.utils.file_properties.send_file", AsyncMock(side_effect=ChatWriteForbidden("ChatWriteForbidden"))):
        await service._process_job(job)

    assert job.status == StorageStatus.FAILED
    assert job.retry_count == 0  # No retry attempted!

    doc = await mock_db.get_storage_job(job.job_id)
    assert doc["status"] == "FAILED"
    await service.shutdown()


# ==============================================================================
# PQ16. Folder session cancel marks jobs CANCELLED
# ==============================================================================
@pytest.mark.asyncio
async def test_pq16_folder_session_cancel_marks_jobs_cancelled():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    sess_id = "sess_pq16"
    job1 = await service.enqueue_storage(
        db_id=str(ObjectId()),
        file_id="f1",
        file_name="f1.mp4",
        file_size=100,
        file_info={"mime_type": "video/mp4"},
        session_id=sess_id,
    )
    job2 = await service.enqueue_storage(
        db_id=str(ObjectId()),
        file_id="f2",
        file_name="f2.mp4",
        file_size=200,
        file_info={"mime_type": "video/mp4"},
        session_id=sess_id,
    )

    cancelled = service.cancel_session_jobs(sess_id)
    assert cancelled >= 2
    assert job1.status == StorageStatus.CANCELLED
    assert job2.status == StorageStatus.CANCELLED

    await mock_db.cancel_storage_jobs_for_session(sess_id)
    doc1 = await mock_db.get_storage_job(job1.job_id)
    assert doc1["status"] == "CANCELLED"
    await service.shutdown()


# ==============================================================================
# PQ17. Late completion after folder cancel does not commit to folder
# ==============================================================================
@pytest.mark.asyncio
async def test_pq17_late_completion_after_folder_cancel_does_not_commit():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    sess_id = "sess_pq17"
    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="f_late",
        file_name="late.mp4",
        file_size=100,
        file_info={"mime_type": "video/mp4"},
        session_id=sess_id,
        target_channel_id=-10012345,
    )

    log_msg = make_mock_log_msg(999)
    bot0.delete_messages = AsyncMock()

    async def mock_send_file(*args, **kwargs):
        service.record_cancelled_session(sess_id)
        return log_msg

    with patch("FileStream.utils.file_properties.send_file", side_effect=mock_send_file):
        success = await service._process_job(job)

    assert success is False
    assert job.status == StorageStatus.CANCELLED
    bot0.delete_messages.assert_awaited()
    await service.shutdown()


# ==============================================================================
# PQ18. Multi-token client rotation works under persistent queue
# ==============================================================================
@pytest.mark.asyncio
async def test_pq18_multi_token_client_rotation():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="bot_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50, concurrency_per_client=1)

    log_msg = make_mock_log_msg(111)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        res1 = await service.store_file(
            db_id=str(ObjectId()),
            file_id="f_rot1",
            file_name="r1.mp4",
            file_size=100,
            file_info={"mime_type": "video/mp4"},
        )
        assert res1.success is True

    await service.shutdown()


# ==============================================================================
# PQ19. Offload to secondary clients preserved
# ==============================================================================
@pytest.mark.asyncio
async def test_pq19_offload_to_secondary_clients_preserved():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="bot_sec")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # Both are idle; client 1 (secondary) should be selected over client 0 (primary)
    best = service.select_best_client()
    assert best is not None
    assert best[0] == 1
    await service.shutdown()


# ==============================================================================
# PQ20. Offload failover to primary client when secondaries unavailable
# ==============================================================================
@pytest.mark.asyncio
async def test_pq20_offload_failover_to_primary_when_secondaries_unavailable():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="bot_sec")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # Secondary in cooldown
    stat1 = service._ensure_client_stat(1, bot1)
    stat1.cooldown_until = time.monotonic() + 60.0

    # Failover to client 0
    best = service.select_best_client()
    assert best is not None
    assert best[0] == 0
    await service.shutdown()


# ==============================================================================
# PQ21. Standalone file storage works through persistent queue
# ==============================================================================
@pytest.mark.asyncio
async def test_pq21_standalone_file_storage_works():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    log_msg = make_mock_log_msg(777)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        res = await service.store_file(
            db_id=db_id,
            file_id="stand_f1",
            file_name="standalone.mp4",
            file_size=5000,
            file_info={"mime_type": "video/mp4"},
        )

    assert res.success is True
    assert res.message_id == 777
    doc = await mock_db.get_storage_job(f"{db_id}:STORE_TO_FLOG")
    assert doc["status"] == "STORED"
    assert doc["flog_msg_id"] == 777
    await service.shutdown()


# ==============================================================================
# PQ22. Standalone file storage failover works
# ==============================================================================
@pytest.mark.asyncio
async def test_pq22_standalone_file_storage_failover():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1, username="worker_1")
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1}, max_queue_size=50)

    # bot1 is initially preferred, but in cooldown
    stat1 = service._ensure_client_stat(1, bot1)
    stat1.cooldown_until = time.monotonic() + 10.0

    log_msg = make_mock_log_msg(888)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        res = await service.store_file(
            db_id=str(ObjectId()),
            file_id="stand_f2",
            file_name="standalone2.mp4",
            file_size=5000,
            file_info={"mime_type": "video/mp4"},
        )

    assert res.success is True
    assert res.client_id == 0
    await service.shutdown()


# ==============================================================================
# PQ23. Forwarded file storage works through persistent queue
# ==============================================================================
@pytest.mark.asyncio
async def test_pq23_forwarded_file_storage_works():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    log_msg = make_mock_log_msg(991)
    msg = MockMessage(caption="Original Forward Caption")
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        res = await service.store_file(
            db_id=db_id,
            file_id="fwd_f1",
            file_name="fwd.mp4",
            file_size=1234,
            file_info={"mime_type": "video/mp4"},
            message=msg,
        )

    assert res.success is True
    doc = await mock_db.get_storage_job(f"{db_id}:STORE_TO_FLOG")
    assert doc["status"] == "STORED"
    await service.shutdown()


# ==============================================================================
# PQ24. Media group files remain independent persistent jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq24_media_group_files_remain_independent():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    mg_id = "album_12345"
    db_id1 = str(ObjectId())
    db_id2 = str(ObjectId())
    db_id3 = str(ObjectId())

    j1 = await service.enqueue_storage(
        db_id=db_id1,
        file_id="mg_f1",
        file_name="p1.jpg",
        file_size=100,
        file_info={"media_group_id": mg_id},
    )
    j2 = await service.enqueue_storage(
        db_id=db_id2,
        file_id="mg_f2",
        file_name="p2.jpg",
        file_size=200,
        file_info={"media_group_id": mg_id},
    )
    j3 = await service.enqueue_storage(
        db_id=db_id3,
        file_id="mg_f3",
        file_name="p3.jpg",
        file_size=300,
        file_info={"media_group_id": mg_id},
    )

    assert j1.job_id != j2.job_id
    assert j2.job_id != j3.job_id
    assert len(mock_db.storage_jobs.docs) == 3
    await service.shutdown()


# ==============================================================================
# PQ25. Media group ID preserved across persistent jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq25_media_group_id_preserved_across_jobs():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    mg_id = "album_9999"
    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="mg_f",
        file_name="photo.jpg",
        file_size=500,
        file_info={"media_group_id": mg_id},
    )

    doc = await mock_db.get_storage_job(job.job_id)
    assert doc["media_group_id"] == mg_id
    await service.shutdown()


# ==============================================================================
# PQ26. Folder files not visible in /files during processing
# ==============================================================================
@pytest.mark.asyncio
async def test_pq26_folder_files_not_visible_in_files_during_processing():
    mock_db = MockDatabase()

    # Standalone file
    s_fid = await mock_db.add_file({
        "file_name": "standalone.mp4",
        "file_size": 1000,
        "user_id": 1234,
        "is_folder_file": False,
        "storage_status": "stored",
        "time": time.time(),
    })

    # Folder file still in pending status
    f_fid = await mock_db.add_file({
        "file_name": "folder_item.mp4",
        "file_size": 1000,
        "user_id": 1234,
        "is_folder_file": True,
        "storage_status": "pending",
        "folder_session_id": "sess_active",
        "time": time.time() + 1,
    })

    cursor, total = await mock_db.find_files(1234, (1, 10))
    user_files = [d async for d in cursor]
    # File in processing folder must not appear in user's general /files
    assert not any(str(f["_id"]) == str(f_fid) for f in user_files)
    assert any(str(f["_id"]) == str(s_fid) for f in user_files)


# ==============================================================================
# PQ27. Folder files visible after finalization
# ==============================================================================
@pytest.mark.asyncio
async def test_pq27_folder_files_visible_after_finalization():
    mock_db = MockDatabase()
    folder_svc = FolderService(mock_db)

    sess = folder_svc.start_session(1234)
    file_info = {
        "_id": ObjectId(),
        "file_id": "fid_final",
        "file_unique_id": "uniq_final",
        "file_name": "final.mp4",
        "file_size": 2048,
        "storage_status": "stored",
        "flog_msg_id": 101,
        "flog_channel_id": -100123,
    }
    await folder_svc.add_file(1234, file_info)

    folder_doc, url = await folder_svc.finalize_folder(1234, title="My Public Folder")
    assert folder_doc is not None
    assert len(folder_doc["files"]) == 1


# ==============================================================================
# PQ28. Folder finalization waits for durable pending jobs
# ==============================================================================
@pytest.mark.asyncio
async def test_pq28_folder_finalization_waits_for_durable_pending_jobs():
    mock_db = MockDatabase()
    folder_svc = FolderService(mock_db)

    sess = folder_svc.start_session(5555)
    sess_id = sess["session_id"]
    file_info = {
        "_id": ObjectId(),
        "file_id": "fid_pending",
        "file_unique_id": "uniq_pending",
        "file_name": "pending.mp4",
        "file_size": 1024,
        "storage_status": "pending",
    }
    await folder_svc.add_file(5555, file_info)

    # Insert active storage job into DB for this folder session
    await mock_db.create_storage_job(
        file_id="fid_pending",
        db_id=str(file_info["_id"]),
        folder_session_id=sess_id,
        is_folder_file=True,
    )

    # Attempt to finalize should raise ValueError because durable job is active
    with pytest.raises(ValueError, match="Files are still processing in this folder"):
        await folder_svc.finalize_folder(5555)


# ==============================================================================
# PQ29. MongoDB temporary disconnection triggers graceful retry
# ==============================================================================
@pytest.mark.asyncio
async def test_pq29_mongodb_temporary_disconnection_triggers_graceful_retry():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    # Mock DB failure during create_storage_job
    with patch.object(mock_db, "create_storage_job", AsyncMock(side_effect=Exception("MongoDB Connection Timeout"))):
        # Should not crash enqueue_storage
        job = await service.enqueue_storage(
            db_id=str(ObjectId()),
            file_id="f_mongo_err",
            file_name="err.mp4",
            file_size=1000,
            file_info={"mime_type": "video/mp4"},
        )
        assert job is not None
        assert job.status in (StorageStatus.PENDING, StorageStatus.CLAIMED)
    await service.shutdown()


# ==============================================================================
# PQ30. Process shutdown drains local queue cleanly
# ==============================================================================
@pytest.mark.asyncio
async def test_pq30_process_shutdown_drains_local_queue_cleanly():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)
    service.ensure_started()

    log_msg = make_mock_log_msg(333)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        for i in range(5):
            await service.enqueue_storage(
                db_id=str(ObjectId()),
                file_id=f"f_drain_{i}",
                file_name=f"drain_{i}.mp4",
                file_size=1000,
                file_info={"mime_type": "video/mp4"},
            )
        # Shutdown with 3s timeout should drain all 5
        await service.shutdown(timeout=3.0)

    assert service._queue.empty()
    assert service.total_successful_jobs == 5


# ==============================================================================
# PQ31. Process shutdown leaves uncompleted jobs recoverable in MongoDB
# ==============================================================================
@pytest.mark.asyncio
async def test_pq31_process_shutdown_leaves_uncompleted_jobs_recoverable():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="f_uncompleted",
        file_name="uncompleted.mp4",
        file_size=1000,
        file_info={"mime_type": "video/mp4"},
    )
    # Immediately shutdown without worker running
    await service.shutdown(timeout=0.01)

    # Verify job document in DB is intact (not lost)
    doc = await mock_db.get_storage_job(job.job_id)
    assert doc is not None
    assert doc["status"] in ("PENDING", "CLAIMED")


# ==============================================================================
# PQ32. Double-process simulation: two distinct worker processes process without race
# ==============================================================================
@pytest.mark.asyncio
async def test_pq32_double_process_simulation():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    proc_a = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)
    proc_b = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=50)

    assert proc_a.worker_id != proc_b.worker_id

    # Create 10 pending jobs in MongoDB
    for i in range(10):
        await mock_db.create_storage_job(
            file_id=f"f_dp_{i}",
            db_id=f"db_dp_{i}",
            metadata={"file_name": f"f_{i}.mp4", "file_size": 100},
        )

    # Process A and Process B claim from DB
    proc_a_claimed = []
    proc_b_claimed = []

    for _ in range(5):
        d_a = await mock_db.claim_storage_job(proc_a.worker_id, lease_seconds=60)
        if d_a:
            proc_a_claimed.append(d_a["job_id"])
        d_b = await mock_db.claim_storage_job(proc_b.worker_id, lease_seconds=60)
        if d_b:
            proc_b_claimed.append(d_b["job_id"])

    total = len(proc_a_claimed) + len(proc_b_claimed)
    assert total == 10
    assert set(proc_a_claimed).isdisjoint(set(proc_b_claimed))

    await proc_a.shutdown()
    await proc_b.shutdown()


# ==============================================================================
# PQ33. High load: 500 files submitted; all 500 stored to FLOG and marked STORED
# ==============================================================================
@pytest.mark.asyncio
async def test_pq33_high_load_stress_test():
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=600)
    service.ensure_started()

    log_msg = make_mock_log_msg(5555)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        for i in range(500):
            db_id = str(ObjectId())
            await service.enqueue_storage(
                db_id=db_id,
                file_id=f"stress_{i}",
                file_name=f"stress_{i}.mp4",
                file_size=1000 + i,
                file_info={"mime_type": "video/mp4"},
            )

        # Allow workers to drain the 500 jobs
        await service.shutdown(timeout=10.0)

    assert service.total_successful_jobs == 500
    assert len(mock_db.storage_jobs.docs) == 500
    stored_count = sum(1 for d in mock_db.storage_jobs.docs.values() if d["status"] == "STORED")
    assert stored_count == 500
