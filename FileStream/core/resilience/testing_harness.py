"""
Soak testing harness and Telegram failure injection framework for File To Link V2.

Subsystems #29 (Long-duration soak testing) and #30 (Realistic Telegram failure-injection testing).

Provides:
- SoakTestRunner: orchestrates extended-duration test scenarios
- FailureInjector: injects realistic failures (FloodWait, network, permission errors)
- FaultyClient: drop-in replacement for Pyrofork Client that randomly fails
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import enum
import logging
import random
import time
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

logger = logging.getLogger("FileStream.testing")


# ---------------------------------------------------------------------------
# Failure Injection
# ---------------------------------------------------------------------------

class FailureType(str, enum.Enum):
    """Types of failures that can be injected."""
    FLOODWAIT = "FloodWait"
    NETWORK_ERROR = "NetworkError"
    PERMISSION_ERROR = "PermissionError"
    TIMEOUT = "Timeout"
    CHANNEL_INVALID = "ChannelInvalid"
    FILE_REFERENCE_EXPIRED = "FileReferenceExpired"
    RPC_ERROR = "RPCError"
    SLOW_RESPONSE = "SlowResponse"


@dataclasses.dataclass
class FailureConfig:
    """Configuration for a specific failure type."""
    failure_type: FailureType
    probability: float = 0.1  # 0.0 - 1.0
    min_delay: float = 0.0  # seconds
    max_delay: float = 0.0  # seconds
    floodwait_seconds: int = 30  # for FloodWait type
    enabled: bool = True


class InjectedFloodWait(Exception):
    """Simulated FloodWait exception for testing."""
    def __init__(self, seconds: int = 10, value: int | None = None):
        sec = value if value is not None else seconds
        super().__init__(f"A wait of {sec} seconds is required")
        self.value = sec


class FailureInjector:
    """
    Injects realistic Telegram client failures for testing.

    Can simulate:
    - FloodWait errors at configurable rates
    - Network timeouts and connection failures
    - Permission errors (ChatWriteForbidden, ChatAdminRequired)
    - RPC errors with various codes
    - Slow responses / hanging operations
    """

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self._configs: dict[FailureType, FailureConfig] = {}
        self._injection_count = 0
        self._injection_log: collections.deque[dict[str, Any]] = collections.deque(maxlen=1000)
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def configure(self, config: FailureConfig) -> None:
        """Add or update a failure injection configuration."""
        self._configs[config.failure_type] = config

    def configure_scenario(self, scenario: str) -> None:
        """
        Apply a pre-defined failure scenario.

        Scenarios:
        - "light": 5% FloodWait, 2% network errors
        - "moderate": 15% FloodWait, 5% network, 3% permission
        - "heavy": 30% FloodWait, 10% network, 5% permission, 5% timeout
        - "catastrophic": 50% FloodWait, 20% network, 10% permission
        """
        self._configs.clear()
        scenarios = {
            "light": [
                FailureConfig(FailureType.FLOODWAIT, probability=0.05, floodwait_seconds=5),
                FailureConfig(FailureType.NETWORK_ERROR, probability=0.02),
            ],
            "moderate": [
                FailureConfig(FailureType.FLOODWAIT, probability=0.15, floodwait_seconds=15),
                FailureConfig(FailureType.NETWORK_ERROR, probability=0.05),
                FailureConfig(FailureType.PERMISSION_ERROR, probability=0.03),
            ],
            "heavy": [
                FailureConfig(FailureType.FLOODWAIT, probability=0.30, floodwait_seconds=30),
                FailureConfig(FailureType.NETWORK_ERROR, probability=0.10),
                FailureConfig(FailureType.PERMISSION_ERROR, probability=0.05),
                FailureConfig(FailureType.TIMEOUT, probability=0.05, min_delay=5.0, max_delay=15.0),
            ],
            "catastrophic": [
                FailureConfig(FailureType.FLOODWAIT, probability=0.50, floodwait_seconds=60),
                FailureConfig(FailureType.NETWORK_ERROR, probability=0.20),
                FailureConfig(FailureType.PERMISSION_ERROR, probability=0.10),
                FailureConfig(FailureType.TIMEOUT, probability=0.10, min_delay=10.0, max_delay=30.0),
                FailureConfig(FailureType.RPC_ERROR, probability=0.05),
            ],
        }
        for config in scenarios.get(scenario, scenarios["light"]):
            self._configs[config.failure_type] = config
        self._enabled = True

    async def maybe_inject(self, operation: str = "send_file") -> None:
        """
        Check configured failure probabilities and raise an appropriate exception.
        Call this before actual Telegram operations to simulate failures.
        """
        if not self._enabled:
            return

        for ftype, config in self._configs.items():
            if not config.enabled:
                continue
            if self._rng.random() < config.probability:
                self._injection_count += 1
                self._injection_log.append({
                    "type": ftype.value,
                    "operation": operation,
                    "timestamp": time.time(),
                    "count": self._injection_count,
                })

                if config.min_delay > 0 or config.max_delay > 0:
                    delay = self._rng.uniform(config.min_delay, config.max_delay)
                    await asyncio.sleep(delay)

                raise self._make_exception(ftype, config)

    def _make_exception(self, ftype: FailureType, config: FailureConfig) -> Exception:
        """Create a realistic exception for the given failure type."""
        if ftype == FailureType.FLOODWAIT:
            return InjectedFloodWait(config.floodwait_seconds)
        elif ftype == FailureType.NETWORK_ERROR:
            return ConnectionError("Connection to Telegram servers lost")
        elif ftype == FailureType.PERMISSION_ERROR:
            return PermissionError("ChatWriteForbidden")
        elif ftype == FailureType.TIMEOUT:
            return asyncio.TimeoutError("Operation timed out")
        elif ftype == FailureType.CHANNEL_INVALID:
            return ValueError("Channel not found or invalid")
        elif ftype == FailureType.FILE_REFERENCE_EXPIRED:
            return Exception("File reference expired")
        elif ftype == FailureType.RPC_ERROR:
            return Exception("RPC Error: INTERNAL_SERVER_ERROR")
        elif ftype == FailureType.SLOW_RESPONSE:
            return asyncio.TimeoutError("Slow response timeout")
        return Exception(f"Injected failure: {ftype.value}")

    def get_stats(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "total_injections": self._injection_count,
            "configurations": {
                ft.value: {"probability": c.probability, "enabled": c.enabled}
                for ft, c in self._configs.items()
            },
            "recent_injections": list(self._injection_log)[-10:],
        }

    def reset(self) -> None:
        self._configs.clear()
        self._injection_count = 0
        self._injection_log.clear()
        self._enabled = False


_failure_injector = FailureInjector()


def get_failure_injector() -> FailureInjector:
    return _failure_injector


# ---------------------------------------------------------------------------
# Faulty Client (drop-in mock)
# ---------------------------------------------------------------------------

def create_faulty_client(
    client_id: int = 0,
    injector: FailureInjector | None = None,
    default_response: Any = None,
) -> MagicMock:
    """
    Create a MagicMock Telegram client that randomly fails based on FailureInjector config.
    Useful for integration testing with realistic failure patterns.
    """
    inj = injector or get_failure_injector()

    async def faulty_send_message(*args: Any, **kwargs: Any) -> Any:
        await inj.maybe_inject("send_message")
        msg = MagicMock()
        msg.id = random.randint(1, 999999)
        msg.chat = MagicMock()
        msg.chat.id = kwargs.get("chat_id", -1001234567890)
        return default_response or msg

    async def faulty_send_document(*args: Any, **kwargs: Any) -> Any:
        await inj.maybe_inject("send_document")
        msg = MagicMock()
        msg.id = random.randint(1, 999999)
        msg.chat = MagicMock()
        msg.chat.id = kwargs.get("chat_id", -1001234567890)
        return default_response or msg

    client = MagicMock()
    client.send_message = AsyncMock(side_effect=faulty_send_message)
    client.send_document = AsyncMock(side_effect=faulty_send_document)
    client.get_chat = AsyncMock(return_value=MagicMock(id=-1001234567890, title="Test Channel"))
    client.ME = MagicMock(id=client_id, is_bot=True)
    return client


# ---------------------------------------------------------------------------
# Soak Test Runner
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SoakTestResult:
    """Results from a soak test run."""
    total_operations: int = 0
    successful_operations: int = 0
    failed_operations: int = 0
    total_duration_seconds: float = 0.0
    operations_per_second: float = 0.0
    error_types: dict[str, int] = dataclasses.field(default_factory=dict)
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    peak_queue_size: int = 0
    peak_in_flight: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class SoakTestRunner:
    """
    Orchestrates long-duration stress/soak testing for the storage pipeline.

    Can run configurable workloads against the storage service with:
    - Configurable duration and concurrency
    - Failure injection integration
    - Latency tracking and percentile reporting
    - Checkpoint reporting during the test
    """

    def __init__(
        self,
        duration_seconds: float = 300.0,
        concurrency: int = 10,
        operations_per_second: float = 5.0,
        checkpoint_interval: float = 30.0,
        error_threshold: float = 0.1,
    ):
        self.duration = duration_seconds
        self.concurrency = concurrency
        self.ops_per_second = operations_per_second
        self.checkpoint_interval = checkpoint_interval
        self.error_threshold = error_threshold
        self._running = False
        self._result: SoakTestResult | None = None
        self._latencies: list[float] = []
        self._checkpoints: list[dict[str, Any]] = []

    async def run(
        self,
        operation: Callable[..., Any],
        *args: Any,
        on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
        **kwargs: Any,
    ) -> SoakTestResult:
        """
        Run the soak test.

        Args:
            operation: Async callable to repeatedly invoke
            on_checkpoint: Optional callback for periodic status updates
        """
        self._running = True
        result = SoakTestResult()
        self._latencies = []
        self._checkpoints = []

        start_time = time.monotonic()
        last_checkpoint = start_time
        semaphore = asyncio.Semaphore(self.concurrency)
        delay = 1.0 / self.ops_per_second if self.ops_per_second > 0 else 0.0

        async def worker():
            nonlocal last_checkpoint
            while self._running and (time.monotonic() - start_time) < self.duration:
                async with semaphore:
                    t0 = time.monotonic()
                    try:
                        await operation(*args, **kwargs)
                        elapsed_ms = (time.monotonic() - t0) * 1000
                        result.successful_operations += 1
                        self._latencies.append(elapsed_ms)
                    except Exception as exc:
                        elapsed_ms = (time.monotonic() - t0) * 1000
                        result.failed_operations += 1
                        self._latencies.append(elapsed_ms)
                        err_name = type(exc).__name__
                        result.error_types[err_name] = result.error_types.get(err_name, 0) + 1

                    result.total_operations += 1

                    # Checkpoint reporting
                    now = time.monotonic()
                    if now - last_checkpoint >= self.checkpoint_interval:
                        last_checkpoint = now
                        cp = self._make_checkpoint(result, start_time)
                        self._checkpoints.append(cp)
                        if on_checkpoint:
                            try:
                                on_checkpoint(cp)
                            except Exception:
                                pass

                if delay > 0:
                    await asyncio.sleep(delay)

        # Launch concurrent workers
        workers = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            self._running = False

        # Compute final stats
        result.total_duration_seconds = time.monotonic() - start_time
        result.operations_per_second = (
            result.total_operations / result.total_duration_seconds
            if result.total_duration_seconds > 0
            else 0.0
        )

        if self._latencies:
            sorted_lat = sorted(self._latencies)
            n = len(sorted_lat)
            result.p50_latency_ms = sorted_lat[int(n * 0.50)]
            result.p95_latency_ms = sorted_lat[min(int(n * 0.95), n - 1)]
            result.p99_latency_ms = sorted_lat[min(int(n * 0.99), n - 1)]

        self._result = result
        return result

    def stop(self) -> None:
        """Signal the soak test to stop early."""
        self._running = False

    def _make_checkpoint(self, result: SoakTestResult, start_time: float) -> dict[str, Any]:
        elapsed = time.monotonic() - start_time
        return {
            "elapsed_seconds": round(elapsed, 1),
            "total_ops": result.total_operations,
            "successful": result.successful_operations,
            "failed": result.failed_operations,
            "ops_per_second": round(result.total_operations / elapsed, 2) if elapsed > 0 else 0,
            "error_types": dict(result.error_types),
        }

    def get_checkpoints(self) -> list[dict[str, Any]]:
        return list(self._checkpoints)

    def get_result(self) -> SoakTestResult | None:
        return self._result
