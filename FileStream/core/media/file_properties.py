from __future__ import annotations
import asyncio
import logging
import html
import re

logger = logging.getLogger(__name__)
import mimetypes
import os
from datetime import datetime
from pyrogram import Client
from typing import Any, Optional

from pyrogram.enums import ParseMode, ChatType
from pyrogram.types import Message
from pyrogram.file_id import FileId
from FileStream.bot import FileStream, multi_clients
from FileStream.database.database import Database
from FileStream.core.media.category import detect_category
from FileStream.core.runtime.file_cleanup import delete_file_entry, invalidate_runtime_access
from FileStream.core.runtime.optional_channels import (
    disable_optional_channel,
    is_invalid_optional_channel_error,
    optional_channel_available,
    warm_optional_channel_peer,
)
from FileStream.core.runtime.flog_channels import (
    has_configured_flog_channels,
    optional_channel_name_for_id,
    resolve_active_flog_target,
    resolve_file_flog_channel_id,
)
from FileStream.bot.helpers.client_identity import get_bot_name, get_bot_username
from FileStream.core.runtime.runtime_cache import cache_flog_validation, is_flog_validation_fresh
from FileStream.config import Telegram, Server
from FileStream.server.exceptions import FileNotFound
from FileStream.core.monitoring.audit_manager import audit_manager

db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)

_FLOG_MISSING_MARKERS = (
    "message_id_invalid",
    "msg_id_invalid",
    "message ids empty",
    "message not found",
    "chat not found",
)


def _flog_enabled(channel_name: str, channel_id: int | None) -> bool:
    return optional_channel_available(channel_name, channel_id)


def _handle_invalid_flog(exc: Exception, channel_name: str, channel_id: int | None) -> bool:
    if not is_invalid_optional_channel_error(exc):
        return False
    disable_optional_channel(channel_name, channel_id, exc)
    return True


async def _call_with_flog_retry(client: Client, operation, channel_name: str, channel_id: int | None):
    try:
        return await operation()
    except Exception as exc:
        if not is_invalid_optional_channel_error(exc):
            raise
        if await warm_optional_channel_peer(client, channel_name, channel_id):
            return await operation()
        raise


def _resolve_file_channel_binding(file_info: dict | None) -> tuple[str, int | None]:
    channel_id = resolve_file_flog_channel_id(file_info)
    return optional_channel_name_for_id(channel_id), channel_id


async def _resolve_storage_write_target(file_info: dict | None) -> tuple[str, int | None]:
    existing_channel_id = resolve_file_flog_channel_id(file_info)
    if file_info and file_info.get("flog_msg_id") and existing_channel_id:
        return optional_channel_name_for_id(existing_channel_id), existing_channel_id

    user_id = None
    if file_info:
        try:
            raw_user_id = file_info.get("user_id")
            if raw_user_id not in (None, ""):
                user_id = int(raw_user_id)
        except Exception:
            user_id = None

    _, channel_name, channel_id = await resolve_active_flog_target(db, user_id=user_id)
    return channel_name, channel_id


async def get_file_ids(client: Client | bool, db_id: str, multi_clients, message=None) -> Optional[FileId]:
    logging.debug("Starting of get_file_ids")
    file_info = await db.get_file(db_id)
    if not file_info or not file_info.get("file_id"):
        raise FileNotFound
    if ("file_ids" not in file_info) or not client:
        if not has_configured_flog_channels():
            if not client:
                return
            file_id_info = file_info.setdefault("file_ids", {})
            file_id_info[str(client.id)] = file_info.get("file_id", "")
            await db.update_file_ids(db_id, file_id_info)
        else:
            try:
                logging.debug("Storing file_id of all clients in DB")
                if isinstance(client, Client):
                    target_client = client
                else:
                    try:
                        from FileStream.services.storage_service import storage_service
                        best_sel = storage_service.select_best_client()
                        target_client = best_sel[1] if best_sel else FileStream
                    except Exception:
                        target_client = FileStream
                log_msg = await send_file(target_client, db_id, file_info['file_id'], message, file_name=file_info.get('file_name'), file_info=file_info)
                _, flog_channel_id = _resolve_file_channel_binding(file_info)
                file_ids = await update_file_id(log_msg.id, multi_clients, channel_id=flog_channel_id)
                await db.update_file_ids(db_id, file_ids)
                file_info["file_ids"] = file_ids
                logging.debug("Stored file_id of all clients in DB")
                if not client:
                    return
            except FileNotFound:
                if not client:
                    return
                file_id_info = file_info.setdefault("file_ids", {})
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)
            except Exception as exc:
                flog_channel_name, flog_channel_id = await _resolve_storage_write_target(file_info)
                if not _handle_invalid_flog(exc, flog_channel_name, flog_channel_id):
                    raise
                if not client:
                    return
                file_id_info = file_info.setdefault("file_ids", {})
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)

    file_id_info = file_info.setdefault("file_ids", {})
    if str(client.id) not in file_id_info:
        if not has_configured_flog_channels():
            file_id_info[str(client.id)] = file_info.get("file_id", "")
            await db.update_file_ids(db_id, file_id_info)
        else:
            try:
                logging.debug("Storing file_id in DB")
                target_client = client if isinstance(client, Client) else FileStream
                log_msg = await send_file(target_client, db_id, file_info['file_id'], message, file_name=file_info.get('file_name'), file_info=file_info)
                flog_channel_name, flog_channel_id = _resolve_file_channel_binding(file_info)
                async def _get_flog_message():
                    return await client.get_messages(flog_channel_id, log_msg.id)

                msg = await _call_with_flog_retry(client, _get_flog_message, flog_channel_name, flog_channel_id)
                media = get_media_from_message(msg)
                file_id_info[str(client.id)] = getattr(media, "file_id", "")
                await db.update_file_ids(db_id, file_id_info)
                logging.debug("Stored file_id in DB")
            except FileNotFound:
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)
            except Exception as exc:
                flog_channel_name, flog_channel_id = _resolve_file_channel_binding(file_info)
                if not _handle_invalid_flog(exc, flog_channel_name, flog_channel_id):
                    raise
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)

    logging.debug("Middle of get_file_ids")
    if not file_id_info.get(str(client.id)):
        # Try to refresh missing/empty file_id for this client
        flog_channel_name, flog_channel_id = _resolve_file_channel_binding(file_info)
        if _flog_enabled(flog_channel_name, flog_channel_id):
            try:
                target_client = client if isinstance(client, Client) else FileStream
                log_msg = await send_file(target_client, db_id, file_info['file_id'], message, file_name=file_info.get('file_name'))
                file_info = await db.get_file(db_id)
                flog_channel_name, flog_channel_id = _resolve_file_channel_binding(file_info)
                async def _get_flog_message():
                    return await client.get_messages(flog_channel_id, log_msg.id)

                msg = await _call_with_flog_retry(client, _get_flog_message, flog_channel_name, flog_channel_id)
                media = get_media_from_message(msg)
                file_id_info[str(client.id)] = getattr(media, "file_id", "")
                await db.update_file_ids(db_id, file_id_info)
            except FileNotFound:
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)
            except Exception as exc:
                if not _handle_invalid_flog(exc, flog_channel_name, flog_channel_id):
                    raise
                file_id_info[str(client.id)] = file_info.get("file_id", "")
                await db.update_file_ids(db_id, file_id_info)
        else:
            file_id_info[str(client.id)] = file_info.get("file_id", "")
            await db.update_file_ids(db_id, file_id_info)

    if not file_id_info.get(str(client.id)):
        raise FileNotFound

    file_id = FileId.decode(file_id_info[str(client.id)])
    setattr(file_id, "file_size", file_info['file_size'])
    setattr(file_id, "mime_type", file_info['mime_type'])
    setattr(file_id, "file_name", file_info['file_name'])
    setattr(file_id, "unique_id", file_info['file_unique_id'])
    logging.debug("Ending of get_file_ids")
    return file_id


async def ensure_flog_media_exists(
    file_info: dict,
    bot: Client | None = None,
    prune_stale: bool = False,
    db_instance: Database | None = None,
) -> dict:
    if not file_info:
        raise FileNotFound

    flog_channel_name, flog_channel_id = _resolve_file_channel_binding(file_info)
    if not _flog_enabled(flog_channel_name, flog_channel_id):
        return file_info

    flog_msg_id = file_info.get("flog_msg_id")
    if not flog_msg_id:
        return file_info

    if is_flog_validation_fresh(file_info):
        return file_info

    client = bot or FileStream
    log_msg = None

    try:
        async def _get_flog_message():
            return await client.get_messages(flog_channel_id, int(flog_msg_id))

        log_msg = await _call_with_flog_retry(client, _get_flog_message, flog_channel_name, flog_channel_id)
    except Exception as exc:
        if _handle_invalid_flog(exc, flog_channel_name, flog_channel_id):
            return file_info
        error_text = str(exc or "").lower()
        if not any(marker in error_text for marker in _FLOG_MISSING_MARKERS):
            logging.warning("Unable to validate FLOG media for %s: %s", file_info.get("_id"), exc)
            return file_info

    if not log_msg:
        return file_info

    media = get_media_from_message(log_msg)
    if media:
        cache_flog_validation(file_info)
        return file_info

    file_db_id = str(file_info.get("_id") or "")
    invalidate_runtime_access(file_db_id)

    if prune_stale and getattr(log_msg, "empty", False):
        cleanup_db = db_instance or db
        try:
            await delete_file_entry(cleanup_db, file_info, bot=client)
        except Exception:
            logging.debug("Failed to prune stale file %s after missing FLOG media", file_db_id, exc_info=True)

    raise FileNotFound("File no longer available")


def get_media_from_message(message: "Message") -> Any:
    if message is None:
        return None
    media_types = (
        "audio",
        "document",
        "photo",
        "sticker",
        "animation",
        "video",
        "voice",
        "video_note",
    )
    for attr in media_types:
        media = getattr(message, attr, None)
        if media:
            return media
    return None


def _is_secondary_worker(client) -> bool:
    try:
        from FileStream.bot.clients import is_secondary_worker
        return is_secondary_worker(client)
    except Exception:
        idx = getattr(client, "client_index", 0)
        return int(idx or 0) > 0


def _resolve_source_client(
    file_info: dict | None,
    preferred_client: Client | None = None,
    message: Message | None = None,
) -> Client:
    # In Telegram MTProto, media received in a private chat contains an access_hash
    # scoped strictly to the bot client that received it. Secondary bot tokens
    # CANNOT send_cached_media or access private message peers of other bots.
    message_client = getattr(message, "_client", None) or getattr(message, "bot", None)

    source_bot_id = None
    try:
        raw_source_bot_id = (file_info or {}).get("source_bot_id")
        if raw_source_bot_id not in (None, ""):
            source_bot_id = int(raw_source_bot_id)
    except Exception:
        source_bot_id = None

    if source_bot_id is not None:
        if getattr(message_client, "id", None) == source_bot_id:
            return message_client
        if getattr(preferred_client, "id", None) == source_bot_id:
            return preferred_client
        for candidate in multi_clients.values():
            if getattr(candidate, "id", None) == source_bot_id:
                return candidate
        if getattr(FileStream, "id", None) == source_bot_id:
            return FileStream

    if message_client is not None:
        return message_client

    if message is not None:
        return multi_clients.get(0, FileStream)

    if preferred_client is not None and not _is_secondary_worker(preferred_client):
        return preferred_client

    return multi_clients.get(0, FileStream)


def _format_source_bot_label(client: Client | None) -> str:
    username = get_bot_username(client)
    if username:
        return f"@{html.escape(username)}"

    bot_name = get_bot_name(client)
    bot_id = getattr(client, "id", None)
    safe_name = html.escape(bot_name or "Bot")
    if bot_id not in (None, ""):
        return f"{safe_name} (<code>{int(bot_id)}</code>)"
    return safe_name


def get_media_file_size(m):
    media = get_media_from_message(m)
    return getattr(media, "file_size", "None")


def get_message_media_type(message: Message | None) -> str:
    """Return the media type string for a Telegram message."""
    if message is None:
        return "unknown"
    for attr in (
        "photo",
        "document",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note",
        "sticker",
    ):
        if getattr(message, attr, None):
            return attr
    media = getattr(message, "media", None)
    if media is not None:
        val = getattr(media, "value", str(media))
        return str(val).lower()
    return "unknown"


def get_name(media_msg: Message | FileId | None) -> str:
    file_name = ""

    if isinstance(media_msg, Message) or hasattr(media_msg, "document") or hasattr(media_msg, "chat"):
        media = get_media_from_message(media_msg)
        file_name = getattr(media, "file_name", "") if media else ""
    elif isinstance(media_msg, FileId):
        file_name = getattr(media_msg, "file_name", "")

    if not file_name:
        media_type = None
        if isinstance(media_msg, Message) or hasattr(media_msg, "document") or hasattr(media_msg, "chat"):
            media = getattr(media_msg, "media", None)
            if media is not None:
                raw_type = getattr(media, "value", str(media))
                if raw_type:
                    media_type = str(raw_type).lower()
            if not media_type or media_type in ("none", "unknown"):
                for attr in ("photo", "audio", "voice", "video", "animation", "video_note", "document", "sticker"):
                    if getattr(media_msg, attr, None):
                        media_type = attr
                        break
        elif hasattr(media_msg, "file_type") and media_msg.file_type:
            media_type = media_msg.file_type.name.lower()

        if not media_type:
            media_type = "file"

        formats = {
            "photo": "jpg", "audio": "mp3", "voice": "ogg",
            "video": "mp4", "animation": "mp4", "video_note": "mp4",
            "sticker": "webp"
        }

        ext = formats.get(media_type)
        ext = "." + ext if ext else ""

        date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        file_name = f"{media_type}-{date}{ext}"

    return file_name



def _guess_mime(file_name: str, mime_type: str | None = None) -> str:
    if mime_type:
        return mime_type
    if not file_name:
        return ""
    guess, _ = mimetypes.guess_type(file_name)
    return guess or ""


def _extract_uploader(message: Message) -> tuple[str, int | None, str | None]:
    if getattr(message, "from_user", None):
        raw_first = getattr(message.from_user, "first_name", "")
        raw_last = getattr(message.from_user, "last_name", "")
        first_name = str(raw_first or "").strip() if isinstance(raw_first, str) else ""
        last_name = str(raw_last or "").strip() if isinstance(raw_last, str) else ""
        full_name = " ".join(x for x in [first_name, last_name] if x).strip()
        raw_user = getattr(message.from_user, "username", None)
        username = str(raw_user).strip() if isinstance(raw_user, str) else None
        # Clean unwanted punctuation so that lone punctuation like ',' doesn't become the display name
        cleaned_name = full_name.strip(" ,._-;:~|/\\")
        if not any(c.isalnum() for c in cleaned_name):
            cleaned_name = f"@{username}" if username else "User"
        return cleaned_name, getattr(message.from_user, "id", None), username

    title = str(getattr(message.chat, "title", None) or "Channel").strip()
    cleaned_title = title.strip(" ,._-;:~|/\\")
    if not any(c.isalnum() for c in cleaned_title):
        cleaned_title = "Channel"
    return cleaned_title, getattr(message.chat, "id", None), None


def get_file_info(message):
    if message is None or not getattr(message, "chat", None):
        return None

    if message.chat.type == ChatType.PRIVATE and not getattr(message, "from_user", None):
        return None

    media = get_media_from_message(message)
    if message.chat.type == ChatType.PRIVATE:
        user_idx = message.from_user.id
    else:
        user_idx = message.chat.id

    if not getattr(media, "file_id", None):
        # No valid media found
        return None

    file_name = get_name(message)
    file_ext = os.path.splitext(file_name)[1].lower() if file_name else ""
    mime_type = _guess_mime(file_name, getattr(media, "mime_type", ""))
    category = detect_category(file_name=file_name, mime_type=mime_type, file_ext=file_ext)

    uploader_name, uploader_id, uploader_username = _extract_uploader(message)

    msg_id = getattr(message, "id", None)
    try:
        msg_id = int(msg_id) if msg_id is not None else None
    except Exception:
        msg_id = None

    msg_cl = getattr(message, "_client", None) or getattr(message, "bot", None)
    source_bot_id = None
    try:
        raw_id = (
            getattr(msg_cl, "id", None)
            or getattr(getattr(msg_cl, "me", None), "id", None)
            or getattr(FileStream, "id", None)
        )
        if raw_id not in (None, ""):
            source_bot_id = int(raw_id)
    except Exception:
        source_bot_id = None

    source_bot_index = 0
    try:
        source_bot_index = int(getattr(msg_cl, "client_index", 0) or 0)
    except Exception:
        source_bot_index = 0

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is not None:
        media_group_id = str(media_group_id)

    def _safe_int(val) -> int:
        if type(val) in (int, float) and not isinstance(val, bool):
            try:
                return int(val)
            except Exception:
                return 0
        return 0

    media_duration = _safe_int(getattr(media, "duration", 0))
    width = _safe_int(getattr(media, "width", 0))
    height = _safe_int(getattr(media, "height", 0))

    # Also inspect Document attributes if duration or dimensions are missing
    attrs = getattr(media, "attributes", None)
    if isinstance(attrs, (list, tuple)):
        for attr in attrs:
            cls_name = getattr(type(attr), "__name__", "")
            if "Video" in cls_name:
                if not media_duration:
                    media_duration = _safe_int(getattr(attr, "duration", 0))
                if not width:
                    width = _safe_int(getattr(attr, "w", None) or getattr(attr, "width", 0))
                if not height:
                    height = _safe_int(getattr(attr, "h", None) or getattr(attr, "height", 0))
            elif "Audio" in cls_name:
                if not media_duration:
                    media_duration = _safe_int(getattr(attr, "duration", 0))
            elif "ImageSize" in cls_name:
                if not width:
                    width = _safe_int(getattr(attr, "w", None) or getattr(attr, "width", 0))
                if not height:
                    height = _safe_int(getattr(attr, "h", None) or getattr(attr, "height", 0))

    resolution = ""
    if width > 0 and height > 0:
        max_dim = max(width, height)
        min_dim = min(width, height)
        if min_dim >= 2160 or max_dim >= 3840:
            resolution = "4K / 2160p"
        elif min_dim >= 1440 or max_dim >= 2560:
            resolution = "1440p / 2K"
        elif min_dim >= 1080 or max_dim >= 1920:
            resolution = "1080p Full HD"
        elif min_dim >= 720 or max_dim >= 1280:
            resolution = "720p HD"
        elif min_dim >= 480:
            resolution = "480p SD"
        else:
            resolution = f"{width}x{height}"
    elif file_name:
        qual_match = re.search(r"\b(2160p|4k|1440p|2k|1080p|720p|480p|360p)\b", file_name, re.IGNORECASE)
        if qual_match:
            q_str = qual_match.group(1).upper()
            resolution = "2160p (4K)" if q_str == "4K" else q_str

    video_codec = ""
    audio_codec = ""
    if file_name:
        v_match = re.search(r"\b(x265|h265|hevc|x264|h264|avc|vp9|av1|10bit)\b", file_name, re.IGNORECASE)
        if v_match:
            v_str = v_match.group(1).upper()
            video_codec = "HEVC / H.265" if v_str in ("X265", "H265", "HEVC") else ("AVC / H.264" if v_str in ("X264", "H264", "AVC") else v_str)

        a_match = re.search(r"\b(aac|ac3|eac3|dts|flac|mp3|opus|ddp5\.1|dd5\.1)\b", file_name, re.IGNORECASE)
        if a_match:
            audio_codec = a_match.group(1).upper()

    return {
        "user_id": user_idx,
        "file_id": getattr(media, "file_id", ""),
        "file_unique_id": getattr(media, "file_unique_id", ""),
        "file_name": file_name,
        "file_size": getattr(media, "file_size", 0),
        "mime_type": mime_type,
        "file_ext": file_ext,
        "category": category,
        "duration": media_duration,
        "width": width,
        "height": height,
        "resolution": resolution,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "uploader": uploader_name,
        "uploader_id": uploader_id,
        "uploader_username": uploader_username,
        "message_id": msg_id,
        "chat_id": getattr(message.chat, "id", None),
        "source_bot_id": source_bot_id,
        "source_bot_index": source_bot_index,
        "media_group_id": media_group_id,
    }


async def update_file_id(msg_id, multi_clients, channel_id: int | None = None):
    file_ids = {}
    channel_name = optional_channel_name_for_id(channel_id)
    if not _flog_enabled(channel_name, channel_id):
        return file_ids

    async def get_id(client):
        try:
            async def _get_flog_message():
                return await client.get_messages(channel_id, msg_id)

            log_msg = await _call_with_flog_retry(client, _get_flog_message, channel_name, channel_id)
            media = get_media_from_message(log_msg)
            return str(client.id), getattr(media, "file_id", "")
        except Exception as exc:
            if _handle_invalid_flog(exc, channel_name, channel_id):
                return str(client.id), ""
            return str(client.id), ""

    # Run concurrently for speed
    tasks = [get_id(client) for client in multi_clients.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        client_id, file_id = result
        file_ids[client_id] = file_id

    return file_ids


async def send_file(client: Client, db_id, file_id: str, message=None, file_name: str | None = None, file_info: dict | None = None):
    if file_info is None:
        file_info = await db.get_file(db_id)
    flog_channel_name, flog_channel_id = await _resolve_storage_write_target(file_info)
    if not _flog_enabled(flog_channel_name, flog_channel_id):
        if not has_configured_flog_channels():
            logger.info("[STORAGE] No FLOG storage channels configured; storing file %s directly", db_id)
            return None
        logger.warning(
            "[STORAGE] FLOG channel %s=%s disabled or unavailable; falling back to direct storage for file %s",
            flog_channel_name, flog_channel_id, db_id
        )
        return None
    source_client = _resolve_source_client(file_info, preferred_client=client, message=message)

    if message:
        file_caption = getattr(message, 'caption', None) or get_name(message)
    else:
        file_caption = file_name or "file"

    if not isinstance(file_caption, str):
        file_caption = str(file_caption)
    file_caption = file_caption.replace("\n", " ").replace("\r", " ")
    if len(file_caption) > 1000:
        file_caption = file_caption[:1000] + "…"

    safe_caption = html.escape(file_caption)

    try:
        async def _send_cached():
            # If message is available and source_client supports copy_message, prefer copy_message
            # to forward/copy the message into the FLOG storage channel directly.
            if (
                message is not None
                and getattr(message, "chat", None) is not None
                and getattr(message, "id", None) is not None
                and callable(getattr(source_client, "copy_message", None))
            ):
                try:
                    res = source_client.copy_message(
                        chat_id=flog_channel_id,
                        from_chat_id=message.chat.id,
                        message_id=message.id,
                        caption=f"<b>{safe_caption}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        res = await res
                    if res:
                        return res
                except Exception as copy_err:
                    logger.debug("[STORAGE] copy_message failed, falling back to send_cached_media: %s", copy_err)

            return await source_client.send_cached_media(
                chat_id=flog_channel_id,
                file_id=file_id,
                caption=f"<b>{safe_caption}</b>",
                parse_mode=ParseMode.HTML,
            )

        log_msg = await _call_with_flog_retry(source_client, _send_cached, flog_channel_name, flog_channel_id)
    except Exception as exc:
        if _handle_invalid_flog(exc, flog_channel_name, flog_channel_id):
            logger.warning(
                "[STORAGE] FLOG channel %s=%s inaccessible (%s); falling back to direct storage for file %s",
                flog_channel_name, flog_channel_id, exc, db_id
            )
            return None
        raise
    if log_msg and getattr(log_msg, "id", None) is not None:
        try:
            await db.update_file_flog_msg(db_id, log_msg.id, flog_channel_id)
            if file_info is not None:
                file_info["flog_msg_id"] = log_msg.id
                file_info["flog_channel_id"] = flog_channel_id
        except Exception:
            pass

    if log_msg and getattr(log_msg, "id", None) is not None and message and message.chat:
        source_bot_label = _format_source_bot_label(source_client)

        async def _send_flog_reply():
            try:
                if message.chat.type == ChatType.PRIVATE and message.from_user:
                    name = html.escape(message.from_user.first_name or "User")
                    await log_msg.reply_text(
                        text=(
                            f"<b>RᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :</b> <a href='tg://user?id={message.from_user.id}'>{name}</a>\n"
                            f"<b>Uꜱᴇʀ ɪᴅ :</b> <code>{message.from_user.id}</code>\n"
                            f"<b>Bᴏᴛ :</b> {source_bot_label}\n"
                            f"<b>Fɪʟᴇ ɪᴅ :</b> <code>{db_id}</code>"
                        ),
                        disable_web_page_preview=True, parse_mode=ParseMode.HTML, quote=True)
                else:
                    title = html.escape(message.chat.title or "Channel")
                    await log_msg.reply_text(
                        text=(
                            f"<b>RᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :</b> {title}\n"
                            f"<b>Cʜᴀɴɴᴇʟ ɪᴅ :</b> <code>{message.chat.id}</code>\n"
                            f"<b>Bᴏᴛ :</b> {source_bot_label}\n"
                            f"<b>Fɪʟᴇ ɪᴅ :</b> <code>{db_id}</code>"
                        ),
                        disable_web_page_preview=True, parse_mode=ParseMode.HTML, quote=True)
            except Exception as exc:
                logging.debug("Non-critical FLOG reply log failed: %s", exc)

        audit_manager.enqueue(_send_flog_reply)

    return log_msg
    # return await client.send_cached_media(Telegram.BIN_CHANNEL, file_id)
