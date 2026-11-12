from __future__ import annotations

import asyncio
import datetime
from datetime import timezone, timedelta
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from FileStream.bot import FileStream, multi_clients
from FileStream.config import Telegram
from FileStream.database.database import Database
from FileStream.core.monitoring.audit_manager import audit_manager
from FileStream.core.resilience.floodwait import call_with_floodwait, is_floodwait, get_floodwait_delay
from FileStream.core.runtime.flog_channels import (
    has_configured_flog_channels,
    optional_channel_name_for_id,
    resolve_file_flog_channel_id,
)
from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry
from FileStream.core.monitoring.metrics import get_metrics

logger = logging.getLogger(__name__)


def _is_secondary_worker(client: Any) -> bool:
    try:
        from FileStream.bot.clients import is_secondary_worker
        return is_secondary_worker(client)
    except Exception:
        idx = getattr(client, "client_index", 0)
        return int(idx or 0) > 0


class StatusStr(str):
    """Case-insensitive string subclass for backward-compatible StorageStatus comparisons."""
    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            s1, s2 = self.upper(), other.upper()
            if s1 == s2:
                return True
            if s1 in ("PENDING", "CLAIMED") and s2 in ("PENDING", "CLAIMED"):
                return True
            if s1 in ("RUNNING", "PROCESSING") and s2 in ("RUNNING", "PROCESSING"):
                return True
            if s1 in ("RETRY_WAIT", "RETRYING") and s2 in ("RETRY_WAIT", "RETRYING"):
                return True
            return False
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.upper())


class StorageStatus:
    PENDING = StatusStr("PENDING")
    CLAIMED = StatusStr("CLAIMED")
    RUNNING = StatusStr("RUNNING")
    PROCESSING = StatusStr("RUNNING")
    STORED = StatusStr("STORED")
    RETRY_WAIT = StatusStr("RETRY_WAIT")
    RETRYING = StatusStr("RETRY_WAIT")
    FAILED = StatusStr("FAILED")
    CANCELLED = StatusStr("CANCELLED")


@dataclass
class StorageClientStat:
    index: int
    client: Client
    semaphore: asyncio.Semaphore
    successful_sends: int = 0
    failed_sends: int = 0
    floodwait_count: int = 0
    consecutive_failures: int = 0
    last_success_time: float = 0.0
    last_failure_time: float = 0.0
    current_inflight: int = 0
    cooldown_until: float = 0.0
    is_alive: bool = True
    cannot_access_flog: bool = False
    disabled: bool = False

    @property
    def client_inflight(self) -> int:
        return self.current_inflight

    @property
    def client_completed(self) -> int:
        return self.successful_sends

    @property
    def client_failed(self) -> int:
        return self.failed_sends

    @property
    def client_floodwait_count(self) -> int:
        return self.floodwait_count

    @property
    def client_cooldown_until(self) -> float:
        return self.cooldown_until


@dataclass
class StorageResult:
    success: bool
    file_id: str
    db_id: str | None = None
    message_id: int | None = None
    client_id: int | None = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.success


def _generate_worker_id() -> str:
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "localhost"
    return f"filestream:{hostname}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseRenewer:
    """Periodically renews the lease for a job currently in flight."""
    def __init__(self, db: Database, job_id: str, worker_id: str, lease_seconds: float = 120.0):
        self.db = db
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = float(lease_seconds)
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None and hasattr(self.db, "renew_job_lease"):
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        interval = max(self.lease_seconds / 3.0, 0.05)
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
                if self._stopped:
                    break
                await self.db.renew_job_lease(self.job_id, self.worker_id, lease_seconds=self.lease_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[STORAGE] Lease renewal error for %s: %s", self.job_id, e)

    async def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


@dataclass
class StorageJob:
    db_id: str
    file_id: str
    file_name: str
    file_size: int
    file_info: dict
    message: Message | None = None
    user_id: int | None = None
    session_id: str | None = None
    target_channel_id: int | None = None
    storage_mode: str = "main"
    status: str = StorageStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.monotonic)
    error: str | None = None
    future: asyncio.Future | None = None
    log_msg_id: int | None = None
    client_id: int | None = None
    job_id: str = ""
    storage_operation_key: str = ""
    folder_id: str | None = None
    media_group_id: str | None = None
    worker_id: str | None = None
    preferred_client_id: int | None = None
    assigned_client_id: int | None = None
    lease_until: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    recovery_count: int = 0


class StorageService:
    """
    Dedicated Multi-Client FLOG Storage Service with Persistent MongoDB Job Queue.
    - MongoDB persistent storage jobs as durable source of truth
    - Local bounded asynchronous queue as fast execution buffer
    - Smart storage-client selection and client reservation
    - Atomic claims with lease timeout and background lease renewal
    - Multi-process safety with zero cross-process collisions
    - Automatic FloodWait client cooldown without stalling other clients
    - Safe graceful shutdown, restart recovery, and drain
    - Session cancellation and late-completion pruning
    """

    def __init__(
        self,
        db: Database | None = None,
        clients: dict[int, Client] | None = None,
        max_queue_size: int | None = None,
        concurrency_per_client: int | None = None,
    ):
        self.db = db or Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
        self._clients_dict = clients if clients is not None else multi_clients
        self.max_queue_size = (
            max_queue_size
            if max_queue_size is not None
            else getattr(Telegram, "FLOG_STORAGE_QUEUE_SIZE", getattr(Telegram, "FLOG_STORAGE_MAX_QUEUE", 500))
        )
        self.concurrency_per_client = (
            concurrency_per_client
            if concurrency_per_client is not None
            else getattr(Telegram, "FLOG_CLIENT_CONCURRENCY", 2)
        )
        self.lease_seconds = getattr(Telegram, "FLOG_JOB_LEASE_SECONDS", 120)
        self.batch_size = getattr(Telegram, "FLOG_JOB_BATCH_SIZE", 20)
        self.recovery_interval = getattr(Telegram, "FLOG_JOB_RECOVERY_INTERVAL_SECONDS", 15)
        self.poll_interval = getattr(Telegram, "FLOG_STORAGE_POLL_INTERVAL", 0.5)
        self.worker_id = _generate_worker_id()

        self._queue: asyncio.Queue[StorageJob] = asyncio.Queue(maxsize=self.max_queue_size)
        self._client_stats: dict[int, StorageClientStat] = {}
        self._cancelled_sessions: set[str] = set()
        self._cancelled_session_times: dict[str, float] = {}
        self._workers: list[asyncio.Task] = []
        self._scheduler_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._shutting_down = False
        self._started = False
        self._lock = asyncio.Lock()
        self.storage_queue_peak: int = 0
        self._in_progress_jobs: dict[str, StorageJob] = {}
        self.total_successful_jobs: int = 0
        self.total_failed_jobs: int = 0
        self.total_cancelled_jobs: int = 0
        self._heartbeat_task: asyncio.Task | None = None
        self._last_scale_action: float = 0.0
        self._last_priority_aging_time: float = 0.0
        self._workers_min = getattr(Telegram, "FLOG_WORKERS_MIN", 2)
        self._workers_max = getattr(Telegram, "FLOG_WORKERS_MAX", getattr(Telegram, "FLOG_STORAGE_WORKERS", 6))
        self._scale_up_threshold = getattr(Telegram, "FLOG_WORKER_SCALE_UP_THRESHOLD", 5)
        self._scale_down_idle_seconds = getattr(Telegram, "FLOG_WORKER_SCALE_DOWN_IDLE_SECONDS", 30.0)
        self._scale_cooldown_seconds = getattr(Telegram, "FLOG_WORKER_SCALE_COOLDOWN_SECONDS", 10.0)
        self._draining_workers: set[str] = set()
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def storage_workers(self) -> int:
        return len([w for w in self._workers if not w.done()])

    @property
    def active_clients(self) -> int:
        return len(self._get_active_clients())

    @property
    def storage_queue_size(self) -> int:
        return self._queue.qsize()

    def _ensure_client_stat(self, idx: int, client: Client) -> StorageClientStat:
        if idx not in self._client_stats:
            sem = asyncio.Semaphore(self.concurrency_per_client)
            self._client_stats[idx] = StorageClientStat(
                index=idx,
                client=client,
                semaphore=sem,
            )
        return self._client_stats[idx]

    def _scale_workers(self, queue_depth: int | None = None) -> None:
        if not self._started or self._shutting_down:
            return

        now = time.monotonic()
        active_count = len(self._get_active_clients())
        client_capacity = max(active_count * self.concurrency_per_client, 2)
        q_size = self._queue.qsize() if queue_depth is None else int(queue_depth)

        target = max(self._workers_min, min(client_capacity, self._workers_max))
        if q_size >= self._scale_up_threshold and (now - self._last_scale_action) >= self._scale_cooldown_seconds:
            boosted = min(self._workers_max, max(target, target + 2))
            if boosted > target:
                target = boosted
                self._last_scale_action = now

        active_workers = len([w for w in self._workers if not w.done()])
        if active_workers < target:
            for i in range(active_workers, target):
                self._workers.append(asyncio.create_task(self._worker_loop(f"flog-worker-{i}")))
            logger.info("Scaled storage worker pool from %d to %d workers (queue: %d)", active_workers, target, q_size)

    def register_client(self, idx: int, client: Client) -> None:
        """Register or update an active client in the storage service."""
        self._clients_dict[idx] = client
        self._ensure_client_stat(idx, client)
        self._scale_workers()
        logger.info("[StorageService] Registered client %d (active clients: %d)", idx, len(self._get_active_clients()))

    def unregister_client(self, idx: int) -> None:
        """Unregister or mark a client inactive in the storage service."""
        if idx in self._clients_dict and idx != 0:
            self._clients_dict.pop(idx, None)
        if idx in self._client_stats:
            self._client_stats[idx].is_alive = False
        logger.info("[StorageService] Unregistered client %d (active clients: %d)", idx, len(self._get_active_clients()))

    def get_client_stats(self, client_id: int | None = None) -> dict[int, dict] | dict | None:
        """Return runtime health snapshot for observability."""
        for c_idx, cl in list(self._clients_dict.items()):
            self._ensure_client_stat(c_idx, cl)
        snapshot = {}
        now = time.monotonic()
        for idx, stat in self._client_stats.items():
            snapshot[idx] = {
                "index": stat.index,
                "successful_sends": stat.successful_sends,
                "failed_sends": stat.failed_sends,
                "floodwait_count": stat.floodwait_count,
                "consecutive_failures": stat.consecutive_failures,
                "current_inflight": stat.current_inflight,
                "in_cooldown": (stat.cooldown_until > now),
                "cooldown_remaining_s": round(max(stat.cooldown_until - now, 0.0), 2),
                "is_alive": stat.is_alive,
                "cannot_access_flog": stat.cannot_access_flog,
                "disabled": getattr(stat, "disabled", False),
            }
        if client_id is not None:
            return snapshot.get(int(client_id))
        return snapshot

    def disable_client(self, idx: int) -> bool:
        """Disable a client from being selected for storage jobs."""
        client = self._clients_dict.get(idx)
        if client is None and idx not in self._client_stats:
            return False
        stat = self._ensure_client_stat(idx, client) if client else self._client_stats.get(idx)
        if stat is None:
            return False
        stat.disabled = True
        logger.info("[StorageService] Disabled client %d", idx)
        return True

    def enable_client(self, idx: int) -> bool:
        """Re-enable a previously disabled client."""
        client = self._clients_dict.get(idx)
        if client is None and idx not in self._client_stats:
            return False
        stat = self._ensure_client_stat(idx, client) if client else self._client_stats.get(idx)
        if stat is None:
            return False
        stat.disabled = False
        logger.info("[StorageService] Enabled client %d", idx)
        return True

    def drain_worker(self, worker_id: str) -> bool:
        """Mark a worker as draining so it gracefully shuts down after current work."""
        if not worker_id:
            return False
        self._draining_workers.add(str(worker_id))
        logger.info("[StorageService] Worker %s marked as draining", worker_id)
        return True

    def is_worker_draining(self, worker_id: str) -> bool:
        """Check if a worker has been requested to drain."""
        return str(worker_id) in self._draining_workers

    def get_metrics(self) -> dict:
        """Return comprehensive scheduler and queue metrics."""
        now = time.monotonic()
        active = self._get_active_clients()
        cooldown_count = sum(
            1 for idx, _ in active
            if (self._client_stats.get(idx) and self._client_stats[idx].cooldown_until > now)
        )
        available_count = sum(
            1 for idx, _ in active
            if (
                self._client_stats.get(idx)
                and self._client_stats[idx].cooldown_until <= now
                and self._client_stats[idx].current_inflight < self.concurrency_per_client
            )
        )
        active_jobs = sum(stat.current_inflight for stat in self._client_stats.values())
        total_floodwaits = sum(stat.floodwait_count for stat in self._client_stats.values())

        per_client_success = {idx: stat.successful_sends for idx, stat in self._client_stats.items()}
        per_client_failure = {idx: stat.failed_sends for idx, stat in self._client_stats.items()}
        per_client_inflight = {idx: stat.current_inflight for idx, stat in self._client_stats.items()}

        return {
            "active_clients": len(active),
            "available_clients": available_count,
            "cooldown_clients": cooldown_count,
            "queue_size": self.storage_queue_size,
            "storage_queue_size": self.storage_queue_size,
            "storage_queue_peak": self.storage_queue_peak,
            "storage_workers": self.storage_workers,
            "active_storage_jobs": active_jobs,
            "in_flight_jobs": active_jobs,
            "successful_jobs": self.total_successful_jobs,
            "failed_jobs": self.total_failed_jobs,
            "cancelled_jobs": self.total_cancelled_jobs,
            "floodwait_count": total_floodwaits,
            "per_client_success": per_client_success,
            "per_client_failure": per_client_failure,
            "per_client_inflight": per_client_inflight,
            "worker_id": self.worker_id,
            "lease_seconds": self.lease_seconds,
            "poll_interval": self.poll_interval,
            "recovery_interval": self.recovery_interval,
            "clients": {
                idx: {
                    "client_inflight": stat.current_inflight,
                    "client_completed": stat.successful_sends,
                    "client_failed": stat.failed_sends,
                    "client_floodwait_count": stat.floodwait_count,
                    "client_cooldown_until": stat.cooldown_until,
                    "in_cooldown": (stat.cooldown_until > now),
                    "cooldown_remaining_s": round(max(stat.cooldown_until - now, 0.0), 2),
                    "is_alive": stat.is_alive,
                    "cannot_access_flog": stat.cannot_access_flog,
                }
                for idx, stat in self._client_stats.items()
            },
        }

    def _get_active_clients(self) -> list[tuple[int, Client]]:
        """Retrieve all active, capable Telegram clients (Client 0 fallback if empty)."""
        active = []
        for idx, client in self._clients_dict.items():
            if client is None:
                continue
            stat = self._client_stats.get(idx)
            if stat is not None and (not stat.is_alive or stat.cannot_access_flog or getattr(stat, "disabled", False)):
                continue
            if hasattr(client, "is_connected") and not client.is_connected:
                continue
            active.append((idx, client))

        if not active and FileStream is not None:
            stat0 = self._client_stats.get(0)
            if stat0 is None or (stat0.is_alive and not stat0.cannot_access_flog and not getattr(stat0, "disabled", False)):
                active.append((0, FileStream))
        return sorted(active, key=lambda x: x[0])

    def select_best_client(
        self, preferred_id: int | None = None, exclude_indices: set[int] | None = None
    ) -> tuple[int, Client, StorageClientStat] | None:
        """
        Smart storage client selection:
        Selects the healthiest available client that is:
        1. connected
        2. authorized
        3. capable of accessing FLOG destination
        4. not in FloodWait cooldown (checked BEFORE scoring)
        5. below reasonable inflight load (concurrency_per_client)
        6. offloads FLOG storage to secondary workers over Client 0 when available
        """
        active = self._get_active_clients()
        if not active:
            return None

        now = time.monotonic()
        candidates: list[tuple[float, int, Client, StorageClientStat]] = []
        has_secondary_clients = any(idx != 0 for idx, _ in active)

        for idx, client in active:
            if exclude_indices and idx in exclude_indices:
                continue
            stat = self._ensure_client_stat(idx, client)
            if not stat.is_alive or stat.cannot_access_flog:
                continue

            # Check if connected
            if hasattr(client, "is_connected") and not client.is_connected:
                continue

            # Check if authorized
            if getattr(client, "id", None) is None and not getattr(client, "me", None):
                if client is not FileStream and not hasattr(client, "client_index"):
                    continue

            # Check FloodWait cooldown BEFORE scoring!
            if stat.cooldown_until > now:
                continue

            # Check Circuit Breaker
            if not get_circuit_breaker_registry().can_execute(idx):
                continue

            # Check inflight capacity
            if stat.current_inflight >= self.concurrency_per_client:
                continue

            # Preferred client bonus if matched (by client index or Telegram bot ID)
            is_preferred = (
                preferred_id is not None
                and (idx == preferred_id or getattr(client, "id", None) == preferred_id)
            )
            pref_bonus = -1000 if is_preferred else 0

            # Secondary workers cannot forward or access private media sent to other bots.
            # When a preferred client is specified (e.g. source bot index 0 for private user messages),
            # non-matching secondary workers must NOT be selected as candidates.
            if preferred_id is not None and not is_preferred and _is_secondary_worker(client):
                continue

            # Offload FLOG storage: Client 0 penalty when secondary clients exist and no preferred client is specified.
            # When an incoming private file specifies preferred_id=0, is_preferred is True and penalty is 0.
            primary_penalty = 500 if (idx == 0 and has_secondary_clients and not is_preferred) else 0

            # Score formula: lower is healthier / less loaded
            score = (
                (stat.current_inflight * 10)
                + (stat.consecutive_failures * 25)
                + (stat.floodwait_count * 5)
                + pref_bonus
                + primary_penalty
            )
            candidates.append((score, idx, client, stat))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
        return best[1], best[2], best[3]

    async def reserve_client(
        self, preferred_id: int | None = None, exclude_indices: set[int] | None = None
    ) -> tuple[int, Client, StorageClientStat] | None:
        """
        Atomically selects and reserves a client under lock, incrementing its in-flight count.
        Avoids race conditions where concurrent workers select the same client simultaneously.
        """
        async with self._lock:
            selection = self.select_best_client(preferred_id=preferred_id, exclude_indices=exclude_indices)
            if selection is not None:
                idx, client, stat = selection
                stat.current_inflight += 1
                logger.info(
                    "[STORAGE] STORAGE_CLIENT_RESERVED: client=%s inflight=%s",
                    idx, stat.current_inflight
                )
                return idx, client, stat
            return None

    def release_client(self, stat: StorageClientStat | int) -> None:
        """Safely decrement in-flight counter for a client."""
        if isinstance(stat, int):
            stat = self._client_stats.get(stat)
        if stat is not None:
            stat.current_inflight = max(0, stat.current_inflight - 1)

    def get_earliest_cooldown(self) -> float:
        """Calculate minimum time until at least one client exits cooldown."""
        now = time.monotonic()
        cooldowns = [
            stat.cooldown_until - now
            for stat in self._client_stats.values()
            if stat.cooldown_until > now
        ]
        if not cooldowns:
            return 0.05
        return max(min(cooldowns), 0.05)

    def cancel_session_jobs(self, session_id: str) -> int:
        """Mark all pending/retrying/in-flight jobs for session_id as cancelled, cancel their futures."""
        if not session_id:
            return 0
        self.record_cancelled_session(session_id)
        cancelled_count = 0
        for job in list(self._queue._queue):
            if getattr(job, "session_id", None) == session_id:
                if job.status in (StorageStatus.PENDING, StorageStatus.CLAIMED, StorageStatus.RETRYING, StorageStatus.RETRY_WAIT):
                    job.status = StorageStatus.CANCELLED
                    if job.future and not job.future.done():
                        job.future.cancel()
                    self._in_progress_jobs.pop(job.db_id, None)
                    cancelled_count += 1
        for db_id, job in list(self._in_progress_jobs.items()):
            if getattr(job, "session_id", None) == session_id:
                if job.status in (StorageStatus.PENDING, StorageStatus.CLAIMED, StorageStatus.PROCESSING, StorageStatus.RUNNING, StorageStatus.RETRYING, StorageStatus.RETRY_WAIT):
                    job.status = StorageStatus.CANCELLED
                    if job.future and not job.future.done():
                        job.future.cancel()
                    self._in_progress_jobs.pop(db_id, None)
                    cancelled_count += 1
        if cancelled_count:
            self.total_cancelled_jobs += cancelled_count
            logger.info("[STORAGE] STORAGE_CANCELLED: session=%s count=%d storage jobs cancelled", session_id, cancelled_count)

        if hasattr(self.db, "cancel_storage_jobs_for_session"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.db.cancel_storage_jobs_for_session(session_id))
            except RuntimeError:
                pass

        return cancelled_count

    def record_cancelled_session(self, session_id: str) -> None:
        if not session_id:
            return
        self._cancelled_sessions.add(session_id)
        self._cancelled_session_times[session_id] = time.time()
        cutoff = time.time() - 3600
        old = [sid for sid, t in self._cancelled_session_times.items() if t < cutoff]
        for sid in old:
            self._cancelled_sessions.discard(sid)
            self._cancelled_session_times.pop(sid, None)

    def is_session_cancelled(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        return session_id in self._cancelled_sessions

    async def enqueue_storage(
        self,
        db_id: str,
        file_id: str,
        file_name: str,
        file_size: int,
        file_info: dict,
        message: Message | None = None,
        user_id: int | None = None,
        target_channel_id: int | None = None,
        storage_mode: str = "main",
        session_id: str | None = None,
        max_retries: int | None = None,
        create_future: bool = False,
    ) -> StorageJob:
        """
        Enqueue a file for asynchronous FLOG storage forwarding.
        Guarantees ONE FILE -> ONE ACTIVE STORAGE JOB -> ONE COMMIT.
        Provides bounded queue backpressure.
        """
        if self._shutting_down:
            raise RuntimeError("StorageService is shutting down; cannot accept new jobs")

        # Admission control checks
        if hasattr(self.db, "count_pending_jobs"):
            try:
                max_pending = getattr(Telegram, "MAX_PENDING_STORAGE_JOBS", 5000)
                pending_cnt = await self.db.count_pending_jobs()
                if pending_cnt >= max_pending:
                    logger.warning("[ADMISSION_CONTROL] Global pending jobs limit reached: %d >= %d", pending_cnt, max_pending)
                    raise RuntimeError(f"Storage system is at capacity ({pending_cnt}/{max_pending} jobs). Please try again later.")

                is_folder = bool(session_id or (file_info and (file_info.get("is_folder_file") or file_info.get("folder_session_id") or file_info.get("folder_id"))))
                if user_id and not is_folder:
                    max_user = getattr(Telegram, "MAX_USER_PENDING_JOBS", 50)
                    user_cnt = await self.db.count_user_pending_jobs(user_id)
                    if user_cnt >= max_user:
                        logger.warning("[ADMISSION_CONTROL] User %s pending jobs limit reached: %d >= %d", user_id, user_cnt, max_user)
                        raise RuntimeError(f"You have reached maximum concurrent uploads ({user_cnt}/{max_user}). Please wait for them to finish.")

                f_id = (file_info.get("folder_id") or session_id) if file_info else session_id
                if f_id:
                    max_folder = getattr(Telegram, "MAX_FOLDER_PENDING_JOBS", 500)
                    folder_cnt = await self.db.count_folder_pending_jobs(f_id)
                    if folder_cnt >= max_folder:
                        logger.warning("[ADMISSION_CONTROL] Folder %s pending jobs limit reached: %d >= %d", f_id, folder_cnt, max_folder)
                        raise RuntimeError(f"Folder queue is at capacity ({folder_cnt}/{max_folder}). Please wait for processing to finish.")
            except RuntimeError:
                raise
            except Exception as ac_err:
                logger.debug("[ADMISSION_CONTROL] Non-fatal check error: %s", ac_err)

        get_metrics().record_job_created()

        retries = max_retries if max_retries is not None else getattr(Telegram, "FLOG_MAX_RETRIES", 5)
        db_id_str = str(db_id)
        file_id_str = str(file_id)
        job_id_val = f"{db_id_str}:STORE_TO_FLOG"

        # Atomic duplicate job deduplication
        async with self._lock:
            if db_id_str in self._in_progress_jobs:
                existing_job = self._in_progress_jobs[db_id_str]
                logger.info(
                    "[STORAGE] STORAGE_JOB_DEDUPLICATED: job_id=%s file_id=%s already in progress",
                    db_id_str, file_id_str
                )
                return existing_job

            fut = None
            if create_future:
                try:
                    fut = asyncio.get_running_loop().create_future()
                except RuntimeError:
                    pass

            pref_client_id = None
            if message is not None:
                msg_client = getattr(message, "_client", None) or getattr(message, "bot", None)
                pref_client_id = getattr(msg_client, "client_index", 0) if msg_client is not None else 0
            elif file_info and file_info.get("source_bot_index") is not None:
                pref_client_id = file_info.get("source_bot_index")
            elif user_id:
                pref_client_id = 0

            job = StorageJob(
                db_id=db_id_str,
                file_id=file_id_str,
                file_name=file_name,
                file_size=int(file_size),
                file_info=dict(file_info),
                message=message,
                user_id=user_id,
                session_id=session_id,
                target_channel_id=target_channel_id,
                storage_mode=storage_mode,
                max_retries=retries,
                future=fut,
                job_id=job_id_val,
                storage_operation_key=job_id_val,
                folder_id=file_info.get("folder_id") if file_info else None,
                media_group_id=str(file_info.get("media_group_id")) if (file_info and file_info.get("media_group_id") is not None) else None,
                preferred_client_id=pref_client_id,
            )

            if self.is_session_cancelled(session_id):
                job.status = StorageStatus.CANCELLED
                if fut and not fut.done():
                    fut.cancel()
                self.total_cancelled_jobs += 1
                return job

            self._in_progress_jobs[db_id_str] = job

        # 1. Persist job in MongoDB storage_jobs (durable state)
        try:
            if hasattr(self.db, "create_storage_job"):
                media_group_id = file_info.get("media_group_id") if file_info else None
                folder_id = file_info.get("folder_id") if file_info else None
                doc = await self.db.create_storage_job(
                    file_id=file_id_str,
                    db_id=db_id_str,
                    user_id=user_id,
                    folder_id=folder_id,
                    folder_session_id=session_id,
                    is_folder_file=bool(session_id or folder_id),
                    media_group_id=str(media_group_id) if media_group_id is not None else None,
                    max_attempts=retries,
                    preferred_client_id=pref_client_id,
                    metadata={"file_name": file_name, "file_size": file_size, "storage_mode": storage_mode},
                )
                if doc:
                    job.job_id = doc.get("job_id", job_id_val)
                    job.storage_operation_key = doc.get("storage_operation_key", job_id_val)
        except Exception as p_err:
            logger.warning("[STORAGE] Failed to persist storage_job in MongoDB for %s: %s", db_id_str, p_err)

        # 2. Claim job atomically in MongoDB
        try:
            if hasattr(self.db, "claim_storage_job"):
                claim_doc = await self.db.claim_storage_job(
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    job_id=job.job_id,
                )
                if claim_doc:
                    job.status = StorageStatus.CLAIMED
                    job.worker_id = self.worker_id
                    job.lease_until = claim_doc.get("lease_until")
        except Exception as c_err:
            logger.warning("[STORAGE] Failed to claim storage_job %s: %s", job.job_id, c_err)

        # 3. Update file document in MongoDB with initial pending status
        try:
            from bson import ObjectId
            if ObjectId.is_valid(db_id_str):
                await self.db.file.update_one(
                    {"_id": ObjectId(db_id_str), "storage_status": {"$ne": "stored"}},
                    {"$set": {"storage_status": "pending"}},
                )
        except Exception:
            pass

        # 4. Put into local execution queue with backpressure handling
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            try:
                await asyncio.wait_for(self._queue.put(job), timeout=0.1)
            except asyncio.TimeoutError:
                if hasattr(self.db, "release_claimed_job"):
                    try:
                        await self.db.release_claimed_job(job.job_id, self.worker_id)
                    except Exception:
                        pass
                async with self._lock:
                    self._in_progress_jobs.pop(db_id_str, None)
                    if fut and not fut.done():
                        fut.cancel()
                raise RuntimeError(f"Storage queue full ({self.max_queue_size}); backpressure rejection")

        self.storage_queue_peak = max(self.storage_queue_peak, self._queue.qsize())
        logger.info(
            "[STORAGE] STORAGE_JOB_QUEUED: job_id=%s file_id=%s queue_size=%d user_id=%s session=%s",
            job.db_id, job.file_id, self._queue.qsize(), job.user_id, job.session_id
        )
        return job

    async def store_file(
        self,
        db_id: str,
        file_id: str,
        file_name: str,
        file_size: int,
        file_info: dict,
        message: Message | None = None,
        user_id: int | None = None,
        target_channel_id: int | None = None,
        storage_mode: str = "main",
        session_id: str | None = None,
        max_retries: int | None = None,
    ) -> StorageResult:
        """
        Synchronous/cooperative interface to store a file via StorageService.
        If background workers are running, waits on the job's future.
        If workers are not active, executes _process_job directly.
        Returns a StorageResult object.
        """
        if self.is_session_cancelled(session_id):
            logger.info("[STORAGE] Skipping store_file for already cancelled session %s", session_id)
            async with self._lock:
                self.total_cancelled_jobs += 1
            return StorageResult(success=False, file_id=str(file_id), db_id=str(db_id), error="Session cancelled")

        if not self._shutting_down:
            self.ensure_started()

        retries = max_retries if max_retries is not None else getattr(Telegram, "FLOG_MAX_RETRIES", 5)
        has_workers = self._started and any(not w.done() for w in self._workers)
        if has_workers:
            try:
                job = await self.enqueue_storage(
                    db_id=db_id,
                    file_id=file_id,
                    file_name=file_name,
                    file_size=file_size,
                    file_info=file_info,
                    message=message,
                    user_id=user_id,
                    target_channel_id=target_channel_id,
                    storage_mode=storage_mode,
                    session_id=session_id,
                    max_retries=retries,
                    create_future=True,
                )
            except Exception as eq_err:
                logger.warning("[STORAGE] Failed to enqueue storage job %s: %s", db_id, eq_err)
                return StorageResult(success=False, file_id=str(file_id), db_id=str(db_id), error=str(eq_err))

            if job.status == StorageStatus.CANCELLED:
                return StorageResult(success=False, file_id=str(file_id), db_id=str(db_id), error="Job cancelled")
            try:
                if job.future:
                    await job.future
            except asyncio.CancelledError:
                return StorageResult(success=False, file_id=str(file_id), db_id=str(db_id), error="Cancelled")
            except Exception as exc:
                logger.warning("[STORAGE] Error waiting for storage job %s: %s", db_id, exc)
                return StorageResult(success=False, file_id=str(file_id), db_id=str(db_id), error=str(exc))

            success = (job.status == StorageStatus.STORED)
            return StorageResult(
                success=success,
                file_id=str(file_id),
                db_id=str(db_id),
                message_id=job.log_msg_id,
                client_id=job.client_id,
                error=job.error if not success else None,
            )
        else:
            db_id_str = str(db_id)
            async with self._lock:
                if db_id_str in self._in_progress_jobs:
                    job = self._in_progress_jobs[db_id_str]
                else:
                    pref_client_id = None
                    if message is not None:
                        msg_client = getattr(message, "_client", None) or getattr(message, "bot", None)
                        pref_client_id = getattr(msg_client, "client_index", 0) if msg_client is not None else 0
                    job = StorageJob(
                        db_id=db_id_str,
                        file_id=str(file_id),
                        file_name=file_name,
                        file_size=int(file_size),
                        file_info=dict(file_info),
                        message=message,
                        user_id=user_id,
                        session_id=session_id,
                        target_channel_id=target_channel_id,
                        storage_mode=storage_mode,
                        max_retries=retries,
                        preferred_client_id=pref_client_id,
                    )
                    self._in_progress_jobs[db_id_str] = job
            success = await self._process_job(job)
            return StorageResult(
                success=success,
                file_id=str(file_id),
                db_id=str(db_id),
                message_id=job.log_msg_id,
                client_id=job.client_id,
                error=job.error if not success else None,
            )

    async def reconcile_unsettled_files(self) -> int:
        """
        Reconcile files left in intermediate states ('pending', 'processing', 'retrying')
        after an unexpected process crash/restart.
        If a file document has a valid flog_msg_id, settle it as STORED.
        Otherwise, mark as FAILED with an informative error.
        """
        if not self.db:
            return 0
        count = 0
        try:
            # 1. Any file with flog_msg_id but non-stored status -> promote to STORED
            stored_res = await self.db.file.update_many(
                {
                    "storage_status": {"$in": [StorageStatus.PENDING, StorageStatus.PROCESSING, StorageStatus.RETRYING]},
                    "flog_msg_id": {"$exists": True, "$ne": None},
                },
                {"$set": {"storage_status": StorageStatus.STORED}},
            )
            count += getattr(stored_res, "modified_count", 0)

            # 2. Any file without flog_msg_id in intermediate status -> mark as FAILED
            failed_res = await self.db.file.update_many(
                {
                    "storage_status": {"$in": [StorageStatus.PENDING, StorageStatus.PROCESSING, StorageStatus.RETRYING]},
                    "$or": [
                        {"flog_msg_id": {"$exists": False}},
                        {"flog_msg_id": None},
                    ],
                },
                {"$set": {"storage_status": StorageStatus.FAILED, "storage_error": "Interrupted by process restart"}},
            )
            count += getattr(failed_res, "modified_count", 0)
            if count > 0:
                logger.info("[STORAGE] Reconciled %d unsettled file records on startup", count)
        except Exception as err:
            logger.warning("[STORAGE] Failed to reconcile unsettled files: %s", err)
        return count

    def ensure_started(self) -> None:
        """Ensure background worker pool and scheduler/recovery/heartbeat tasks are active."""
        if self._started and any(not w.done() for w in self._workers):
            if self._scheduler_task is None or self._scheduler_task.done():
                self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            if self._recovery_task is None or self._recovery_task.done():
                self._recovery_task = asyncio.create_task(self._recovery_loop())
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return

        configured_workers = getattr(Telegram, "FLOG_STORAGE_WORKERS", 6)
        active_count = len(self._get_active_clients())
        num_workers = min(configured_workers, max(active_count * self.concurrency_per_client, self._workers_min))
        num_workers = max(num_workers, 1)

        self._workers = [
            asyncio.create_task(self._worker_loop(f"flog-worker-{i}"))
            for i in range(num_workers)
        ]
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._started = True
        logger.info("Started FLOG storage worker pool with %d concurrent workers (worker_id=%s)", num_workers, self.worker_id)
        try:
            loop = asyncio.get_running_loop()
            t1 = loop.create_task(self.reconcile_unsettled_files())
            self._background_tasks.add(t1)
            t1.add_done_callback(self._background_tasks.discard)
            if hasattr(self.db, "register_worker_health"):
                t2 = loop.create_task(self.db.register_worker_health(self.worker_id, socket.gethostname(), os.getpid()))
                self._background_tasks.add(t2)
                t2.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            pass

    async def startup_recovery(self) -> dict:
        """
        Runs on startup to ensure database indexes, recover expired leases,
        reconcile FLOG storage jobs, and reconcile unsettled files.
        """
        results = {
            "indexes_ensured": False,
            "expired_leases_recovered": 0,
            "flog_jobs_reconciled": 0,
            "unsettled_files_reconciled": 0,
            "expired_jobs_cleaned": 0,
            "worker_health_registered": False,
        }
        if not self.db:
            return results

        try:
            if hasattr(self.db, "ensure_indexes"):
                await self.db.ensure_indexes()
                results["indexes_ensured"] = True
        except Exception as e:
            logger.warning("[STORAGE] Failed to ensure indexes: %s", e)

        try:
            if hasattr(self.db, "register_worker_health"):
                await self.db.register_worker_health(self.worker_id, socket.gethostname(), os.getpid())
                results["worker_health_registered"] = True
        except Exception as e:
            logger.warning("[STORAGE] Failed to register worker health: %s", e)

        try:
            if hasattr(self.db, "recover_expired_leases"):
                recovered = await self.db.recover_expired_leases()
                results["expired_leases_recovered"] = recovered
                if recovered:
                    logger.info("[STORAGE] Startup recovered %d expired job leases", recovered)
        except Exception as e:
            logger.warning("[STORAGE] Failed to recover expired leases: %s", e)

        try:
            if hasattr(self.db, "reconcile_flog_storage_jobs"):
                reconciled = await self.db.reconcile_flog_storage_jobs()
                results["flog_jobs_reconciled"] = reconciled
                if reconciled:
                    logger.info("[STORAGE] Startup reconciled %d flog storage jobs", reconciled)
        except Exception as e:
            logger.warning("[STORAGE] Failed to reconcile flog storage jobs: %s", e)

        try:
            results["unsettled_files_reconciled"] = await self.reconcile_unsettled_files()
        except Exception as e:
            logger.warning("[STORAGE] Failed to reconcile unsettled files: %s", e)

        try:
            if hasattr(self.db, "clean_expired_terminal_jobs"):
                cleaned = await self.db.clean_expired_terminal_jobs()
                results["expired_jobs_cleaned"] = cleaned
        except Exception as e:
            logger.warning("[STORAGE] Failed to clean expired jobs: %s", e)

        try:
            from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry
            get_circuit_breaker_registry().reset_all()
            for stat in self._client_stats.values():
                stat.cannot_access_flog = False
                stat.consecutive_failures = 0
            results["circuit_breakers_reset"] = True
            logger.info("[STORAGE] Startup reset all client circuit breakers to CLOSED")
        except Exception as cb_err:
            logger.debug("[STORAGE] Startup circuit breaker reset error: %s", cb_err)

        self.ensure_started()
        return results

    async def _heartbeat_loop(self) -> None:
        """Periodic heartbeat maintaining durable worker health in MongoDB."""
        logger.debug("[STORAGE] Heartbeat loop started for worker %s", self.worker_id)
        while not self._shutting_down:
            try:
                interval = getattr(Telegram, "FLOG_WORKER_HEARTBEAT_INTERVAL_SECONDS", 5.0)
                await asyncio.sleep(interval)
                if self._shutting_down:
                    break

                if hasattr(self.db, "update_worker_heartbeat"):
                    active_job_id = next(iter(self._in_progress_jobs.keys())) if self._in_progress_jobs else None
                    await self.db.update_worker_heartbeat(
                        self.worker_id,
                        health_state="HEALTHY",
                        current_job_id=active_job_id,
                        draining=self._shutting_down,
                    )

                if hasattr(self.db, "mark_stale_workers_unhealthy"):
                    stale_timeout = getattr(Telegram, "FLOG_WORKER_STALE_TIMEOUT_SECONDS", 30.0)
                    stale_cnt = await self.db.mark_stale_workers_unhealthy(timeout_seconds=stale_timeout)
                    if stale_cnt > 0:
                        logger.warning("[STORAGE] Marked %d stale workers UNHEALTHY", stale_cnt)

                # Record metrics snapshot & evaluate system health for operational alerting
                try:
                    from FileStream.core.monitoring.metrics import get_metrics
                    get_metrics().record_snapshot(
                        active_jobs=len(self._in_progress_jobs),
                        queue_depth=self.storage_queue_size,
                    )
                except Exception:
                    pass

                try:
                    from FileStream.core.monitoring.alert_manager import get_alert_manager
                    await get_alert_manager().evaluate_system_health(self.db, self)
                except Exception:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[STORAGE] Heartbeat loop error: %s", e)
        logger.debug("[STORAGE] Heartbeat loop stopped for worker %s", self.worker_id)

    async def _scheduler_loop(self) -> None:
        """
        Background scheduler that polls MongoDB for claimable jobs and feeds them
        into the local execution queue, bounded by queue capacity and batch size.
        """
        logger.debug("[STORAGE] Scheduler loop started for worker %s", self.worker_id)
        while not self._shutting_down:
            try:
                available_capacity = self.max_queue_size - self._queue.qsize()
                if available_capacity > 0 and hasattr(self.db, "claim_storage_job"):
                    fetch_count = min(self.batch_size, available_capacity)
                    for _ in range(fetch_count):
                        if self._shutting_down or self._queue.full():
                            break
                        doc = await self.db.claim_storage_job(self.worker_id, lease_seconds=self.lease_seconds)
                        if not doc:
                            break
                        db_id = str(doc.get("db_id") or doc.get("file_id") or "")
                        # Check if already in progress locally
                        async with self._lock:
                            if db_id in self._in_progress_jobs:
                                continue
                            meta = doc.get("metadata") or {}
                            file_info = doc.get("file_info") or {}
                            file_name = meta.get("file_name") or file_info.get("file_name") or "file"
                            file_size = int(meta.get("file_size") or file_info.get("file_size") or 0)
                            preferred_client_id = doc.get("preferred_client_id")
                            if preferred_client_id is None:
                                preferred_client_id = file_info.get("source_bot_index")
                            if preferred_client_id is None and doc.get("user_id"):
                                preferred_client_id = 0

                            job = StorageJob(
                                db_id=db_id,
                                file_id=str(doc.get("file_id") or db_id),
                                file_name=file_name,
                                file_size=file_size,
                                file_info=file_info,
                                user_id=doc.get("user_id"),
                                session_id=doc.get("folder_session_id"),
                                target_channel_id=doc.get("flog_channel_id"),
                                storage_mode=meta.get("storage_mode", "main"),
                                status=StorageStatus.CLAIMED,
                                retry_count=max(0, int(doc.get("attempts", 1)) - 1),
                                max_retries=int(doc.get("max_attempts", 3)),
                                job_id=doc.get("job_id", f"{db_id}:STORE_TO_FLOG"),
                                storage_operation_key=doc.get("storage_operation_key", f"{db_id}:STORE_TO_FLOG"),
                                folder_id=doc.get("folder_id"),
                                media_group_id=doc.get("media_group_id"),
                                worker_id=self.worker_id,
                                preferred_client_id=preferred_client_id,
                                lease_until=doc.get("lease_until"),
                            )
                            self._in_progress_jobs[db_id] = job

                        try:
                            self._queue.put_nowait(job)
                            self.storage_queue_peak = max(self.storage_queue_peak, self._queue.qsize())
                        except asyncio.QueueFull:
                            if hasattr(self.db, "release_claimed_job"):
                                try:
                                    await self.db.release_claimed_job(job.job_id, self.worker_id)
                                except Exception:
                                    pass
                            async with self._lock:
                                self._in_progress_jobs.pop(db_id, None)
                            break

                # Dynamic worker auto-scaling
                self._scale_workers(self._queue.qsize())

                # Anti-starvation priority aging
                now_ts = time.time()
                if (now_ts - self._last_priority_aging_time) >= getattr(Telegram, "FLOG_PRIORITY_AGING_SECONDS", 60.0):
                    self._last_priority_aging_time = now_ts
                    if hasattr(self.db, "apply_priority_aging"):
                        try:
                            await self.db.apply_priority_aging(aging_threshold_seconds=getattr(Telegram, "FLOG_PRIORITY_AGING_SECONDS", 60.0))
                        except Exception:
                            pass

                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[STORAGE] Scheduler loop error: %s", e)
                await asyncio.sleep(self.poll_interval)
        logger.debug("[STORAGE] Scheduler loop stopped for worker %s", self.worker_id)

    async def _recovery_loop(self) -> None:
        """Background task recovering expired leases from dead/crashed processes."""
        logger.debug("[STORAGE] Recovery loop started")
        while not self._shutting_down:
            try:
                await asyncio.sleep(self.recovery_interval)
                if self._shutting_down:
                    break
                if hasattr(self.db, "recover_expired_leases"):
                    recovered = await self.db.recover_expired_leases()
                    if recovered > 0:
                        logger.info("[STORAGE] Recovered %d expired job leases", recovered)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[STORAGE] Recovery loop error: %s", e)
        logger.debug("[STORAGE] Recovery loop stopped")

    async def _worker_loop(self, name: str) -> None:
        """Background worker that continuously processes storage jobs."""
        logger.debug("Storage worker %s started", name)
        while True:
            if self._shutting_down and self._queue.empty():
                break
            if self.is_worker_draining(name):
                logger.info("Storage worker %s is draining; exiting loop", name)
                self._draining_workers.discard(name)
                try:
                    await self.db.set_worker_health_state(name, state="DRAINING", draining=True)
                except Exception:
                    pass
                break
            try:
                try:
                    job = await asyncio.wait_for(self._queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    if self._shutting_down:
                        break
                    continue

                try:
                    await self._process_job(job)
                except Exception as exc:
                    logger.error("Unexpected error in storage worker %s for job %s: %s", name, job.db_id, exc)
                    async with self._lock:
                        self.total_failed_jobs += 1
                        self._in_progress_jobs.pop(job.db_id, None)
                    if job.future and not job.future.done():
                        job.future.set_exception(exc)
                finally:
                    self._queue.task_done()

                # Check draining AFTER task_done (outside finally to avoid SyntaxWarning)
                if self.is_worker_draining(name):
                    logger.info("Storage worker %s drained after job completion; exiting loop", name)
                    self._draining_workers.discard(name)
                    try:
                        await self.db.set_worker_health_state(name, state="DRAINING", draining=True)
                    except Exception:
                        pass
                    break

            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.debug("Storage worker %s loop exception: %s", name, loop_err)
                await asyncio.sleep(0.1)

        logger.debug("Storage worker %s stopped", name)

    @staticmethod
    def _is_permanent_error(exc: Exception) -> bool:
        """Determine if an exception represents a permanent Telegram channel/API permission error."""
        name = exc.__class__.__name__
        msg = str(exc).lower()
        permanent_types = (
            "ChatWriteForbidden",
            "ChannelPrivate",
            "ChatAdminRequired",
            "UserIsBlocked",
            "UserBannedInChannel",
            "PeerIdInvalid",
        )
        if name in permanent_types:
            return True
        if any(err in msg for err in (
            "chat_write_forbidden",
            "channel_private",
            "chat_admin_required",
            "peer_id_invalid",
            "user_banned_in_channel",
        )):
            return True
        return False

    @staticmethod
    def _is_fatal_client_error(exc: Exception) -> bool:
        """
        Determine if an exception represents a fatal MTProto client-level failure
        (e.g. account deactivated, session revoked, bot token invalid).
        Payload, channel, or rate-limit errors (MediaEmpty, FloodWait) are NOT fatal to the client.
        """
        name = exc.__class__.__name__
        msg = str(exc).lower()
        fatal_types = (
            "Unauthorized",
            "UserDeactivated",
            "UserDeactivatedBan",
            "AuthKeyUnregistered",
            "AuthKeyDuplicated",
            "SessionRevoked",
            "SessionExpired",
            "BotMethodInvalid",
        )
        if name in fatal_types:
            return True
        fatal_patterns = (
            "unauthorized",
            "user_deactivated",
            "auth_key_unregistered",
            "session_revoked",
            "session_expired",
            "bot_method_invalid",
        )
        if any(p in msg for p in fatal_patterns):
            return True
        return False

    async def _process_job(self, job: StorageJob) -> bool:
        """Execute the actual storage forward to Telegram FLOG channel."""
        from FileStream.core.media.file_properties import send_file, update_file_id

        job_id = job.job_id or f"{job.db_id}:STORE_TO_FLOG"

        # Pre-check cancellation
        if job.status == StorageStatus.CANCELLED or self.is_session_cancelled(job.session_id):
            job.status = StorageStatus.CANCELLED
            if job.future and not job.future.done():
                job.future.cancel()
            async with self._lock:
                self.total_cancelled_jobs += 1
                self._in_progress_jobs.pop(job.db_id, None)
            if hasattr(self.db, "cancel_storage_job"):
                try:
                    await self.db.cancel_storage_job(job_id, reason="Cancelled before start")
                except Exception:
                    pass
            logger.info("[STORAGE] STORAGE_CANCELLED: job_id=%s session=%s", job.db_id, job.session_id)
            return False

        # Idempotency check: if media was already stored to FLOG (e.g. from prior attempt or DB record), don't duplicate!
        try:
            from bson import ObjectId
            if ObjectId.is_valid(job.db_id):
                existing_doc = await self.db.file.find_one({"_id": ObjectId(job.db_id)})
                if existing_doc and existing_doc.get("flog_msg_id"):
                    job.status = StorageStatus.STORED
                    job.log_msg_id = existing_doc["flog_msg_id"]
                    if job.future and not job.future.done():
                        class _StoredMsgRef:
                            def __init__(self, mid):
                                self.id = mid
                        job.future.set_result(_StoredMsgRef(job.log_msg_id))
                    async with self._lock:
                        self.total_successful_jobs += 1
                        self._in_progress_jobs.pop(job.db_id, None)
                    if hasattr(self.db, "mark_job_stored"):
                        try:
                            await self.db.mark_job_stored(job_id, flog_msg_id=job.log_msg_id, flog_channel_id=existing_doc.get("flog_channel_id"))
                        except Exception:
                            pass
                    logger.info("[STORAGE] STORAGE_SUCCESS: job_id=%s file_id=%s (already stored in FLOG msg_id=%s)", job.db_id, job.file_id, job.log_msg_id)
                    return True
        except Exception:
            pass

        job.status = StorageStatus.RUNNING
        logger.info(
            "[STORAGE] STORAGE_JOB_STARTED: job_id=%s file_id=%s user_id=%s session=%s queue_size=%d in_flight=%d",
            job.db_id, job.file_id, job.user_id, job.session_id, self._queue.qsize(),
            sum(s.current_inflight for s in self._client_stats.values())
        )

        # Mark RUNNING in MongoDB
        if hasattr(self.db, "mark_job_running"):
            try:
                await self.db.mark_job_running(job_id, self.worker_id, lease_seconds=self.lease_seconds)
            except Exception as r_err:
                logger.debug("[STORAGE] Failed to mark job running in DB for %s: %s", job_id, r_err)

        # Start lease renewer for long operations
        renewer = LeaseRenewer(self.db, job_id, self.worker_id, self.lease_seconds)
        renewer.start()

        # Resolve and reserve target client atomically
        selection = None
        excluded: set[int] = set()
        while selection is None:
            if job.status == StorageStatus.CANCELLED or self.is_session_cancelled(job.session_id):
                job.status = StorageStatus.CANCELLED
                if job.future and not job.future.done():
                    job.future.cancel()
                async with self._lock:
                    self.total_cancelled_jobs += 1
                    self._in_progress_jobs.pop(job.db_id, None)
                await renewer.stop()
                if hasattr(self.db, "cancel_storage_job"):
                    try:
                        await self.db.cancel_storage_job(job_id, reason="Cancelled while reserving client")
                    except Exception:
                        pass
                logger.info("[STORAGE] STORAGE_CANCELLED: job_id=%s session=%s", job.db_id, job.session_id)
                return False

            selection = await self.reserve_client(
                preferred_id=job.preferred_client_id,
                exclude_indices=excluded if len(excluded) < len(self._clients_dict) else None
            )
            if selection is not None:
                break

            if self._shutting_down:
                break

            now = time.monotonic()
            active_clients = self._get_active_clients()
            all_in_cooldown = bool(active_clients) and all(
                self._client_stats.get(idx) and self._client_stats[idx].cooldown_until > now
                for idx, _ in active_clients
            )
            if all_in_cooldown:
                wait_delay = min(max(self.get_earliest_cooldown(), 0.05), 60.0)
                logger.warning(
                    "[STORAGE] STORAGE_ALL_CLIENTS_COOLDOWN FLOG_COOLDOWN_ALL: job_id=%s all clients in cooldown, waiting %.2fs",
                    job.db_id, wait_delay
                )
            else:
                wait_delay = 0.05
                logger.debug("[STORAGE] Storage clients busy or in cooldown; worker waiting %.2fs", wait_delay)
            await asyncio.sleep(wait_delay)
            excluded.clear()

        if selection is None:
            await renewer.stop()
            if job.future and not job.future.done():
                job.future.cancel()
            async with self._lock:
                self.total_failed_jobs += 1
                self._in_progress_jobs.pop(job.db_id, None)
            if hasattr(self.db, "mark_job_failed"):
                try:
                    await self.db.mark_job_failed(job_id, error="No available clients", error_type="NoClientAvailable")
                except Exception:
                    pass
            return False

        client_idx, client, stat = selection
        job.client_id = client_idx
        if hasattr(self.db, "mark_job_running"):
            try:
                await self.db.mark_job_running(job_id, self.worker_id, assigned_client_id=client_idx, lease_seconds=self.lease_seconds)
            except Exception:
                pass
        logger.info(
            "[STORAGE] STORAGE_CLIENT_SELECTED FLOG_CLIENT_SELECTED: job_id=%s client=%s inflight=%s",
            job.db_id, client_idx, stat.current_inflight
        )

        channel_id = None
        try:
            async with stat.semaphore:
                # Critical check: Did client enter FloodWait cooldown while waiting for semaphore?
                if stat.cooldown_until > time.monotonic():
                    logger.warning(
                        "[STORAGE] STORAGE_FAILOVER: Client %s entered cooldown while waiting for semaphore; failover job %s",
                        client_idx, job.db_id
                    )
                    await self._queue.put(job)
                    return False

                if job.status == StorageStatus.CANCELLED or self.is_session_cancelled(job.session_id):
                    job.status = StorageStatus.CANCELLED
                    if job.future and not job.future.done():
                        job.future.cancel()
                    async with self._lock:
                        self.total_cancelled_jobs += 1
                        self._in_progress_jobs.pop(job.db_id, None)
                    if hasattr(self.db, "cancel_storage_job"):
                        try:
                            await self.db.cancel_storage_job(job_id, reason="Cancelled waiting for semaphore")
                        except Exception:
                            pass
                    logger.info("[STORAGE] STORAGE_CANCELLED: job_id=%s session=%s", job.db_id, job.session_id)
                    return False

                logger.debug(
                    "[STORAGE] Worker routing file %s to client %s (inflight: %d)",
                    job.db_id, client_idx, stat.current_inflight
                )

                log_msg = await send_file(
                    client,
                    job.db_id,
                    job.file_id,
                    message=job.message,
                    file_name=job.file_name,
                    file_info=job.file_info,
                )

                # Post-send check: Did session cancel while send_file was in-flight?
                channel_id = job.target_channel_id or resolve_file_flog_channel_id(job.file_info)
                if self.is_session_cancelled(job.session_id) or job.status == StorageStatus.CANCELLED:
                    logger.warning("[STORAGE] Job %s finished send after session %s cancelled. Purging late message.", job.db_id, job.session_id)
                    if log_msg and hasattr(log_msg, "id"):
                        try:
                            ch_id = int(channel_id) if channel_id is not None else int(getattr(Telegram, "ULOG_CHANNEL", 0) or 0)
                            await client.delete_messages(ch_id, int(log_msg.id))
                        except Exception as purge_err:
                            logger.warning("[STORAGE] Failed to delete late message: %s", purge_err)
                    try:
                        from bson import ObjectId
                        if ObjectId.is_valid(job.db_id):
                            await self.db.file.delete_one({"_id": ObjectId(job.db_id)})
                    except Exception:
                        pass
                    job.status = StorageStatus.CANCELLED
                    if job.future and not job.future.done():
                        job.future.cancel()
                    async with self._lock:
                        self.total_cancelled_jobs += 1
                        self._in_progress_jobs.pop(job.db_id, None)
                    if hasattr(self.db, "cancel_storage_job"):
                        try:
                            await self.db.cancel_storage_job(job_id, reason="Cancelled during upload")
                        except Exception:
                            pass
                    return False

                # Success
                async with self._lock:
                    stat.successful_sends += 1
                    stat.consecutive_failures = 0
                    stat.last_success_time = time.monotonic()
                    self.total_successful_jobs += 1
                    self._in_progress_jobs.pop(job.db_id, None)

                get_circuit_breaker_registry().record_success(client_idx)
                if channel_id:
                    try:
                        from FileStream.core.runtime.flog_channels import get_channel_monitor
                        get_channel_monitor().record_success(channel_id)
                    except Exception:
                        pass
                if hasattr(self.db, "update_client_health"):
                    try:
                        await self.db.update_client_health(client_idx, status="HEALTHY", failure_count=0)
                    except Exception:
                        pass
                if hasattr(self.db, "record_worker_success"):
                    try:
                        await self.db.record_worker_success(self.worker_id)
                    except Exception:
                        pass
                get_metrics().record_job_completed(
                    latency_seconds=time.monotonic() - job.created_at,
                    client_id=client_idx,
                    bytes_count=job.file_size,
                )

                job.status = StorageStatus.STORED
                job.log_msg_id = getattr(log_msg, "id", None)

                if hasattr(self.db, "mark_job_stored"):
                    try:
                        await self.db.mark_job_stored(
                            job_id,
                            flog_msg_id=job.log_msg_id,
                            flog_channel_id=int(channel_id) if channel_id not in (None, "") else None,
                        )
                    except Exception as s_err:
                        logger.debug("[STORAGE] Failed to mark job stored in DB: %s", s_err)

                try:
                    from bson import ObjectId
                    if ObjectId.is_valid(job.db_id):
                        updates = {
                            "storage_status": "stored",
                        }
                        if job.log_msg_id is not None:
                            updates["flog_msg_id"] = int(job.log_msg_id)
                        if channel_id not in (None, ""):
                            updates["flog_channel_id"] = int(channel_id)
                        if not log_msg:
                            c_id = getattr(client, "id", None) or getattr(getattr(client, "me", None), "id", None)
                            c_id_key = str(c_id) if c_id is not None else str(client_idx)
                            curr_file_ids = dict((job.file_info or {}).get("file_ids") or {})
                            curr_file_ids[c_id_key] = job.file_id
                            updates["file_ids"] = curr_file_ids
                        await self.db.file.update_one(
                            {"_id": ObjectId(job.db_id)},
                            {"$set": updates},
                        )
                except Exception:
                    pass

                # Multi-client update if multiple clients configured
                if len(self._clients_dict) > 1 and log_msg and hasattr(log_msg, "id"):
                    try:
                        file_ids = await update_file_id(log_msg.id, self._clients_dict, channel_id=channel_id)
                        if file_ids:
                            await self.db.update_file_ids(job.db_id, file_ids)
                    except Exception as mc_err:
                        logger.debug("Multi-client update failed for file %s: %s", job.db_id, mc_err)

                if job.future and not job.future.done():
                    if log_msg is not None:
                        job.future.set_result(log_msg)
                    else:
                        class _DirectMsgRef:
                            def __init__(self, fid):
                                self.id = None
                                self.file_id = fid
                        job.future.set_result(_DirectMsgRef(job.file_id))

                logger.info(
                    "[STORAGE] STORAGE_SUCCESS FLOG_STORAGE_SUCCESS: job_id=%s client=%s channel=%s msg_id=%s",
                    job.db_id, client_idx, channel_id, job.log_msg_id
                )
                return True

        except Exception as exc:
            if is_floodwait(exc):
                wait_s = get_floodwait_delay(exc)
                safety_margin = getattr(Telegram, "FLOG_CLIENT_COOLDOWN_MARGIN", 1.0)
                cooldown_dur = wait_s + safety_margin
                async with self._lock:
                    stat.floodwait_count += 1
                    stat.cooldown_until = max(stat.cooldown_until, time.monotonic() + cooldown_dur)
                get_circuit_breaker_registry().record_failure(client_idx, exc, is_floodwait=True, floodwait_seconds=cooldown_dur)
                get_metrics().record_floodwait()
                if hasattr(self.db, "update_client_health"):
                    try:
                        await self.db.update_client_health(client_idx, status="COOLDOWN", cooldown_until=time.time() + cooldown_dur, last_error=f"FloodWait: {wait_s}s")
                    except Exception:
                        pass
                logger.warning(
                    "[STORAGE] STORAGE_FLOODWAIT FLOG_FLOODWAIT: job_id=%s client=%s wait_sec=%s margin=%s. Cooldown armed.",
                    job.db_id, client_idx, wait_s, safety_margin
                )
                logger.info(
                    "[STORAGE] STORAGE_CLIENT_COOLDOWN: client=%s cooldown_seconds=%.2f",
                    client_idx, cooldown_dur
                )
                logger.info(
                    "[STORAGE] STORAGE_FAILOVER: job_id=%s from_client=%s to secondary/next available",
                    job.db_id, client_idx
                )
                try:
                    from FileStream.core.runtime.client_balance import record_client_cooldown
                    record_client_cooldown(client_idx, wait_s)
                except Exception:
                    pass
            else:
                async with self._lock:
                    stat.failed_sends += 1
                    stat.consecutive_failures += 1
                    stat.last_failure_time = time.monotonic()
                get_metrics().record_job_failed(client_id=client_idx)
                is_fatal = self._is_fatal_client_error(exc)
                is_perm_channel = self._is_permanent_error(exc)
                get_circuit_breaker_registry().record_failure(client_idx, exc, is_fatal=is_fatal)
                if hasattr(self.db, "update_client_health"):
                    try:
                        status_str = "UNHEALTHY" if is_fatal else get_circuit_breaker_registry().get(client_idx).state.value
                        await self.db.update_client_health(client_idx, status=status_str, failure_count=stat.consecutive_failures, last_error=str(exc))
                    except Exception:
                        pass
                if hasattr(self.db, "record_worker_error"):
                    try:
                        await self.db.record_worker_error(self.worker_id, str(exc))
                    except Exception:
                        pass
                logger.warning("[STORAGE] Send failed on client %s for file %s: %s", client_idx, job.db_id, exc)
                if is_fatal:
                    stat.cannot_access_flog = True
                    logger.critical("[STORAGE] Client %s encountered fatal MTProto account error: %s", client_idx, exc)
                elif is_perm_channel:
                    stat.cannot_access_flog = True
                    logger.error("[STORAGE] Client %s encountered permanent channel access error: %s", client_idx, exc)
                    if channel_id:
                        try:
                            from FileStream.core.runtime.flog_channels import get_channel_monitor
                            get_channel_monitor().record_failure(channel_id, str(exc))
                        except Exception:
                            pass

            if self.is_session_cancelled(job.session_id):
                job.status = StorageStatus.CANCELLED
                if job.future and not job.future.done():
                    job.future.cancel()
                async with self._lock:
                    self.total_cancelled_jobs += 1
                    self._in_progress_jobs.pop(job.db_id, None)
                if hasattr(self.db, "cancel_storage_job"):
                    try:
                        await self.db.cancel_storage_job(job_id, reason="Session cancelled during failure")
                    except Exception:
                        pass
                logger.info("[STORAGE] STORAGE_CANCELLED: job_id=%s session=%s", job.db_id, job.session_id)
                return False

            # Permanent errors terminate immediately without retries
            if not is_floodwait(exc) and self._is_permanent_error(exc):
                job.status = StorageStatus.FAILED
                job.error = str(exc)
                async with self._lock:
                    self.total_failed_jobs += 1
                    self._in_progress_jobs.pop(job.db_id, None)
                logger.error("[STORAGE] STORAGE_FAILED FLOG_STORAGE_FAILED: job_id=%s failed permanently due to channel error: %s", job.db_id, exc)
                if hasattr(self.db, "mark_job_failed"):
                    try:
                        await self.db.mark_job_failed(job_id, error=str(exc), error_type=exc.__class__.__name__)
                    except Exception:
                        pass
                if job.future and not job.future.done():
                    job.future.set_exception(exc)
                try:
                    from bson import ObjectId
                    if ObjectId.is_valid(job.db_id):
                        await self.db.file.update_one(
                            {"_id": ObjectId(job.db_id), "storage_status": {"$ne": "stored"}},
                            {"$set": {"storage_status": "failed", "storage_error": str(exc)}},
                        )
                except Exception:
                    pass
                return False

            # Re-queue if retries remain
            job.retry_count += 1
            if job.retry_count <= job.max_retries and not self._shutting_down:
                job.status = StorageStatus.RETRYING
                logger.info(
                    "[STORAGE] STORAGE_RETRY FLOG_RETRY: job_id=%s attempt=%d/%d reason=%s",
                    job.db_id, job.retry_count, job.max_retries, "FloodWait" if is_floodwait(exc) else type(exc).__name__
                )
                delay_s = (wait_s + 1.0) if is_floodwait(exc) else 1.0
                next_retry = datetime.datetime.now(timezone.utc) + timedelta(seconds=delay_s)
                if hasattr(self.db, "schedule_job_retry"):
                    try:
                        await self.db.schedule_job_retry(
                            job_id,
                            next_retry_at=next_retry,
                            error=str(exc),
                            error_type="FloodWait" if is_floodwait(exc) else exc.__class__.__name__,
                        )
                    except Exception:
                        pass
                await self._queue.put(job)
                return False
            else:
                job.status = StorageStatus.FAILED
                job.error = str(exc)
                async with self._lock:
                    self.total_failed_jobs += 1
                    self._in_progress_jobs.pop(job.db_id, None)
                logger.error("[STORAGE] STORAGE_FAILED FLOG_STORAGE_FAILED: job_id=%s failed permanently: %s", job.db_id, exc)
                get_metrics().record_job_dead_letter()
                if hasattr(self.db, "mark_job_failed"):
                    try:
                        await self.db.mark_job_failed(
                            job_id,
                            error=str(exc),
                            error_type=exc.__class__.__name__,
                            final_client_id=client_idx,
                            attempts=job.retry_count,
                            worker_id=self.worker_id,
                        )
                    except Exception:
                        pass
                elif hasattr(self.db, "mark_job_dead_letter"):
                    try:
                        await self.db.mark_job_dead_letter(job_id, error=str(exc), error_type=exc.__class__.__name__, final_client_id=client_idx)
                    except Exception:
                        pass
                if hasattr(self.db, "record_worker_error"):
                    try:
                        await self.db.record_worker_error(self.worker_id, f"Job {job_id} exhausted retries: {exc}")
                    except Exception:
                        pass
                if hasattr(self.db, "record_audit_event"):
                    try:
                        await self.db.record_audit_event("STORAGE_JOB_DEAD_LETTER", actor="storage_worker", target_id=job_id, details={"error": str(exc), "attempts": job.retry_count})
                    except Exception:
                        pass
                if job.future and not job.future.done():
                    job.future.set_exception(exc)
                try:
                    from bson import ObjectId
                    if ObjectId.is_valid(job.db_id):
                        await self.db.file.update_one(
                            {"_id": ObjectId(job.db_id), "storage_status": {"$ne": "stored"}},
                            {"$set": {"storage_status": "failed", "storage_error": str(exc)}},
                        )
                except Exception:
                    pass
                return False

        finally:
            await renewer.stop()
            self.release_client(stat)

    async def shutdown(self, timeout: float = 5.0) -> None:
        """
        Gracefully shutdown storage workers:
        1. Stop accepting new jobs
        2. Cancel scheduler and recovery loops
        3. Wait for pending queue items to finish up to timeout
        4. Cancel background workers
        """
        self._shutting_down = True
        logger.info("Shutting down StorageService (pending queue items: %d)...", self._queue.qsize())

        if hasattr(self.db, "set_worker_health_state"):
            try:
                await self.db.set_worker_health_state(self.worker_id, "DRAINING", draining=True)
            except Exception:
                pass

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()

        # Allow workers to drain pending work and in-flight operations
        if any(not w.done() for w in self._workers):
            start_wait = time.monotonic()
            while (not self._queue.empty() or len(self._in_progress_jobs) > 0) and (time.monotonic() - start_wait < timeout):
                await asyncio.sleep(0.05)

        # Release any uncompleted claimed jobs back to PENDING so other workers/processes can claim them
        if hasattr(self.db, "release_claimed_job"):
            for job in list(self._in_progress_jobs.values()):
                if job.status in (StorageStatus.CLAIMED, StorageStatus.RUNNING):
                    try:
                        await self.db.release_claimed_job(job.job_id, self.worker_id)
                    except Exception:
                        pass

        # Cancel workers
        for worker in self._workers:
            if not worker.done():
                worker.cancel()

        tasks_to_gather = [w for w in self._workers if not w.done()]
        if self._heartbeat_task and not self._heartbeat_task.done():
            tasks_to_gather.append(self._heartbeat_task)
        if self._scheduler_task and not self._scheduler_task.done():
            tasks_to_gather.append(self._scheduler_task)
        if self._recovery_task and not self._recovery_task.done():
            tasks_to_gather.append(self._recovery_task)

        if tasks_to_gather:
            await asyncio.gather(*tasks_to_gather, return_exceptions=True)
        self._workers.clear()
        self._heartbeat_task = None
        self._scheduler_task = None
        self._recovery_task = None
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()
        self._started = False
        self._shutting_down = False

        if hasattr(self.db, "set_worker_health_state"):
            try:
                await self.db.set_worker_health_state(self.worker_id, "STOPPED")
            except Exception:
                pass

        get_circuit_breaker_registry().reset_all()
        logger.info("StorageService shutdown complete.")

    stop = shutdown

    def record_client_floodwait(self, client_idx: int, wait_seconds: float) -> None:
        """Explicitly record a FloodWait event and put the client into cooldown."""
        client = self._clients_dict.get(client_idx)
        if client is not None:
            stat = self._ensure_client_stat(client_idx, client)
            stat.floodwait_count += 1
            stat.cooldown_until = max(stat.cooldown_until, time.monotonic() + wait_seconds)
            get_circuit_breaker_registry().record_failure(client_idx, "FloodWait", is_floodwait=True, floodwait_seconds=wait_seconds)
            get_metrics().record_floodwait()


# Shared singleton instance
storage_service = StorageService()
