"""Fail-closed provider iframe contract registry.

There are NO verified production contracts. Environment variables cannot approve
an endpoint. Add a contract only after official provider and security review,
including attribution and real-device acceptance. Tests replace this empty map
with explicitly synthetic contracts; they never authorize a real provider.
"""

import ipaddress
import re
from types import MappingProxyType
from urllib.parse import urlsplit, parse_qsl

APPROVED_CONTRACTS = MappingProxyType({})
ALLOWED_SANDBOX = frozenset({"allow-scripts", "allow-same-origin", "allow-popups"})


def valid_url(value, publisher_origin):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value):
        return False
    try:
        url = urlsplit(value)
        parent = urlsplit(publisher_origin)
        host = url.hostname or ""
        parent_host = parent.hostname or ""
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.username is not None
            or url.password is not None
            or url.fragment
            or parent.scheme != "https"
            or not parent_host
            or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host)
            or "." not in host
            or not re.fullmatch(r"[a-z-]{2,63}", host.rsplit(".", 1)[-1])
            or host.endswith((".localhost", ".local", ".internal"))
            or host == parent_host
            or host.endswith("." + parent_host)
            or parent_host.endswith("." + host)
            or re.search(r"(?:^|/)(?:invoke\.js|22)(?:/|$)", url.path, re.I)
            or url.path.lower().endswith((".js", ".mjs"))
        ):
            return False
        if any(
            re.search(
                r"token|session|auth|secret|password|signature|^(?:sid|sig)$", key, re.I
            )
            for key, _ in parse_qsl(url.query, keep_blank_values=True)
        ):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            return True
    except ValueError:
        return False


def approved_frame(value, width, height, publisher_origin):
    if (width, height) not in ((320, 50), (300, 250), (728, 90)) or not valid_url(
        value, publisher_origin
    ):
        return None
    contract = APPROVED_CONTRACTS.get(value)
    if not contract or publisher_origin not in contract.get("publisher_origins", ()):
        return None
    if (width, height) != contract.get("size"):
        return None
    sandbox = contract.get("sandbox", "").split()
    # No top-navigation, forms, downloads, storage-access or sandbox escapes.
    # Compatibility with these restrictions must be verified, never assumed.
    if not sandbox or not set(sandbox).issubset(ALLOWED_SANDBOX):
        return None
    referrer = contract.get("referrer_policy")
    if referrer not in ("origin", "no-referrer"):
        return None
    if not contract.get("evidence"):
        return None
    return dict(
        url=value,
        width=width,
        height=height,
        sandbox=" ".join(sandbox),
        referrer_policy=referrer,
    )
