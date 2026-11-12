"""Genuinely generic helpers and backward-compatibility facades.

Generic utilities:
- human_readable: humanbytes
- time_format: get_readable_time
- translation: translation helper

All domain-specific utilities have been moved to FileStream.core and FileStream.bot.helpers.
"""
from .human_readable import humanbytes
from .time_format import get_readable_time


def __getattr__(name: str):
    if name == "get_readable_time":
        from .time_format import get_readable_time
        return get_readable_time
    if name == "humanbytes":
        from .human_readable import humanbytes
        return humanbytes
    if name in ("get_name", "get_file_ids"):
        from FileStream.core.media import file_properties
        return getattr(file_properties, name)
    if name == "ByteStreamer":
        from FileStream.core.media.custom_dl import ByteStreamer
        return ByteStreamer
    try:
        import importlib
        mod = importlib.import_module(f".{name}", __name__)
        return mod
    except ImportError:
        pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["humanbytes", "get_readable_time"]