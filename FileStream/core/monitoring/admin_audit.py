import collections
import datetime
import logging
from typing import NamedTuple

logger = logging.getLogger("FileStream.admin")


class AdminActionEvent(NamedTuple):
    timestamp: str
    user_id: int
    action: str
    details: str


# Bounded in-memory ring buffer for recent administrative actions
_AUDIT_LOG: collections.deque[AdminActionEvent] = collections.deque(maxlen=200)


def log_admin_action(user_id: int, action: str, details: str = "") -> None:
    """
    Log an administrative action both to standard logging ("FileStream.admin")
    and to the in-memory ring buffer for rapid Telegram dashboard inspection.
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_action = str(action).strip()
    clean_details = str(details).strip()

    event = AdminActionEvent(
        timestamp=now_str,
        user_id=int(user_id),
        action=clean_action,
        details=clean_details,
    )
    _AUDIT_LOG.append(event)

    detail_suffix = f" - {clean_details}" if clean_details else ""
    log_msg = f"[ADMIN_ACTION] user={user_id} action={clean_action}{detail_suffix}"
    logger.info(log_msg)


def get_recent_admin_actions(limit: int = 20) -> list[str]:
    """
    Retrieve formatted administrative action log lines, newest first.
    """
    events = list(_AUDIT_LOG)
    events.reverse()
    result = []
    for ev in events[:limit]:
        detail_part = f" ({ev.details})" if ev.details else ""
        line = (
            f"[{ev.timestamp}] [INFO] [FileStream.admin]: "
            f"[ADMIN_ACTION] user={ev.user_id} action={ev.action}{detail_part}"
        )
        result.append(line)
    return result


def clear_admin_audit_log_for_testing() -> None:
    """Clear in-memory audit log for isolated unit tests."""
    _AUDIT_LOG.clear()
