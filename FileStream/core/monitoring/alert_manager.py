from __future__ import annotations

import collections
import dataclasses
import enum
import logging
import time
from typing import Any, Callable

logger = logging.getLogger("FileStream.alerts")


class AlertSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclasses.dataclass
class Alert:
    key: str
    severity: AlertSeverity
    message: str
    timestamp: float = dataclasses.field(default_factory=time.time)
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "details": self.details,
        }


class AlertManager:
    """
    Central operational alerting engine for File To Link V2.
    Monitors worker pool degradation, queue backpressure, dead-letter spikes,
    circuit breaker trips, and database connectivity.
    Includes alert throttling / de-duplication to prevent notification storms.
    """

    def __init__(self, throttle_seconds: float = 300.0, max_history: int = 200):
        self.throttle_seconds = float(throttle_seconds)
        self._last_alert_times: dict[str, float] = {}
        self._active_alerts: dict[str, Alert] = {}
        self._alert_history: collections.deque[Alert] = collections.deque(maxlen=max_history)
        self._handlers: list[Callable[[Alert], Any]] = []

    def register_handler(self, handler: Callable[[Alert], Any]) -> None:
        self._handlers.append(handler)

    def trigger_alert(
        self,
        key: str,
        severity: AlertSeverity,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Alert | None:
        """
        Record and dispatch an alert if not throttled.
        Returns the Alert if dispatched, None if throttled.
        """
        now = time.time()
        last_time = self._last_alert_times.get(key, 0.0)
        is_throttled = (now - last_time) < self.throttle_seconds

        alert = Alert(
            key=key,
            severity=severity,
            message=message,
            timestamp=now,
            details=details or {},
        )

        self._active_alerts[key] = alert
        self._alert_history.append(alert)

        if is_throttled:
            logger.debug("[ALERT_THROTTLED] %s [%s]: %s", severity.value, key, message)
            return None

        self._last_alert_times[key] = now
        logger.warning("[ALERT_TRIGGERED] %s [%s]: %s", severity.value, key, message)

        for handler in self._handlers:
            try:
                res = handler(alert)
                if hasattr(res, "__await__"):
                    import asyncio
                    asyncio.create_task(res)
            except Exception as ex:
                logger.error("Error executing alert handler: %s", ex)

        return alert

    def resolve_alert(self, key: str) -> None:
        """Mark an alert condition as resolved."""
        if key in self._active_alerts:
            resolved_alert = self._active_alerts.pop(key)
            logger.info("[ALERT_RESOLVED] [%s]: %s", key, resolved_alert.message)

    def get_active_alerts(self) -> list[Alert]:
        return list(self._active_alerts.values())

    def get_alert_history(self, limit: int = 50) -> list[Alert]:
        alerts = list(self._alert_history)
        return alerts[-limit:] if limit > 0 else alerts

    async def evaluate_system_health(
        self,
        db: Any,
        storage_service: Any = None,
    ) -> list[Alert]:
        """
        Evaluate full system health conditions and trigger alerts accordingly.
        """
        triggered: list[Alert] = []

        # 1. Check MongoDB Connectivity
        db_healthy = True
        try:
            if hasattr(db, "ping") and callable(db.ping):
                await db.ping()
            elif hasattr(db, "client") and getattr(db.client, "admin", None):
                await db.client.admin.command("ping")
            elif hasattr(db, "total_users_count"):
                await db.total_users_count()
        except Exception as exc:
            db_healthy = False
            a = self.trigger_alert(
                key="MONGODB_DISCONNECTED",
                severity=AlertSeverity.CRITICAL,
                message=f"MongoDB connection failed: {exc}",
                details={"error": str(exc)},
            )
            if a:
                triggered.append(a)

        if db_healthy:
            self.resolve_alert("MONGODB_DISCONNECTED")

        # 2. Check Storage Service & Worker Pool
        if storage_service is not None:
            metrics = storage_service.get_metrics()
            active_workers = metrics.get("storage_workers", 0)
            queue_size = metrics.get("storage_queue_size", 0)
            queue_cap = metrics.get("storage_queue_capacity", 500)

            # Worker pool check
            from FileStream.config import Telegram
            min_workers = getattr(Telegram, "FLOG_WORKERS_MIN", 2)
            if active_workers < min_workers and not getattr(storage_service, "_shutting_down", False):
                a = self.trigger_alert(
                    key="WORKER_POOL_DEGRADED",
                    severity=AlertSeverity.WARNING,
                    message=f"Active storage workers ({active_workers}) below minimum threshold ({min_workers})",
                    details={"active_workers": active_workers, "min_workers": min_workers},
                )
                if a:
                    triggered.append(a)
            else:
                self.resolve_alert("WORKER_POOL_DEGRADED")

            # Queue backpressure check
            if queue_cap > 0 and (queue_size / queue_cap) >= 0.8:
                a = self.trigger_alert(
                    key="QUEUE_BACKED_UP",
                    severity=AlertSeverity.WARNING,
                    message=f"Storage buffer near capacity ({queue_size}/{queue_cap} jobs, {round(queue_size/queue_cap*100)}%)",
                    details={"queue_size": queue_size, "capacity": queue_cap},
                )
                if a:
                    triggered.append(a)
            else:
                self.resolve_alert("QUEUE_BACKED_UP")

        # 3. Check Circuit Breakers
        try:
            from FileStream.core.resilience.circuit_breaker import get_circuit_breaker_registry, CircuitBreakerState
            registry = get_circuit_breaker_registry()
            tripped = []
            for cid, breaker in registry._breakers.items():
                if breaker.state == CircuitBreakerState.OPEN:
                    tripped.append(cid)
            if tripped:
                a = self.trigger_alert(
                    key="CIRCUIT_BREAKER_TRIPPED",
                    severity=AlertSeverity.WARNING,
                    message=f"Circuit breakers TRIPPED (OPEN) for clients: {tripped}",
                    details={"tripped_clients": tripped},
                )
                if a:
                    triggered.append(a)
            else:
                self.resolve_alert("CIRCUIT_BREAKER_TRIPPED")
        except Exception:
            pass

        # 4. Check Dead-Letter Spikes
        try:
            if hasattr(db, "get_storage_jobs_stats"):
                stats = await db.get_storage_jobs_stats()
                dead_letter = stats.get("dead_letter", 0)
                if dead_letter > 20:
                    a = self.trigger_alert(
                        key="HIGH_DEAD_LETTER_RATE",
                        severity=AlertSeverity.WARNING,
                        message=f"High count of dead-letter storage jobs detected: {dead_letter}",
                        details={"dead_letter_count": dead_letter},
                    )
                    if a:
                        triggered.append(a)
                else:
                    self.resolve_alert("HIGH_DEAD_LETTER_RATE")
        except Exception:
            pass

        # 5. Check FLOG Channel Health
        try:
            from FileStream.core.runtime.flog_channels import get_channel_monitor
            monitor = get_channel_monitor()
            unhealthy_channels = [ch for ch in monitor.list_channels_health() if not ch.get("is_healthy")]
            if unhealthy_channels:
                cids = [ch["channel_id"] for ch in unhealthy_channels]
                a = self.trigger_alert(
                    key="FLOG_CHANNEL_DEGRADED",
                    severity=AlertSeverity.CRITICAL,
                    message=f"FLOG storage channels degraded/unreachable: {cids}",
                    details={"channels": unhealthy_channels},
                )
                if a:
                    triggered.append(a)
            else:
                self.resolve_alert("FLOG_CHANNEL_DEGRADED")
        except Exception:
            pass

        return triggered

    def reset(self) -> None:
        self._last_alert_times.clear()
        self._active_alerts.clear()
        self._alert_history.clear()


_alert_manager = AlertManager()


def get_alert_manager() -> AlertManager:
    return _alert_manager
