"""Runtime cache, storage channel sync, multi-client balancing, and public links."""

__all__ = [
    "cache_file_info",
    "get_cached_file_info",
    "cache_file_reference",
    "get_cached_file_reference",
    "is_flog_validation_fresh",
    "cache_flog_validation",
    "invalidate_file_runtime",
    "stream_cache",
    "warm_stream_cache",
    "LocalStreamCache",
    "invalidate_file_stream_cache",
    "choose_best_client",
    "ensure_client_stat",
    "configured_flog_channels",
    "optional_channel_name_for_mode",
    "reconcile_flog_storage",
    "start_flog_sync_task",
    "stop_flog_sync_task",
    "delete_file_entry",
    "invalidate_runtime_access",
    "warm_optional_channel_peer",
    "build_base_url",
    "build_public_file_url",
    "build_public_folder_url",
    "UploadProgressTracker",
    "format_progress_bar",
]


def __getattr__(name: str):
    if name in (
        "cache_file_info",
        "get_cached_file_info",
        "cache_file_reference",
        "get_cached_file_reference",
        "is_flog_validation_fresh",
        "cache_flog_validation",
        "invalidate_file_runtime",
    ):
        from . import runtime_cache
        return getattr(runtime_cache, name)
    if name in ("stream_cache", "warm_stream_cache", "LocalStreamCache", "invalidate_file_stream_cache"):
        from . import stream_cache
        return getattr(stream_cache, name)
    if name in ("choose_best_client", "ensure_client_stat"):
        from . import client_balance
        return getattr(client_balance, name)
    if name in ("configured_flog_channels", "optional_channel_name_for_mode"):
        from . import flog_channels
        return getattr(flog_channels, name)
    if name in ("reconcile_flog_storage", "start_flog_sync_task", "stop_flog_sync_task"):
        from . import flog_sync
        return getattr(flog_sync, name)
    if name in ("delete_file_entry", "invalidate_runtime_access"):
        from . import file_cleanup
        return getattr(file_cleanup, name)
    if name in ("warm_optional_channel_peer",):
        from . import optional_channels
        return getattr(optional_channels, name)
    if name in ("build_base_url", "build_public_file_url", "build_public_folder_url"):
        from . import public_links
        return getattr(public_links, name)
    if name in ("UploadProgressTracker", "format_progress_bar"):
        from . import upload_progress
        return getattr(upload_progress, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
