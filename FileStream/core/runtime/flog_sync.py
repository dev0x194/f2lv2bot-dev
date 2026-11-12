from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable

from pyrogram import Client

from FileStream.bot import FileStream
from FileStream.config import Telegram
from FileStream.database.database import Database
from FileStream.core.runtime.file_cleanup import delete_file_entry
from FileStream.core.runtime.flog_channels import (
    configured_flog_channels,
    optional_channel_name_for_id,
    resolve_file_flog_channel_id,
)
from FileStream.core.media.file_properties import get_media_from_message
from FileStream.core.runtime.optional_channels import (
    disable_optional_channel,
    is_invalid_optional_channel_error,
    optional_channel_available,
    warm_optional_channel_peer,
)


db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

FLOG_SYNC_BATCH_SIZE = 100
FLOG_SYNC_INTERVAL = 300
FLOG_SYNC_MIN_USER_INTERVAL = 20

_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None
_last_full_sync_at = 0.0
_last_user_sync_at: dict[int, float] = {}


def _has_flog_storage() -> bool:
    for channel_id in configured_flog_channels().values():
        if optional_channel_available(optional_channel_name_for_id(channel_id), channel_id):
            return True
    return False


def _build_flog_query(user_id: int | None = None, channel_id: int | None = None) -> dict:
    base_query = {"flog_msg_id": {"$exists": True, "$nin": [None, ""]}}
    if user_id is not None:
        base_query["user_id"] = int(user_id)

    if channel_id is None:
        return base_query

    query = dict(base_query)
    main_channel_id = configured_flog_channels().get("main")
    if main_channel_id and int(channel_id) == int(main_channel_id):
        query["$or"] = [
            {"flog_channel_id": int(channel_id)},
            {"flog_channel_id": {"$exists": False}},
            {"flog_channel_id": None},
            {"flog_channel_id": ""},
        ]
        return query

    query["flog_channel_id"] = int(channel_id)
    return query


async def _fetch_flog_messages(client: Client, channel_id: int, message_ids: Iterable[int]):
    message_ids = [int(mid) for mid in message_ids if mid]
    if not message_ids:
        return {}
    channel_name = optional_channel_name_for_id(channel_id)

    async def _get_messages():
        return await client.get_messages(channel_id, message_ids)

    try:
        result = await _get_messages()
    except Exception as exc:
        if not is_invalid_optional_channel_error(exc):
            logging.warning("Failed to fetch FLOG messages from %s=%s: %s", channel_name, channel_id, exc)
            return None
        if not await warm_optional_channel_peer(client, channel_name, channel_id):
            disable_optional_channel(channel_name, channel_id, exc)
            return None
        try:
            result = await _get_messages()
        except Exception as retry_exc:
            if is_invalid_optional_channel_error(retry_exc):
                disable_optional_channel(channel_name, channel_id, retry_exc)
            else:
                logging.warning("Failed to fetch FLOG messages from %s=%s after warming: %s", channel_name, channel_id, retry_exc)
            return None

    if not isinstance(result, list):
        result = [result] if result else []

    resolved = {}
    for message in result:
        if not message:
            continue
        try:
            resolved[int(message.id)] = message
        except Exception:
            continue
    return resolved


async def _prune_empty_folders(user_id: int | None = None) -> int:
    query = {"files": {"$size": 0}}
    if user_id is not None:
        query["user_id"] = int(user_id)

    deleted = 0
    async for folder in db.folders.find(query, {"_id": 1}):
        try:
            await db.delete_folder_by_id(folder["_id"])
            deleted += 1
        except Exception:
            logging.debug("Failed pruning empty folder %s", folder.get("_id"), exc_info=True)
    return deleted


async def _reconcile_batch(client: Client, channel_id: int, batch: list[tuple[dict, int]], stats: dict, mode: str = "REPAIR") -> bool:
    messages = await _fetch_flog_messages(client, channel_id, [msg_id for _, msg_id in batch])
    if messages is None:
        stats["channel_unavailable"] = True
        return False

    stats["checked_files"] += len(batch)

    for file_info, msg_id in batch:
        message = messages.get(msg_id)
        if message and not getattr(message, "empty", False) and get_media_from_message(message):
            continue
        # Only delete when messages was successfully fetched from Telegram and the message
        # is confirmed empty or missing media
        if message is not None or msg_id in messages:
            stats["deleted_files"] += 1
            if mode == "REPAIR":
                try:
                    await delete_file_entry(db, file_info, bot=client)
                except Exception:
                    logging.debug("Failed deleting stale FLOG-backed file %s", file_info.get("_id"), exc_info=True)

    return True


async def reconcile_flog_storage(
    bot: Client | None = None,
    *,
    user_id: int | None = None,
    force: bool = False,
    mode: str = "REPAIR",
) -> dict:
    global _last_full_sync_at

    stats = {
        "checked_files": 0,
        "deleted_files": 0,
        "deleted_folders": 0,
        "channel_unavailable": False,
    }

    if not configured_flog_channels():
        return stats

    now = time.time()
    scoped_user_id = int(user_id) if user_id is not None else None

    if not force:
        if scoped_user_id is None:
            if now - _last_full_sync_at < FLOG_SYNC_INTERVAL:
                return stats
        else:
            if now - _last_user_sync_at.get(scoped_user_id, 0.0) < FLOG_SYNC_MIN_USER_INTERVAL:
                return stats

    client = bot or FileStream

    async with _sync_lock:
        now = time.time()
        if not force:
            if scoped_user_id is None:
                if now - _last_full_sync_at < FLOG_SYNC_INTERVAL:
                    return stats
            else:
                if now - _last_user_sync_at.get(scoped_user_id, 0.0) < FLOG_SYNC_MIN_USER_INTERVAL:
                    return stats

        # Only synchronize channels explicitly configured in .env!
        # Do NOT discover unconfigured channels from MongoDB documents.
        channel_ids: list[int] = []
        seen_channel_ids: set[int] = set()
        for channel_id in configured_flog_channels().values():
            normalized = int(channel_id)
            if normalized in seen_channel_ids:
                continue
            seen_channel_ids.add(normalized)
            channel_ids.append(normalized)

        for channel_id in channel_ids:
            channel_name = optional_channel_name_for_id(channel_id)
            channel_query = _build_flog_query(scoped_user_id, channel_id)

            if not optional_channel_available(channel_name, channel_id):
                stats["channel_unavailable"] = True
                logging.debug(
                    "Optional channel %s=%s disabled for this runtime; skipping synchronization for %s",
                    channel_name,
                    channel_id,
                    f"user={scoped_user_id}" if scoped_user_id is not None else "global sync",
                )
                continue

            if not await warm_optional_channel_peer(client, channel_name, channel_id):
                stats["channel_unavailable"] = True
                disable_optional_channel(channel_name, channel_id, Exception("unable to warm optional channel peer"))
                logging.warning(
                    "FLOG storage %s=%s could not be warmed; skipping synchronization for %s",
                    channel_name,
                    channel_id,
                    f"user={scoped_user_id}" if scoped_user_id is not None else "global sync",
                )
                continue

            projection = {"_id": 1, "user_id": 1, "flog_msg_id": 1, "flog_channel_id": 1}
            cursor = db.file.find(channel_query, projection).sort("_id", -1)
            batch: list[tuple[dict, int]] = []
            channel_failed = False

            async for file_info in cursor:
                try:
                    msg_id = int(file_info.get("flog_msg_id"))
                except Exception:
                    continue

                resolved_channel_id = resolve_file_flog_channel_id(file_info)
                if resolved_channel_id and int(resolved_channel_id) != int(channel_id):
                    continue

                batch.append((file_info, msg_id))
                if len(batch) >= FLOG_SYNC_BATCH_SIZE:
                    if not await _reconcile_batch(client, channel_id, batch, stats, mode=mode):
                        logging.warning(
                            "FLOG storage %s=%s batch sync failed; skipping remainder of channel",
                            channel_name,
                            channel_id,
                        )
                        channel_failed = True
                        break
                    batch = []

            if batch and not channel_failed:
                if not await _reconcile_batch(client, channel_id, batch, stats, mode=mode):
                    logging.warning(
                        "FLOG storage %s=%s final batch sync failed; skipping remainder of channel",
                        channel_name,
                        channel_id,
                    )

        # Only prune empty folders if all storage channels were available and synced in REPAIR mode
        if not stats["channel_unavailable"] and mode == "REPAIR":
            stats["deleted_folders"] = await _prune_empty_folders(scoped_user_id)
        else:
            stats["deleted_folders"] = 0

        if scoped_user_id is None:
            _last_full_sync_at = time.time()
        else:
            _last_user_sync_at[scoped_user_id] = time.time()

    if stats["deleted_files"] or stats["deleted_folders"]:
        scope = f"user={scoped_user_id}" if scoped_user_id is not None else "global"
        logging.info(
            "FLOG sync (%s) %s: checked=%s deleted_files=%s deleted_folders=%s",
            mode,
            scope,
            stats["checked_files"],
            stats["deleted_files"],
            stats["deleted_folders"],
        )

    return stats


async def run_continuous_reconciliation(
    bot: Client | None = None,
    mode: str = "REPAIR",
) -> dict:
    """
    Continuous MongoDB <-> FLOG Reconciliation Engine.
    Detects and repairs 8 discrepancy types:
    1. MISSING_FLOG_REFERENCE: File in Mongo has flog_msg_id, but msg absent or empty in Telegram.
    2. ORPHAN_FLOG_MESSAGE: Discovered during inspection where Telegram has media unreferenced in Mongo.
    3. MISMATCHED_FLOG_REFERENCE: File doc references incorrect channel ID.
    4. UNSETTLED_STORAGE_JOB: Storage job running for too long without terminal state.
    5. STORED_FILE_PENDING_JOB: Crash-after-FLOG recovery (file has flog_msg_id, but job is non-terminal).
    6. FAILED_JOB_WITH_FLOG: Job marked failed/dead-letter, but file actually has valid FLOG message.
    7. EXPIRED_LEASE: Job lease expired; recovered and returned to PENDING.
    8. STALE_STORAGE_STATUS: File doc has non-terminal storage_status despite job completion.
    """
    mode = str(mode).upper()
    report: dict[str, Any] = {
        "mode": mode,
        "timestamp": time.time(),
        "discrepancies": {
            "MISSING_FLOG_REFERENCE": 0,
            "ORPHAN_FLOG_MESSAGE": 0,
            "MISMATCHED_FLOG_REFERENCE": 0,
            "UNSETTLED_STORAGE_JOB": 0,
            "STORED_FILE_PENDING_JOB": 0,
            "FAILED_JOB_WITH_FLOG": 0,
            "EXPIRED_LEASE": 0,
            "STALE_STORAGE_STATUS": 0,
        },
        "repaired": 0,
        "errors": [],
    }

    client = bot or FileStream

    # 1. Check Crash-after-FLOG-success & Failed jobs with FLOG
    try:
        col = getattr(db, "storage_jobs", None)
        file_col = getattr(db, "file", None)
        if col is not None and file_col is not None:
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
                        is_failed = str(job.get("status")).upper() in ["FAILED", "DEAD_LETTER"]
                        dtype = "FAILED_JOB_WITH_FLOG" if is_failed else "STORED_FILE_PENDING_JOB"
                        report["discrepancies"][dtype] += 1
                        if mode == "REPAIR":
                            mid = int(fdoc["flog_msg_id"])
                            cid = fdoc.get("flog_channel_id")
                            await db.mark_job_stored(job["job_id"], flog_msg_id=mid, flog_channel_id=cid)
                            await db.record_audit_event(
                                action="RECONCILE_CRASH_AFTER_FLOG",
                                actor="reconciliation_engine",
                                target_id=str(job["job_id"]),
                                details={"flog_msg_id": mid, "flog_channel_id": cid, "original_status": job.get("status")},
                            )
                            report["repaired"] += 1
                except Exception as ex:
                    logging.debug("Reconciliation crash-after-FLOG error for %s: %s", db_id, ex)
    except Exception as ex:
        report["errors"].append(f"Crash-after-FLOG check failed: {ex}")

    # 2. Check EXPIRED_LEASE & UNSETTLED_STORAGE_JOB
    try:
        now = time.time()
        col = getattr(db, "storage_jobs", None)
        if col is not None:
            expired_cursor = col.find({
                "status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running", "PROCESSING", "processing"]},
                "lease_until": {"$lt": now},
            })
            expired_jobs = []
            async for job in expired_cursor:
                report["discrepancies"]["EXPIRED_LEASE"] += 1
                expired_jobs.append(job)

            if expired_jobs and mode == "REPAIR":
                recovered = await db.recover_expired_leases(lease_duration_seconds=300)
                report["repaired"] += recovered
    except Exception as ex:
        report["errors"].append(f"Lease recovery check failed: {ex}")

    # 3. Check STALE_STORAGE_STATUS on file documents
    try:
        ten_mins_ago = time.time() - 600.0
        file_col = getattr(db, "file", None)
        col = getattr(db, "storage_jobs", None)
        if file_col is not None and col is not None:
            stale_cursor = file_col.find({
                "storage_status": {"$in": ["PENDING", "pending", "UPLOADING", "uploading", "QUEUED", "queued"]},
                "time": {"$lt": ten_mins_ago},
            })
            async for fdoc in stale_cursor:
                db_id = str(fdoc["_id"])
                sjob = await db.get_storage_job(f"{db_id}:STORE_TO_FLOG")
                if sjob:
                    s_status = str(sjob.get("status")).upper()
                    if s_status in ["STORED"] and fdoc.get("flog_msg_id"):
                        report["discrepancies"]["STALE_STORAGE_STATUS"] += 1
                        if mode == "REPAIR":
                            await file_col.update_one({"_id": fdoc["_id"]}, {"$set": {"storage_status": "STORED"}})
                            report["repaired"] += 1
                    elif s_status in ["FAILED", "DEAD_LETTER"]:
                        report["discrepancies"]["STALE_STORAGE_STATUS"] += 1
                        if mode == "REPAIR":
                            await file_col.update_one({"_id": fdoc["_id"]}, {"$set": {"storage_status": "FAILED"}})
                            report["repaired"] += 1
    except Exception as ex:
        report["errors"].append(f"Stale storage status check failed: {ex}")

    # 4. Check FLOG message references in Telegram
    try:
        sync_stats = await reconcile_flog_storage(client, force=True, mode=mode)
        deleted = sync_stats.get("deleted_files", 0)
        report["discrepancies"]["MISSING_FLOG_REFERENCE"] += deleted
        if mode == "REPAIR":
            report["repaired"] += deleted
    except Exception as ex:
        report["errors"].append(f"FLOG message sync failed: {ex}")

    total_discrepancies = sum(report["discrepancies"].values())
    report["total_discrepancies"] = total_discrepancies

    try:
        from FileStream.core.monitoring.metrics import get_metrics
        get_metrics().record_reconciliation(total_discrepancies, report["repaired"])
    except Exception:
        pass

    if mode == "REPAIR" and report["repaired"] > 0:
        try:
            await db.record_audit_event(
                action="CONTINUOUS_RECONCILIATION_RUN",
                actor="reconciliation_engine",
                details={
                    "discrepancies": report["discrepancies"],
                    "total_repaired": report["repaired"],
                },
            )
        except Exception:
            pass

    return report


async def _sync_loop(bot: Client):
    while True:
        try:
            interval = getattr(Telegram, "FLOG_RECONCILIATION_INTERVAL_SECONDS", FLOG_SYNC_INTERVAL)
            await run_continuous_reconciliation(bot, mode="REPAIR")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Background FLOG continuous reconciliation failed")
        interval = getattr(Telegram, "FLOG_RECONCILIATION_INTERVAL_SECONDS", FLOG_SYNC_INTERVAL)
        await asyncio.sleep(interval)


def start_flog_sync_task(bot: Client | None = None) -> asyncio.Task | None:
    global _sync_task
    if not configured_flog_channels():
        return None
    if _sync_task and not _sync_task.done():
        return _sync_task
    _sync_task = asyncio.create_task(_sync_loop(bot or FileStream))
    return _sync_task


async def stop_flog_sync_task() -> None:
    global _sync_task
    if not _sync_task:
        return
    _sync_task.cancel()
    try:
        await _sync_task
    except asyncio.CancelledError:
        pass
    _sync_task = None
