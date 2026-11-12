import asyncio
import logging
from typing import TypeVar, Callable, Awaitable, Any

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_floodwait(exc: BaseException | None) -> bool:
    """Return True if exception is a Telegram MTProto FloodWait error."""
    if exc is None:
        return False
    cls_name = exc.__class__.__name__
    return cls_name == "FloodWait" or (hasattr(exc, "value") and "flood" in cls_name.lower())


def get_floodwait_delay(exc: BaseException | None, default: float = 1.0) -> float:
    """Extract wait seconds from a FloodWait exception safely."""
    if exc is None:
        return float(default)
    val = getattr(exc, "value", None)
    if val is not None:
        try:
            return max(float(val), 0.1)
        except (TypeError, ValueError):
            pass
    return float(default)


async def call_with_floodwait(
    coro_fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    max_wait_seconds: float = 60.0,
    backoff_factor: float = 1.0,
    on_floodwait: Callable[[float, int], Any] | None = None,
    action_description: str = "Telegram API call",
    **kwargs: Any,
) -> T:
    """
    Execute an async Telegram callable with centralized FloodWait handling.

    - Never catches or swallows asyncio.CancelledError.
    - Never retries infinitely (strictly bounded by max_retries).
    - Obeys Telegram wait duration up to max_wait_seconds.
    - Applies exponential backoff when backoff_factor > 1.0.
    - Never loses state or duplicates status messages.
    """
    attempt = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not is_floodwait(exc):
                raise
            attempt += 1
            wait_s = get_floodwait_delay(exc)
            if attempt > max_retries:
                logger.error(
                    "FloodWait retries exhausted (%d/%d) for %s (required: %ss): %s",
                    attempt - 1, max_retries, action_description, wait_s, exc
                )
                raise
            if wait_s > max_wait_seconds:
                logger.error(
                    "FloodWait delay (%ss) exceeds limit (%ss) for %s",
                    wait_s, max_wait_seconds, action_description
                )
                raise

            sleep_duration = wait_s * (backoff_factor ** (attempt - 1))
            logger.warning(
                "FloodWait encountered: sleeping %ss (attempt %d/%d) for %s",
                round(sleep_duration, 2), attempt, max_retries, action_description
            )
            if on_floodwait:
                try:
                    res = on_floodwait(sleep_duration, attempt)
                    if asyncio.iscoroutine(res):
                        await res
                except asyncio.CancelledError:
                    raise
                except Exception as cb_err:
                    logger.debug("on_floodwait callback error: %s", cb_err)
            await asyncio.sleep(sleep_duration)
