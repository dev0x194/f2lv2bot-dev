from __future__ import annotations

from FileStream.config import Telegram


def configured_flog_channels() -> dict[str, int]:
    channels: dict[str, int] = {}

    if Telegram.FLOG_CHANNEL:
        channels["main"] = int(Telegram.FLOG_CHANNEL)

    if Telegram.ADMIN_FLOG_CHANNEL:
        admin_channel = int(Telegram.ADMIN_FLOG_CHANNEL)
        if admin_channel != channels.get("main"):
            channels["admin"] = admin_channel

    return channels


def has_configured_flog_channels() -> bool:
    return bool(configured_flog_channels())


def normalize_flog_storage_mode(mode: str | None) -> str:
    return "admin" if str(mode or "").strip().lower() == "admin" else "main"


def optional_channel_name_for_mode(mode: str | None) -> str:
    return "ADMIN_FLOG_CHANNEL" if normalize_flog_storage_mode(mode) == "admin" else "FLOG_CHANNEL"


def resolve_flog_mode_for_channel_id(channel_id: int | None) -> str:
    if not channel_id:
        return "main"

    try:
        normalized = int(channel_id)
    except Exception:
        return "main"

    for mode, configured_id in configured_flog_channels().items():
        if int(configured_id) == normalized:
            return mode

    if Telegram.ADMIN_FLOG_CHANNEL and normalized == int(Telegram.ADMIN_FLOG_CHANNEL):
        return "admin"

    return "main"


def optional_channel_name_for_id(channel_id: int | None) -> str:
    return optional_channel_name_for_mode(resolve_flog_mode_for_channel_id(channel_id))


def resolve_file_flog_channel_id(file_info: dict | None) -> int | None:
    configured = configured_flog_channels()
    if not configured:
        return None

    if not file_info:
        return configured.get("main") or configured.get("admin")

    raw = file_info.get("flog_channel_id")
    try:
        if raw not in (None, ""):
            candidate = int(raw)
            if candidate in configured.values():
                return candidate
    except Exception:
        pass

    return configured.get("main") or configured.get("admin")


def resolve_file_flog_mode(file_info: dict | None) -> str:
    return resolve_flog_mode_for_channel_id(resolve_file_flog_channel_id(file_info))


async def resolve_active_flog_target(db, user_id: int | None = None) -> tuple[str, str, int | None]:
    channels = configured_flog_channels()
    mode = "main"
    if user_id is not None and channels.get("admin"):
        try:
            if await db.is_admin_flog_user(user_id):
                mode = "admin"
        except Exception:
            mode = "main"

    if mode == "admin" and channels.get("admin"):
        return "admin", "ADMIN_FLOG_CHANNEL", int(channels["admin"])

    if channels.get("main"):
        return "main", "FLOG_CHANNEL", int(channels["main"])

    return mode, optional_channel_name_for_mode(mode), None


import time


class ChannelHealthMonitor:
    """Tracks accessibility, consecutive failures, and operational health of FLOG channels."""

    def __init__(self, failure_threshold: int = 3):
        self.failure_threshold = failure_threshold
        self._stats: dict[int, dict] = {}

    def _get_entry(self, channel_id: int) -> dict:
        cid = int(channel_id)
        if cid not in self._stats:
            self._stats[cid] = {
                "channel_id": cid,
                "consecutive_failures": 0,
                "total_success": 0,
                "total_failures": 0,
                "last_success_time": 0.0,
                "last_failure_time": 0.0,
                "last_error": None,
            }
        return self._stats[cid]

    def record_success(self, channel_id: int) -> None:
        entry = self._get_entry(channel_id)
        entry["consecutive_failures"] = 0
        entry["total_success"] += 1
        entry["last_success_time"] = time.time()

    def record_failure(self, channel_id: int, error: str) -> None:
        entry = self._get_entry(channel_id)
        entry["consecutive_failures"] += 1
        entry["total_failures"] += 1
        entry["last_failure_time"] = time.time()
        entry["last_error"] = str(error)

    def is_healthy(self, channel_id: int) -> bool:
        entry = self._get_entry(channel_id)
        return entry["consecutive_failures"] < self.failure_threshold

    def get_status(self, channel_id: int) -> str:
        entry = self._get_entry(channel_id)
        cf = entry["consecutive_failures"]
        if cf == 0:
            return "HEALTHY"
        elif cf < self.failure_threshold:
            return "DEGRADED"
        return "UNHEALTHY"

    def get_channel_health(self, channel_id: int) -> dict:
        entry = dict(self._get_entry(channel_id))
        entry["status"] = self.get_status(channel_id)
        entry["is_healthy"] = self.is_healthy(channel_id)
        return entry

    def list_channels_health(self) -> list[dict]:
        return [self.get_channel_health(cid) for cid in self._stats]

    def reset(self) -> None:
        self._stats.clear()


_channel_monitor = ChannelHealthMonitor()


def get_channel_monitor() -> ChannelHealthMonitor:
    return _channel_monitor
