from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from FileStream.database.database import Database

logger = logging.getLogger(__name__)


class AnalyticsEvents:
    """Standard event types for privacy-first behavioral analytics."""
    START = "start"
    UPLOAD_ATTEMPT = "upload_attempt"
    UPLOAD_SUCCESS = "upload_success"
    UPLOAD_FAILURE = "upload_failure"
    FOLDER_START = "folder_start"
    FOLDER_RESUME = "folder_resume"
    FOLDER_COMPLETE = "folder_complete"
    FOLDER_CANCEL = "folder_cancel"
    STATS_VIEW = "stats_view"
    MILESTONE_REACHED = "milestone_reached"
    SETTINGS_VIEW = "settings_view"
    SETTINGS_CHANGE = "settings_change"
    REFERRAL_VIEW = "referral_view"
    REFERRAL_ATTRIBUTED = "referral_attributed"
    NOTIFICATION_SENT = "notification_sent"
    NOTIFICATION_OPT_OUT = "notification_opt_out"
    TIP_SHOWN = "tip_shown"
    TIP_DISMISSED = "tip_dismissed"


async def track_event(
    db: Database | None,
    event_type: str,
    user_id: int | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    """Track a privacy-first behavioral event without recording sensitive user data or file contents."""
    if not db:
        return
    try:
        clean_props = {}
        if properties:
            for k, v in properties.items():
                if k in {"file_name", "caption", "text", "query"}:
                    continue
                clean_props[str(k)] = v
        await db.track_analytics_event(event_type, user_id=user_id, properties=clean_props)
    except Exception as e:
        logger.debug("Analytics event tracking failed silently: %s", e)


def get_user_variant(user_id: int, experiment_name: str, variants: list[str] | tuple[str, ...] | None = None) -> str:
    """Deterministic, consistent A/B test variant assignment per user."""
    if variants is None:
        variants = ["A", "B"]
    if not variants:
        return ""
    if not user_id:
        return variants[0]
    seed_string = f"{user_id}:{experiment_name}".encode("utf-8")
    hash_int = int(hashlib.md5(seed_string).hexdigest(), 16)
    return variants[hash_int % len(variants)]


async def get_funnel_summary(db: Database | None) -> dict[str, Any]:
    """Retrieve high-level user funnel metrics without leaking PII."""
    if not db:
        return {}
    try:
        return await db.get_funnel_metrics()
    except Exception as e:
        logger.debug("Funnel summary calculation failed: %s", e)
        return {}

