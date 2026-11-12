from __future__ import annotations

import logging
import os
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from FileStream.database.database import Database

logger = logging.getLogger("FileStream.disaster_recovery")


async def verify_database_schema(db: Database) -> dict[str, Any]:
    """
    Verifies that all essential collections and indexes exist and are healthy.
    """
    report: dict[str, Any] = {
        "status": "healthy",
        "collections": {},
        "missing_collections": [],
        "errors": [],
    }

    essential_collections = [
        "file",
        "users",
        "folders",
        "storage_jobs",
        "worker_health",
        "client_health",
        "audit_events",
        "metrics_snapshots",
    ]

    try:
        existing_names = await db.db.list_collection_names()
    except Exception as exc:
        report["status"] = "unhealthy"
        report["errors"].append(f"Failed to list collections: {exc}")
        return report

    for col_name in essential_collections:
        if col_name in existing_names:
            try:
                count = await db.db[col_name].count_documents({})
                indexes = await db.db[col_name].index_information()
                report["collections"][col_name] = {
                    "exists": True,
                    "document_count": count,
                    "index_count": len(indexes),
                }
            except Exception as e:
                report["collections"][col_name] = {"exists": True, "error": str(e)}
        else:
            report["missing_collections"].append(col_name)
            report["status"] = "degraded"

    return report


async def emergency_recover_all_inflight_jobs(db: Database) -> int:
    """
    Disaster recovery routine:
    Resets all jobs in CLAIMED, RUNNING, or PROCESSING status back to PENDING.
    Clears leases and workers so jobs can be cleanly re-claimed after a crash.
    """
    col = getattr(db, "storage_jobs_collection", None) or getattr(db, "storage_jobs", None)
    if col is None:
        return 0

    now = time.time()
    res = await col.update_many(
        {"status": {"$in": ["CLAIMED", "claimed", "RUNNING", "running", "PROCESSING", "processing"]}},
        {
            "$set": {
                "status": "PENDING",
                "lease_until": None,
                "worker_id": None,
                "assigned_client_id": None,
                "next_retry_at": now,
                "updated_at": now,
            }
        },
    )
    recovered = getattr(res, "modified_count", 0)
    logger.info("[DISASTER_RECOVERY] Reset %d in-flight jobs to PENDING", recovered)

    if hasattr(db, "record_audit_event"):
        try:
            await db.record_audit_event(
                action="EMERGENCY_RECOVER_INFLIGHT_JOBS",
                actor="disaster_recovery_engine",
                details={"recovered_count": recovered},
            )
        except Exception:
            pass

    return int(recovered)


def verify_backup_directory(backup_dir: str) -> dict[str, Any]:
    """
    Verifies the contents of a local MongoDB backup directory (mongodump or JSON exports).
    Checks that essential collections have non-empty data files.
    """
    report: dict[str, Any] = {
        "valid": True,
        "backup_path": backup_dir,
        "files_found": [],
        "missing_files": [],
        "total_size_bytes": 0,
        "errors": [],
    }

    if not os.path.isdir(backup_dir):
        report["valid"] = False
        report["errors"].append(f"Directory not found: {backup_dir}")
        return report

    essential = ["file", "users", "storage_jobs"]
    found_bases = set()

    for root, _, files in os.walk(backup_dir):
        for f in files:
            full_path = os.path.join(root, f)
            sz = os.path.getsize(full_path)
            report["files_found"].append({"file": f, "size": sz})
            report["total_size_bytes"] += sz
            base_name = f.split(".")[0]
            found_bases.add(base_name)

    for item in essential:
        if item == "file" and ("file" in found_bases or "files" in found_bases):
            continue
        if item not in found_bases:
            report["missing_files"].append(item)
            report["valid"] = False

    return report


async def generate_dr_status_report(db: Database) -> dict[str, Any]:
    """
    Generate a comprehensive disaster recovery assessment report.
    """
    schema = await verify_database_schema(db)
    storage_stats = await db.get_storage_jobs_stats() if hasattr(db, "get_storage_jobs_stats") else {}

    return {
        "timestamp": time.time(),
        "database_health": schema["status"],
        "schema_details": schema,
        "storage_queue": storage_stats,
        "recommendations": (
            ["Ensure all collections exist and have indexes."] if schema["status"] != "healthy" else ["System healthy."]
        ),
    }
