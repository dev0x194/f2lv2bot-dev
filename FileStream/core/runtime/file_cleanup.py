from __future__ import annotations

import logging

from pyrogram import Client

from FileStream.core.runtime.flog_channels import resolve_file_flog_channel_id
from FileStream.core.runtime.runtime_cache import invalidate_file_runtime
from FileStream.core.runtime.stream_cache import invalidate_file_stream_cache


def invalidate_runtime_access(file_id: str) -> None:
    file_id = str(file_id or "")
    if not file_id:
        return

    invalidate_file_runtime(file_id)
    invalidate_file_stream_cache(file_id)

    try:
        from FileStream.server.stream_routes import invalidate_file_access

        invalidate_file_access(file_id)
    except Exception:
        logging.debug("Runtime cache invalidation failed for %s", file_id, exc_info=True)


async def delete_file_entry(db, file_info: dict, bot: Client | None = None) -> None:
    file_id = str(file_info.get("_id") or "")
    invalidate_runtime_access(file_id)

    flog_channel_id = resolve_file_flog_channel_id(file_info)
    if bot and flog_channel_id and file_info.get("flog_msg_id"):
        try:
            await bot.delete_messages(flog_channel_id, int(file_info["flog_msg_id"]))
        except Exception:
            logging.debug("FLOG delete failed for %s", file_id, exc_info=True)

    try:
        await db.delete_one_file(file_info["_id"])
    finally:
        try:
            await db.remove_file_from_folders(file_id)
        except Exception:
            logging.debug("Folder cleanup failed for %s", file_id, exc_info=True)

        try:
            if file_info.get("user_id") is not None:
                await db.count_links(file_info.get("user_id"), "-")
        except Exception:
            logging.debug("Link count cleanup failed for %s", file_id, exc_info=True)

        invalidate_runtime_access(file_id)
