import asyncio
import logging
from typing import Callable, Awaitable, Any

logger = logging.getLogger(__name__)


class AuditTaskManager:
    """
    Manages non-critical background audit tasks (like FLOG info cards) with:
    - Bounded concurrency to prevent MTProto channel rate limiting
    - Strong task references to prevent premature garbage collection
    - Cap on pending tasks to prevent unbounded memory growth during bursts
    - Safe exception handling at DEBUG level so ingestion never fails
    - Graceful drain and cancellation at shutdown
    """

    def __init__(self, max_concurrent: int = 10, max_queue: int = 250):
        self._max_concurrent = max_concurrent
        self._max_queue = max_queue
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: set[asyncio.Task] = set()
        self._shutting_down = False

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)

    def enqueue(self, coro_fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> bool:
        """
        Schedule a non-critical audit coroutine.
        Returns True if enqueued, False if dropped or shutting down.
        """
        if self._shutting_down:
            return False

        if len(self._active_tasks) >= self._max_queue:
            logger.debug(
                "Audit queue full (%d active tasks); dropping non-critical audit task",
                len(self._active_tasks)
            )
            return False

        async def _runner():
            async with self._semaphore:
                try:
                    await coro_fn(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Non-critical background audit task failed: %s", exc)

        try:
            task = asyncio.create_task(_runner())
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            return True
        except RuntimeError:
            return False

    async def shutdown(self, timeout: float = 3.0) -> None:
        """Gracefully drain or cancel pending background audit tasks."""
        self._shutting_down = True
        if not self._active_tasks:
            return
        tasks = list(self._active_tasks)
        logger.info("Draining %d pending audit tasks (timeout: %ss)...", len(tasks), timeout)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            for t in tasks:
                if not t.done():
                    t.cancel()
        self._active_tasks.clear()


audit_manager = AuditTaskManager(max_concurrent=10, max_queue=250)
