import enum
import logging
import time
from typing import Any
from FileStream.config import Telegram

logger = logging.getLogger(__name__)


class CircuitBreakerState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    COOLDOWN = "COOLDOWN"


class ClientCircuitBreaker:
    """
    Per-Telegram client circuit breaker.
    Protects Telegram workers from cascading failures, FloodWait storms, and dead sessions.
    """

    def __init__(
        self,
        client_id: int,
        failure_threshold: int | None = None,
        cooldown_duration: float | None = None,
        half_open_probes: int | None = None,
    ):
        self.client_id = int(client_id)
        self.failure_threshold = (
            failure_threshold
            if failure_threshold is not None
            else getattr(Telegram, "FLOG_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5)
        )
        self.cooldown_duration = (
            cooldown_duration
            if cooldown_duration is not None
            else getattr(Telegram, "FLOG_CIRCUIT_BREAKER_TIMEOUT_SECONDS", 30.0)
        )
        self.half_open_probes = (
            half_open_probes
            if half_open_probes is not None
            else getattr(Telegram, "FLOG_CIRCUIT_BREAKER_HALF_OPEN_PROBES", 1)
        )

        self.state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self.failure_count: int = 0
        self.success_count: int = 0
        self.cooldown_until: float = 0.0
        self.last_error: str | None = None
        self.last_state_change: float = time.time()
        self._probe_in_flight: bool = False

    def can_execute(self) -> bool:
        """Return True if client is eligible to accept jobs."""
        now = time.time()

        if self.state == CircuitBreakerState.COOLDOWN:
            if now >= self.cooldown_until:
                logger.info("[CIRCUIT_BREAKER] Client %d cooldown elapsed. Restoring CLOSED.", self.client_id)
                self._transition_to(CircuitBreakerState.CLOSED)
                return True
            return False

        if self.state == CircuitBreakerState.CLOSED:
            return True

        if self.state == CircuitBreakerState.OPEN:
            if now >= self.cooldown_until:
                logger.info("[CIRCUIT_BREAKER] Client %d timeout elapsed. Moving to HALF_OPEN probe.", self.client_id)
                self._transition_to(CircuitBreakerState.HALF_OPEN)
                self._probe_in_flight = True
                return True
            return False

        if self.state == CircuitBreakerState.HALF_OPEN:
            if not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

        return False

    def record_success(self) -> None:
        """Record a successful operation on this client."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.half_open_probes:
                logger.info(
                    "[CIRCUIT_BREAKER] Client %d probe succeeded. Closing circuit breaker.",
                    self.client_id,
                )
                self._transition_to(CircuitBreakerState.CLOSED)
                self.failure_count = 0
                self._probe_in_flight = False
        elif self.state == CircuitBreakerState.CLOSED:
            self.failure_count = 0

    def record_failure(
        self,
        error: Any,
        is_floodwait: bool = False,
        floodwait_seconds: float = 0.0,
        is_fatal: bool = False,
    ) -> None:
        """Record an error on this client, tripping circuit breaker if threshold exceeded."""
        now = time.time()
        error_str = str(error)
        self.last_error = error_str

        if is_floodwait:
            return

        # Safeguard: Media-level payload errors (e.g. MediaEmpty, FileReferenceInvalid)
        # are payload-specific and must NEVER permanently open the client's circuit breaker.
        # Only true client-level authentication / account failures should be fatal.
        non_fatal_media_patterns = (
            "media_empty",
            "mediaempty",
            "filereferenceinvalid",
            "filereferenceexpired",
            "file_reference_invalid",
            "file_reference_expired",
            "mediainvalid",
            "media_invalid",
            "filepartsinvalid",
            "file_parts_invalid",
            "file_id_invalid",
            "file not found",
            "filenotfound",
        )
        if is_fatal and any(pattern in error_str.lower() for pattern in non_fatal_media_patterns):
            is_fatal = False

        if is_fatal:
            self.cooldown_until = now + 86400 * 365
            self._transition_to(CircuitBreakerState.OPEN)
            self._probe_in_flight = False
            logger.critical(
                "[CIRCUIT_BREAKER] Client %d fatal error: %s. Circuit permanently OPEN.",
                self.client_id,
                error_str,
            )
            return

        self.failure_count += 1
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.cooldown_until = now + self.cooldown_duration
            self._transition_to(CircuitBreakerState.OPEN)
            self._probe_in_flight = False
            logger.warning(
                "[CIRCUIT_BREAKER] Client %d probe failed (%s). Reopening circuit for %.1fs",
                self.client_id,
                error_str,
                self.cooldown_duration,
            )
        elif self.failure_count >= self.failure_threshold:
            self.cooldown_until = now + self.cooldown_duration
            self._transition_to(CircuitBreakerState.OPEN)
            self._probe_in_flight = False
            logger.warning(
                "[CIRCUIT_BREAKER] Client %d exceeded failure threshold (%d/%d): %s. Tripping circuit to OPEN for %.1fs",
                self.client_id,
                self.failure_count,
                self.failure_threshold,
                error_str,
                self.cooldown_duration,
            )

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED."""
        self._transition_to(CircuitBreakerState.CLOSED)
        self.failure_count = 0
        self.success_count = 0
        self.cooldown_until = 0.0
        self.last_error = None
        self._probe_in_flight = False

    def trip(self, duration_seconds: float = 30.0, reason: str = "manual") -> None:
        """Manually trip the circuit breaker to OPEN."""
        self.cooldown_until = time.time() + duration_seconds
        self.last_error = f"Manual trip: {reason}"
        self._transition_to(CircuitBreakerState.OPEN)
        self._probe_in_flight = False

    def get_status(self) -> dict[str, Any]:
        """Return human-readable status dict."""
        now = time.time()
        cooldown_rem = max(0.0, self.cooldown_until - now) if self.cooldown_until > now else 0.0
        return {
            "client_id": self.client_id,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "cooldown_remaining": round(cooldown_rem, 1),
            "last_error": self.last_error,
            "is_available": self.can_execute(),
        }

    def _transition_to(self, new_state: CircuitBreakerState) -> None:
        if self.state != new_state:
            self.state = new_state
            self.last_state_change = time.time()
            if new_state == CircuitBreakerState.HALF_OPEN:
                self.success_count = 0


class CircuitBreakerRegistry:
    """Registry managing circuit breakers across all configured Telegram clients."""

    def __init__(self):
        self._breakers: dict[int, ClientCircuitBreaker] = {}

    def get(self, client_id: int) -> ClientCircuitBreaker:
        cid = int(client_id)
        if cid not in self._breakers:
            self._breakers[cid] = ClientCircuitBreaker(cid)
        return self._breakers[cid]

    get_breaker = get

    def can_execute(self, client_id: int) -> bool:
        return self.get(client_id).can_execute()

    def record_success(self, client_id: int) -> None:
        self.get(client_id).record_success()

    def record_failure(
        self,
        client_id: int,
        error: Any,
        is_floodwait: bool = False,
        floodwait_seconds: float = 0.0,
        is_fatal: bool = False,
    ) -> None:
        self.get(client_id).record_failure(
            error=error,
            is_floodwait=is_floodwait,
            floodwait_seconds=floodwait_seconds,
            is_fatal=is_fatal,
        )

    def reset_client(self, client_id: int) -> None:
        cid = int(client_id)
        if cid in self._breakers:
            self._breakers[cid].reset()

    def reset_all(self) -> None:
        for b in self._breakers.values():
            b.reset()

    def get_all_statuses(self) -> dict[int, dict[str, Any]]:
        return {cid: b.get_status() for cid, b in self._breakers.items()}


_global_registry = CircuitBreakerRegistry()


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    return _global_registry
