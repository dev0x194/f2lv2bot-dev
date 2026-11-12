"""
Workload tracking utility for FileStreamBot.
Tracks real-time workload metrics:
- Active tasks (asyncio.all_tasks)
- Active connections (HTTP requests being served)
- Requests/Min (rolling 60-second window)
- Files processing (files currently being ingested/uploaded)
- Active streams (concurrent media streams)
- Queue size (Pyrogram updates_queue)
"""
from __future__ import annotations

import asyncio
import collections
import inspect
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class WorkloadTracker:
    """
    Application-level thread/async-safe tracker for runtime workload counters.
    Guarantees counters never become negative and correctly handle exceptions and cancellations.
    """
    def __init__(self) -> None:
        self._active_connections: int = 0
        self._files_processing: int = 0
        self._active_streams: int = 0
        self._request_timestamps: collections.deque[float] = collections.deque(maxlen=5000)

    # --- Connections ---
    @property
    def active_connections(self) -> int:
        return max(0, self._active_connections)

    def enter_connection(self) -> None:
        self._active_connections += 1

    def exit_connection(self) -> None:
        self._active_connections = max(0, self._active_connections - 1)

    # --- Files Processing ---
    @property
    def files_processing(self) -> int:
        return max(0, self._files_processing)

    def enter_file_processing(self) -> None:
        self._files_processing += 1

    def exit_file_processing(self) -> None:
        self._files_processing = max(0, self._files_processing - 1)

    # --- Active Streams ---
    @property
    def active_streams(self) -> int:
        return max(0, self._active_streams)

    def enter_stream(self) -> None:
        self._active_streams += 1

    def exit_stream(self) -> None:
        self._active_streams = max(0, self._active_streams - 1)

    # --- Composite Workload Properties ---
    @property
    def active_http_requests(self) -> int:
        """Returns total active HTTP requests (regular in-flight requests + streaming downloads)."""
        return max(0, self._active_connections) + max(0, self._active_streams)

    @property
    def active_operations(self) -> int:
        """Returns total active workload operations (active media streams + files processing)."""
        return max(0, self._active_streams) + max(0, self._files_processing)

    # --- Requests / Min ---
    def record_request(self, timestamp: float | None = None) -> None:
        """Records an incoming request timestamp in a bounded rolling window."""
        self._request_timestamps.append(timestamp if timestamp is not None else time.monotonic())

    def get_requests_per_minute(self, now: float | None = None) -> int:
        """Calculates total requests recorded within the last 60 seconds."""
        current_time = now if now is not None else time.monotonic()
        cutoff = current_time - 60.0
        while self._request_timestamps and self._request_timestamps[0] < cutoff:
            self._request_timestamps.popleft()
        return len(self._request_timestamps)

    def get_stats(self) -> dict[str, Any]:
        return {
            "active_operations": self.active_operations,
            "active_http_requests": self.active_http_requests,
            "active_connections": self.active_connections,
            "files_processing": self.files_processing,
            "active_streams": self.active_streams,
            "requests_per_minute": self.get_requests_per_minute(),
        }

    # --- Context Managers ---
    class _ContextManager:
        def __init__(self, enter_fn, exit_fn):
            self._enter_fn = enter_fn
            self._exit_fn = exit_fn

        def __enter__(self):
            self._enter_fn()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self._exit_fn()
            return False

        async def __aenter__(self):
            self._enter_fn()
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self._exit_fn()
            return False

    def track_connection(self):
        return self._ContextManager(self.enter_connection, self.exit_connection)

    def track_file_processing(self):
        return self._ContextManager(self.enter_file_processing, self.exit_file_processing)

    def track_stream(self):
        return self._ContextManager(self.enter_stream, self.exit_stream)

    def reset(self) -> None:
        """Resets all metrics to clean zero state (intended for unit tests)."""
        self._active_connections = 0
        self._files_processing = 0
        self._active_streams = 0
        self._request_timestamps.clear()

    def reset_for_testing(self) -> None:
        self.reset()


workload_tracker = WorkloadTracker()


def get_active_tasks() -> int | str:
    """Returns number of active asyncio tasks excluding the current inspection task."""
    try:
        tasks = [t for t in asyncio.all_tasks() if not t.done()]
        current_task = asyncio.current_task()
        if current_task is not None:
            tasks = [t for t in tasks if t is not current_task]
        return len(tasks)
    except RuntimeError:
        return 0
    except Exception:
        return "Unavailable"


def get_active_operations() -> int | str:
    """Returns number of active workload operations (active media streams + files processing)."""
    try:
        return workload_tracker.active_operations
    except Exception:
        return "Unavailable"


def get_active_http_requests() -> int | str:
    """Returns number of active HTTP requests (including ongoing streaming downloads)."""
    try:
        return workload_tracker.active_http_requests
    except Exception:
        return "Unavailable"


def get_active_connections() -> int | str:
    """Returns the current number of active HTTP/streaming connections."""
    try:
        return workload_tracker.active_http_requests
    except Exception:
        return "Unavailable"


def get_requests_per_minute() -> int | str:
    """Returns total HTTP requests handled in the rolling 60-second window."""
    try:
        return workload_tracker.get_requests_per_minute()
    except Exception:
        return "Unavailable"


def get_files_processing() -> int | str:
    """Returns number of files currently being processed / uploaded."""
    try:
        return workload_tracker.files_processing
    except Exception:
        return "Unavailable"


def get_active_streams() -> int | str:
    """Returns number of active client download streams."""
    try:
        val = workload_tracker.active_streams
        if val > 0:
            return val
        try:
            from FileStream.bot import work_loads
            if isinstance(work_loads, dict) and work_loads:
                wl_val = sum(max(0, int(v)) for v in work_loads.values())
                if wl_val > 0:
                    return wl_val
        except Exception:
            pass
        return val
    except Exception:
        return "Unavailable"


def get_queue_size(bot: Any = None) -> int | str:
    """Returns the pending update queue size from the Pyrogram dispatcher or queue object."""
    try:
        if bot is not None and hasattr(bot, "qsize") and callable(getattr(bot, "qsize")):
            try:
                val = bot.qsize()
                if inspect.iscoroutine(val) or inspect.isawaitable(val):
                    try:
                        val.close()
                    except Exception:
                        pass
                    return 0
                return int(val)
            except Exception:
                return 0
        client = bot
        if client is None:
            try:
                from FileStream.bot import FileStream
                client = FileStream
            except Exception:
                client = None
        if client is not None:
            dispatcher = getattr(client, "dispatcher", None)
            queue = getattr(dispatcher, "updates_queue", None)
            if queue is not None and hasattr(queue, "qsize"):
                val = queue.qsize()
                if inspect.iscoroutine(val) or inspect.isawaitable(val):
                    try:
                        val.close()
                    except Exception:
                        pass
                    return 0
                return int(val)
        return 0
    except Exception:
        return "Unavailable"


def get_workload_stats(bot: Any = None) -> dict[str, Any]:
    """
    Returns an aggregated dictionary of real-time workload stats.
    Guarantees every metric fails gracefully to 'Unavailable'.
    """
    return {
        "active_operations": get_active_operations(),
        "http_requests": get_active_http_requests(),
        "active_connections": get_active_connections(),
        "requests_per_minute": get_requests_per_minute(),
        "files_processing": get_files_processing(),
        "streams_active": get_active_streams(),
        "active_tasks": get_active_tasks(),
        "queue": get_queue_size(bot),
    }
