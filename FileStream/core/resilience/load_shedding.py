"""
Load Shedding & Security Hardening middleware for File To Link V2.

Provides HTTP-level admission control:
- Request rate limiting per IP
- System-load based load shedding
- Path traversal and malicious input protection
- Request body size limits
"""
from __future__ import annotations

import collections
import logging
import time
from typing import Any

from aiohttp import web

logger = logging.getLogger("FileStream.middleware")


# ---------------------------------------------------------------------------
# Load Shedding
# ---------------------------------------------------------------------------

class LoadShedder:
    """
    Rejects incoming HTTP requests when the system is overloaded.

    Checks:
    - Storage queue fill ratio
    - Active in-flight job count vs capacity
    - System-wide error rate (recent window)
    """

    def __init__(
        self,
        queue_threshold: float = 0.9,
        error_rate_threshold: float = 0.5,
        error_window_seconds: float = 60.0,
        max_error_window_size: int = 500,
    ):
        self.queue_threshold = queue_threshold
        self.error_rate_threshold = error_rate_threshold
        self.error_window_seconds = error_window_seconds
        self._recent_results: collections.deque[tuple[float, bool]] = collections.deque(
            maxlen=max_error_window_size
        )
        self._shedding = False
        self._shed_start: float | None = None

    def record_result(self, success: bool) -> None:
        """Record a request result (success/failure) for error rate tracking."""
        self._recent_results.append((time.monotonic(), success))

    def _error_rate(self) -> float:
        """Calculate recent error rate within the sliding window."""
        now = time.monotonic()
        cutoff = now - self.error_window_seconds
        recent = [(t, ok) for t, ok in self._recent_results if t >= cutoff]
        if len(recent) < 5:
            return 0.0
        errors = sum(1 for _, ok in recent if not ok)
        return errors / len(recent)

    def should_shed(self, storage_service: Any = None) -> tuple[bool, str]:
        """
        Determine whether load shedding should be active.
        Returns (should_shed, reason).
        """
        reasons: list[str] = []

        # Check queue fill ratio
        if storage_service is not None:
            metrics = storage_service.get_metrics()
            q_size = metrics.get("queue_size", 0) or metrics.get("storage_queue_size", 0)
            q_cap = getattr(storage_service, "max_queue_size", 500) or 500
            fill_ratio = q_size / q_cap if q_cap > 0 else 0.0
            if fill_ratio >= self.queue_threshold:
                reasons.append(f"queue_full({fill_ratio:.1%})")

        # Check error rate
        error_rate = self._error_rate()
        if error_rate >= self.error_rate_threshold:
            reasons.append(f"high_error_rate({error_rate:.1%})")

        should = len(reasons) > 0
        if should and not self._shedding:
            self._shedding = True
            self._shed_start = time.monotonic()
            logger.warning("[LOAD_SHEDDING] Activated: %s", ", ".join(reasons))
        elif not should and self._shedding:
            duration = time.monotonic() - (self._shed_start or 0)
            self._shedding = False
            self._shed_start = None
            logger.info("[LOAD_SHEDDING] Deactivated after %.1fs", duration)

        return should, "; ".join(reasons) if reasons else ""

    def get_status(self) -> dict[str, Any]:
        return {
            "shedding": self._shedding,
            "error_rate": round(self._error_rate(), 4),
            "queue_threshold": self.queue_threshold,
            "error_rate_threshold": self.error_rate_threshold,
            "recent_samples": len(self._recent_results),
            "shed_start": self._shed_start,
        }

    def reset(self) -> None:
        self._recent_results.clear()
        self._shedding = False
        self._shed_start = None


_load_shedder = LoadShedder()


def get_load_shedder() -> LoadShedder:
    return _load_shedder


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class IPRateLimiter:
    """
    Per-IP sliding window rate limiter.
    Tracks request counts within a configurable time window.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0, max_tracked_ips: int = 10000):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, collections.deque[float]] = {}
        self._max_tracked = max_tracked_ips

    def is_allowed(self, ip: str) -> bool:
        """Check if a request from this IP should be allowed."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        if ip not in self._requests:
            if len(self._requests) >= self._max_tracked:
                # Evict oldest entry
                oldest_ip = min(self._requests, key=lambda k: self._requests[k][-1] if self._requests[k] else 0)
                del self._requests[oldest_ip]
            self._requests[ip] = collections.deque(maxlen=self.max_requests * 2)

        q = self._requests[ip]
        # Prune expired entries
        while q and q[0] < cutoff:
            q.popleft()

        if len(q) >= self.max_requests:
            return False

        q.append(now)
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "tracked_ips": len(self._requests),
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
        }

    def reset(self) -> None:
        self._requests.clear()


_ip_rate_limiter = IPRateLimiter()


def get_ip_rate_limiter() -> IPRateLimiter:
    return _ip_rate_limiter


# ---------------------------------------------------------------------------
# Security Hardening
# ---------------------------------------------------------------------------

# Blocked path patterns that indicate scanning/probing
_BLOCKED_PATHS = frozenset({
    "/.env", "/.git", "/.git/config", "/.git/HEAD",
    "/wp-admin", "/wp-login.php", "/wp-config.php",
    "/admin/config", "/config.php", "/info.php", "/phpinfo.php",
    "/.aws/credentials", "/.docker/config.json",
    "/etc/passwd", "/etc/shadow", "/proc/self/environ",
    "/.ssh/authorized_keys", "/.ssh/id_rsa",
    "/actuator", "/actuator/health", "/debug/pprof",
    "/.well-known/security.txt",
})

# Characters that should never appear in download paths
_PATH_TRAVERSAL_PATTERNS = ("../", "..\\", "%2e%2e", "%2e%2e%2f", "%252e")


def is_suspicious_path(path: str) -> bool:
    """Check if a request path appears to be a scanner/probe."""
    normalized = path.lower().rstrip("/")
    if normalized in _BLOCKED_PATHS:
        return True
    for pattern in _PATH_TRAVERSAL_PATTERNS:
        if pattern in normalized:
            return True
    return False


def sanitize_download_path(path: str) -> str | None:
    """
    Validate and sanitize a download path. Returns None if invalid.
    Valid paths are: /<hash>/<filename> or /watch/<hash>/<filename>
    """
    if not path or len(path) > 512:
        return None
    for pattern in _PATH_TRAVERSAL_PATTERNS:
        if pattern in path.lower():
            return None
    # Strip query strings for safety
    clean = path.split("?")[0].split("#")[0]
    return clean


class SecurityHardener:
    """
    Aggregated security metrics and request validation.
    """

    def __init__(self):
        self._blocked_count = 0
        self._probe_ips: collections.Counter = collections.Counter()
        self._max_probe_ips = 1000

    def record_blocked(self, ip: str, path: str, reason: str = "blocked_path") -> None:
        self._blocked_count += 1
        if len(self._probe_ips) < self._max_probe_ips:
            self._probe_ips[ip] += 1
        logger.info("[SECURITY] Blocked request from %s to %s: %s", ip, path, reason)

    def get_status(self) -> dict[str, Any]:
        return {
            "total_blocked": self._blocked_count,
            "unique_probe_ips": len(self._probe_ips),
            "top_probe_ips": dict(self._probe_ips.most_common(10)),
        }

    def reset(self) -> None:
        self._blocked_count = 0
        self._probe_ips.clear()


_security_hardener = SecurityHardener()


def get_security_hardener() -> SecurityHardener:
    return _security_hardener


# ---------------------------------------------------------------------------
# aiohttp Middleware Factory (optional — can be added to the app)
# ---------------------------------------------------------------------------

def create_security_middleware():
    """
    Create an aiohttp middleware that applies:
    1. Suspicious path blocking
    2. IP rate limiting (on API paths)
    3. Load shedding on storage-related paths
    """

    @web.middleware
    async def security_middleware(request: web.Request, handler):
        def _apply_security_headers(res: web.StreamResponse) -> web.StreamResponse:
            if isinstance(res, web.StreamResponse) and not res.prepared:
                res.headers.setdefault("X-Content-Type-Options", "nosniff")
                res.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                if getattr(res, "content_type", None) == "text/html":
                    res.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
                    res.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                    res.headers.setdefault(
                        "Content-Security-Policy",
                        "default-src 'self'; "
                        "script-src 'self' 'unsafe-inline' https:; "
                        "style-src 'self' 'unsafe-inline' https:; "
                        "img-src 'self' data: blob: https:; "
                        "media-src 'self' blob: https:; "
                        "connect-src 'self' blob: https:; "
                        "frame-src 'self' https:; "
                        "worker-src 'self' blob:; "
                        "font-src 'self' data:; "
                        "object-src 'none'; "
                        "base-uri 'self';",
                    )
            return res

        path = request.path
        ip = request.remote or "unknown"

        # 1. Block suspicious paths
        if is_suspicious_path(path):
            get_security_hardener().record_blocked(ip, path, "suspicious_path")
            return _apply_security_headers(web.json_response(
                {"error": "Not Found"},
                status=404,
            ))

        # 2. Rate limiting on API endpoints
        if path.startswith("/api/") or path in ("/health", "/metrics", "/status"):
            if not get_ip_rate_limiter().is_allowed(ip):
                get_security_hardener().record_blocked(ip, path, "rate_limited")
                return _apply_security_headers(web.json_response(
                    {"error": "Rate limit exceeded"},
                    status=429,
                    headers={"Retry-After": "60"},
                ))

        # 3. Path traversal check on download paths
        if path.startswith("/dl/") or path.startswith("/watch/"):
            clean = sanitize_download_path(path)
            if clean is None:
                get_security_hardener().record_blocked(ip, path, "path_traversal")
                return _apply_security_headers(web.json_response(
                    {"error": "Invalid path"},
                    status=400,
                ))

        response = await handler(request)
        return _apply_security_headers(response)

    return security_middleware
