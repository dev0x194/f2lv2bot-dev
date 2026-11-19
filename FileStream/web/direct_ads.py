"""Validation for explicitly unsafe, opt-in parent-context JavaScript tags."""

import ipaddress
import re
from urllib.parse import urlsplit

# Reviewed invocation hosts from the supplied publisher tags. No environment override.
SCRIPT_HOSTS = frozenset(
    {
        "bellnewyork.org",
        "accountut.com",
        "www.highperformanceformat.com",
        "cardinaltangible.com",
    }
)
SMARTLINK_HOSTS = frozenset({"ardance.org", "cardinaltangible.com"})


def script_url(value, hosts=None):
    hosts = SCRIPT_HOSTS if hosts is None else hosts
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) <= 32 or ord(c) == 127 or c in "\\\"'<>" for c in value)
    ):
        return ""
    try:
        u = urlsplit(value)
        host = u.hostname or ""
        if (
            u.scheme != "https"
            or u.port not in (None, 443)
            or u.username is not None
            or u.password is not None
            or u.fragment
            or host not in hosts
            or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", host)
            or host.endswith((".local", ".localhost", ".internal"))
            or u.path in ("", "/")
        ):
            return ""
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            return value
    except ValueError:
        return ""


def banner(slot, hosts=None):
    key = str(slot.get("key", ""))
    size = (slot.get("width"), slot.get("height"))
    url = script_url(slot.get("invoke_url", ""), hosts)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or size not in (
        (320, 50),
        (300, 250),
        (728, 90),
    ):
        return False
    if "/22/" in urlsplit(url).path and urlsplit(url).path != "/22/" + key:
        return False
    return bool(url)


def smartlink_url(value):
    return script_url(value, SMARTLINK_HOSTS)
