"""
Comprehensive Test Suite for File To Link V2 — Production V4/V5 Upgrades.

Covers:
1. Metrics history, snapshot retention, and Prometheus export (Subsystems #3, #15, #26)
2. FLOG Channel Health Monitoring (Subsystem #17)
3. Operational Alerting & Health Evaluation (Subsystem #14)
4. Disaster Recovery & Backup Verification (Subsystems #27, #28)
5. Load Shedding & Admission Control (Subsystems #9, #24)
6. Security Hardening & Path Traversal Prevention (Subsystem #25)
7. Failure Injection Testing Framework (Subsystem #30)
8. Soak Testing Harness (Subsystem #29)
9. Worker Draining & Graceful Deployment (Subsystem #13)
10. Client Disabling & Circuit Breaker Controls (Subsystems #10, #21)
11. Admin V4/V5 Job & Worker Controls (Subsystems #20, #21)
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from FileStream.utils.metrics import MetricsCollector, get_metrics
from FileStream.utils.flog_channels import ChannelHealthMonitor, get_channel_monitor
from FileStream.utils.alert_manager import AlertManager, AlertSeverity, get_alert_manager
from FileStream.utils.disaster_recovery import (
    verify_database_schema,
    emergency_recover_all_inflight_jobs,
    verify_backup_directory,
    generate_dr_status_report,
)
from FileStream.utils.load_shedding import (
    LoadShedder,
    IPRateLimiter,
    SecurityHardener,
    is_suspicious_path,
    sanitize_download_path,
    create_security_middleware,
)
from FileStream.utils.testing_harness import (
    FailureInjector,
    FailureType,
    FailureConfig,
    create_faulty_client,
    SoakTestRunner,
    InjectedFloodWait,
)
from FileStream.services.storage_service import StorageService, StorageClientStat
from tests.test_folder_mode import MockDatabase, MockBot


# ==============================================================================
# 1. METRICS HISTORY & PROMETHEUS EXPORT
# ==============================================================================

def test_metrics_prometheus_export():
    collector = MetricsCollector()
    collector.record_job_completed(latency_seconds=1.5, client_id=1, bytes_count=1024)
    collector.record_job_failed(client_id=1)
    collector.record_job_retried(client_id=1)

    prom_text = collector.to_prometheus()
    assert "# HELP filestream_jobs_completed_total" in prom_text
    assert "# TYPE filestream_jobs_completed_total counter" in prom_text
    assert "filestream_jobs_completed_total 1" in prom_text
    assert "filestream_jobs_failed_total 1" in prom_text
    assert 'filestream_client_success_total{client_id="1"} 1' in prom_text


def test_metrics_history_and_retention():
    collector = MetricsCollector()
    # Record snapshots
    collector.record_snapshot(active_jobs=5, queue_depth=10, worker_count=3)
    collector.record_snapshot(active_jobs=8, queue_depth=15, worker_count=3)

    history = collector.get_history(limit=10)
    assert len(history) == 2
    assert history[0]["active_jobs"] == 5
    assert history[1]["queue_depth"] == 15

    # Test pruning with keep_count
    collector.prune_history(keep_count=1)
    history_after = collector.get_history()
    assert len(history_after) == 1
    assert history_after[0]["active_jobs"] == 8


# ==============================================================================
# 2. FLOG CHANNEL HEALTH MONITORING
# ==============================================================================

def test_channel_health_monitor_lifecycle():
    monitor = ChannelHealthMonitor(failure_threshold=3)
    channel_id = -1001234567890

    # Initial state
    assert monitor.is_healthy(channel_id) is True

    # Record failures below threshold
    monitor.record_failure(channel_id, "Temporary network timeout")
    assert monitor.is_healthy(channel_id) is True
    status = monitor.get_channel_health(channel_id)
    assert status["consecutive_failures"] == 1
    assert status["status"] == "DEGRADED"

    # Record more failures to trip threshold
    monitor.record_failure(channel_id, "ChatWriteForbidden")
    monitor.record_failure(channel_id, "ChatWriteForbidden")
    assert monitor.is_healthy(channel_id) is False
    status = monitor.get_channel_health(channel_id)
    assert status["is_healthy"] is False
    assert status["consecutive_failures"] == 3
    assert status["status"] == "UNHEALTHY"

    # Success restores health
    monitor.record_success(channel_id)
    assert monitor.is_healthy(channel_id) is True
    status = monitor.get_channel_health(channel_id)
    assert status["consecutive_failures"] == 0
    assert status["is_healthy"] is True
    assert status["status"] == "HEALTHY"


def test_channel_health_list_channels():
    monitor = ChannelHealthMonitor()
    monitor.record_success(-1001)
    monitor.record_failure(-1002, "Error")

    channels = monitor.list_channels_health()
    assert len(channels) == 2
    ids = [c["channel_id"] for c in channels]
    assert -1001 in ids
    assert -1002 in ids


# ==============================================================================
# 3. OPERATIONAL ALERTING & SYSTEM HEALTH EVALUATION
# ==============================================================================

def test_alert_manager_trigger_and_throttle():
    mgr = AlertManager(throttle_seconds=10.0)

    # First alert fires
    a1 = mgr.trigger_alert("TEST_ALERT", AlertSeverity.WARNING, "Test warning message")
    assert a1 is not None
    assert a1.key == "TEST_ALERT"
    assert a1.severity == AlertSeverity.WARNING

    # Immediate second alert is throttled
    a2 = mgr.trigger_alert("TEST_ALERT", AlertSeverity.WARNING, "Repeated warning")
    assert a2 is None

    # Different key is NOT throttled
    a3 = mgr.trigger_alert("OTHER_ALERT", AlertSeverity.CRITICAL, "Critical message")
    assert a3 is not None

    active = mgr.get_active_alerts()
    assert len(active) == 2

    # Resolve alert
    mgr.resolve_alert("TEST_ALERT")
    assert len(mgr.get_active_alerts()) == 1


@pytest.mark.asyncio
async def test_alert_manager_evaluate_system_health():
    mgr = AlertManager(throttle_seconds=0.0)

    mock_db = MagicMock()
    mock_db.ping = AsyncMock(side_effect=ConnectionError("MongoDB down"))

    storage_svc = MagicMock()
    storage_svc.get_metrics.return_value = {
        "storage_workers": 1,
        "storage_queue_size": 450,
        "storage_queue_capacity": 500,
    }
    storage_svc._shutting_down = False

    alerts = await mgr.evaluate_system_health(mock_db, storage_svc)
    alert_keys = [a.key for a in alerts]
    assert "MONGODB_DISCONNECTED" in alert_keys
    assert "QUEUE_BACKED_UP" in alert_keys


# ==============================================================================
# 4. DISASTER RECOVERY & BACKUP VERIFICATION
# ==============================================================================

@pytest.mark.asyncio
async def test_disaster_recovery_schema_verification():
    mock_db = MagicMock()
    mock_database = MagicMock()
    mock_db.db = mock_database

    mock_database.list_collection_names = AsyncMock(
        return_value=["file", "storage_jobs", "folders", "users", "worker_health", "client_health", "audit_events", "metrics_snapshots"]
    )
    col_mock = MagicMock()
    col_mock.count_documents = AsyncMock(return_value=10)
    col_mock.index_information = AsyncMock(return_value={"_id_": {}})
    mock_database.__getitem__.return_value = col_mock

    report = await verify_database_schema(mock_db)
    assert report["status"] == "healthy"
    assert "file" in report["collections"]
    assert len(report["missing_collections"]) == 0


@pytest.mark.asyncio
async def test_emergency_recover_all_inflight_jobs():
    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_db.storage_jobs_collection = mock_col
    mock_db.storage_jobs = mock_col
    update_res = MagicMock()
    update_res.modified_count = 5
    mock_col.update_many = AsyncMock(return_value=update_res)

    recovered = await emergency_recover_all_inflight_jobs(mock_db)
    assert recovered == 5
    mock_col.update_many.assert_called_once()


def test_verify_backup_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Empty directory
        report = verify_backup_directory(tmpdir)
        assert report["valid"] is False

        # Add mock bson files
        with open(os.path.join(tmpdir, "files.bson"), "wb") as f:
            f.write(b"mock_bson_data")
        with open(os.path.join(tmpdir, "storage_jobs.bson"), "wb") as f:
            f.write(b"mock_bson_data")
        with open(os.path.join(tmpdir, "users.bson"), "wb") as f:
            f.write(b"mock_bson_data")

        report2 = verify_backup_directory(tmpdir)
        assert report2["valid"] is True
        assert report2["total_size_bytes"] > 0


# ==============================================================================
# 5. LOAD SHEDDING & ADMISSION CONTROL
# ==============================================================================

def test_load_shedder_activation():
    shedder = LoadShedder(queue_threshold=0.8, error_rate_threshold=0.5)

    # Mock storage service below threshold
    svc = MagicMock()
    svc.get_metrics.return_value = {"storage_queue_size": 200}
    svc.max_queue_size = 500

    should, reason = shedder.should_shed(svc)
    assert should is False

    # Simulate queue fill above 80% (450/500 = 90%)
    svc.get_metrics.return_value = {"storage_queue_size": 450}
    should, reason = shedder.should_shed(svc)
    assert should is True
    assert "queue_full" in reason

    # Simulate error rate spike
    shedder.reset()
    for _ in range(10):
        shedder.record_result(False)  # 10 consecutive failures
    should2, reason2 = shedder.should_shed(None)
    assert should2 is True
    assert "high_error_rate" in reason2


def test_ip_rate_limiter():
    limiter = IPRateLimiter(max_requests=3, window_seconds=60.0)
    ip = "192.168.1.100"

    assert limiter.is_allowed(ip) is True
    assert limiter.is_allowed(ip) is True
    assert limiter.is_allowed(ip) is True
    # Exceeded
    assert limiter.is_allowed(ip) is False

    # Different IP is fine
    assert limiter.is_allowed("192.168.1.101") is True


# ==============================================================================
# 6. SECURITY HARDENING & PATH SANITIZATION
# ==============================================================================

def test_suspicious_path_detection():
    assert is_suspicious_path("/.env") is True
    assert is_suspicious_path("/.git/config") is True
    assert is_suspicious_path("/wp-admin") is True
    assert is_suspicious_path("/actuator/health") is True
    assert is_suspicious_path("/etc/passwd") is True
    assert is_suspicious_path("/watch/abc123/video.mp4") is False
    assert is_suspicious_path("/dl/hash/file.pdf") is False


def test_path_traversal_sanitization():
    assert sanitize_download_path("/dl/../secret/file") is None
    assert sanitize_download_path("/dl/%2e%2e/etc/passwd") is None
    assert sanitize_download_path("/dl/valid_hash/movie.mkv") == "/dl/valid_hash/movie.mkv"
    assert sanitize_download_path("/watch/valid_hash/movie.mp4?dl=1") == "/watch/valid_hash/movie.mp4"


# ==============================================================================
# 7. FAILURE INJECTION TESTING
# ==============================================================================

@pytest.mark.asyncio
async def test_failure_injector():
    injector = FailureInjector(seed=42)
    injector.configure(FailureConfig(FailureType.FLOODWAIT, probability=1.0, floodwait_seconds=15))
    injector.enable()

    with pytest.raises(Exception) as exc_info:
        await injector.maybe_inject("send_file")
    assert "wait of 15 seconds" in str(exc_info.value)
    assert getattr(exc_info.value, "value", None) == 15

    # Disable injector
    injector.disable()
    # Should not raise
    await injector.maybe_inject("send_file")


@pytest.mark.asyncio
async def test_faulty_client():
    injector = FailureInjector()
    injector.configure(FailureConfig(FailureType.NETWORK_ERROR, probability=1.0))
    injector.enable()

    client = create_faulty_client(client_id=1, injector=injector)
    with pytest.raises(ConnectionError):
        await client.send_document(chat_id=-1001, document="test_file")


# ==============================================================================
# 8. SOAK TEST HARNESS
# ==============================================================================

@pytest.mark.asyncio
async def test_soak_test_runner():
    runner = SoakTestRunner(duration_seconds=0.2, concurrency=2, operations_per_second=50.0)

    async def mock_operation():
        await asyncio.sleep(0.001)

    result = await runner.run(mock_operation)
    assert result.total_operations > 0
    assert result.successful_operations == result.total_operations
    assert result.failed_operations == 0
    assert result.p50_latency_ms >= 0.0


# ==============================================================================
# 9. WORKER DRAINING & CLIENT CONTROLS
# ==============================================================================

@pytest.mark.asyncio
async def test_worker_draining_controls():
    mock_db = MockDatabase()
    bot0 = MockBot()
    service = StorageService(db=mock_db, clients={0: bot0}, max_queue_size=10)

    # Worker draining
    assert service.is_worker_draining("worker-1") is False
    assert service.drain_worker("worker-1") is True
    assert service.is_worker_draining("worker-1") is True

    # Client disable / enable
    assert service.disable_client(0) is True
    stats = service.get_client_stats(0)
    assert stats is not None
    assert stats["disabled"] is True

    # Active clients should not return disabled client
    active = service._get_active_clients()
    assert 0 not in [idx for idx, _ in active]

    # Re-enable client
    assert service.enable_client(0) is True
    stats = service.get_client_stats(0)
    assert stats["disabled"] is False
    active = service._get_active_clients()
    assert 0 in [idx for idx, _ in active]

    await service.shutdown()


# ==============================================================================
# 10. ADMIN V4/V5 COMMAND REGISTRATION & USAGE
# ==============================================================================

def test_admin_commands_registered():
    from FileStream.utils.bot_commands import ADMIN_COMMANDS, OWNER_COMMANDS
    from FileStream.bot.plugins.admin import OWNER_ONLY_ADMIN_ACTIONS

    admin_cmd_names = [cmd.command for cmd in ADMIN_COMMANDS]
    owner_cmd_names = [cmd.command for cmd in OWNER_COMMANDS]

    # Check V4/V5 commands are registered
    assert "storage_jobs" in admin_cmd_names
    assert "storage_job" in admin_cmd_names
    assert "retry_storage_job" in admin_cmd_names
    assert "cancel_storage_job" in admin_cmd_names
    assert "storage_queue" in admin_cmd_names
    assert "reconcile_flog" in owner_cmd_names
    assert "worker_status" in admin_cmd_names
    assert "client_status" in admin_cmd_names
    assert "drain_worker" in owner_cmd_names
    assert "disable_client" in owner_cmd_names
    assert "enable_client" in owner_cmd_names

    # Check owner-only security constraints
    assert "drain_worker" in OWNER_ONLY_ADMIN_ACTIONS
    assert "disable_client" in OWNER_ONLY_ADMIN_ACTIONS
    assert "enable_client" in OWNER_ONLY_ADMIN_ACTIONS


# ==============================================================================
# 12. CONFIGURATION VALIDATION & RUNTIME TYPE RESOLUTION
# ==============================================================================

def test_configuration_validation_success_and_failures():
    from FileStream.config import validate_configuration, Telegram
    from unittest.mock import patch

    # 1. Valid configuration
    summary = validate_configuration()
    assert isinstance(summary, dict)
    assert summary["bot_token"] == "***REDACTED***"
    assert summary["database_url"] == "***REDACTED***"

    # 2. Missing required database_url
    with patch.object(Telegram, "DATABASE_URL", ""):
        with pytest.raises(ValueError, match="DATABASE_URL must be configured"):
            validate_configuration()

    # 3. Missing bot_token
    with patch.object(Telegram, "BOT_TOKEN", ""):
        with pytest.raises(ValueError, match="BOT_TOKEN must be configured"):
            validate_configuration()

    # 4. Workers min > max
    with patch.object(Telegram, "FLOG_WORKERS_MIN", 10), patch.object(Telegram, "FLOG_WORKERS_MAX", 2):
        with pytest.raises(ValueError, match="cannot exceed FLOG_WORKERS_MAX"):
            validate_configuration()

    # 5. Invalid queue size
    with patch.object(Telegram, "FLOG_STORAGE_QUEUE_SIZE", 5):
        with pytest.raises(ValueError, match="FLOG_STORAGE_QUEUE_SIZE must be at least 10"):
            validate_configuration()

    # 6. Invalid lease seconds
    with patch.object(Telegram, "FLOG_JOB_LEASE_SECONDS", 2):
        with pytest.raises(ValueError, match="FLOG_JOB_LEASE_SECONDS must be at least 10s"):
            validate_configuration()

    # 7. Invalid max retries
    with patch.object(Telegram, "FLOG_MAX_RETRIES", 0):
        with pytest.raises(ValueError, match="FLOG_MAX_RETRIES must be at least 1"):
            validate_configuration()

