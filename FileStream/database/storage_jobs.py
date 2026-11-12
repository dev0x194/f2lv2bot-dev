import datetime
import logging
import time
from typing import Any
import pymongo
from pymongo import ReturnDocument

logger = logging.getLogger(__name__)


def _is_matched(res: Any) -> bool:
    if not res:
        return False
    mc = getattr(res, "matched_count", None)
    if isinstance(mc, (int, float)):
        return mc > 0
    mod_c = getattr(res, "modified_count", None)
    if isinstance(mod_c, (int, float)):
        return mod_c > 0
    return True


def _count_modified(res: Any) -> int:
    if not res:
        return 0
    mc = getattr(res, "modified_count", 0)
    if isinstance(mc, (int, float)):
        return int(mc)
    return 1 if res else 0


def _to_timestamp(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, datetime.datetime):
        return val.timestamp()
    return time.time()


class StorageJobRepositoryMixin:
    """
    Durable MongoDB repository mixin for storage jobs.
    Works seamlessly with both Motor AsyncIOMotorCollection and MockCollection in test suites.
    """

    @property
    def storage_jobs_collection(self):
        return getattr(self, "storage_jobs", None)

    @property
    def worker_health_collection(self):
        return getattr(self, "worker_health", None)

    @property
    def client_health_collection(self):
        return getattr(self, "client_health", None)

    @property
    def audit_events_collection(self):
        return getattr(self, "audit_events", None)

    @property
    def metrics_snapshots_collection(self):
        return getattr(self, "metrics_snapshots", None)

    async def create_storage_job(
        self,
        job_doc: dict | None = None,
        *,
        file_id: str | None = None,
        db_id: str | None = None,
        user_id: int | None = None,
        folder_id: str | None = None,
        folder_session_id: str | None = None,
        is_folder_file: bool = False,
        media_group_id: str | None = None,
        operation: str = "STORE_TO_FLOG",
        status: str = "PENDING",
        priority: int = 0,
        max_attempts: int = 3,
        preferred_client_id: int | None = None,
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        """
        Create a durable storage job with deterministic idempotency.
        One logical storage operation produces exactly one durable job.
        Concurrent or duplicate enqueues return the existing job.
        """
        col = self.storage_jobs_collection
        if col is None:
            raise RuntimeError("storage_jobs collection is not initialized")

        doc: dict[str, Any] = dict(job_doc) if job_doc else {}
        if file_id is not None:
            doc["file_id"] = str(file_id)
        if db_id is not None:
            doc["db_id"] = str(db_id)
        if user_id is not None:
            doc["user_id"] = user_id
        if folder_id is not None:
            doc["folder_id"] = folder_id
        if folder_session_id is not None:
            doc["folder_session_id"] = folder_session_id
        if is_folder_file:
            doc["is_folder_file"] = is_folder_file
        if media_group_id is not None:
            doc["media_group_id"] = str(media_group_id)
        if preferred_client_id is not None:
            doc["preferred_client_id"] = preferred_client_id
        if metadata:
            doc.setdefault("metadata", {}).update(metadata)
        doc.update(kwargs)

        db_id_val = str(doc.get("db_id") or doc.get("file_id") or "")
        op_val = str(doc.get("operation") or operation)
        op_key = str(doc.get("storage_operation_key") or f"{db_id_val}:{op_val}")
        job_id_val = str(doc.get("job_id") or f"{db_id_val}:{op_val}")

        now = time.time()
        doc["storage_operation_key"] = op_key
        doc["job_id"] = job_id_val
        doc["db_id"] = db_id_val
        doc.setdefault("operation", op_val)
        doc.setdefault("status", status)
        doc.setdefault("priority", priority)
        doc.setdefault("attempts", 0)
        doc.setdefault("max_attempts", max_attempts)
        doc.setdefault("created_at", now)
        doc.setdefault("updated_at", now)
        doc.setdefault("started_at", None)
        doc.setdefault("completed_at", None)
        doc.setdefault("next_retry_at", None)
        doc.setdefault("lease_until", None)
        doc.setdefault("worker_id", None)
        doc.setdefault("assigned_client_id", None)
        doc.setdefault("last_error", None)
        doc.setdefault("last_error_type", None)
        doc.setdefault("recovery_count", 0)
        doc.setdefault("metadata", {})

        await col.update_one(
            {"storage_operation_key": op_key},
            {"$setOnInsert": doc},
            upsert=True,
        )

        existing = await col.find_one({"storage_operation_key": op_key})
        return existing if existing else doc

    async def get_storage_job(self, job_id: str) -> dict | None:
        col = self.storage_jobs_collection
        if col is None:
            return None
        return await col.find_one({"$or": [{"job_id": str(job_id)}, {"storage_operation_key": str(job_id)}]})

    async def get_storage_job_by_key(self, storage_operation_key: str) -> dict | None:
        col = self.storage_jobs_collection
        if col is None:
            return None
        return await col.find_one({"storage_operation_key": str(storage_operation_key)})

    async def claim_storage_job(
        self,
        worker_id: str,
        lease_seconds: float = 120.0,
        job_id: str | None = None,
    ) -> dict | None:
        """
        Atomically claim a PENDING or RETRY_WAIT job whose next_retry_at has arrived.
        Multi-process safe using find_one_and_update.
        """
        col = self.storage_jobs_collection
        if col is None:
            return None

        now = time.time()
        filter_q: dict[str, Any] = {
            "status": {"$in": ["PENDING", "pending", "RETRY_WAIT", "retry_wait", "retrying"]},
            "$or": [
                {"next_retry_at": None},
                {"next_retry_at": {"$lte": now}},
            ],
        }
        if job_id:
            filter_q["job_id"] = str(job_id)
        update_q = {
            "$set": {
                "status": "CLAIMED",
                "worker_id": str(worker_id),
                "lease_until": now + float(lease_seconds),
                "started_at": now,
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        }

        return await col.find_one_and_update(
            filter_q,
            update_q,
            sort=[("priority", pymongo.DESCENDING), ("created_at", pymongo.ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def release_claimed_job(
        self,
        job_id: str,
        worker_id: str,
    ) -> bool:
        """Release a claimed job back to PENDING (e.g. if local queue backpressure rejects)."""
        col = self.storage_jobs_collection
        if col is None:
            return False
        now = time.time()
        res = await col.update_one(
            {"job_id": str(job_id), "worker_id": str(worker_id), "status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running"]}},
            {
                "$set": {
                    "status": "PENDING",
                    "worker_id": None,
                    "lease_until": None,
                    "updated_at": now,
                },
                "$inc": {"attempts": -1},
            },
        )
        return _is_matched(res)

    async def mark_job_running(
        self,
        job_id: str,
        worker_id: str,
        assigned_client_id: int | None = None,
        lease_seconds: float = 120.0,
    ) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        sets: dict[str, Any] = {
            "status": "RUNNING",
            "lease_until": now + float(lease_seconds),
            "updated_at": now,
        }
        if assigned_client_id is not None:
            sets["assigned_client_id"] = int(assigned_client_id)

        res = await col.update_one(
            {
                "job_id": str(job_id),
                "worker_id": str(worker_id),
                "status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running", "processing"]},
            },
            {"$set": sets},
        )
        return _is_matched(res)

    async def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = 120.0,
    ) -> bool:
        """
        Renew an active lease.
        Verifies ownership: worker_id must match and lease must not have expired.
        """
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        res = await col.update_one(
            {
                "job_id": str(job_id),
                "worker_id": str(worker_id),
                "status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running", "processing"]},
                "lease_until": {"$gte": now},
            },
            {
                "$set": {
                    "lease_until": now + float(lease_seconds),
                    "updated_at": now,
                }
            },
        )
        return _is_matched(res)

    async def mark_job_stored(
        self,
        job_id: str,
        flog_msg_id: int,
        flog_channel_id: int | None = None,
        client_id: int | None = None,
    ) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        sets: dict[str, Any] = {
            "status": "STORED",
            "flog_msg_id": int(flog_msg_id),
            "completed_at": now,
            "updated_at": now,
        }
        if flog_channel_id is not None:
            sets["flog_channel_id"] = int(flog_channel_id)
        if client_id is not None:
            sets["assigned_client_id"] = int(client_id)

        res = await col.update_one(
            {"job_id": str(job_id), "status": {"$nin": ["CANCELLED", "cancelled"]}},
            {"$set": sets},
        )
        return _is_matched(res)

    async def schedule_job_retry(
        self,
        job_id: str,
        next_retry_at: float | datetime.datetime,
        error: str = "",
        error_type: str = "",
    ) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        retry_ts = _to_timestamp(next_retry_at)
        res = await col.update_one(
            {"job_id": str(job_id), "status": {"$nin": ["STORED", "stored", "CANCELLED", "cancelled"]}},
            {
                "$set": {
                    "status": "RETRY_WAIT",
                    "next_retry_at": retry_ts,
                    "worker_id": None,
                    "assigned_client_id": None,
                    "lease_until": None,
                    "last_error": str(error),
                    "last_error_type": str(error_type),
                    "updated_at": now,
                }
            },
        )
        return _is_matched(res)

    async def mark_job_failed(
        self,
        job_id: str,
        error: str = "",
        error_type: str = "",
        final_client_id: int | None = None,
        attempts: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        sets: dict[str, Any] = {
            "status": "FAILED",
            "completed_at": now,
            "last_error": str(error),
            "last_error_type": str(error_type),
            "failure_reason": str(error),
            "failure_type": str(error_type),
            "last_failed_at": now,
            "dead_letter": True,
            "lease_until": None,
            "updated_at": now,
        }
        if final_client_id is not None:
            sets["assigned_client_id"] = int(final_client_id)
            sets["last_client"] = int(final_client_id)
        if attempts is not None:
            sets["attempt_count"] = int(attempts)
        if worker_id is not None:
            sets["last_worker"] = str(worker_id)

        res = await col.update_one(
            {"job_id": str(job_id), "status": {"$nin": ["STORED", "stored"]}},
            {"$set": sets},
        )
        return _is_matched(res)

    async def cancel_storage_job(self, job_id: str) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        res = await col.update_one(
            {"job_id": str(job_id), "status": {"$nin": ["STORED", "stored"]}},
            {
                "$set": {
                    "status": "CANCELLED",
                    "completed_at": now,
                    "updated_at": now,
                }
            },
        )
        return _is_matched(res)

    async def cancel_storage_jobs_for_session(self, folder_session_id: str) -> int:
        col = self.storage_jobs_collection
        if col is None or not folder_session_id:
            return 0

        now = time.time()
        res = await col.update_many(
            {
                "folder_session_id": str(folder_session_id),
                "status": {"$nin": ["STORED", "stored", "CANCELLED", "cancelled"]},
            },
            {
                "$set": {
                    "status": "CANCELLED",
                    "completed_at": now,
                    "updated_at": now,
                }
            },
        )
        return _count_modified(res)

    async def recover_expired_leases(self, now: float | None = None) -> int:
        """
        Recover jobs whose leases have expired back to PENDING state.
        Race safe: only targets leases where lease_until < now.
        """
        col = self.storage_jobs_collection
        if col is None:
            return 0

        now_ts = time.time() if now is None else float(now)
        res = await col.update_many(
            {
                "status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running", "processing"]},
                "lease_until": {"$lt": now_ts},
            },
            {
                "$set": {
                    "status": "PENDING",
                    "worker_id": None,
                    "assigned_client_id": None,
                    "lease_until": None,
                    "updated_at": now_ts,
                },
                "$inc": {"recovery_count": 1},
            },
        )
        recovered = _count_modified(res)
        if recovered > 0:
            logger.info("[STORAGE] STORAGE_JOB_RECOVERED: Recovered %d expired storage jobs back to PENDING", recovered)
        return recovered

    async def count_active_storage_jobs_for_session(self, folder_session_id: str) -> int:
        col = self.storage_jobs_collection
        if col is None or not folder_session_id:
            return 0

        return await col.count_documents({
            "folder_session_id": str(folder_session_id),
            "status": {
                "$in": [
                    "PENDING", "pending",
                    "CLAIMED", "claimed",
                    "RUNNING", "running", "processing",
                    "RETRY_WAIT", "retry_wait", "retrying",
                ]
            },
        })

    async def get_storage_jobs_stats(self) -> dict:
        col = self.storage_jobs_collection
        if col is None:
            return {
                "pending": 0, "claimed": 0, "running": 0, "retry_wait": 0,
                "stored": 0, "failed": 0, "cancelled": 0, "recovered": 0,
                "oldest_pending_age": 0.0,
            }

        now = time.time()
        counts: dict[str, int] = {
            "pending": 0, "claimed": 0, "running": 0, "retry_wait": 0,
            "stored": 0, "failed": 0, "cancelled": 0,
        }

        for st in ("PENDING", "CLAIMED", "RUNNING", "RETRY_WAIT", "STORED", "FAILED", "CANCELLED"):
            aliases = [st, st.lower()]
            if st == "RUNNING":
                aliases.extend(["processing", "PROCESSING"])
            elif st == "RETRY_WAIT":
                aliases.extend(["retrying", "RETRYING", "retry_wait"])
            c = await col.count_documents({"status": {"$in": aliases}})
            counts[st.lower()] = c

        oldest_pending_age = 0.0
        oldest_pending = await col.find_one(
            {"status": {"$in": ["PENDING", "pending"]}},
            sort=[("created_at", 1)],
        )
        if oldest_pending and oldest_pending.get("created_at"):
            oldest_pending_age = max(0.0, round(now - float(oldest_pending["created_at"]), 1))

        # Sum total recovery_count
        recovered_total = 0
        try:
            cursor = col.find({"recovery_count": {"$gt": 0}})
            async for j in cursor:
                recovered_total += int(j.get("recovery_count") or 0)
        except Exception:
            pass

        return {
            **counts,
            "recovered": recovered_total,
            "oldest_pending_age": oldest_pending_age,
        }

    async def clean_expired_terminal_jobs(self, retention_days: int = 7) -> int:
        col = self.storage_jobs_collection
        if col is None:
            return 0

        cutoff = time.time() - (max(int(retention_days), 1) * 86400)
        res = await col.delete_many({
            "status": {"$in": ["STORED", "stored", "FAILED", "failed", "CANCELLED", "cancelled"]},
            "completed_at": {"$lt": cutoff, "$ne": None},
        })
        dc = getattr(res, "deleted_count", 0)
        return int(dc) if isinstance(dc, (int, float)) else (1 if res else 0)

    async def reconcile_flog_storage_jobs(self) -> int:
        """
        Reconcile files that succeeded in FLOG but whose jobs were left in non-stored state
        due to an unexpected process termination.
        """
        col = self.storage_jobs_collection
        file_col = getattr(self, "file", None)
        if col is None or file_col is None:
            return 0

        reconciled = 0
        try:
            cursor = col.find({
                "status": {"$nin": ["STORED", "stored", "CANCELLED", "cancelled"]}
            })
            from bson import ObjectId

            async for job in cursor:
                db_id = str(job.get("db_id") or job.get("job_id") or "")
                if not db_id:
                    continue
                try:
                    query_id = ObjectId(db_id) if ObjectId.is_valid(db_id) else db_id
                    fdoc = await file_col.find_one({"_id": query_id})
                    if fdoc and fdoc.get("flog_msg_id"):
                        mid = int(fdoc["flog_msg_id"])
                        cid = fdoc.get("flog_channel_id")
                        await self.mark_job_stored(job["job_id"], flog_msg_id=mid, flog_channel_id=cid)
                        reconciled += 1
                except Exception as ex:
                    logger.debug("Reconciliation error for job %s: %s", db_id, ex)
        except Exception as err:
            logger.warning("[STORAGE] Error during reconcile_flog_storage_jobs: %s", err)

        if reconciled > 0:
            logger.info("[STORAGE] Reconciled %d storage jobs against existing FLOG files", reconciled)
        return reconciled

    # =========================================================================
    # Dead-Letter Handling
    # =========================================================================

    async def mark_job_dead_letter(
        self,
        job_id: str,
        error: str = "",
        error_type: str = "",
        final_client_id: int | None = None,
    ) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        sets: dict[str, Any] = {
            "status": "DEAD_LETTER",
            "completed_at": now,
            "last_error": str(error),
            "last_error_type": str(error_type),
            "lease_until": None,
            "worker_id": None,
            "updated_at": now,
        }
        if final_client_id is not None:
            sets["assigned_client_id"] = int(final_client_id)

        res = await col.update_one(
            {"job_id": str(job_id), "status": {"$nin": ["STORED", "stored"]}},
            {"$set": sets},
        )
        return _is_matched(res)

    async def list_dead_letter_jobs(self, limit: int = 50, skip: int = 0) -> list[dict]:
        col = self.storage_jobs_collection
        if col is None:
            return []
        cursor = col.find({
            "$or": [
                {"status": {"$in": ["DEAD_LETTER", "dead_letter"]}},
                {"dead_letter": True},
            ]
        })
        cursor = cursor.sort("completed_at", -1).skip(skip).limit(limit)
        results = []
        async for doc in cursor:
            results.append(dict(doc))
        return results

    async def list_storage_jobs(
        self,
        status: str | None = None,
        limit: int = 50,
        skip: int = 0,
    ) -> list[dict]:
        col = self.storage_jobs_collection
        if col is None:
            return []
        query: dict[str, Any] = {}
        if status:
            s = str(status).strip().upper()
            if s == "DEAD_LETTER":
                query = {"$or": [{"status": {"$in": ["DEAD_LETTER", "dead_letter"]}}, {"dead_letter": True}]}
            else:
                query = {"status": {"$in": [s, s.lower()]}}
        cursor = col.find(query).sort("created_at", -1).skip(skip).limit(limit)
        results = []
        async for doc in cursor:
            results.append(dict(doc))
        return results

    async def retry_dead_letter_job(self, job_id: str) -> bool:
        col = self.storage_jobs_collection
        if col is None:
            return False

        now = time.time()
        res = await col.update_one(
            {
                "job_id": str(job_id),
                "$or": [
                    {"status": {"$in": ["DEAD_LETTER", "dead_letter", "FAILED", "failed"]}},
                    {"dead_letter": True},
                ],
            },
            {
                "$set": {
                    "status": "PENDING",
                    "attempts": 0,
                    "lease_until": None,
                    "worker_id": None,
                    "assigned_client_id": None,
                    "next_retry_at": None,
                    "last_error": None,
                    "last_error_type": None,
                    "dead_letter": False,
                    "failure_reason": None,
                    "failure_type": None,
                    "updated_at": now,
                }
            },
        )
        return _is_matched(res)

    # =========================================================================
    # Priority Aging & Scheduling
    # =========================================================================

    async def apply_priority_aging(
        self,
        aging_threshold_seconds: float = 60.0,
        max_priority_bump: int = 10,
    ) -> int:
        """
        Anti-starvation mechanism:
        Increments priority of pending/retry_wait jobs that have waited longer than aging_threshold_seconds.
        """
        col = self.storage_jobs_collection
        if col is None:
            return 0

        cutoff = time.time() - max(float(aging_threshold_seconds), 1.0)
        res = await col.update_many(
            {
                "status": {"$in": ["PENDING", "pending", "RETRY_WAIT", "retry_wait"]},
                "created_at": {"$lt": cutoff},
                "priority": {"$lt": int(max_priority_bump)},
            },
            {
                "$inc": {"priority": 1},
                "$set": {"updated_at": time.time()},
            },
        )
        return _count_modified(res)

    # =========================================================================
    # Admission Control & Capacity Counts
    # =========================================================================

    async def count_pending_jobs(self) -> int:
        col = self.storage_jobs_collection
        if col is None:
            return 0
        return await col.count_documents({"status": {"$in": ["PENDING", "pending", "RETRY_WAIT", "retry_wait"]}})

    async def count_user_pending_jobs(self, user_id: int) -> int:
        col = self.storage_jobs_collection
        if col is None or user_id is None:
            return 0
        return await col.count_documents({
            "user_id": user_id,
            "status": {"$in": ["PENDING", "pending", "CLAIMED", "claimed", "RUNNING", "running", "RETRY_WAIT", "retry_wait"]},
        })

    async def count_folder_pending_jobs(self, folder_id: str) -> int:
        col = self.storage_jobs_collection
        if col is None or not folder_id:
            return 0
        return await col.count_documents({
            "folder_id": str(folder_id),
            "status": {"$in": ["PENDING", "pending", "CLAIMED", "claimed", "RUNNING", "running", "RETRY_WAIT", "retry_wait"]},
        })

    # =========================================================================
    # Worker Health Management
    # =========================================================================

    async def register_worker_health(
        self,
        worker_id: str,
        host: str,
        pid: int,
        metadata: dict | None = None,
    ) -> dict:
        col = self.worker_health_collection
        if col is None:
            return {}

        now = time.time()
        doc = {
            "worker_id": str(worker_id),
            "host": str(host),
            "pid": int(pid),
            "process_start_time": now,
            "started_at": now,
            "last_heartbeat": now,
            "health_state": "HEALTHY",
            "current_job_id": None,
            "current_client_id": None,
            "jobs_completed": 0,
            "jobs_failed": 0,
            "jobs_recovered": 0,
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_at": None,
            "draining": False,
            "metadata": metadata or {},
        }
        await col.update_one(
            {"worker_id": str(worker_id)},
            {"$set": doc},
            upsert=True,
        )
        return doc

    async def update_worker_heartbeat(
        self,
        worker_id: str,
        health_state: str | None = None,
        current_job_id: str | None = None,
        current_client_id: int | None = None,
        jobs_completed: int | None = None,
        jobs_failed: int | None = None,
        draining: bool | None = None,
    ) -> bool:
        col = self.worker_health_collection
        if col is None:
            return False

        now = time.time()
        sets: dict[str, Any] = {"last_heartbeat": now}
        if health_state is not None:
            sets["health_state"] = health_state
        if current_job_id is not None:
            sets["current_job_id"] = current_job_id
        if current_client_id is not None:
            sets["current_client_id"] = current_client_id
        if draining is not None:
            sets["draining"] = bool(draining)

        update: dict[str, Any] = {"$set": sets}
        incs: dict[str, int] = {}
        if jobs_completed is not None and jobs_completed > 0:
            incs["jobs_completed"] = jobs_completed
        if jobs_failed is not None and jobs_failed > 0:
            incs["jobs_failed"] = jobs_failed
        if incs:
            update["$inc"] = incs

        res = await col.update_one({"worker_id": str(worker_id)}, update)
        return _is_matched(res)

    async def record_worker_error(self, worker_id: str, error_msg: str) -> None:
        col = self.worker_health_collection
        if col is None:
            return
        now = time.time()
        await col.update_one(
            {"worker_id": str(worker_id)},
            {
                "$set": {"last_error": str(error_msg), "last_error_at": now},
                "$inc": {"consecutive_failures": 1, "jobs_failed": 1},
            },
        )
        doc = await col.find_one({"worker_id": str(worker_id)})
        if doc and doc.get("consecutive_failures", 0) >= 3 and doc.get("health_state") == "HEALTHY":
            await col.update_one({"worker_id": str(worker_id)}, {"$set": {"health_state": "DEGRADED"}})

    async def record_worker_success(self, worker_id: str) -> None:
        col = self.worker_health_collection
        if col is None:
            return
        doc = await col.find_one({"worker_id": str(worker_id)})
        updates: dict[str, Any] = {"consecutive_failures": 0}
        if doc and doc.get("health_state") == "DEGRADED":
            updates["health_state"] = "HEALTHY"
        await col.update_one(
            {"worker_id": str(worker_id)},
            {"$set": updates, "$inc": {"jobs_completed": 1}},
        )

    async def set_worker_health_state(self, worker_id: str, state: str, draining: bool | None = None) -> bool:
        col = self.worker_health_collection
        if col is None:
            return False
        sets: dict[str, Any] = {"health_state": str(state), "last_heartbeat": time.time()}
        if draining is not None:
            sets["draining"] = bool(draining)
        res = await col.update_one({"worker_id": str(worker_id)}, {"$set": sets})
        return _is_matched(res)

    async def get_worker_health(self, worker_id: str) -> dict | None:
        col = self.worker_health_collection
        if col is None:
            return None
        return await col.find_one({"worker_id": str(worker_id)})

    async def list_workers_health(self, filter_query: dict | None = None) -> list[dict]:
        col = self.worker_health_collection
        if col is None:
            return []
        cursor = col.find(filter_query or {})
        res = []
        async for doc in cursor:
            res.append(dict(doc))
        return res

    async def get_stale_workers(self, timeout_seconds: float = 30.0) -> list[dict]:
        col = self.worker_health_collection
        if col is None:
            return []
        cutoff = time.time() - float(timeout_seconds)
        cursor = col.find({
            "last_heartbeat": {"$lt": cutoff},
            "health_state": {"$nin": ["STOPPED", "stopped", "UNHEALTHY", "unhealthy"]},
        })
        res = []
        async for doc in cursor:
            res.append(dict(doc))
        return res

    async def mark_stale_workers_unhealthy(self, timeout_seconds: float = 30.0) -> int:
        col = self.worker_health_collection
        if col is None:
            return 0
        cutoff = time.time() - float(timeout_seconds)
        res = await col.update_many(
            {
                "last_heartbeat": {"$lt": cutoff},
                "health_state": {"$nin": ["STOPPED", "stopped", "UNHEALTHY", "unhealthy"]},
            },
            {"$set": {"health_state": "UNHEALTHY"}},
        )
        return _count_modified(res)

    # =========================================================================
    # Client Health Management
    # =========================================================================

    async def update_client_health(
        self,
        client_id: int,
        status: str,
        failure_count: int | None = None,
        cooldown_until: float | None = None,
        last_error: str | None = None,
    ) -> bool:
        col = self.client_health_collection
        if col is None:
            return False
        now = time.time()
        sets: dict[str, Any] = {
            "client_id": int(client_id),
            "status": str(status),
            "last_updated": now,
        }
        if failure_count is not None:
            sets["consecutive_failures"] = int(failure_count)
        if cooldown_until is not None:
            sets["cooldown_until"] = float(cooldown_until)
        if last_error is not None:
            sets["last_error"] = str(last_error)
        res = await col.update_one({"client_id": int(client_id)}, {"$set": sets}, upsert=True)
        return _is_matched(res)

    async def get_client_health(self, client_id: int) -> dict | None:
        col = self.client_health_collection
        if col is None:
            return None
        return await col.find_one({"client_id": int(client_id)})

    async def list_clients_health(self) -> list[dict]:
        col = self.client_health_collection
        if col is None:
            return []
        cursor = col.find({})
        res = []
        async for doc in cursor:
            res.append(dict(doc))
        return res

    # =========================================================================
    # Audit Events & Metrics Snapshots
    # =========================================================================

    async def record_audit_event(
        self,
        action: str,
        actor: str = "system",
        target_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        col = self.audit_events_collection
        if col is None:
            return
        doc = {
            "timestamp": time.time(),
            "action": str(action),
            "actor": str(actor),
            "target_id": str(target_id) if target_id is not None else None,
            "details": details or {},
        }
        await col.insert_one(doc)

    async def list_audit_events(
        self,
        limit: int = 100,
        action: str | None = None,
    ) -> list[dict]:
        col = self.audit_events_collection
        if col is None:
            return []
        query: dict[str, Any] = {}
        if action:
            query["action"] = str(action)
        cursor = col.find(query).sort("timestamp", -1).limit(limit)
        res = []
        async for doc in cursor:
            res.append(dict(doc))
        return res

    async def save_metrics_snapshot(self, metrics_data: dict) -> None:
        col = self.metrics_snapshots_collection
        if col is None:
            return
        await col.insert_one({"timestamp": time.time(), "metrics": metrics_data})

    async def clean_expired_metrics(self, retention_days: int = 7) -> int:
        col = self.metrics_snapshots_collection
        if col is None:
            return 0
        cutoff = time.time() - (max(int(retention_days), 1) * 86400)
        res = await col.delete_many({"timestamp": {"$lt": cutoff}})
        dc = getattr(res, "deleted_count", 0)
        return int(dc) if isinstance(dc, (int, float)) else (1 if res else 0)

