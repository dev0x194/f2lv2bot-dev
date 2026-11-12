"""Media processing, MP4 box virtualization, chunk streaming, and properties."""

__all__ = [
    "MP4MetadataManager",
    "mp4_metadata_manager",
    "find_boxes",
    "find_moov_atom",
    "patch_moov_chunk_offsets",
    "ByteStreamer",
    "schedule_prefetch_window",
    "get_name",
    "get_file_ids",
    "ensure_flog_media_exists",
    "get_media_from_message",
    "get_media_file_size",
    "get_message_media_type",
    "get_file_info",
    "update_file_id",
    "send_file",
    "detect_category",
    "CATEGORIES",
]


def __getattr__(name: str):
    if name in ("MP4MetadataManager", "mp4_metadata_manager", "find_boxes", "find_moov_atom", "patch_moov_chunk_offsets"):
        from . import mp4_metadata
        return getattr(mp4_metadata, name)
    if name in ("ByteStreamer", "schedule_prefetch_window"):
        from . import custom_dl
        return getattr(custom_dl, name)
    if name in (
        "get_name", "get_file_ids", "ensure_flog_media_exists", "get_media_from_message",
        "get_media_file_size", "get_message_media_type", "get_file_info", "update_file_id", "send_file"
    ):
        from . import file_properties
        return getattr(file_properties, name)
    if name in ("detect_category", "CATEGORIES"):
        from . import category
        return getattr(category, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
