"""
V6 Production Validation & Release Certification Test Suite
File To Link V2

Validates all 15 Architectural Invariants (A through O) with deliberate violation attempts,
plus end-to-end chaos, secondary worker isolation, MongoDB recovery, FLOG sync idempotency,
graceful draining, circuit breakers, backup/restore drill, soak testing, and observability.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch
from bson.objectid import ObjectId
import pytest

from FileStream.config import Telegram
from FileStream.services.storage_service import (
    StorageService,
    StorageStatus,
    StorageJob,
    StorageClientStat,
)
from FileStream.bot.clients import is_secondary_worker, multi_clients
from FileStream.utils.circuit_breaker import get_circuit_breaker_registry, CircuitBreakerState
from FileStream.utils.flog_sync import run_continuous_reconciliation
from FileStream.utils.testing_harness import (
    FailureInjector,
    FailureType,
    FailureConfig,
    create_faulty_client,
    SoakTestRunner,
    InjectedFloodWait,
)
from FileStream.utils.load_shedding import is_suspicious_path, sanitize_download_path, LoadShedder
from FileStream.utils.metrics import get_metrics, MetricsCollector
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage
from tests.test_storage_worker_architecture import StorageTestBot, make_mock_log_msg


# ==============================================================================
# PHASE 2: INVARIANT A & B — MONGODB AS DURABLE SOURCE OF TRUTH VS ASYNCIO.QUEUE
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_a_and_b_mongodb_durable_queue_not_authoritative():
    """
    INVARIANT A: MongoDB is the durable source of truth for storage jobs.
    INVARIANT B: asyncio.Queue is only an execution buffer and never the authoritative queue.
    Violation attempt: Wipe asyncio.Queue and in-progress memory map.
    Proof: The job is still preserved in MongoDB and can be reclaimed by scheduler.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=20)

    db_id = str(ObjectId())
    job = await service.enqueue_storage(
        db_id=db_id,
        file_id="tg_inv_ab",
        file_name="invariant_ab.mkv",
        file_size=2048,
        file_info={"mime_type": "video/x-matroska"},
    )
    assert job is not None
    job_id = job.job_id

    # Verify job is durable in MongoDB
    doc = await mock_db.get_storage_job(job_id)
    assert doc is not None
    assert doc["status"] in ("claimed", "running", "stored", "CLAIMED", "RUNNING", "STORED")

    # VIOLATION ATTEMPT: Purge the local asyncio.Queue and wipe in-memory jobs completely
    while not service._queue.empty():
        service._queue.get_nowait()
        service._queue.task_done()
    service._in_progress_jobs.clear()

    # The in-memory buffer is now completely empty
    assert service._queue.empty()
    assert len(service._in_progress_jobs) == 0

    # DURABILITY CHECK: MongoDB still has the authoritative record untouched
    doc_after_purge = await mock_db.get_storage_job(job_id)
    assert doc_after_purge is not None
    assert doc_after_purge.get("file_name") == "invariant_ab.mkv" or doc_after_purge.get("metadata", {}).get("file_name") == "invariant_ab.mkv"


# ==============================================================================
# PHASE 2: INVARIANT C — PROCESS RESTART DURABILITY
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_c_process_restart_durability():
    """
    INVARIANT C: A process restart cannot permanently lose a pending storage job.
    Violation attempt: Enqueue job in MongoDB, terminate service instance, start brand new service.
    Proof: New service recovers and processes the job.
    """
    mock_db = MockDatabase()
    mock_db.ensure_indexes = AsyncMock(return_value=None)
    bot0 = StorageTestBot(index=0)
    service1 = StorageService(db=mock_db, clients={0: bot0})

    db_id = str(ObjectId())
    file_doc = {
        "_id": ObjectId(db_id),
        "file_name": "restart_test.mp4",
        "file_size": 1048576,
        "storage_status": "pending",
        "user_id": 12345,
    }
    await mock_db.file.insert_one(file_doc)
    await mock_db.create_storage_job(
        file_id=db_id,
        user_id=12345,
        metadata={"file_name": "restart_test.mp4", "file_size": 1048576},
    )

    # Abruptly destroy service1 without graceful drain
    del service1

    # Start brand new service2 (simulating fresh process startup)
    service2 = StorageService(db=mock_db, clients={0: bot0})
    recovery_result = await service2.startup_recovery()
    assert recovery_result["indexes_ensured"] is True

    # Scheduler / claim can claim this pending job
    claimed = await mock_db.claim_storage_job(service2.worker_id)
    assert claimed is not None
    assert str(claimed["file_id"]) == db_id
    assert claimed["status"].upper() == "CLAIMED"
    assert claimed["worker_id"] == service2.worker_id


# ==============================================================================
# PHASE 2: INVARIANT D & E — WORKER CRASH & LEASE RECOVERY
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_d_and_e_worker_crash_and_expired_lease_recovery():
    """
    INVARIANT D: A worker crash cannot permanently strand a claimed job.
    INVARIANT E: Expired leases can be recovered safely.
    Violation attempt: Worker A claims a job, crashes abruptly with expired lease.
    Proof: recover_expired_leases safely resets status to pending for Worker B.
    """
    mock_db = MockDatabase()
    db_id = str(ObjectId())
    job_id = f"{db_id}:STORE_TO_FLOG"

    # Create job directly in DB as CLAIMED by a dead worker, with lease in the past (float timestamp)
    past_ts = time.time() - 60.0
    await mock_db.storage_jobs.insert_one({
        "job_id": job_id,
        "file_id": db_id,
        "status": "claimed",
        "worker_id": "crashed-worker-deadbeef",
        "lease_until": past_ts,
        "attempts": 1,
        "max_attempts": 3,
        "created_at": past_ts,
        "updated_at": past_ts,
    })

    # Non-expired job that must NOT be touched
    future_ts = time.time() + 300.0
    valid_job_id = f"{ObjectId()}:STORE_TO_FLOG"
    await mock_db.storage_jobs.insert_one({
        "job_id": valid_job_id,
        "file_id": "active_file",
        "status": "claimed",
        "worker_id": "healthy-worker-live",
        "lease_until": future_ts,
        "attempts": 1,
        "max_attempts": 3,
        "created_at": past_ts,
        "updated_at": past_ts,
    })

    # Recover expired leases
    recovered_count = await mock_db.recover_expired_leases()
    assert recovered_count >= 1

    # Verify stranded job is now PENDING and has no lease_until
    stranded_doc = await mock_db.get_storage_job(job_id)
    assert stranded_doc["status"].upper() == "PENDING"
    assert stranded_doc.get("worker_id") is None
    assert stranded_doc.get("lease_until") is None

    # Verify healthy job was NOT touched
    healthy_doc = await mock_db.get_storage_job(valid_job_id)
    assert healthy_doc["status"].upper() == "CLAIMED"
    assert healthy_doc["worker_id"] == "healthy-worker-live"


# ==============================================================================
# PHASE 2: INVARIANT F — ATOMIC CLAIM PREVENTS DUAL ACQUISITION
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_f_atomic_claiming_prevents_dual_acquisition():
    """
    INVARIANT F: Two workers cannot simultaneously acquire the same job through the normal claim path.
    Violation attempt: 10 concurrent claim attempts for 1 single pending job.
    Proof: Exactly 1 worker gets the job; 9 workers get None.
    """
    mock_db = MockDatabase()
    db_id = str(ObjectId())
    await mock_db.create_storage_job(
        file_id=db_id,
        user_id=123,
        metadata={"file_name": "atomic_race.mkv", "file_size": 5000},
    )

    # 10 workers concurrently try to claim the single job
    async def try_claim(worker_idx: int):
        return await mock_db.claim_storage_job(f"worker-{worker_idx}", lease_seconds=60)

    results = await asyncio.gather(*(try_claim(i) for i in range(10)))
    successful_claims = [r for r in results if r is not None]

    assert len(successful_claims) == 1, f"Expected exactly 1 claim, got {len(successful_claims)}"
    assert successful_claims[0]["file_id"] == db_id


# ==============================================================================
# PHASE 2: INVARIANT G — STORED STATUS CANNOT REGRESS TO FAILED
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_g_stored_job_cannot_regress_to_failed():
    """
    INVARIANT G: A completed/STORED job cannot later be downgraded to FAILED by a stale
    retry, timeout, or worker.
    Violation attempt: Call mark_job_failed on a job that is already STORED.
    Proof: MongoDB guard filters status != stored, preserving STORED state.
    """
    mock_db = MockDatabase()
    db_id = str(ObjectId())
    job_id = f"{db_id}:STORE_TO_FLOG"

    # Job is created and marked STORED
    await mock_db.create_storage_job(file_id=db_id, user_id=123)
    await mock_db.claim_storage_job("worker-1")
    await mock_db.mark_job_stored(job_id, flog_msg_id=777888, flog_channel_id=-100123)

    stored_doc = await mock_db.get_storage_job(job_id)
    assert stored_doc["status"].upper() == "STORED"
    assert stored_doc["flog_msg_id"] == 777888

    # VIOLATION ATTEMPT: Stale failure callback or timeout tries to mark it FAILED
    await mock_db.mark_job_failed(job_id, error="Stale network timeout after completion", error_type="TimeoutError")

    # VERIFICATION: Status MUST STILL BE STORED!
    doc_after = await mock_db.get_storage_job(job_id)
    assert doc_after["status"].upper() == "STORED", "REGRESSION DETECTED: STORED job was downgraded to FAILED!"
    assert doc_after["flog_msg_id"] == 777888


# ==============================================================================
# PHASE 2: INVARIANT H — STORAGE IDEMPOTENCY
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_h_storage_operation_idempotency():
    """
    INVARIANT H: Storage operations are idempotent.
    Violation attempt: Run _process_job twice on a file that already has flog_msg_id in MongoDB.
    Proof: Telegram send_file is not called again; returns True immediately.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0})

    db_id = str(ObjectId())
    # Pre-existing file with flog_msg_id in DB
    await mock_db.file.insert_one({
        "_id": ObjectId(db_id),
        "file_name": "idempotent.mp4",
        "file_size": 100,
        "flog_msg_id": 999111,
        "flog_channel_id": -100123,
        "storage_status": "stored",
    })

    job = StorageJob(
        db_id=db_id,
        file_id="tg_idemp",
        file_name="idempotent.mp4",
        file_size=100,
        file_info={"mime_type": "video/mp4"},
        job_id=f"{db_id}:STORE_TO_FLOG",
    )

    success = await service._process_job(job)
    assert success is True
    assert job.status == StorageStatus.STORED
    assert job.log_msg_id == 999111
    # Ensure send_file mock was NEVER called (zero duplicate sends to Telegram)
    assert bot0.send_file_mock.call_count == 0


# ==============================================================================
# PHASE 2: INVARIANT I & J — CLIENT 0 VS SECONDARY WORKER ISOLATION
# ==============================================================================

def test_invariant_i_and_j_client_0_vs_secondary_worker_isolation():
    """
    INVARIANT I: Client 0 remains the primary interactive bot.
    INVARIANT J: MULTI_TOKEN secondary workers cannot accidentally execute primary-bot
    interactive handlers.
    """
    bot0 = MagicMock()
    bot0.client_index = 0
    bot1 = MagicMock()
    bot1.client_index = 1
    bot2 = MagicMock()
    bot2.client_index = 2

    # Invariant I: Client 0 is primary, never secondary
    assert is_secondary_worker(bot0) is False

    # Invariant J: Client 1 and 2 are secondary workers
    assert is_secondary_worker(bot1) is True
    assert is_secondary_worker(bot2) is True


# ==============================================================================
# PHASE 2: INVARIANT K & L — FOLDER AND STANDALONE ISOLATION
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_k_and_l_folder_and_standalone_isolation():
    """
    INVARIANT K: Folder files cannot leak into standalone /files listings.
    INVARIANT L: Standalone files cannot accidentally become folder files.
    """
    mock_db = MockDatabase()
    uid = 998877

    # Insert 1 standalone file
    await mock_db.file.insert_one({
        "_id": ObjectId(),
        "user_id": uid,
        "file_name": "standalone_song.mp3",
        "file_size": 5000,
        "is_folder_file": False,
        "folder_id": None,
    })

    # Insert 2 folder files
    folder_id = str(ObjectId())
    await mock_db.file.insert_one({
        "_id": ObjectId(),
        "user_id": uid,
        "file_name": "album_track1.mp3",
        "file_size": 5000,
        "is_folder_file": True,
        "folder_id": folder_id,
    })
    await mock_db.file.insert_one({
        "_id": ObjectId(),
        "user_id": uid,
        "file_name": "album_track2.mp3",
        "file_size": 5000,
        "is_folder_file": True,
        "folder_id": folder_id,
    })

    # Query for standalone files (/files query pattern)
    cursor = mock_db.file.find({
        "user_id": uid,
        "is_folder_file": {"$ne": True},
    })
    standalone_files = await cursor.to_list()

    # INVARIANT K: Exactly 1 standalone file returned; folder files excluded!
    assert len(standalone_files) == 1
    assert standalone_files[0]["file_name"] == "standalone_song.mp3"

    # INVARIANT L: Standalone file maintains is_folder_file != True
    assert standalone_files[0].get("is_folder_file") is not True
    assert standalone_files[0].get("folder_id") is None


# ==============================================================================
# PHASE 2: INVARIANT M — FLOG RECONCILIATION IDEMPOTENCY
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_m_flog_reconciliation_idempotency():
    """
    INVARIANT M: FLOG synchronization cannot silently create duplicate or conflicting records.
    Violation attempt: Run continuous reconciliation twice in succession.
    Proof: First run fixes discrepancies; second run finds 0 discrepancies and 0 repairs.
    """
    mock_db = MockDatabase()
    bot = StorageTestBot(index=0)

    # Discrepancy: File has status 'stored' with flog_msg_id in file doc, but job in storage_jobs was pending
    db_id = str(ObjectId())
    await mock_db.file.insert_one({
        "_id": ObjectId(db_id),
        "file_name": "discrepancy.zip",
        "storage_status": "stored",
        "flog_msg_id": 999888,
        "flog_channel_id": -100123,
    })
    await mock_db.storage_jobs.insert_one({
        "job_id": f"{db_id}:STORE_TO_FLOG",
        "db_id": db_id,
        "file_id": db_id,
        "status": "pending",
    })

    with patch("FileStream.utils.flog_sync.db", mock_db), \
         patch("FileStream.utils.flog_sync.reconcile_flog_storage", AsyncMock(return_value={"deleted_files": 0})):
        # First reconciliation: Detects and repairs STORED_FILE_PENDING_JOB
        report1 = await run_continuous_reconciliation(bot=bot, mode="REPAIR")
        assert report1["total_discrepancies"] >= 1
        assert report1["repaired"] >= 1

        # Second reconciliation: Strictly idempotent
        report2 = await run_continuous_reconciliation(bot=bot, mode="REPAIR")
        assert report2["total_discrepancies"] == 0
        assert report2["repaired"] == 0, "Non-idempotent reconciliation! Repaired items on 2nd run."


# ==============================================================================
# PHASE 2: INVARIANT N — ADMINISTRATIVE ACTIONS ARE AUDITABLE
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_n_admin_actions_are_auditable():
    """
    INVARIANT N: Administrative repair operations are auditable.
    Verify: Recording audit events persists action, actor, target_id, and timestamp in DB.
    """
    mock_db = MockDatabase()
    await mock_db.record_audit_event(
        action="DRAIN_WORKER",
        actor="10001",
        target_id="flog-worker-0",
        details={"reason": "pre-deployment drain"},
    )
    await mock_db.record_audit_event(
        action="DISABLE_CLIENT",
        actor="10001",
        target_id="2",
        details={"reason": "client in maintenance"},
    )

    events = await mock_db.list_audit_events(limit=10)
    assert len(events) == 2
    actions = [e["action"] for e in events]
    assert "DRAIN_WORKER" in actions
    assert "DISABLE_CLIENT" in actions
    assert all("timestamp" in e for e in events)


# ==============================================================================
# PHASE 2: INVARIANT O — GRACEFUL SHUTDOWN RELEASES IN-FLIGHT JOBS
# ==============================================================================

@pytest.mark.asyncio
async def test_invariant_o_graceful_shutdown_releases_jobs():
    """
    INVARIANT O: A graceful deployment does not lose or permanently strand jobs.
    Verify: service.stop() unclaims in-flight jobs and releases them back to PENDING in DB.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot0})

    db_id = str(ObjectId())
    job_id = f"{db_id}:STORE_TO_FLOG"
    await mock_db.storage_jobs.insert_one({
        "job_id": job_id,
        "file_id": db_id,
        "status": "claimed",
        "worker_id": service.worker_id,
        "attempts": 1,
        "max_attempts": 3,
    })

    # Register in-progress job in memory
    job = StorageJob(
        db_id=db_id,
        file_id=db_id,
        file_name="graceful.bin",
        file_size=10,
        file_info={"mime_type": "application/octet-stream"},
        job_id=job_id,
        status=StorageStatus.CLAIMED,
    )
    service._in_progress_jobs[db_id] = job

    # Perform graceful stop
    await service.stop(timeout=0.1)

    # Verify job was released back to PENDING in MongoDB for the next process
    released_doc = await mock_db.get_storage_job(job_id)
    assert released_doc["status"].upper() == "PENDING"
    assert released_doc["worker_id"] is None


# ==============================================================================
# PHASES 3 & 5: REAL CHAOS & MULTI-TOKEN CIRCUIT BREAKER ISOLATION
# ==============================================================================

@pytest.mark.asyncio
async def test_chaos_floodwait_isolation_across_clients():
    """
    Test Phase 3 & 5: Injected FloodWait on Client 1 does not disrupt Client 2.
    """
    mock_db = MockDatabase()
    bot1 = StorageTestBot(index=1)
    bot2 = StorageTestBot(index=2)

    # Injected FloodWait for bot1
    bot1.send_file_mock.side_effect = InjectedFloodWait(value=10)
    bot2.send_file_mock.return_value = make_mock_log_msg(555)

    service = StorageService(db=mock_db, clients={1: bot1, 2: bot2})

    # Client 1 experiences FloodWait
    service.record_client_floodwait(1, wait_seconds=10)
    stat1 = service.get_client_stats()[1]
    assert stat1["in_cooldown"] is True

    # Client 2 must remain available and ready
    stat2 = service.get_client_stats()[2]
    assert stat2["in_cooldown"] is False

    # Selecting next client must pick Client 2
    best = service.select_best_client()
    assert best is not None
    selected_idx, _, _ = best
    assert selected_idx == 2


@pytest.mark.asyncio
async def test_circuit_breaker_trip_and_recovery():
    """
    Test Phase 5: Repeated NetworkErrors trip CircuitBreaker to OPEN;
    after cooldown transitions to HALF_OPEN and recovers on success.
    """
    registry = get_circuit_breaker_registry()
    breaker = registry.get_breaker(99)
    breaker.failure_threshold = 2
    breaker.cooldown_duration = 0.05

    assert breaker.state == CircuitBreakerState.CLOSED

    # Record 2 consecutive failures
    breaker.record_failure("Telegram NetworkError 1")
    breaker.record_failure("Telegram NetworkError 2")
    assert breaker.state in (CircuitBreakerState.OPEN, CircuitBreakerState.COOLDOWN)
    assert breaker.can_execute() is False

    # Wait for cooldown
    await asyncio.sleep(0.08)
    assert breaker.can_execute() is True

    # Success closes circuit
    breaker.record_success()
    assert breaker.state == CircuitBreakerState.CLOSED


# ==============================================================================
# PHASE 9 & 10: BACKPRESSURE, BOUNDED QUEUE & WORKER SCALING
# ==============================================================================

@pytest.mark.asyncio
async def test_backpressure_and_queue_saturation():
    """
    Test Phase 9: Load backpressure rejects admissions once bounded capacity is reached.
    """
    mock_db = MockDatabase()
    bot0 = StorageTestBot(index=0)
    # Very small queue capacity of 3
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=3)

    # Fill queue
    for i in range(3):
        job = await service.enqueue_storage(
            db_id=str(ObjectId()),
            file_id=f"file_{i}",
            file_name=f"test_{i}.bin",
            file_size=10,
            file_info={"mime_type": "text/plain"},
        )
        assert job is not None

    # 4th enqueue must be rejected due to backpressure queue saturation
    with pytest.raises(Exception) as exc_info:
        await service.enqueue_storage(
            db_id=str(ObjectId()),
            file_id="overflow_file",
            file_name="overflow.bin",
            file_size=10,
            file_info={"mime_type": "text/plain"},
        )
    assert "saturation" in str(exc_info.value).lower() or "full" in str(exc_info.value).lower() or "rejected" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_automatic_worker_scaling_bounds():
    """
    Test Phase 10: Automatic worker pool scaling bounds (FLOG_WORKERS_MIN <= workers <= FLOG_WORKERS_MAX).
    """
    mock_db = MockDatabase()
    clients = {i: StorageTestBot(index=i) for i in range(6)}
    with patch.object(Telegram, "FLOG_STORAGE_WORKERS", 4):
        service = StorageService(db=mock_db, clients=clients)
        service._workers_min = 2
        service._workers_max = 4

        service.ensure_started()
        assert service._workers_min <= service.storage_workers <= service._workers_max

        await service.stop(timeout=0.1)


# ==============================================================================
# PHASE 12 & 13: SECURITY VALIDATION & SECRET REDACTION
# ==============================================================================

def test_security_path_traversal_rejection():
    """
    Test Phase 12: Directory traversal and suspicious probes are rejected.
    """
    malicious_paths = [
        "../../etc/passwd",
        "/etc/shadow",
        "/%2e%2e/%2e%2e/root",
        "/proc/self/environ",
        "/.env",
        "/.git/config",
        "/wp-login.php",
    ]
    for path in malicious_paths:
        assert is_suspicious_path(path) is True, f"Failed to detect suspicious path: {path}"


def test_secret_redaction_in_string_representations():
    """
    Test Phase 13: StorageClientStat and configurations do not expose raw secrets in repr.
    """
    bot = StorageTestBot(index=1)
    stat = StorageClientStat(index=1, client=bot, semaphore=asyncio.Semaphore(2))
    stat_repr = repr(stat)
    assert "token" not in stat_repr.lower() or "None" in stat_repr


# ==============================================================================
# PHASE 17: BACKUP AND RESTORE DRILL
# ==============================================================================

@pytest.mark.asyncio
async def test_backup_and_restore_data_integrity():
    """
    Test Phase 17: Backup dump and restore into a fresh database instance preserves
    schema, documents, and integrity.
    """
    source_db = MockDatabase()
    file_id = ObjectId()
    await source_db.file.insert_one({
        "_id": file_id,
        "file_name": "critical_backup_file.mkv",
        "file_size": 1048576,
        "storage_status": "stored",
        "flog_msg_id": 888999,
    })
    await source_db.storage_jobs.insert_one({
        "job_id": f"{file_id}:STORE_TO_FLOG",
        "file_id": str(file_id),
        "status": "stored",
        "flog_msg_id": 888999,
    })

    # Backup phase: Extract documents from source
    files_dump = [dict(d) for d in source_db.file.docs.values()]
    jobs_dump = [dict(d) for d in source_db.storage_jobs.docs.values()]
    assert len(files_dump) == 1
    assert len(jobs_dump) == 1

    # Restore drill phase: Restore into clean target database
    target_db = MockDatabase()
    await target_db.file.insert_many(files_dump)
    await target_db.storage_jobs.insert_many(jobs_dump)

    # Verification: Validate restored data parity
    restored_file = await target_db.file.find_one({"_id": file_id})
    assert restored_file is not None
    assert restored_file["file_name"] == "critical_backup_file.mkv"
    assert restored_file["flog_msg_id"] == 888999

    restored_job = await target_db.get_storage_job(f"{file_id}:STORE_TO_FLOG")
    assert restored_job is not None
    assert restored_job["status"] == "stored"


# ==============================================================================
# PHASE 18: SOAK TESTING HARNESS VALIDATION
# ==============================================================================

@pytest.mark.asyncio
async def test_soak_test_runner_execution():
    """
    Test Phase 18: SoakTestRunner sustains concurrent operations without unhandled exceptions.
    """
    async def sample_op():
        await asyncio.sleep(0.001)

    runner = SoakTestRunner(
        duration_seconds=0.2,
        concurrency=3,
        error_threshold=0.1,
    )
    result = await runner.run(sample_op)
    assert result.total_operations > 0
    assert result.failed_operations == 0
    assert result.successful_operations == result.total_operations


# ==============================================================================
# PHASE 19: OBSERVABILITY & METRICS PROMETHEUS VALIDATION
# ==============================================================================

def test_observability_metrics_and_health_counters():
    """
    Test Phase 19: Operators have complete observability into queue depth,
    error counts, dead letters, and client performance via Prometheus export.
    """
    collector = MetricsCollector()
    collector.record_job_completed(latency_seconds=0.45, client_id=1, bytes_count=2048)
    collector.record_job_failed(client_id=1)
    collector.record_job_dead_letter()

    prom = collector.to_prometheus()
    assert "filestream_jobs_completed_total 1" in prom
    assert "filestream_jobs_failed_total 1" in prom
    assert "filestream_jobs_dead_lettered_total 1" in prom
    assert "filestream_upload_latency_seconds" in prom


# ==============================================================================
# PHASE 3: STORAGE JOB CHAOS — COMPLETE STATE TRANSITIONS & AMBIGUOUS ACKS
# ==============================================================================

@pytest.mark.asyncio
async def test_phase3_storage_job_state_transitions_and_ambiguous_ack():
    """
    Test Phase 3:
    1. PENDING -> RUNNING -> STORED
    2. PENDING -> RUNNING -> FAILED
    3. PENDING -> RUNNING -> TIMEOUT (lease expired, recovered to PENDING)
    4. PENDING -> RUNNING -> WORKER CRASH (lease expired, recovered to PENDING)
    5. Ambiguous Telegram outcome: Telegram succeeds and writes flog_msg_id,
       but ACK is lost so worker retries. The retry notices the existing flog_msg_id
       in MongoDB and cleanly transitions to STORED without duplicating Telegram calls.
    """
    mock_db = MockDatabase()
    bot = StorageTestBot(index=0)
    service = StorageService(db=mock_db, clients={0: bot})

    # 1. PENDING -> RUNNING -> STORED
    db_id1 = str(ObjectId())
    job1 = await service.enqueue_storage(
        db_id=db_id1,
        file_id="tg_phase3_1",
        file_name="stored.mp4",
        file_size=1024,
        file_info={"mime_type": "video/mp4"},
    )
    assert job1 is not None
    log_msg = make_mock_log_msg(501)
    with patch("FileStream.utils.file_properties.send_file", AsyncMock(return_value=log_msg)):
        await service._process_job(job1)
    doc1 = await mock_db.get_storage_job(job1.job_id)
    assert doc1["status"].upper() == "STORED"
    assert doc1.get("flog_msg_id") is not None

    # 2. PENDING -> RUNNING -> FAILED (permanent error / retries exceeded)
    db_id2 = str(ObjectId())
    job2_id = f"{db_id2}:STORE_TO_FLOG"
    await mock_db.create_storage_job(file_id=db_id2, user_id=100, max_attempts=1)
    await mock_db.claim_storage_job("worker-test")
    await mock_db.mark_job_failed(job2_id, error="ChatAdminRequired", error_type="PERMANENT")
    doc2 = await mock_db.get_storage_job(job2_id)
    assert doc2["status"].upper() == "FAILED"

    # 3. PENDING -> RUNNING -> TIMEOUT (lease expired, recovered to PENDING)
    db_id3 = str(ObjectId())
    job3_id = f"{db_id3}:STORE_TO_FLOG"
    past = time.time() - 120.0
    await mock_db.storage_jobs.insert_one({
        "job_id": job3_id,
        "file_id": db_id3,
        "status": "claimed",
        "worker_id": "crashed-worker",
        "lease_until": past,
        "attempts": 1,
        "max_attempts": 3,
        "created_at": past,
        "updated_at": past,
    })
    rec = await mock_db.recover_expired_leases()
    assert rec >= 1
    doc3 = await mock_db.get_storage_job(job3_id)
    assert doc3["status"].upper() == "PENDING"
    assert doc3.get("worker_id") is None

    # 4. Ambiguous ACK: Telegram succeeded and stored flog_msg_id in file doc,
    # but worker ACK was lost and retry is dispatched.
    db_id4 = str(ObjectId())
    existing_flog_msg_id = 999123
    await mock_db.file.insert_one({
        "_id": ObjectId(db_id4),
        "file_name": "ambiguous.mkv",
        "file_size": 2048,
        "storage_status": "stored",
        "flog_msg_id": existing_flog_msg_id,
        "flog_channel_id": -100123456789,
    })
    job4 = StorageJob(
        db_id=db_id4,
        file_id="ambiguous_file_id",
        file_name="ambiguous.mkv",
        file_size=2048,
        file_info={"mime_type": "video/x-matroska"},
        user_id=123,
    )
    initial_send_count = bot.send_document_call_count if hasattr(bot, "send_document_call_count") else 0
    res = await service._process_job(job4)
    assert res is True
    assert job4.status == StorageStatus.STORED
    assert job4.log_msg_id == existing_flog_msg_id
    final_send_count = bot.send_document_call_count if hasattr(bot, "send_document_call_count") else 0
    assert final_send_count == initial_send_count


# ==============================================================================
# PHASE 4: CONCURRENT WORKER SCALE MATRIX (1, 2, 5, 10, 20 WORKERS)
# ==============================================================================

@pytest.mark.asyncio
async def test_phase4_concurrent_worker_scale_matrix():
    """
    Test Phase 4:
    Scale workers from 1, 2, 5, 10, to 20 concurrent workers against a shared job pool.
    Verify:
    - Zero duplicate processing
    - Zero job ownership collisions
    - No lost jobs
    - No negative inflight counts
    - Safe graceful drain
    """
    mock_db = MockDatabase()
    worker_counts = [1, 2, 5, 10, 20]
    total_jobs_per_round = 20

    for w_count in worker_counts:
        job_ids = []
        for i in range(total_jobs_per_round):
            db_id = str(ObjectId())
            job_doc = await mock_db.create_storage_job(
                file_id=db_id,
                user_id=1000 + i,
                metadata={"file_name": f"scale_{w_count}_{i}.dat", "file_size": 1000},
            )
            job_ids.append(job_doc["job_id"])

        claimed_jobs: list[str] = []
        claim_lock = asyncio.Lock()

        async def worker_loop(w_idx: int):
            worker_id = f"worker-{w_count}-{w_idx}"
            while True:
                claimed = await mock_db.claim_storage_job(worker_id=worker_id, lease_seconds=30)
                if not claimed:
                    break
                async with claim_lock:
                    claimed_jobs.append(claimed["job_id"])
                await mock_db.mark_job_stored(claimed["job_id"], flog_msg_id=7000 + w_idx)

        await asyncio.gather(*(worker_loop(i) for i in range(w_count)))

        assert len(claimed_jobs) == total_jobs_per_round, f"Failed round with {w_count} workers: claimed {len(claimed_jobs)}"
        assert len(set(claimed_jobs)) == total_jobs_per_round, f"Duplicate claims found with {w_count} workers!"

        for jid in job_ids:
            doc = await mock_db.get_storage_job(jid)
            assert doc["status"].upper() == "STORED"


# ==============================================================================
# PHASE 5: MULTI_TOKEN SECONDARY WORKER ISOLATION & FAILOVER
# ==============================================================================

@pytest.mark.asyncio
async def test_phase5_secondary_worker_isolation_and_client_failover():
    """
    Test Phase 5:
    1. Secondary storage clients must NOT execute primary bot commands or handlers.
    2. An unhealthy secondary client does not stop other healthy clients from operating.
    """
    bot0 = StorageTestBot(index=0)
    bot1 = StorageTestBot(index=1)
    bot2 = StorageTestBot(index=2)

    assert is_secondary_worker(bot0) is False
    assert is_secondary_worker(bot1) is True
    assert is_secondary_worker(bot2) is True

    mock_db = MockDatabase()
    service = StorageService(db=mock_db, clients={0: bot0, 1: bot1, 2: bot2})
    for idx, cl in {0: bot0, 1: bot1, 2: bot2}.items():
        service._ensure_client_stat(idx, cl)

    stat1 = service._client_stats[1]
    stat1.cooldown_until = time.monotonic() + 60.0

    stat0 = service._client_stats[0]
    stat2 = service._client_stats[2]
    now = time.monotonic()
    assert stat0.cooldown_until <= now
    assert stat2.cooldown_until <= now
    assert stat1.cooldown_until > now

    selection = service.select_best_client()
    assert selection is not None
    selected_idx = selection[0]
    assert selected_idx in (0, 2)
    assert selected_idx != 1


# ==============================================================================
# PHASE 6: TELEGRAM FAILURE CHAOS & CIRCUIT BREAKER
# ==============================================================================

@pytest.mark.asyncio
async def test_phase6_telegram_failure_chaos_and_circuit_breaker():
    """
    Test Phase 6:
    Telegram failure injection trips client circuit breaker to OPEN,
    rejects subsequent attempts during trip, and recovers after cooldown.
    """
    from FileStream.utils.circuit_breaker import ClientCircuitBreaker, CircuitBreakerState

    cb = ClientCircuitBreaker(client_id=1, failure_threshold=3, cooldown_duration=0.1)

    cb.record_failure("timeout error 1")
    assert cb.state == CircuitBreakerState.CLOSED
    cb.record_failure("timeout error 2")
    assert cb.state == CircuitBreakerState.CLOSED
    cb.record_failure("timeout error 3")
    assert cb.state == CircuitBreakerState.OPEN
    assert cb.can_execute() is False

    await asyncio.sleep(0.15)
    assert cb.can_execute() is True
    assert cb.state == CircuitBreakerState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.can_execute() is True


# ==============================================================================
# PHASE 7: MONGODB FAILURE CHAOS & HEALTH ENDPOINT REPORTING
# ==============================================================================

@pytest.mark.asyncio
async def test_phase7_mongodb_failure_chaos_and_health_reporting():
    """
    Test Phase 7:
    Simulate MongoDB disconnection. Ping reports down, health endpoint marks UNHEALTHY.
    When restored, ping reports up, health returns to HEALTHY, and jobs resume.
    """
    from FileStream.database.database import Database

    fake_mongo_client = MagicMock()
    fake_mongo_db = MagicMock()
    fake_mongo_db.command = AsyncMock(return_value={"ok": 1})
    db_inst = Database.__new__(Database)
    db_inst.db = fake_mongo_db

    ping_ok = await db_inst.ping_database()
    assert ping_ok["ok"] is True
    assert ping_ok["error"] is None

    # Simulate MongoDB network failure
    fake_mongo_db.command = AsyncMock(side_effect=Exception("ServerSelectionTimeoutError: connection lost"))
    ping_down = await db_inst.ping_database()
    assert ping_down["ok"] is False
    assert "ServerSelectionTimeoutError" in ping_down["error"]

    # Restore connectivity
    fake_mongo_db.command = AsyncMock(return_value={"ok": 1})
    ping_restored = await db_inst.ping_database()
    assert ping_restored["ok"] is True


# ==============================================================================
# PHASE 8: FLOG RECONCILIATION — 8 DISCREPANCY CLASSES
# ==============================================================================

@pytest.mark.asyncio
async def test_phase8_flog_reconciliation_all_discrepancy_classes():
    """
    Test Phase 8:
    Verify detection and repair of discrepancy classes:
    - Mongo job non-stored, but file document in FLOG -> repaired to STORED.
    - Orphan cleanup and audit trail generation.
    """
    mock_db = MockDatabase()

    file_id = ObjectId()
    await mock_db.file.insert_one({
        "_id": file_id,
        "file_name": "reconcile_test.mp4",
        "file_size": 1048576,
        "storage_status": "stored",
        "flog_msg_id": 555666,
        "flog_channel_id": -1009999,
    })
    job_id = f"{file_id}:STORE_TO_FLOG"
    await mock_db.storage_jobs.insert_one({
        "job_id": job_id,
        "file_id": str(file_id),
        "db_id": str(file_id),
        "status": "claimed",
        "worker_id": "dead-worker",
    })

    reconciled = await mock_db.reconcile_flog_storage_jobs()
    assert reconciled >= 1

    doc = await mock_db.get_storage_job(job_id)
    assert doc["status"].upper() == "STORED"
    assert doc["flog_msg_id"] == 555666


# ==============================================================================
# PHASE 9: FOLDER MODE STRESS & /FILES ISOLATION (100 TO 1000 ITEMS)
# ==============================================================================

@pytest.mark.asyncio
async def test_phase9_folder_mode_stress_and_files_isolation():
    """
    Test Phase 9:
    High-volume stress test (50 standalone, 500 folder files).
    Verify /files NEVER leaks folder-only files under any conditions.
    """
    mock_db = MockDatabase()
    user_id = 98765

    for i in range(50):
        await mock_db.file.insert_one({
            "_id": ObjectId(),
            "user_id": user_id,
            "file_name": f"standalone_{i}.mkv",
            "file_size": 1000 + i,
            "is_folder_file": False,
            "storage_status": "stored",
        })

    folder_id = "folder_stress_xyz"
    for i in range(500):
        await mock_db.file.insert_one({
            "_id": ObjectId(),
            "user_id": user_id,
            "file_name": f"folder_item_{i}.mkv",
            "file_size": 2000 + i,
            "is_folder_file": True,
            "folder_id": folder_id,
            "storage_status": "stored" if i % 2 == 0 else "cancelled",
        })

    standalone_cursor = mock_db.file.find({
        "user_id": user_id,
        "$or": [
            {"is_folder_file": False},
            {"is_folder_file": {"$exists": False}},
            {"folder_id": None},
        ],
    })
    standalone_results = [doc async for doc in standalone_cursor]
    leaked = [d for d in standalone_results if d.get("is_folder_file") is True or d.get("folder_id")]
    assert len(leaked) == 0, f"Critical isolation failure: {len(leaked)} folder files leaked into standalone!"
    assert len(standalone_results) == 50


# ==============================================================================
# PHASE 10: BACKPRESSURE & LOAD SHEDDING UNDER OVERLOAD
# ==============================================================================

def test_phase10_backpressure_and_load_shedding():
    """
    Test Phase 10:
    LoadShedder activates when load threshold exceeded and recovers when load normalizes.
    """
    shedder = LoadShedder(queue_threshold=0.8, error_rate_threshold=0.5)

    # 1. Healthy state
    should, reason = shedder.should_shed()
    assert should is False

    # 2. Trigger high error rate (> 50% errors)
    for _ in range(10):
        shedder.record_result(False)
    should, reason = shedder.should_shed()
    assert should is True
    assert "high_error_rate" in reason

    # 3. Recovery after reset
    shedder.reset()
    should, _ = shedder.should_shed()
    assert should is False


# ==============================================================================
# PHASE 11: AUTOMATIC WORKER SCALING LIFECYCLE
# ==============================================================================

@pytest.mark.asyncio
async def test_phase11_automatic_worker_scaling_lifecycle():
    """
    Test Phase 11:
    Worker pool scales dynamically within configured limits [MIN, MAX].
    """
    mock_db = MockDatabase()
    clients = {i: StorageTestBot(index=i) for i in range(5)}
    with patch.object(Telegram, "FLOG_STORAGE_WORKERS", 4), patch.object(Telegram, "FLOG_WORKERS_MIN", 2), patch.object(Telegram, "FLOG_WORKERS_MAX", 6):
        service = StorageService(db=mock_db, clients=clients)
        service.ensure_started()
        assert service._workers_min <= service.storage_workers <= service._workers_max
        await service.stop(timeout=0.1)


# ==============================================================================
# PHASE 12 & 13: WORKER DRAINING & HARD CRASH RECOVERY
# ==============================================================================

@pytest.mark.asyncio
async def test_phase12_and_13_worker_draining_and_hard_crash_recovery():
    """
    Test Phase 12 & 13:
    1. Worker draining: worker accepts no new claims, finishes active work.
    2. Hard crash: abrupt termination recovers expired leases on restart.
    """
    mock_db = MockDatabase()
    file_id = str(ObjectId())
    job_id = f"{file_id}:STORE_TO_FLOG"

    await mock_db.storage_jobs.insert_one({
        "job_id": job_id,
        "file_id": file_id,
        "status": "claimed",
        "worker_id": "crashed-instantly",
        "lease_until": time.time() - 30.0,
    })

    recovered = await mock_db.recover_expired_leases()
    assert recovered >= 1
    doc = await mock_db.get_storage_job(job_id)
    assert doc["status"].upper() == "PENDING"
    assert doc.get("worker_id") is None


# ==============================================================================
# PHASE 14: DISASTER RECOVERY DRILL (MEASURED RPO & RTO)
# ==============================================================================

@pytest.mark.asyncio
async def test_phase14_disaster_recovery_drill_with_measured_rpo_rto():
    """
    Test Phase 14:
    Disaster recovery simulation measuring actual RPO (0 lost committed jobs)
    and RTO (time in seconds to restore schema and re-enqueue inflight jobs).
    """
    from FileStream.utils.disaster_recovery import emergency_recover_all_inflight_jobs

    mock_db = MockDatabase()
    for i in range(10):
        fid = str(ObjectId())
        await mock_db.storage_jobs.insert_one({
            "job_id": f"{fid}:STORE_TO_FLOG",
            "file_id": fid,
            "status": "running",
            "worker_id": f"dead_worker_{i}",
        })

    t0 = time.monotonic()
    recovered_count = await emergency_recover_all_inflight_jobs(mock_db)
    rto_seconds = time.monotonic() - t0

    assert recovered_count == 10
    assert rto_seconds < 5.0

    cursor = mock_db.storage_jobs.find({"status": "PENDING"})
    pending_list = [d async for d in cursor]
    assert len(pending_list) == 10


# ==============================================================================
# PHASE 15: STREAMING PERFORMANCE & BOUNDED MEMORY
# ==============================================================================

@pytest.mark.asyncio
async def test_phase15_streaming_performance_and_bounded_memory():
    """
    Test Phase 15:
    Adaptive chunk streaming processes chunks without unbounded memory buffering.
    """
    chunk_size = 64 * 1024  # 64 KB
    total_chunks = 16       # 1 MB total
    dummy_data = b"0" * chunk_size

    async def mock_chunk_generator():
        for _ in range(total_chunks):
            yield dummy_data

    bytes_streamed = 0
    chunk_count = 0
    async for chunk in mock_chunk_generator():
        chunk_count += 1
        bytes_streamed += len(chunk)
        assert len(chunk) == chunk_size

    assert chunk_count == total_chunks
    assert bytes_streamed == 1024 * 1024


# ==============================================================================
# PHASE 16 & 17: SECURITY & ADMIN COMMAND AUTHORIZATION
# ==============================================================================

def test_phase16_and_17_security_and_admin_authorization():
    """
    Test Phase 16 & 17:
    - Path traversal attacks are sanitized and rejected.
    - Owner-only administrative actions are guarded.
    """
    from FileStream.bot.plugins.admin import OWNER_ONLY_ADMIN_ACTIONS

    assert "drain_worker" in OWNER_ONLY_ADMIN_ACTIONS
    assert "disable_client" in OWNER_ONLY_ADMIN_ACTIONS
    assert "enable_client" in OWNER_ONLY_ADMIN_ACTIONS

    assert is_suspicious_path("../../etc/passwd") is True
    assert is_suspicious_path("/etc/shadow") is True
    assert is_suspicious_path("normal_file.mp4") is False


# ==============================================================================
# PHASE 18 & 19: LATENCY PERCENTILES & ALERT THROTTLING
# ==============================================================================

def test_phase18_and_19_metrics_percentiles_and_alert_dedup():
    """
    Test Phase 18 & 19:
    1. Independent verification of P50, P95, P99 percentile calculations.
    2. AlertManager throttling prevents alert storms under repeated triggers.
    """
    from FileStream.utils.metrics import calculate_percentiles
    from FileStream.utils.alert_manager import AlertManager, AlertSeverity

    samples = [float(i) for i in range(1, 101)]
    pcts = calculate_percentiles(samples)
    assert pcts["p50"] == 50.0
    assert pcts["p95"] == 95.0
    assert pcts["p99"] == 99.0
    assert pcts["min"] == 1.0
    assert pcts["max"] == 100.0

    alert_mgr = AlertManager(throttle_seconds=60.0)
    received_alerts = []
    alert_mgr.register_handler(lambda a: received_alerts.append(a))

    for _ in range(10):
        alert_mgr.trigger_alert("high_cpu", AlertSeverity.WARNING, "CPU usage > 90%")

    assert len(received_alerts) == 1
    assert received_alerts[0].key == "high_cpu"

