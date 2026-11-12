from __future__ import annotations

import asyncio
import contextlib
import time
import math
import logging
import mimetypes
import traceback
import os
import re
import secrets
from pathlib import Path
from typing import Any
import urllib.parse
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from FileStream.bot import multi_clients, work_loads, FileStream
from FileStream.config import Telegram, Server
from FileStream.server.exceptions import FileNotFound, InvalidHash
from FileStream import utils, StartTime, __version__
from FileStream.database.database import Database
from FileStream.services.token_service import token_service
from FileStream.core.media.file_properties import ensure_flog_media_exists
from FileStream.core.runtime.client_balance import choose_best_client
from FileStream.core.runtime.public_links import build_base_url, build_public_file_url, build_public_folder_url
from FileStream.server.render_template import (
    render_landing_page,
    render_page,
    render_folder,
    render_public_page,
    render_public_folder,
    render_public_error_page,
    render_policy_page,
    render_robots_txt,
    render_sitemap_xml,
    resolve_request_bot,
)
from FileStream.bot.helpers.client_identity import get_bot_username
from FileStream.bot.messages import WebMessages
from FileStream.core.media.mp4_metadata import mp4_metadata_manager
from FileStream.services.thumbnail_service import thumbnail_service
from FileStream.utils.human_readable import humanbytes

MIME_MAP: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
}


def _canonical_mime_type(ext: str, raw_mime: str = "") -> str:
    ext = (ext or "").lower()
    if ext in MIME_MAP:
        return MIME_MAP[ext]
    if raw_mime and raw_mime != "application/octet-stream":
        if raw_mime == "video/matroska":
            return "video/x-matroska"
        return raw_mime
    guessed, _ = mimetypes.guess_type(f"file{ext}")
    return guessed or "application/octet-stream"

routes = web.RouteTableDef()
db = Database(Telegram.DATABASE_URL, Telegram.SESSION_NAME)
TELEGRAM_GETFILE_LIMIT = 1024 * 1024
_OBJECT_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
PAGE_SESSION_COOKIE = "fs_page_session"
PAGE_SESSION_TTL = 60 * 60 * 12

class_cache: dict = {}


def _eligible_client_indexes(file_info: dict | None) -> list[int] | Any:
    if not file_info or not file_info.get("flog_msg_id"):
        src_idx = 0
        if file_info:
            try:
                src_idx = int(file_info.get("source_bot_index", 0) or 0)
            except Exception:
                src_idx = 0
        if src_idx in multi_clients:
            return [src_idx]
        return [0] if 0 in multi_clients else list(multi_clients.keys())
    return list(multi_clients.keys())


def invalidate_file_access(path: str) -> None:
    path = str(path or "")
    if not path:
        return

    token_service.invalidate_access_tokens_for_path(path)

    for streamer in class_cache.values():
        try:
            streamer.cached_file_ids.pop(path, None)
        except Exception:
            continue


def _extract_page_token(request: web.Request) -> str:
    return (
        request.headers.get("X-Page-Token")
        or request.query.get("page_token")
        or request.query.get("pageToken")
        or ""
    ).strip()


def _current_page_session_id(request: web.Request) -> str:
    return str(request.cookies.get(PAGE_SESSION_COOKIE, "") or "").strip()


def _current_download_session_id(request: web.Request) -> str:
    return (
        _current_page_session_id(request)
        or str(request.query.get("sid") or "").strip()
        or str(request.query.get("session_id") or "").strip()
        or str(request.headers.get("X-Page-Session") or "").strip()
    )


def _issue_page_session_cookie(response: web.StreamResponse, session_id: str) -> None:
    response.set_cookie(
        PAGE_SESSION_COOKIE,
        str(session_id or "").strip(),
        max_age=PAGE_SESSION_TTL,
        httponly=True,
        secure=bool(Server.HAS_SSL),
        samesite="Lax",
        path="/",
    )


def _validate_page_token(request: web.Request, expected_path: str) -> bool:
    token = _extract_page_token(request)
    if not token:
        return False
    payload = token_service.validate_access_token(token, expected_kind="page", consume=False)
    if not payload:
        return False
    session_id = _current_page_session_id(request)
    token_session_id = str((payload.get("metadata") or {}).get("session_id") or "").strip()
    return (
        str(payload.get("path") or "") == str(expected_path or "")
        and bool(session_id)
        and session_id == token_session_id
    )


@routes.get("/status", allow_head=True)
async def root_route_handler(_):
    bot_username = get_bot_username(FileStream)
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "telegram_bot": f"@{bot_username}" if bot_username else None,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )


@routes.get("/health", allow_head=True)
async def health_route_handler(_):
    db_ok = True
    db_latency_ms = None
    try:
        t0 = time.perf_counter()
        if hasattr(db, "ping") and callable(db.ping):
            await asyncio.wait_for(db.ping(), timeout=2.0)
        elif hasattr(db, "client") and getattr(db.client, "admin", None):
            await asyncio.wait_for(db.client.admin.command("ping"), timeout=2.0)
        else:
            await asyncio.wait_for(db.total_users_count(), timeout=2.0)
        db_latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    except Exception:
        db_ok = False

    try:
        from FileStream.services.storage_service import storage_service
        q_metrics = storage_service.get_metrics()
        workers = q_metrics.get("storage_workers", 0)
        clients = q_metrics.get("active_clients", 0)
    except Exception:
        workers = 0
        clients = len(multi_clients)

    is_healthy = db_ok and (clients > 0 or not multi_clients)
    status_str = "healthy" if is_healthy else ("degraded" if db_ok else "unhealthy")
    http_status = 200 if is_healthy else (200 if db_ok else 503)

    return web.json_response(
        {
            "status": status_str,
            "database": {"connected": db_ok, "latency_ms": db_latency_ms},
            "workers": workers,
            "clients": clients,
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "version": __version__,
        },
        status=http_status,
    )


@routes.get("/metrics", allow_head=True)
async def metrics_route_handler(request: web.Request):
    from FileStream.core.monitoring.metrics import get_metrics
    fmt = request.query.get("format", "").lower()
    accept = request.headers.get("Accept", "").lower()
    collector = get_metrics()
    if fmt == "json" or "application/json" in accept:
        return web.json_response(collector.get_summary())
    return web.Response(
        text=collector.to_prometheus(),
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )


@routes.get("/", allow_head=True)
async def landing_page_handler(request: web.Request):
    return web.Response(text=await render_landing_page(bot=resolve_request_bot(request.host)), content_type="text/html")


@routes.get("/robots.txt", allow_head=True)
async def robots_handler(request: web.Request):
    return web.Response(text=render_robots_txt(bot=resolve_request_bot(request.host)), content_type="text/plain")


@routes.get("/sitemap.xml", allow_head=True)
async def sitemap_handler(request: web.Request):
    return web.Response(text=render_sitemap_xml(bot=resolve_request_bot(request.host)), content_type="application/xml")


@routes.get("/favicon.ico", allow_head=True)
async def favicon_handler(request: web.Request):
    favicon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#09090b"/>'
        '<path d="M7 10a2 2 0 0 1 2-2h4.5a2 2 0 0 1 1.4.6L16.4 10H23a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V10z" fill="#ffffff" fill-opacity="0.9"/>'
        '</svg>'
    )
    return web.Response(
        text=favicon_svg,
        content_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "Access-Control-Allow-Origin": "*",
        },
    )


@routes.get("/legal", allow_head=True)
async def legal_page_handler(request: web.Request):
    return web.Response(text=await render_policy_page("legal", bot=resolve_request_bot(request.host)), content_type="text/html")


@routes.get("/privacy", allow_head=True)
async def privacy_page_handler(request: web.Request):
    return web.Response(text=await render_policy_page("privacy", bot=resolve_request_bot(request.host)), content_type="text/html")


@routes.get("/watch/{path}", allow_head=True)
async def watch_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        file_info, public_link = await db.resolve_file_reference(path)
        raise web.HTTPFound(location=build_public_file_url(public_link["public_id"], file_info=file_info))
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable.\n\nThe file may have been removed. Please reopen the share page and try again.")
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


@routes.get("/gen/{public_id}", allow_head=True)
async def gen_handler(request: web.Request):
    public_id = request.match_info["public_id"]
    request_bot = resolve_request_bot(request.host)
    try:
        session_id = _current_page_session_id(request) or secrets.token_urlsafe(24)
        response = web.Response(
            text=await render_public_page(public_id, session_id=session_id),
            content_type="text/html",
        )
        _issue_page_session_cookie(response, session_id)
        return response
    except FileNotFound:
        return web.Response(
            text=await render_public_error_page("Link unavailable", "This file link is not available anymore. Send the file again to create a fresh link.", bot=request_bot),
            content_type="text/html",
            status=404,
        )
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


@routes.get("/folder/{folder_id}", allow_head=True)
async def folder_handler(request: web.Request):
    try:
        folder_id = request.match_info["folder_id"]
        folder, public_link = await db.resolve_folder_reference(folder_id)
        raise web.HTTPFound(location=build_public_folder_url(public_link["public_id"], folder=folder))
    except FileNotFound:
        raise web.HTTPNotFound(text="Folder unavailable.\n\nThe folder may have been removed. Please reopen the share page and try again.")
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


@routes.get("/folderm/{folder_id}", allow_head=True)
async def folderm_handler(request: web.Request):
    try:
        folder_id = request.match_info["folder_id"]
        folder, public_link = await db.resolve_folder_reference(folder_id)
        raise web.HTTPFound(location=build_public_folder_url(public_link["public_id"], folder=folder))
    except FileNotFound:
        raise web.HTTPNotFound(text="Folder unavailable.\n\nThe folder may have been removed. Please reopen the share page and try again.")
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


@routes.get("/gfolder/{public_id}", allow_head=True)
async def gfolder_handler(request: web.Request):
    public_id = request.match_info["public_id"]
    request_bot = resolve_request_bot(request.host)
    try:
        session_id = _current_page_session_id(request) or secrets.token_urlsafe(24)
        response = web.Response(
            text=await render_public_folder(public_id, title="Folder", session_id=session_id),
            content_type="text/html",
        )
        _issue_page_session_cookie(response, session_id)
        return response
    except FileNotFound:
        return web.Response(
            text=await render_public_error_page("Folder unavailable", "This folder link is not available anymore. Open My Folders or create a new folder.", bot=request_bot),
            content_type="text/html",
            status=404,
        )
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


@routes.get("/dl/{path}", allow_head=True)
async def dl_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        if not _OBJECT_ID_RE.fullmatch(path):
            raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
        return await media_streamer(request, path)
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable.\n\nThe file may have been removed. Please reopen the share page and try again.")
    except (AttributeError, BadStatusLine, ConnectionResetError):
        raise web.HTTPServiceUnavailable(text="Service Unavailable")
    except Exception as e:
        traceback.print_exc()
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=WebMessages.DOWNLOAD_REQUEST_INVALID)


@routes.get("/get-download-token/{path}", allow_head=False)
async def get_download_token_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        if not _validate_page_token(request, path):
            raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
        file_info, _ = await db.resolve_file_reference(path)
        await ensure_flog_media_exists(file_info, bot=FileStream, prune_stale=True, db_instance=db)
        session_id = _current_page_session_id(request)
        token = token_service.create_access_token(
            str(file_info["_id"]),
            kind="download",
            expires_in_seconds=21600,
            single_use=False,
            metadata={"session_id": session_id},
        )
        base = build_base_url(file_info=file_info).rstrip("/")
        download_url = f"{base}/file/{token}"
        if session_id:
            download_url = f"{download_url}?{urllib.parse.urlencode({'sid': session_id})}"
        return web.json_response({
            "success": True,
            "download_url": download_url,
            "expires_in": 21600
        })
    except web.HTTPException:
        raise
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable. Please reopen the share page and try again.")
    except Exception as e:
        logging.error(f"Error generating download token: {e}")
        return web.json_response({"success": False, "error": "Please reload the share page and try again."}, status=500)


@routes.get("/get-stream-token/{path}", allow_head=False)
async def get_stream_token_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        if not _validate_page_token(request, path):
            raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
        file_info, _ = await db.resolve_file_reference(path)
        await ensure_flog_media_exists(file_info, bot=FileStream, prune_stale=True, db_instance=db)
        session_id = _current_page_session_id(request)
        token = token_service.create_access_token(
            str(file_info["_id"]),
            kind="stream",
            expires_in_seconds=1800,
            single_use=False,
            metadata={"session_id": session_id},
        )
        base = build_base_url(file_info=file_info, request=request).rstrip("/")
        file_duration = int(file_info.get("duration") or 0)
        file_name = str(file_info.get("file_name") or "")
        raw_mime = str(file_info.get("mime_type") or "").strip().lower()
        ext = os.path.splitext(file_name)[1].lower()
        canonical_mime = _canonical_mime_type(ext, raw_mime)
        is_native_playable = ext in {".mp4", ".m4v", ".webm"} or "mp4" in canonical_mime or "webm" in canonical_mime
        detected_video_codec = file_info.get("video_codec")
        if not detected_video_codec:
            fn_lower = file_name.lower()
            if any(k in fn_lower for k in ("x265", "hevc", "h265", "h.265")):
                detected_video_codec = "HEVC"
            elif any(k in fn_lower for k in ("x264", "avc", "h264", "h.264")):
                detected_video_codec = "H.264"
            elif "vp9" in fn_lower:
                detected_video_codec = "VP9"
            elif "av1" in fn_lower:
                detected_video_codec = "AV1"

        detected_audio_codec = file_info.get("audio_codec")
        if not detected_audio_codec:
            fn_lower = file_name.lower()
            if "eac3" in fn_lower:
                detected_audio_codec = "E-AC3"
            elif "ac3" in fn_lower:
                detected_audio_codec = "AC3"
            elif "dts" in fn_lower:
                detected_audio_codec = "DTS"
            elif "aac" in fn_lower:
                detected_audio_codec = "AAC"
            elif "opus" in fn_lower:
                detected_audio_codec = "Opus"
            elif "flac" in fn_lower:
                detected_audio_codec = "FLAC"

        internal_db_id = str(file_info["_id"])
        direct_dl_id = path or internal_db_id
        thumbnails_url = f"{base}/thumbnails/{token}/thumbnails.vtt" if getattr(Server, "THUMBNAILS_ENABLED", True) else None
        res = web.json_response(
            {
                "success": True,
                "stream_url": f"{base}/stream/{token}",
                "download_url": f"{base}/dl/{direct_dl_id}",
                "thumbnails_url": thumbnails_url,
                "mime_type": canonical_mime,
                "file_name": file_name,
                "container": ext.lstrip("."),
                "file_duration": file_duration,
                "video_codec": detected_video_codec,
                "audio_codec": detected_audio_codec,
                "resolution": file_info.get("resolution"),
                "file_size": humanbytes(file_info.get("file_size") or 0),
                "file_size_bytes": file_info.get("file_size") or 0,
                "is_native_playable": is_native_playable,
                "expires_in": 1800,
            }
        )
        res.headers["Access-Control-Allow-Origin"] = "*"
        res.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Range, Authorization"
        return res
    except web.HTTPException:
        raise
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.SECURE_LINK_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable. Please reopen the share page and try again.")
    except Exception as e:
        logging.error(f"Error generating stream token: {e}")
        return web.json_response({"success": False, "error": "Please reload the share page and try again."}, status=500)


@routes.get("/file/{token}", allow_head=True)
async def file_handler(request: web.Request):
    try:
        token = request.match_info["token"]
        payload = token_service.validate_access_token(token, expected_kind="download", consume=False)
        
        if not payload:
            raise web.HTTPForbidden(text=WebMessages.DOWNLOAD_EXPIRED)
        session_id = _current_download_session_id(request)
        token_session_id = str((payload.get("metadata") or {}).get("session_id") or "").strip()
        if token_session_id and (not session_id or session_id != token_session_id):
            raise web.HTTPForbidden(text=WebMessages.DOWNLOAD_EXPIRED)
        
        return await media_streamer(request, str(payload["path"]), force_download=True)
    except web.HTTPForbidden:
        raise
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.DOWNLOAD_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable. Please reopen the share page and try again.")
    except Exception as e:
        traceback.print_exc()
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=WebMessages.DOWNLOAD_REQUEST_INVALID)


@routes.get("/stream/{token}", allow_head=True)
async def stream_handler(request: web.Request):
    token = request.match_info.get("token", "")
    redacted_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else "***"
    range_header = request.headers.get("Range", "none")
    client_ip = request.remote or "-"
    logging.info(
        "[DIRECT_STREAM_REQUEST] token=%s range=%s ip=%s method=%s",
        redacted_token, range_header, client_ip, request.method
    )
    try:
        payload = token_service.validate_access_token(token, expected_kind="stream", consume=False)
        if not payload:
            logging.info("[DIRECT_STREAM_REQUEST] method=%s token=%s status=403 reason=invalid_or_expired_token", request.method, redacted_token)
            raise web.HTTPForbidden(text=WebMessages.STREAM_EXPIRED)
        # Browsers authenticate with the page-session cookie; external players (VLC, MX Player,
        # Infuse) cannot send that cookie, so the session binding may also arrive as ?sid=.
        session_id = _current_download_session_id(request)
        token_session_id = str((payload.get("metadata") or {}).get("session_id") or "").strip()
        if session_id and token_session_id and session_id != token_session_id:
            logging.info("[DIRECT_STREAM_REQUEST] method=%s token=%s status=403 reason=session_mismatch", request.method, redacted_token)
            raise web.HTTPForbidden(text=WebMessages.STREAM_EXPIRED)
        return await media_streamer(request, str(payload["path"]), force_download=False)
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        logging.info("[DIRECT_STREAM_DISCONNECT] method=%s token=%s ip=%s client disconnected during streaming", request.method, redacted_token, client_ip)
        raise
    except web.HTTPException:
        raise
    except InvalidHash:
        raise web.HTTPForbidden(text=WebMessages.STREAM_EXPIRED)
    except FileNotFound:
        raise web.HTTPNotFound(text="File unavailable. Please reopen the share page and try again.")
    except (AttributeError, BadStatusLine) as exc:
        logging.warning("[DIRECT_STREAM_ERROR] method=%s token=%s error=%s", request.method, redacted_token, type(exc).__name__)
        raise web.HTTPServiceUnavailable(text="Service Unavailable")


def _validate_stream_token(request: web.Request, token: str) -> dict:
    payload = token_service.validate_access_token(token, expected_kind="stream", consume=False)
    if not payload:
        raise web.HTTPForbidden(text=WebMessages.STREAM_EXPIRED)
    session_id = _current_page_session_id(request)
    token_session_id = str((payload.get("metadata") or {}).get("session_id") or "").strip()
    if session_id and token_session_id and session_id != token_session_id:
        raise web.HTTPForbidden(text=WebMessages.STREAM_EXPIRED)
    return payload


@routes.get("/thumbnails/{token}/thumbnails.vtt", allow_head=True)
async def thumbnail_vtt_handler(request: web.Request):
    try:
        token = request.match_info["token"]
        payload = _validate_stream_token(request, token)
        db_id = str(payload["path"])
        file_info, _ = await db.resolve_file_reference(db_id)
        internal_db_id = str(file_info["_id"])

        vtt_content = thumbnail_service.get_vtt_content(internal_db_id, token)
        if not vtt_content:
            if not thumbnail_service.has_thumbnails(internal_db_id) and not thumbnail_service.is_generating(internal_db_id):
                if work_loads:
                    try:
                        index = choose_best_client(_eligible_client_indexes(file_info))
                        faster_client = multi_clients[index]
                        tg_connect = class_cache.get(faster_client) or utils.ByteStreamer(faster_client)
                        class_cache[faster_client] = tg_connect
                        file_id = await tg_connect.get_file_properties(internal_db_id, multi_clients)
                        duration = int(file_info.get("duration") or 0)
                        asyncio.create_task(
                            thumbnail_service.ensure_thumbnails(
                                file_id=internal_db_id,
                                byte_streamer=tg_connect,
                                tg_file_id=file_id,
                                client_index=index,
                                file_size=file_id.file_size,
                                duration=duration,
                            )
                        )
                    except Exception as ex:
                        logging.debug("Background thumbnail trigger error: %s", ex)

        return web.Response(
            text=vtt_content or "WEBVTT\n\n",
            content_type="text/vtt",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate" if not vtt_content else "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except web.HTTPException:
        raise
    except Exception as e:
        logging.error("Thumbnail VTT error: %s", e)
        raise web.HTTPInternalServerError(text="Failed to serve thumbnail VTT")


@routes.get("/thumbnails/{token}/sprites.webp", allow_head=True)
async def thumbnail_sprite_handler(request: web.Request):
    try:
        token = request.match_info["token"]
        payload = _validate_stream_token(request, token)
        db_id = str(payload["path"])
        file_info, _ = await db.resolve_file_reference(db_id)
        internal_db_id = str(file_info["_id"])

        sprite_path = thumbnail_service.get_sprite_path(internal_db_id)
        if not sprite_path:
            raise web.HTTPNotFound(text="Sprite sheet not ready")

        data = await asyncio.to_thread(sprite_path.read_bytes)
        return web.Response(
            body=data,
            content_type="image/webp",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except web.HTTPException:
        raise
    except Exception as e:
        logging.error("Thumbnail sprite error: %s", e)
        raise web.HTTPInternalServerError(text="Failed to serve sprite sheet")


@routes.get("/thumb/{path}", allow_head=True)
async def thumb_handler(request: web.Request):
    """Extract, disk-cache, and stream native Telegram media thumbnails."""
    try:
        path = request.match_info["path"]
        file_info, _ = await db.resolve_file_reference(path)
        internal_db_id = str(file_info["_id"])

        cache_dir = Path("thumbnails_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"thumb_{internal_db_id}.jpg"

        if cache_file.exists() and cache_file.stat().st_size > 0:
            data = await asyncio.to_thread(cache_file.read_bytes)
            return web.Response(
                body=data,
                content_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        thumb_data = None
        flog_msg_id = file_info.get("flog_msg_id")
        if flog_msg_id and Telegram.FLOG_CHANNEL:
            client_indexes = _eligible_client_indexes(file_info)
            client_idx = choose_best_client(client_indexes) if client_indexes else 0
            client = multi_clients.get(client_idx, FileStream)
            try:
                msg = await client.get_messages(int(Telegram.FLOG_CHANNEL), int(flog_msg_id))
                if msg:
                    media = getattr(msg, "video", None) or getattr(msg, "document", None) or getattr(msg, "photo", None)
                    thumb = None
                    if media:
                        if getattr(media, "thumbs", None):
                            thumb = media.thumbs[-1]
                        elif getattr(msg, "photo", None):
                            thumb = msg.photo
                    if thumb:
                        bio = await client.download_media(thumb.file_id, in_memory=True)
                        if bio:
                            thumb_data = bio.getvalue() if hasattr(bio, "getvalue") else bytes(bio.getbuffer())
            except Exception as ex:
                logging.debug("Telegram thumbnail extraction error: %s", ex)

        if not thumb_data:
            # Check if thumbnail service already has a generated poster
            poster_path = thumbnail_service.get_poster_path(internal_db_id)
            if poster_path and poster_path.exists() and poster_path.stat().st_size > 0:
                thumb_data = await asyncio.to_thread(poster_path.read_bytes)

        if not thumb_data and work_loads:
            # On-demand extraction of canonical non-black poster frame
            mime_type = str(file_info.get("mime_type") or "").lower()
            file_name = str(file_info.get("file_name") or "").lower()
            is_video = "video" in mime_type or any(file_name.endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"))
            if is_video:
                try:
                    client_indexes = _eligible_client_indexes(file_info)
                    client_idx = choose_best_client(client_indexes) if client_indexes else 0
                    faster_client = multi_clients.get(client_idx, FileStream)
                    tg_connect = class_cache.get(faster_client) or utils.ByteStreamer(faster_client)
                    class_cache[faster_client] = tg_connect
                    tg_file_prop = await tg_connect.get_file_properties(internal_db_id, multi_clients)
                    file_size = tg_file_prop.file_size
                    dur = float(file_info.get("duration") or getattr(tg_file_prop, "duration", 0) or 0)
                    gen_poster = await thumbnail_service.generate_poster(
                        file_id=internal_db_id,
                        byte_streamer=tg_connect,
                        tg_file_id=tg_file_prop,
                        client_index=client_idx,
                        file_size=file_size,
                        duration=dur,
                    )
                    if gen_poster and gen_poster.exists() and gen_poster.stat().st_size > 0:
                        thumb_data = await asyncio.to_thread(gen_poster.read_bytes)
                except Exception as ex:
                    logging.debug("On-demand poster extraction error: %s", ex)

        if thumb_data:
            await asyncio.to_thread(cache_file.write_bytes, thumb_data)
            return web.Response(
                body=thumb_data,
                content_type="image/jpeg",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        raise web.HTTPNotFound(text="No thumbnail available")
    except web.HTTPException:
        raise
    except Exception as e:
        logging.debug("Thumbnail request error: %s", e)
        raise web.HTTPNotFound(text="Thumbnail unavailable")


def _sanitize_filename(name: str) -> str:
    if not isinstance(name, str):
        name = str(name or "")
    name = name.replace("\x00", " ").replace("\n", " ").replace("\r", " ")
    name = name.replace("/", " ").replace("\\", " ").replace('"', "").replace(";", "_")
    name = "".join(ch for ch in name if ch.isprintable())
    name = " ".join(name.split()).strip()
    if not name:
        return "file"
    if len(name) > 150:
        name = name[:150]
    return name


def _parse_range(range_header: str, file_size: int):
    if not range_header:
        return None
    header = range_header.strip().lower()
    if not header.startswith("bytes="):
        return False
    range_spec = header.split("=", 1)[1].split(",", 1)[0].strip()
    if "-" not in range_spec:
        return False
    start_str, end_str = range_spec.split("-", 1)
    if start_str == "" and end_str == "":
        return False
    try:
        if start_str == "":
            length = int(end_str)
            if length <= 0:
                return False
            start = max(file_size - length, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            if start < 0 or start >= file_size:
                return False
            if end_str:
                end = int(end_str)
                if end < start:
                    return False
                if end >= file_size:
                    end = file_size - 1
            else:
                end = file_size - 1
    except ValueError:
        return False
    return start, end


async def media_streamer(request: web.Request, db_id: str, *, force_download: bool = False):
    if getattr(request, "transport", None):
        with contextlib.suppress(Exception):
            sock = request.transport.get_extra_info("socket")
            if sock is not None:
                import socket
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    range_header = request.headers.get("Range")

    file_info, _ = await db.resolve_file_reference(db_id)
    await ensure_flog_media_exists(file_info, bot=FileStream, prune_stale=True, db_instance=db)
    internal_db_id = str(file_info["_id"])

    if not work_loads:
        raise web.HTTPServiceUnavailable(text="No available clients")

    try:
        index = choose_best_client(_eligible_client_indexes(file_info))
    except RuntimeError as client_err:
        logging.warning(f"Streaming client selection failed: {client_err}")
        raise web.HTTPServiceUnavailable(text="Streaming service temporarily busy. Please retry shortly.")
    faster_client = multi_clients[index]
    
    if Telegram.MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.headers.get('X-FORWARDED-FOR', request.remote)}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(internal_db_id, multi_clients)
    logging.debug("after calling get_file_properties")
    
    file_size = file_id.file_size
    if not file_size or file_size <= 0:
        raise web.HTTPNotFound(text="File unavailable. Please reopen the share page and try again.")

    _tok = request.match_info.get("token", "")
    _redacted = f"{_tok[:6]}...{_tok[-4:]}" if len(_tok) > 10 else (_tok or "***")

    def _unsatisfiable() -> web.Response:
        logging.warning(
            "[DIRECT_STREAM_ERROR] type=RANGE_ERROR status=416 token=%s range=%s total=%s reason=range_not_satisfiable",
            _redacted, range_header, file_size,
        )
        return web.Response(
            status=416,
            body=WebMessages.DOWNLOAD_REQUEST_INVALID,
            headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
        )

    if range_header:
        parsed = _parse_range(range_header, file_size)
        if parsed is False:
            return _unsatisfiable()
        if parsed is None:
            from_bytes = 0
            until_bytes = file_size - 1
            range_header = None
        else:
            from_bytes, until_bytes = parsed
    else:
        from_bytes = 0
        until_bytes = file_size - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return _unsatisfiable()

    configured_chunk_size = max(int(Server.STREAM_CHUNK_SIZE_MB), 1) * 1024 * 1024
    if configured_chunk_size > TELEGRAM_GETFILE_LIMIT:
        logging.warning(
            "STREAM_CHUNK_SIZE_MB=%s exceeds Telegram upload.GetFile limit; clamping to 1 MB",
            Server.STREAM_CHUNK_SIZE_MB,
        )
    chunk_size = min(configured_chunk_size, TELEGRAM_GETFILE_LIMIT)
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil((until_bytes + 1) / chunk_size) - math.floor(offset / chunk_size)
    file_name = _sanitize_filename(utils.get_name(file_id))
    ext = os.path.splitext(file_name)[1].lower()
    raw_mime = (file_id.mime_type or "").strip()
    mime_type = _canonical_mime_type(ext, raw_mime)

    from urllib.parse import quote
    encoded_name = quote(file_name)

    video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mpeg", ".mpg"}
    audio_ext = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".oga"}
    is_mp4 = (ext == ".mp4" or "mp4" in mime_type)

    body = None
    if request.method != "HEAD":
        cached_range = None
        if range_header and getattr(Server, "MP4_FAST_SCRUB_ENABLED", True):
            cached_range = mp4_metadata_manager.get_cached_range(internal_db_id, from_bytes, until_bytes)

        if cached_range is not None:
            body = cached_range
        elif is_mp4 and getattr(Server, "MP4_FAST_SCRUB_ENABLED", True):
            cached_moov = mp4_metadata_manager.get_cached_moov(internal_db_id)
            if not cached_moov and from_bytes == 0:
                try:
                    cached_moov = await asyncio.wait_for(
                        mp4_metadata_manager.fetch_and_index_moov(
                            tg_connect, file_id, internal_db_id, file_size, index
                        ),
                        timeout=2.5,
                    )
                except Exception as ex:
                    logging.debug("Faststart initial moov indexing timeout: %s", ex)

            if cached_moov and cached_moov.get("is_tail"):
                header_data = cached_moov.get("header_data") or b""
                patched_moov = cached_moov.get("patched_moov") or b""
                prefix = header_data + patched_moov
                prefix_len = len(prefix)
                if prefix_len > 0:
                    if until_bytes < prefix_len:
                        body = prefix[from_bytes : until_bytes + 1]
                    else:
                        body = mp4_metadata_manager.virtual_faststart_range_stream(
                            byte_streamer=tg_connect,
                            file_id=file_id,
                            file_key=internal_db_id,
                            client_index=index,
                            from_bytes=from_bytes,
                            until_bytes=until_bytes,
                            file_size=file_size,
                            chunk_size=chunk_size,
                            cached_moov=cached_moov,
                        )

            if body is None:
                body = tg_connect.yield_file(
                    internal_db_id, file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
                )
        else:
            body = tg_connect.yield_file(
                internal_db_id, file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
            )

    if (
        getattr(Server, "MP4_FAST_SCRUB_ENABLED", True)
        and is_mp4
        and not mp4_metadata_manager.get_cached_moov(internal_db_id)
    ):
        try:
            asyncio.create_task(
                mp4_metadata_manager.fetch_and_index_moov(
                    tg_connect, file_id, internal_db_id, file_size, index
                )
            )
        except Exception:
            pass

    if (
        getattr(Server, "THUMBNAILS_ENABLED", True)
        and (ext in video_ext or "video" in mime_type)
        and not thumbnail_service.has_thumbnails(internal_db_id)
    ):
        try:
            asyncio.create_task(
                thumbnail_service.ensure_thumbnails(
                    file_id=internal_db_id,
                    byte_streamer=tg_connect,
                    tg_file_id=file_id,
                    client_index=index,
                    file_size=file_size,
                )
            )
        except Exception:
            pass

    explicit_download = force_download or request.query.get("download", "").lower() in {"1", "true", "yes"}
    fetch_dest = request.headers.get("Sec-Fetch-Dest", "").lower()
    embedded_media_request = fetch_dest in {"video", "audio", "image"}
    direct_dl_download = request.path.startswith("/dl/") and not embedded_media_request
    if explicit_download or direct_dl_download:
        disposition = "attachment"
    elif "video" in mime_type or "audio" in mime_type or "image" in mime_type or ext in video_ext or ext in audio_ext:
        disposition = "inline"
    else:
        disposition = "attachment"

    status_code = 206 if range_header else 200
    etag = f'"{internal_db_id}-{file_size}"'
    headers = {
        "Content-Type": f"{mime_type}",
        "Content-Length": str(req_length),
        "Content-Disposition": f"{disposition}; filename=\"{file_name}\"; filename*=UTF-8''{encoded_name}",
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Range, Accept, Origin, Content-Type",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Type, ETag, Last-Modified",
        "Cache-Control": "private, no-cache, no-transform",
    }

    uploaded_at = file_info.get("time")
    if isinstance(uploaded_at, (int, float)) and uploaded_at > 0:
        from email.utils import formatdate
        headers["Last-Modified"] = formatdate(float(uploaded_at), usegmt=True)
    
    if range_header:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"

    token = request.match_info.get("token", "")
    redacted_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else (token or "***")
    logging.info(
        "[DIRECT_STREAM_REQUEST] method=%s token=%s range=%s status=%s content_length=%s content_type=%s start=%s end=%s total=%s",
        request.method,
        redacted_token,
        range_header or "none",
        status_code,
        req_length,
        mime_type,
        from_bytes,
        until_bytes,
        file_size,
    )

    return web.Response(
        status=status_code,
        body=body,
        headers=headers,
    )
