from __future__ import annotations

import copy
import time
from collections import defaultdict


FILE_INFO_TTL = 30
FILE_REFERENCE_TTL = 15
FLOG_VALIDATION_TTL = 5

_FILE_INFO_CACHE: dict[str, tuple[float, dict]] = {}
_FILE_REFERENCE_CACHE: dict[str, tuple[float, dict, dict | None]] = {}
_FILE_REFERENCE_ALIASES: dict[str, set[str]] = defaultdict(set)
_FLOG_VALIDATION_CACHE: dict[str, tuple[float, int, int | None]] = {}


def _now() -> float:
    return time.time()


def _clone(value):
    return copy.deepcopy(value)


def _purge_expired_cache(cache: dict) -> None:
    now = _now()
    expired = [key for key, value in cache.items() if value[0] <= now]
    for key in expired:
        cache.pop(key, None)


def _purge_expired_file_reference_cache() -> None:
    now = _now()
    expired = [key for key, value in _FILE_REFERENCE_CACHE.items() if value[0] <= now]
    for key in expired:
        entry = _FILE_REFERENCE_CACHE.pop(key, None)
        if not entry:
            continue
        file_info = entry[1] or {}
        file_id = str(file_info.get("_id") or "")
        if not file_id:
            continue
        aliases = _FILE_REFERENCE_ALIASES.get(file_id)
        if not aliases:
            continue
        aliases.discard(key)
        if not aliases:
            _FILE_REFERENCE_ALIASES.pop(file_id, None)


def get_cached_file_info(file_id: str) -> dict | None:
    file_id = str(file_id or "")
    if not file_id:
        return None
    _purge_expired_cache(_FILE_INFO_CACHE)
    entry = _FILE_INFO_CACHE.get(file_id)
    if not entry:
        return None
    return _clone(entry[1])


def cache_file_info(file_info: dict, ttl: int = FILE_INFO_TTL) -> None:
    file_id = str((file_info or {}).get("_id") or "")
    if not file_id:
        return
    _FILE_INFO_CACHE[file_id] = (_now() + max(int(ttl), 1), _clone(file_info))


def get_cached_file_reference(key: str) -> tuple[dict, dict | None] | None:
    key = str(key or "")
    if not key:
        return None
    _purge_expired_file_reference_cache()
    entry = _FILE_REFERENCE_CACHE.get(key)
    if not entry:
        return None
    return _clone(entry[1]), _clone(entry[2])


def cache_file_reference(
    key: str,
    file_info: dict,
    public_link: dict | None,
    ttl: int = FILE_REFERENCE_TTL,
) -> None:
    file_id = str((file_info or {}).get("_id") or "")
    key = str(key or "")
    if not file_id or not key:
        return

    expires_at = _now() + max(int(ttl), 1)
    aliases = {key, file_id}

    public_id = str((public_link or {}).get("public_id") or "")
    if public_id:
        aliases.add(public_id)

    payload = (_clone(file_info), _clone(public_link))
    for alias in aliases:
        _FILE_REFERENCE_CACHE[alias] = (expires_at, payload[0], payload[1])

    _FILE_REFERENCE_ALIASES[file_id].update(aliases)
    cache_file_info(file_info, ttl=max(ttl, FILE_INFO_TTL))


def is_flog_validation_fresh(file_info: dict) -> bool:
    file_id = str((file_info or {}).get("_id") or "")
    flog_msg_id = (file_info or {}).get("flog_msg_id")
    flog_channel_id = (file_info or {}).get("flog_channel_id")
    if not file_id or not flog_msg_id:
        return False

    _purge_expired_cache(_FLOG_VALIDATION_CACHE)
    entry = _FLOG_VALIDATION_CACHE.get(file_id)
    if not entry:
        return False
    try:
        current_channel_id = int(flog_channel_id) if flog_channel_id not in (None, "") else None
    except Exception:
        current_channel_id = None
    return int(entry[1]) == int(flog_msg_id) and entry[2] == current_channel_id


def cache_flog_validation(file_info: dict, ttl: int = FLOG_VALIDATION_TTL) -> None:
    file_id = str((file_info or {}).get("_id") or "")
    flog_msg_id = (file_info or {}).get("flog_msg_id")
    flog_channel_id = (file_info or {}).get("flog_channel_id")
    if not file_id or not flog_msg_id:
        return
    try:
        normalized_channel_id = int(flog_channel_id) if flog_channel_id not in (None, "") else None
    except Exception:
        normalized_channel_id = None
    _FLOG_VALIDATION_CACHE[file_id] = (_now() + max(int(ttl), 1), int(flog_msg_id), normalized_channel_id)


def invalidate_file_runtime(file_id: str, *aliases: str) -> None:
    file_id = str(file_id or "")
    if not file_id:
        return

    _FILE_INFO_CACHE.pop(file_id, None)
    _FLOG_VALIDATION_CACHE.pop(file_id, None)

    known_aliases = set(_FILE_REFERENCE_ALIASES.pop(file_id, set()))
    for alias in aliases:
        alias = str(alias or "")
        if alias:
            known_aliases.add(alias)
    known_aliases.add(file_id)

    for alias in known_aliases:
        _FILE_REFERENCE_CACHE.pop(alias, None)
