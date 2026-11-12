import asyncio
import logging
import time
try:
    import cloudscraper
except ImportError:
    cloudscraper = None
from abc import ABC, abstractmethod
from base64 import b64encode
from random import random, choice
from urllib.parse import quote, urlparse
from functools import partial
from FileStream.config import Telegram

# Configure Logger
logger = logging.getLogger(__name__)

class ShortenerPlugin(ABC):
    @classmethod
    @abstractmethod
    def matches(cls, domain: str) -> bool:
        pass

    @abstractmethod
    def shorten(self, url: str, api_key: str) -> str:
        pass

# -----------------[ GPlinks Plugin ]----------------- #
class GPLinksPlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        return "gplinks" in domain.lower()

    def shorten(self, url: str, api_key: str) -> str:
        if not self.session:
            return url

        # Prefer the site set by user, then common GPLinks API hosts.
        # API supports text mode: ...&format=text (returns only short URL).
        domain = getattr(self, "domain", "gplinks.com") or "gplinks.com"
        parsed = urlparse(domain if "://" in domain else f"https://{domain}")
        host = (parsed.netloc or parsed.path).strip().lower()

        candidates = []
        if host:
            candidates.append(host)
            if host in ("gplinks.com", "gplinks.in"):
                candidates.append("api.gplinks.com")
        candidates.extend(["api.gplinks.com", "gplinks.com", "gplinks.in"])

        # Keep order but remove duplicates
        seen = set()
        ordered_hosts = []
        for h in candidates:
            if h and h not in seen:
                seen.add(h)
                ordered_hosts.append(h)

        timeout = getattr(self, "request_timeout", 5.0)
        for h in ordered_hosts:
            target = f"https://{h}/api?api={api_key}&url={quote(url)}&format=text"
            try:
                response = self.session.get(target, timeout=timeout)
                if response.status_code != 200:
                    continue

                text_out = response.text.strip()
                if text_out.startswith("http"):
                    return text_out

                # Fallback: some deployments return JSON
                try:
                    data = response.json()
                    if data.get("status") == "success":
                        return data.get("shortenedUrl") or data.get("short_url") or url
                except Exception:
                    pass
            except Exception as e:
                logger.debug(f"GPlinks request failed ({h}): {e}")
        return url

# -----------------[ ShrinkMe.io Plugin ]----------------- #
class ShrinkMePlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        return "shrinkme" in domain.lower()

    def shorten(self, url: str, api_key: str) -> str:
        if not self.session:
            return url
        # ShrinkMe API: https://shrinkme.io/api?api=API_KEY&url=URL
        target = f"https://shrinkme.io/api?api={api_key}&url={quote(url)}"
        try:
            response = self.session.get(target, timeout=getattr(self, "request_timeout", 5.0))
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    return data.get("shortenedUrl")
        except Exception as e:
            logger.debug(f"ShrinkMe request failed: {e}")
        return url

# -----------------[ Ouo.io Plugin ]----------------- #
class OuoIoPlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        return "ouo.io" in domain.lower() or "ouo.press" in domain.lower()

    def shorten(self, url: str, api_key: str) -> str:
        if not self.session:
            return url
        # Ouo API is different: http://ouo.io/api/KEY?s=URL
        target = f"http://ouo.io/api/{api_key}?s={quote(url)}"
        try:
            response = self.session.get(target, timeout=getattr(self, "request_timeout", 5.0))
            if response.status_code == 200:
                return response.text.strip()
        except Exception as e:
            logger.debug(f"Ouo request failed: {e}")
        return url

# -----------------[ YOURLS Plugin ]----------------- #
class YOURLSPlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        dl = domain.lower()
        return "yourls" in dl or "yourls-api.php" in dl

    def shorten(self, url: str, api_key: str) -> str:
        if not self.session:
            return url
        try:
            parsed = urlparse(self.domain if "://" in self.domain else f"https://{self.domain}")
            base = parsed.geturl()
            if "yourls-api.php" not in base.lower():
                base = base.rstrip("/") + "/yourls-api.php"
            target = f"{base}?signature={api_key}&action=shorturl&format=json&url={quote(url)}"
            response = self.session.get(target, timeout=getattr(self, "request_timeout", 5.0))
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    return data.get("shorturl") or data.get("short_url") or url
        except Exception as e:
            logger.debug(f"YOURLS request failed: {e}")
        return url

# -----------------[ Linkvertise Plugin (Legacy) ]----------------- #
class LinkvertisePlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        return "linkvertise" in domain.lower()

    def shorten(self, url: str, api_key: str) -> str:
        encoded_url = quote(b64encode(url.encode("utf-8")))
        # Returns one of the dynamic domains
        return choice([
            f"https://link-to.net/{api_key}/{random() * 1000}/dynamic?r={encoded_url}",
            f"https://up-to-down.net/{api_key}/{random() * 1000}/dynamic?r={encoded_url}",
            f"https://direct-link.net/{api_key}/{random() * 1000}/dynamic?r={encoded_url}",
            f"https://file-link.net/{api_key}/{random() * 1000}/dynamic?r={encoded_url}",
        ])

# -----------------[ Generic / Fallback Plugin ]----------------- #
class GenericShortenerPlugin(ShortenerPlugin):
    @classmethod
    def matches(cls, domain: str) -> bool:
        return True

    def _api_candidates(self) -> list[str]:
        parsed = urlparse(self.domain if "://" in self.domain else f"https://{self.domain}")
        host = (parsed.netloc or parsed.path).strip().lower()

        candidates: list[str] = []

        # If user passed full endpoint/path, preserve it and normalize to /api suffix.
        if parsed.netloc:
            base = f"{parsed.scheme or 'https'}://{parsed.netloc}"
            path = (parsed.path or "").rstrip("/")
            if path:
                if path.endswith("/api"):
                    candidates.append(f"{base}{path}")
                else:
                    candidates.append(f"{base}{path}/api")
            else:
                candidates.append(f"{base}/api")

        # Standard host-based endpoint.
        if host:
            candidates.append(f"https://{host}/api")

            # Support split deploys where web host and API host differ by api.<host>
            if not host.startswith("api."):
                candidates.append(f"https://api.{host}/api")

        # De-duplicate while preserving order.
        seen = set()
        ordered: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
        return ordered

    def shorten(self, url: str, api_key: str) -> str:
        if not self.session:
            return url

        timeout = getattr(self, "request_timeout", 5.0)
        for endpoint in self._api_candidates():
            target_url = f"{endpoint}?api={api_key}&url={quote(url)}"
            try:
                response = self.session.get(target_url, timeout=timeout)
                if response.status_code != 200:
                    continue

                text_out = response.text.strip()
                if text_out.startswith("http"):
                    return text_out

                data = response.json()
                # Common success keys across providers
                if data.get("status") == "success":
                    return data.get("shortenedUrl") or data.get("short_url") or data.get("link") or url
                if "shortenedUrl" in data:
                    return data["shortenedUrl"]
                if "link" in data:
                    return data["link"]
            except Exception as e:
                logger.debug(f"Generic shortener request failed ({endpoint}): {e}")

        return url

# -----------------[ System Logic ]----------------- #
class ShortenerSystem:
    def __init__(self):
        self.session = None
        self.plugin = None
        self.ready = False
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[str, float]] = {}
        self._cache_ttl = 3600
        self._fail_count = 0
        self._cooldown_until = 0.0
        self._request_timeout = float(getattr(Telegram, "URL_SHORTENER_TIMEOUT", 5) or 5)
        self._executor_timeout = self._request_timeout + 1.5
        self._fail_threshold = int(getattr(Telegram, "URL_SHORTENER_FAIL_THRESHOLD", 2) or 2)
        self._cooldown_seconds = int(getattr(Telegram, "URL_SHORTENER_COOLDOWN", 300) or 300)
        self._last_timeout_log = 0.0

    def _get_plugin_class(self, domain: str):
        # Check specific plugins first (exclude Generic from this loop)
        for plugin_class in ShortenerPlugin.__subclasses__():
            if plugin_class is not GenericShortenerPlugin and plugin_class.matches(domain):
                return plugin_class
        # Default to Generic if no specific match found
        return GenericShortenerPlugin

    def _mark_failure(self, reason: str):
        self._fail_count += 1
        if self._fail_count >= self._fail_threshold:
            self._cooldown_until = time.time() + self._cooldown_seconds
            logger.warning(
                f"Shortener entered cooldown for {self._cooldown_seconds}s after repeated failures"
            )

        # Avoid noisy duplicate timeout logs
        now = time.time()
        if now - self._last_timeout_log > 20:
            logger.error(reason)
            self._last_timeout_log = now

    async def initialize(self) -> bool:
        if self.ready:
            return True

        site = getattr(Telegram, "URL_SHORTENER_SITE", "")
        api_key = getattr(Telegram, "URL_SHORTENER_API_KEY", "")

        if not (site and api_key):
            if not getattr(self, "_warned", False):
                logger.warning("Shortener Config missing in config.py")
                self._warned = True
            return False

        if not cloudscraper:
            logger.warning("cloudscraper library is not installed. URL Shortener disabled.")
            return False

        try:
            # Initialize cloudscraper in a separate thread to avoid blocking
            self.session = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    cloudscraper.create_scraper,
                    browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True},
                    delay=1
                )
            )

            plugin_class = self._get_plugin_class(site)
            self.plugin = plugin_class()
            self.plugin.session = self.session
            self.plugin.domain = site
            self.plugin.request_timeout = self._request_timeout
            self.ready = True
            logger.info(
                f"Shortener System Initialized for: {site} using {plugin_class.__name__} "
                f"(timeout={self._request_timeout}s, fail_threshold={self._fail_threshold})"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to initialize ShortenerSystem: {e}")
            return False

    async def short_url(self, url: str) -> str:
        if not self.ready:
            if not await self.initialize():
                return url

        now = time.time()
        if self._cooldown_until:
            if now < self._cooldown_until:
                return url
            # Cooldown elapsed; allow retry
            self._cooldown_until = 0.0
            self._fail_count = 0

        # Avoid shortening if URL already points to the shortener domain
        try:
            if self.plugin and getattr(self.plugin, "domain", None):
                parsed_short = urlparse(self.plugin.domain if "://" in self.plugin.domain else f"https://{self.plugin.domain}")
                short_domain = parsed_short.netloc or parsed_short.path
                parsed_url = urlparse(url)
                if short_domain and short_domain in (parsed_url.netloc or ""):
                    return url
        except Exception:
            pass

        # Cache lookup
        cached = self._cache.get(url)
        if cached and cached[1] > now:
            return cached[0]

        try:
            async with self._lock:
                # Re-check cache after waiting on lock
                cached = self._cache.get(url)
                if cached and cached[1] > time.time():
                    return cached[0]

                # Run with timeout to prevent blocking the executor
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None,
                        self.plugin.shorten,
                        url,
                        Telegram.URL_SHORTENER_API_KEY
                    ),
                    timeout=self._executor_timeout
                )
                final = result or url
                if final == url:
                    self._mark_failure("Shortener returned original URL (fallback used)")
                else:
                    self._fail_count = 0
                    self._cooldown_until = 0.0
                self._cache[url] = (final, time.time() + self._cache_ttl)
                return final
        except asyncio.TimeoutError:
            self._mark_failure("Shortener request timed out")
            return url
        except Exception as e:
            self._mark_failure(f"Error shortening URL: {e}")
            return url

_system = ShortenerSystem()

async def shorten(url: str) -> str:
    if not _system.ready:
        await _system.initialize()
    return await _system.short_url(url)

# Alias for compatibility with bot_utils.py
get_short_link = shorten
