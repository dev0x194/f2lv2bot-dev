from __future__ import annotations

import time

from FileStream.bot import work_loads


client_stats: dict[int, dict] = {}
EWMA_ALPHA = 0.35


def _ewma(current: float, new_value: float, alpha: float = EWMA_ALPHA) -> float:
    if current <= 0:
        return float(new_value)
    return (current * (1.0 - alpha)) + (float(new_value) * alpha)


def ensure_client_stat(index: int) -> dict:
    stat = client_stats.get(index)
    if stat is None:
        stat = {
            "avg_first_byte_ms": 0.0,
            "avg_throughput_bps": 0.0,
            "completed_streams": 0,
            "consecutive_failures": 0,
            "last_completed_at": 0.0,
            "last_error_at": 0.0,
            "cooldown_until": 0.0,
            "floodwait_count": 0,
        }
        client_stats[index] = stat
    else:
        stat.setdefault("cooldown_until", 0.0)
        stat.setdefault("floodwait_count", 0)
    return stat


def record_client_cooldown(index: int, wait_seconds: float) -> None:
    """Record a FloodWait cooldown for a specific client."""
    stat = ensure_client_stat(index)
    stat["floodwait_count"] = int(stat.get("floodwait_count", 0)) + 1
    safety_margin = 1.0
    stat["cooldown_until"] = max(
        float(stat.get("cooldown_until", 0.0)),
        time.monotonic() + float(wait_seconds) + safety_margin
    )
    stat["consecutive_failures"] = int(stat.get("consecutive_failures", 0)) + 1
    stat["last_error_at"] = time.time()


def is_client_in_cooldown(index: int) -> bool:
    """Check whether a client is currently in cooldown."""
    stat = ensure_client_stat(index)
    return float(stat.get("cooldown_until", 0.0)) > time.monotonic()


def get_earliest_client_cooldown(available_indexes=None) -> float:
    """Return the minimum seconds remaining until a client exits cooldown."""
    now = time.monotonic()
    targets = available_indexes if available_indexes is not None else client_stats.keys()
    cooldowns = [
        float(ensure_client_stat(idx).get("cooldown_until", 0.0)) - now
        for idx in targets
        if float(ensure_client_stat(idx).get("cooldown_until", 0.0)) > now
    ]
    if not cooldowns:
        return 0.0
    return max(min(cooldowns), 0.0)


def choose_best_client(available_indexes) -> int:
    available = list(available_indexes)
    if not available:
        raise RuntimeError("No available clients")

    now = time.monotonic()
    # A client currently under FloodWait MUST NOT be selected
    healthy_indexes = [
        index for index in available
        if float(ensure_client_stat(index).get("cooldown_until", 0.0)) <= now
    ]

    if not healthy_indexes:
        earliest = get_earliest_client_cooldown(available)
        raise RuntimeError(
            f"No available clients: all clients are currently in FloodWait cooldown (earliest available in {earliest:.1f}s)"
        )

    best_index = None
    best_score = None

    for index in sorted(healthy_indexes):
        stat = ensure_client_stat(index)
        score = (
            int(work_loads.get(index, 0)),
            int(stat.get("consecutive_failures", 0)),
            int(stat.get("floodwait_count", 0)),
            float(stat.get("avg_first_byte_ms", 0.0)),
            -float(stat.get("avg_throughput_bps", 0.0)),
            index,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_index = index

    if best_index is None:
        raise RuntimeError("No available clients")
    return best_index


def record_stream_completed(index: int, *, bytes_sent: int, duration_s: float, first_byte_s: float | None = None) -> None:
    stat = ensure_client_stat(index)
    stat["completed_streams"] = int(stat.get("completed_streams", 0)) + 1
    stat["consecutive_failures"] = 0
    stat["last_completed_at"] = time.time()

    if first_byte_s is not None and first_byte_s >= 0:
        first_byte_ms = float(first_byte_s) * 1000.0
        stat["avg_first_byte_ms"] = _ewma(float(stat.get("avg_first_byte_ms", 0.0)), first_byte_ms)

    if duration_s > 0 and bytes_sent > 0:
        throughput_bps = float(bytes_sent) / float(duration_s)
        stat["avg_throughput_bps"] = _ewma(float(stat.get("avg_throughput_bps", 0.0)), throughput_bps)


def record_stream_failed(index: int, exc: Exception | None = None) -> None:
    stat = ensure_client_stat(index)
    stat["consecutive_failures"] = int(stat.get("consecutive_failures", 0)) + 1
    stat["last_error_at"] = time.time()
    if exc is not None:
        from FileStream.core.resilience.floodwait import is_floodwait, get_floodwait_delay
        if is_floodwait(exc):
            record_client_cooldown(index, get_floodwait_delay(exc))
