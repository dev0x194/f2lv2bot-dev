import collections
import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)


def calculate_percentiles(values: list[float]) -> dict[str, float]:
    """Calculate P50, P95, P99 percentiles from a list of values."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0}

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _pct(p: float) -> float:
        idx = max(0, min(n - 1, int(math.ceil(p / 100.0 * n)) - 1))
        return round(float(sorted_vals[idx]), 3)

    return {
        "p50": _pct(50),
        "p95": _pct(95),
        "p99": _pct(99),
        "avg": round(sum(sorted_vals) / n, 3),
        "min": round(sorted_vals[0], 3),
        "max": round(sorted_vals[-1], 3),
    }


class MetricsCollector:
    """
    Centralized in-memory metrics collector with rolling latency windows.
    Zero external dependencies, purely Python stdlib.
    """

    def __init__(self, max_samples: int = 1000):
        self.max_samples = max_samples
        self._upload_latencies: collections.deque = collections.deque(maxlen=max_samples)
        self._db_latencies: collections.deque = collections.deque(maxlen=max_samples)
        self._stream_chunk_latencies: collections.deque = collections.deque(maxlen=max_samples)

        # Counters
        self.jobs_created: int = 0
        self.jobs_completed: int = 0
        self.jobs_failed: int = 0
        self.jobs_recovered: int = 0
        self.jobs_dead_lettered: int = 0
        self.jobs_cancelled: int = 0
        self.jobs_retried: int = 0

        self.bytes_uploaded: int = 0
        self.bytes_streamed: int = 0

        self.floodwait_events: int = 0
        self.circuit_trip_events: int = 0
        self.reconciliation_runs: int = 0
        self.discrepancies_detected: int = 0
        self.discrepancies_repaired: int = 0

        self._client_success_counts: collections.defaultdict[int, int] = collections.defaultdict(int)
        self._client_failure_counts: collections.defaultdict[int, int] = collections.defaultdict(int)
        self._client_bytes: collections.defaultdict[int, int] = collections.defaultdict(int)

        # History ring buffer (1440 snapshots = 24h at 1/min)
        self._history: collections.deque = collections.deque(maxlen=1440)

        self.start_time: float = time.time()

    def record_job_created(self) -> None:
        self.jobs_created += 1

    def record_job_completed(self, latency_seconds: float, client_id: int | None = None, bytes_count: int = 0) -> None:
        self.jobs_completed += 1
        if latency_seconds >= 0:
            self._upload_latencies.append(latency_seconds)
        if bytes_count > 0:
            self.bytes_uploaded += bytes_count
        if client_id is not None:
            self._client_success_counts[int(client_id)] += 1
            if bytes_count > 0:
                self._client_bytes[int(client_id)] += bytes_count

    def record_job_failed(self, client_id: int | None = None) -> None:
        self.jobs_failed += 1
        if client_id is not None:
            self._client_failure_counts[int(client_id)] += 1

    def record_job_recovered(self) -> None:
        self.jobs_recovered += 1

    def record_job_dead_letter(self) -> None:
        self.jobs_dead_lettered += 1

    def record_job_cancelled(self) -> None:
        self.jobs_cancelled += 1

    def record_job_retried(self, client_id: int | None = None) -> None:
        self.jobs_retried += 1

    def record_bytes_streamed(self, count: int) -> None:
        if count > 0:
            self.bytes_streamed += count

    def record_stream_chunk_latency(self, latency_seconds: float) -> None:
        if latency_seconds >= 0:
            self._stream_chunk_latencies.append(latency_seconds)

    def record_db_latency(self, latency_seconds: float) -> None:
        if latency_seconds >= 0:
            self._db_latencies.append(latency_seconds)

    def record_floodwait(self) -> None:
        self.floodwait_events += 1

    def record_circuit_trip(self) -> None:
        self.circuit_trip_events += 1

    def record_reconciliation(self, discrepancies_found: int, repaired: int) -> None:
        self.reconciliation_runs += 1
        self.discrepancies_detected += discrepancies_found
        self.discrepancies_repaired += repaired

    def get_summary(self) -> dict[str, Any]:
        """Return a structured summary of all tracked metrics."""
        uptime = max(1.0, time.time() - self.start_time)
        upload_latencies = list(self._upload_latencies)
        db_latencies = list(self._db_latencies)

        throughput_uploads_per_min = round((self.jobs_completed / uptime) * 60.0, 2)
        total_attempts = self.jobs_completed + self.jobs_failed
        success_rate = (
            round((self.jobs_completed / total_attempts) * 100.0, 2)
            if total_attempts > 0
            else 100.0
        )

        return {
            "uptime_seconds": round(uptime, 1),
            "counters": {
                "jobs_created": self.jobs_created,
                "jobs_completed": self.jobs_completed,
                "jobs_failed": self.jobs_failed,
                "jobs_recovered": self.jobs_recovered,
                "jobs_dead_lettered": self.jobs_dead_lettered,
                "jobs_cancelled": self.jobs_cancelled,
                "success_rate_percent": success_rate,
                "throughput_per_min": throughput_uploads_per_min,
                "bytes_uploaded": self.bytes_uploaded,
                "bytes_streamed": self.bytes_streamed,
                "floodwait_events": self.floodwait_events,
                "circuit_trip_events": self.circuit_trip_events,
                "reconciliation_runs": self.reconciliation_runs,
                "discrepancies_detected": self.discrepancies_detected,
                "discrepancies_repaired": self.discrepancies_repaired,
            },
            "latencies": {
                "upload_seconds": calculate_percentiles(upload_latencies),
                "database_seconds": calculate_percentiles(db_latencies),
            },
            "per_client": {
                cid: {
                    "success": self._client_success_counts[cid],
                    "failure": self._client_failure_counts[cid],
                    "bytes": self._client_bytes[cid],
                }
                for cid in sorted(set(list(self._client_success_counts.keys()) + list(self._client_failure_counts.keys())))
            },
        }

    def record_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        """Record an in-memory time-series snapshot."""
        snap = {
            "timestamp": time.time(),
            "summary": self.get_summary(),
            **kwargs,
        }
        self._history.append(snap)
        return snap

    def get_history(self, limit: int = 60) -> list[dict[str, Any]]:
        """Retrieve recent time-series snapshots."""
        snaps = list(self._history)
        return snaps[-limit:] if limit > 0 else snaps

    def prune_history(self, max_age_seconds: float = 86400, keep_count: int | None = None) -> int:
        """Evict snapshots older than max_age_seconds or exceeding keep_count."""
        if keep_count is not None and keep_count >= 0:
            removed = 0
            while len(self._history) > keep_count:
                self._history.popleft()
                removed += 1
            return removed
        now = time.time()
        cutoff = now - max_age_seconds
        initial_len = len(self._history)
        while self._history and self._history[0]["timestamp"] < cutoff:
            self._history.popleft()
        return initial_len - len(self._history)

    def to_prometheus(self) -> str:
        """Export metrics in standard Prometheus exposition format."""
        lines = []
        lines.append("# HELP filestream_jobs_created_total Total storage jobs created.")
        lines.append("# TYPE filestream_jobs_created_total counter")
        lines.append(f"filestream_jobs_created_total {self.jobs_created}")

        lines.append("# HELP filestream_jobs_completed_total Total storage jobs successfully completed.")
        lines.append("# TYPE filestream_jobs_completed_total counter")
        lines.append(f"filestream_jobs_completed_total {self.jobs_completed}")

        lines.append("# HELP filestream_jobs_failed_total Total storage jobs failed.")
        lines.append("# TYPE filestream_jobs_failed_total counter")
        lines.append(f"filestream_jobs_failed_total {self.jobs_failed}")

        lines.append("# HELP filestream_jobs_dead_lettered_total Total storage jobs moved to dead letter.")
        lines.append("# TYPE filestream_jobs_dead_lettered_total counter")
        lines.append(f"filestream_jobs_dead_lettered_total {self.jobs_dead_lettered}")

        lines.append("# HELP filestream_bytes_uploaded_total Total bytes uploaded to Telegram FLOG.")
        lines.append("# TYPE filestream_bytes_uploaded_total counter")
        lines.append(f"filestream_bytes_uploaded_total {self.bytes_uploaded}")

        lines.append("# HELP filestream_bytes_streamed_total Total bytes streamed to web users.")
        lines.append("# TYPE filestream_bytes_streamed_total counter")
        lines.append(f"filestream_bytes_streamed_total {self.bytes_streamed}")

        lines.append("# HELP filestream_floodwait_events_total Total FloodWait events encountered.")
        lines.append("# TYPE filestream_floodwait_events_total counter")
        lines.append(f"filestream_floodwait_events_total {self.floodwait_events}")

        lines.append("# HELP filestream_circuit_trip_events_total Total circuit breaker trip events.")
        lines.append("# TYPE filestream_circuit_trip_events_total counter")
        lines.append(f"filestream_circuit_trip_events_total {self.circuit_trip_events}")

        lines.append("# HELP filestream_reconciliation_runs_total Total reconciliation engine runs.")
        lines.append("# TYPE filestream_reconciliation_runs_total counter")
        lines.append(f"filestream_reconciliation_runs_total {self.reconciliation_runs}")

        lines.append("# HELP filestream_discrepancies_detected_total Total discrepancies detected.")
        lines.append("# TYPE filestream_discrepancies_detected_total counter")
        lines.append(f"filestream_discrepancies_detected_total {self.discrepancies_detected}")

        lines.append("# HELP filestream_discrepancies_repaired_total Total discrepancies repaired.")
        lines.append("# TYPE filestream_discrepancies_repaired_total counter")
        lines.append(f"filestream_discrepancies_repaired_total {self.discrepancies_repaired}")

        upload_pct = calculate_percentiles(list(self._upload_latencies))
        lines.append("# HELP filestream_upload_latency_seconds Upload latency percentiles.")
        lines.append("# TYPE filestream_upload_latency_seconds gauge")
        lines.append(f'filestream_upload_latency_seconds{{quantile="0.5"}} {upload_pct["p50"]}')
        lines.append(f'filestream_upload_latency_seconds{{quantile="0.95"}} {upload_pct["p95"]}')
        lines.append(f'filestream_upload_latency_seconds{{quantile="0.99"}} {upload_pct["p99"]}')

        for cid in sorted(set(list(self._client_success_counts.keys()) + list(self._client_failure_counts.keys()))):
            lines.append(f'filestream_client_success_total{{client_id="{cid}"}} {self._client_success_counts[cid]}')
            lines.append(f'filestream_client_failure_total{{client_id="{cid}"}} {self._client_failure_counts[cid]}')
            lines.append(f'filestream_client_bytes_total{{client_id="{cid}"}} {self._client_bytes[cid]}')

        return "\n".join(lines) + "\n"

    def reset_counters(self) -> None:
        """Reset counters for testing or maintenance."""
        self.jobs_created = 0
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.jobs_recovered = 0
        self.jobs_dead_lettered = 0
        self.jobs_cancelled = 0
        self.bytes_uploaded = 0
        self.bytes_streamed = 0
        self.floodwait_events = 0
        self.circuit_trip_events = 0
        self._upload_latencies.clear()
        self._db_latencies.clear()
        self._stream_chunk_latencies.clear()
        self._history.clear()


_global_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    return _global_metrics
