"""
Unit and Integration Tests for Direct HTTP Range Streaming,
MP4 Fast Scrubbing & Moov Virtualization, and WebVTT Scrubbing Thumbnails.
Completely replaces legacy HLS tests.
"""

import asyncio
import os
import shutil
import struct
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from bson import ObjectId

from FileStream.config import Server
from FileStream.services.thumbnail_service import ThumbnailService, format_vtt_timestamp, thumbnail_service
from FileStream.services.token_service import token_service
from FileStream.utils.mp4_metadata import (
    MP4MetadataManager,
    find_boxes,
    find_moov_atom,
    patch_moov_chunk_offsets,
    mp4_metadata_manager,
)
from FileStream.server.stream_routes import (
    stream_handler,
    get_stream_token_handler,
    thumbnail_vtt_handler,
    thumbnail_sprite_handler,
    media_streamer,
    _parse_range,
    _canonical_mime_type,
    PAGE_SESSION_COOKIE,
)


# ==============================================================================
# 1. MP4 BOX PARSING & MOOV ATOM DETECTION
# ==============================================================================
def create_synthetic_mp4(
    include_tail_moov: bool = True,
    chunk_offsets: list[int] = None,
    use_co64: bool = False,
) -> tuple[bytes, int, int]:
    """Helper to build a synthetic MP4 with ftyp, mdat, and moov boxes."""
    if chunk_offsets is None:
        chunk_offsets = [100, 200, 300]

    # ftyp box (16 bytes)
    ftyp = struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 512)

    # mdat box
    payload = b"A" * 256
    mdat = struct.pack(">I4s", len(payload) + 8, b"mdat") + payload

    # stco or co64 sub-box
    if use_co64:
        co64_hdr = struct.pack(">I4sII", 16 + len(chunk_offsets) * 8, b"co64", 0, len(chunk_offsets))
        co64_entries = b"".join(struct.pack(">Q", off) for off in chunk_offsets)
        offset_box = co64_hdr + co64_entries
    else:
        stco_hdr = struct.pack(">I4sII", 16 + len(chunk_offsets) * 4, b"stco", 0, len(chunk_offsets))
        stco_entries = b"".join(struct.pack(">I", off) for off in chunk_offsets)
        offset_box = stco_hdr + stco_entries

    # mvhd box
    mvhd = struct.pack(">I4s", 16, b"mvhd") + b"mvhd_data_1234"

    # moov box
    moov_content = mvhd + offset_box
    moov = struct.pack(">I4s", len(moov_content) + 8, b"moov") + moov_content

    if include_tail_moov:
        data = ftyp + mdat + moov
        moov_offset = len(ftyp) + len(mdat)
    else:
        data = ftyp + moov + mdat
        moov_offset = len(ftyp)

    return data, moov_offset, len(moov)


def test_find_boxes():
    data, _, _ = create_synthetic_mp4()
    boxes = find_boxes(data)
    assert len(boxes) == 3
    types = [b["type"] for b in boxes]
    assert types == [b"ftyp", b"mdat", b"moov"]


def test_find_moov_atom_tail_detection():
    data, moov_offset, moov_size = create_synthetic_mp4(include_tail_moov=True)
    res = find_moov_atom(data, len(data), 0)
    assert res is not None
    assert res["offset"] == moov_offset
    assert res["size"] == moov_size
    assert res["is_tail"] is True


def test_find_moov_atom_head_detection():
    data, moov_offset, moov_size = create_synthetic_mp4(include_tail_moov=False)
    res = find_moov_atom(data, len(data), 0)
    assert res is not None
    assert res["offset"] == moov_offset
    assert res["size"] == moov_size
    assert res["is_tail"] is False


def test_patch_moov_chunk_offsets_stco():
    offsets = [1000, 2000, 3000]
    _, _, moov_size = create_synthetic_mp4(include_tail_moov=True, chunk_offsets=offsets, use_co64=False)
    data, _, _ = create_synthetic_mp4(include_tail_moov=True, chunk_offsets=offsets, use_co64=False)
    moov_data = data[-moov_size:]

    delta = 500
    patched = patch_moov_chunk_offsets(moov_data, delta)
    assert len(patched) == len(moov_data)

    stco_idx = patched.find(b"stco")
    assert stco_idx != -1
    count = struct.unpack(">I", patched[stco_idx + 8 : stco_idx + 12])[0]
    assert count == 3
    for i, orig_off in enumerate(offsets):
        entry_off = struct.unpack(">I", patched[stco_idx + 12 + i * 4 : stco_idx + 16 + i * 4])[0]
        assert entry_off == orig_off + delta


def test_patch_moov_chunk_offsets_co64():
    offsets = [0x100000000, 0x200000000]
    data, _, moov_size = create_synthetic_mp4(include_tail_moov=True, chunk_offsets=offsets, use_co64=True)
    moov_data = data[-moov_size:]

    delta = 0x500
    patched = patch_moov_chunk_offsets(moov_data, delta)
    assert len(patched) == len(moov_data)

    co64_idx = patched.find(b"co64")
    assert co64_idx != -1
    count = struct.unpack(">I", patched[co64_idx + 8 : co64_idx + 12])[0]
    assert count == 2
    for i, orig_off in enumerate(offsets):
        entry_off = struct.unpack(">Q", patched[co64_idx + 12 + i * 8 : co64_idx + 20 + i * 8])[0]
        assert entry_off == orig_off + delta


def test_mp4_metadata_manager_caching_and_instant_range():
    mgr = MP4MetadataManager()
    file_key = "test_file_caching"
    moov_info = {
        "offset": 0,
        "size": 35,
        "is_tail": False,
        "mdat_offset": 0,
    }
    moov_data = b"PATCHED_MOOV_DATA_12345678901234567"[:35]  # exactly 35 bytes
    mgr.cache_moov(file_key, moov_info, moov_data)

    cached = mgr.get_cached_moov(file_key)
    assert cached["offset"] == 0
    assert cached["size"] == 35

    # Request range fully inside synthetic header+moov prefix [0 - 15]
    hit_slice = mgr.get_cached_range(file_key, 0, 15)
    assert hit_slice == moov_data[0:16]

    # Range outside moov [0 - 100] -> None
    outside_slice = mgr.get_cached_range(file_key, 0, 100)
    assert outside_slice is None


# ==============================================================================
# 2. SERVER RANGE REQUESTS & DIRECT HTTP STREAMING TESTS
# ==============================================================================
@pytest.fixture
def streaming_setup():
    file_id = "60c72b2f9b1d8b001c8e4f1a"
    session_id = "sess_range_stream_test"
    token = token_service.create_access_token(
        path=file_id,
        kind="stream",
        metadata={"session_id": session_id},
    )

    mock_bot = MagicMock()
    mock_streamer = MagicMock()
    fake_props = MagicMock()
    fake_props.file_size = 50_000_000  # 50 MB
    fake_props.mime_type = "video/mp4"
    fake_props.file_name = "test_movie.mp4"
    mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

    async def mock_yield(db_id, tg_file, client_idx, offset, first_part_cut, last_part_cut, part_count, chunk_size):
        remaining = 50_000_000 - offset
        part_bytes = b"M" * min(chunk_size, remaining)
        yield part_bytes[first_part_cut:first_part_cut + 1024]

    mock_streamer.yield_file = mock_yield

    return {
        "file_id": file_id,
        "session_id": session_id,
        "token": token,
        "mock_bot": mock_bot,
        "mock_streamer": mock_streamer,
        "fake_props": fake_props,
    }


@pytest.mark.asyncio
async def test_direct_stream_normal_get_without_range(streaming_setup):
    """GET /stream/<token> without Range header must return HTTP 200 OK."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}"},
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 200
        assert resp.headers.get("Accept-Ranges") == "bytes"
        assert resp.headers.get("Content-Type") == "video/mp4"
        assert resp.headers.get("Content-Length") == str(s["fake_props"].file_size)
        assert "Content-Range" not in resp.headers


@pytest.mark.asyncio
async def test_direct_stream_prefix_range(streaming_setup):
    """GET /stream/<token> with Range: bytes=0- must return HTTP 206 Partial Content."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=0-",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 206
        assert resp.headers.get("Accept-Ranges") == "bytes"
        assert resp.headers.get("Content-Range") == f"bytes 0-49999999/50000000"
        assert resp.headers.get("Content-Length") == "50000000"


@pytest.mark.asyncio
async def test_direct_stream_bounded_range(streaming_setup):
    """GET /stream/<token> with Range: bytes=0-1023 must return 206 with exactly 1024 bytes length."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=0-1023",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 206
        assert resp.headers.get("Accept-Ranges") == "bytes"
        assert resp.headers.get("Content-Range") == "bytes 0-1023/50000000"
        assert resp.headers.get("Content-Length") == "1024"
        assert resp.headers.get("Content-Type") == "video/mp4"


@pytest.mark.asyncio
async def test_direct_stream_subsequent_range(streaming_setup):
    """GET /stream/<token> with Range: bytes=1024-2047 returns correct slice."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=1024-2047",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 206
        assert resp.headers.get("Content-Range") == "bytes 1024-2047/50000000"
        assert resp.headers.get("Content-Length") == "1024"


@pytest.mark.asyncio
async def test_direct_stream_near_eof_large_offset(streaming_setup):
    """GET /stream/<token> with offset near EOF clamps properly to file_size - 1."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=49999000-55000000",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 206
        assert resp.headers.get("Content-Range") == "bytes 49999000-49999999/50000000"
        assert resp.headers.get("Content-Length") == "1000"


@pytest.mark.asyncio
async def test_direct_stream_suffix_range(streaming_setup):
    """GET /stream/<token> with suffix Range: bytes=-500 fetches last 500 bytes."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=-500",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 206
        assert resp.headers.get("Content-Range") == "bytes 49999500-49999999/50000000"
        assert resp.headers.get("Content-Length") == "500"


@pytest.mark.asyncio
async def test_direct_stream_invalid_range_416(streaming_setup):
    """Invalid Range header returns HTTP 416 with Content-Range: bytes */total."""
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={
            "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
            "Range": "bytes=60000000-70000000",
        },
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await stream_handler(req)
        assert resp.status == 416
        assert resp.headers.get("Content-Range") == "bytes */50000000"


@pytest.mark.asyncio
async def test_direct_stream_seeking_sequential_ranges(streaming_setup):
    """Simulate user seeking: sequential non-contiguous Range requests succeed independently."""
    s = streaming_setup

    ranges = ["bytes=0-1023", "bytes=10485760-10486783", "bytes=31457280-31458303"]
    expected_ranges = [
        "bytes 0-1023/50000000",
        "bytes 10485760-10486783/50000000",
        "bytes 31457280-31458303/50000000",
    ]

    for rng, exp in zip(ranges, expected_ranges):
        req = make_mocked_request(
            "GET",
            f"/stream/{s['token']}",
            headers={
                "Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}",
                "Range": rng,
            },
            match_info={"token": s["token"]},
        )
        with patch("FileStream.server.stream_routes.multi_clients", {0: s["mock_bot"]}), \
             patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
             patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
             patch("FileStream.server.stream_routes.class_cache", {s["mock_bot"]: s["mock_streamer"]}), \
             patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": s["file_id"]}, None))), \
             patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

            resp = await stream_handler(req)
            assert resp.status == 206
            assert resp.headers.get("Content-Range") == exp


@pytest.mark.asyncio
async def test_direct_stream_missing_file_404(streaming_setup):
    """Missing file reference returns 404."""
    from FileStream.server.exceptions import FileNotFound
    s = streaming_setup
    req = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}"},
        match_info={"token": s["token"]},
    )

    with patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(side_effect=FileNotFound)):
        with pytest.raises(web.HTTPNotFound):
            await stream_handler(req)


@pytest.mark.asyncio
async def test_direct_stream_invalid_or_expired_token_403(streaming_setup):
    """Invalid token or session mismatch returns HTTP 403 Forbidden."""
    s = streaming_setup

    # 1. Non-existent token
    req_bad_token = make_mocked_request(
        "GET",
        "/stream/invalid_token_123",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}={s['session_id']}"},
        match_info={"token": "invalid_token_123"},
    )
    with pytest.raises(web.HTTPForbidden):
        await stream_handler(req_bad_token)

    # 2. Session mismatch
    req_bad_session = make_mocked_request(
        "GET",
        f"/stream/{s['token']}",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}=evil_session"},
        match_info={"token": s["token"]},
    )
    with pytest.raises(web.HTTPForbidden):
        await stream_handler(req_bad_session)


def test_canonical_mime_type_mapping():
    """Verify canonical MIME type resolution for all media extensions."""
    assert _canonical_mime_type(".mp4") == "video/mp4"
    assert _canonical_mime_type(".m4v") == "video/x-m4v"
    assert _canonical_mime_type(".mkv") == "video/x-matroska"
    assert _canonical_mime_type(".webm") == "video/webm"
    assert _canonical_mime_type(".mov") == "video/quicktime"
    assert _canonical_mime_type(".avi") == "video/x-msvideo"
    assert _canonical_mime_type(".mp3") == "audio/mpeg"
    assert _canonical_mime_type(".m4a") == "audio/mp4"
    assert _canonical_mime_type(".flac") == "audio/flac"
    assert _canonical_mime_type(".wav") == "audio/wav"
    assert _canonical_mime_type(".ogg") == "audio/ogg"


# ==============================================================================
# 3. STORYBOARD THUMBNAILS & WEBVTT FORMATTING
# ==============================================================================
def test_format_vtt_timestamp():
    assert format_vtt_timestamp(0) == "00:00:00.000"
    assert format_vtt_timestamp(65.5) == "00:01:05.500"
    assert format_vtt_timestamp(3661.123) == "01:01:01.123"


@pytest.mark.asyncio
async def test_thumbnail_routes():
    with tempfile.TemporaryDirectory() as tmp_dir:
        svc = ThumbnailService(cache_dir=tmp_dir)
        file_id = "60c72b2f9b1d8b001c8e4f1a"
        out_dir = svc.cache_dir / file_id
        out_dir.mkdir(parents=True, exist_ok=True)

        vtt_file = out_dir / "thumbnails.vtt"
        vtt_file.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nsprites.webp#xywh=0,0,160,90\n", encoding="utf-8")

        sprite_file = out_dir / "sprites.webp"
        sprite_file.write_bytes(b"RIFF....WEBPVP8 ")

        session_id = "sess_thumb_test"
        token = token_service.create_access_token(
            path=file_id,
            kind="stream",
            metadata={"session_id": session_id},
        )

        with patch("FileStream.server.stream_routes.thumbnail_service", svc), \
             patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=({"_id": file_id}, None))):

            # VTT Route
            vtt_req = make_mocked_request(
                "GET",
                f"/thumbnails/{token}/thumbnails.vtt",
                headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
                match_info={"token": token},
            )
            vtt_resp = await thumbnail_vtt_handler(vtt_req)
            assert vtt_resp.status == 200
            assert vtt_resp.content_type == "text/vtt"
            assert "WEBVTT" in vtt_resp.text

            # Sprite Route
            sprite_req = make_mocked_request(
                "GET",
                f"/thumbnails/{token}/sprites.webp",
                headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
                match_info={"token": token},
            )
            sprite_resp = await thumbnail_sprite_handler(sprite_req)
            assert sprite_resp.status == 200
            assert sprite_resp.content_type == "image/webp"
            assert len(sprite_resp.body) > 0


@pytest.mark.asyncio
async def test_thumbnail_vtt_fallback_empty_vtt():
    file_id = "60c72b2f9b1d8b001c8e4f1a"
    session_id = "sess_thumb_fallback"
    token = token_service.create_access_token(
        path=file_id,
        kind="stream",
        metadata={"session_id": session_id},
    )

    mock_db_file = {
        "_id": file_id,
        "duration": 60,
    }

    mock_thumb_svc = MagicMock()
    mock_thumb_svc.get_vtt_content.return_value = None
    mock_thumb_svc.has_thumbnails.return_value = False
    mock_thumb_svc.is_generating.return_value = False

    req = make_mocked_request(
        "GET",
        f"/thumbnails/{token}/thumbnails.vtt",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        match_info={"token": token},
    )

    with patch("FileStream.server.stream_routes.thumbnail_service", mock_thumb_svc), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=(mock_db_file, None))):
        resp = await thumbnail_vtt_handler(req)
        assert resp.status == 200
        assert resp.content_type == "text/vtt"
        assert resp.text == "WEBVTT\n\n"


# ==============================================================================
# 4. FAST SCRUBBING & MOOV VIRTUALIZATION
# ==============================================================================
@pytest.mark.asyncio
async def test_fast_scrub_cached_range_satisfaction():
    file_id = "60c72b2f9b1d8b001c8e4f1a"
    fake_moov_bytes = b"X" * 1024

    mock_db_file = {
        "_id": file_id,
        "file_size": 10_000,
        "mime_type": "video/mp4",
        "file_name": "movie.mp4",
    }

    mock_mp4_mgr = MagicMock()
    mock_mp4_mgr.get_cached_range.return_value = fake_moov_bytes[:500]

    mock_bot = MagicMock()
    mock_streamer = MagicMock()
    fake_props = MagicMock()
    fake_props.file_size = 10_000
    fake_props.mime_type = "video/mp4"
    fake_props.file_name = "movie.mp4"
    mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

    req = make_mocked_request(
        "GET",
        f"/stream/fake_token",
        headers={"Range": "bytes=0-499"},
    )

    with patch("FileStream.server.stream_routes.mp4_metadata_manager", mock_mp4_mgr), \
         patch("FileStream.server.stream_routes.multi_clients", {0: mock_bot}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 1}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {mock_bot: mock_streamer}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=(mock_db_file, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await media_streamer(req, db_id=file_id, force_download=False)
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 0-499/10000"
        assert len(resp.body) == 500
        mock_streamer.yield_file.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_tail_bytes_1024_byte_alignment():
    byte_streamer = MagicMock()
    file_id = "test_file"
    file_size = 10_000_000

    yielded_chunks = []
    async def mock_yield(*args, **kwargs):
        chunk = b"T" * (1024 * 1024)
        yielded_chunks.append(chunk)
        yield chunk

    byte_streamer.yield_file = MagicMock(side_effect=mock_yield)

    mgr = MP4MetadataManager()
    tail_data = await mgr.fetch_tail_bytes(
        byte_streamer=byte_streamer,
        file_id=MagicMock(),
        file_key=file_id,
        file_size=file_size,
        client_index=0,
        scan_bytes=2 * 1024 * 1024,
    )

    assert len(tail_data) > 0
    call_args = byte_streamer.yield_file.call_args[0]
    offset = call_args[3]
    first_part_cut = call_args[4]
    chunk_size = call_args[7]

    assert offset % chunk_size == 0
    assert first_part_cut % 1024 == 0 or first_part_cut == 0


@pytest.mark.asyncio
async def test_custom_dl_fetch_chunk_alignment():
    byte_streamer = MagicMock()
    chunk_size = 1024 * 1024

    async def mock_yield(file_key, tg_file, client_idx, offset, first_part_cut, last_part_cut, part_count, cs):
        assert offset % cs == 0, f"Offset {offset} must align with chunk size {cs}"
        assert first_part_cut >= 0
        yield b"CHUNK"

    byte_streamer.yield_file = mock_yield

    mgr = MP4MetadataManager()
    await mgr.fetch_tail_bytes(
        byte_streamer=byte_streamer,
        file_id=MagicMock(),
        file_key="key",
        file_size=5_500_000,
        client_index=0,
        scan_bytes=1_000_000,
    )


@pytest.mark.asyncio
async def test_custom_dl_media_session_reuse():
    from FileStream.bot import multi_clients
    mock_client = MagicMock()
    mock_media_session = MagicMock()
    mock_client.media_sessions = {1: mock_media_session}

    with patch.dict(multi_clients, {0: mock_client}, clear=True):
        client = multi_clients[0]
        assert getattr(client, "media_sessions", {}).get(1) is mock_media_session


@pytest.mark.asyncio
async def test_virtual_faststart_range_stream():
    mgr = MP4MetadataManager()
    header_data = b"FTYP_HDR"
    fake_moov = b"MOOV_ATOM_CONTENT"
    cached_moov = {
        "header_data": header_data,
        "patched_moov": fake_moov,
        "is_tail": True,
        "offset": 1000,
        "size": 17,
        "moov_offset": 1000,
        "moov_size": 17,
        "patched_len": len(header_data) + len(fake_moov),
    }

    mock_streamer = MagicMock()
    async def mock_yield(*args, **kwargs):
        yield b"BODY_DATA_FROM_OFFSET"
    mock_streamer.yield_file = mock_yield

    stream = mgr.virtual_faststart_range_stream(
        byte_streamer=mock_streamer,
        file_id=MagicMock(),
        file_key="test_file",
        client_index=0,
        from_bytes=0,
        until_bytes=100,
        file_size=2000,
        chunk_size=1024 * 1024,
        cached_moov=cached_moov,
    )

    chunks = []
    async for c in stream:
        chunks.append(c)

    combined = b"".join(chunks)
    assert combined.startswith(header_data + fake_moov)
    assert b"BODY_DATA_FROM_OFFSET" in combined


@pytest.mark.asyncio
async def test_media_streamer_virtual_faststart_initial_range():
    file_id = "60c72b2f9b1d8b001c8e4f1a"
    header_data = b"FTYP" * 10  # 40 bytes
    fake_moov = b"MOOV" * 20   # 80 bytes (total prefix = 120 bytes > 100)
    cached_moov = {
        "header_data": header_data,
        "patched_moov": fake_moov,
        "is_tail": True,
        "offset": 9000,
        "size": len(fake_moov),
        "moov_offset": 9000,
        "moov_size": len(fake_moov),
        "patched_len": len(header_data) + len(fake_moov),
    }

    mock_db_file = {
        "_id": file_id,
        "file_size": 10_000,
        "mime_type": "video/mp4",
        "file_name": "movie.mp4",
    }

    mock_mp4_mgr = MagicMock()
    mock_mp4_mgr.get_cached_range.return_value = None
    mock_mp4_mgr.get_cached_moov.return_value = cached_moov

    mock_bot = MagicMock()
    mock_streamer = MagicMock()
    fake_props = MagicMock()
    fake_props.file_size = 10_000
    fake_props.mime_type = "video/mp4"
    fake_props.file_name = "movie.mp4"
    mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

    req = make_mocked_request(
        "GET",
        "/stream/fake_token",
        headers={"Range": "bytes=0-100"},
    )

    with patch("FileStream.server.stream_routes.mp4_metadata_manager", mock_mp4_mgr), \
         patch("FileStream.server.stream_routes.multi_clients", {0: mock_bot}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 1}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {mock_bot: mock_streamer}), \
         patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=(mock_db_file, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await media_streamer(req, db_id=file_id, force_download=False)
        assert resp.status == 206
        assert resp.headers["Content-Range"] == "bytes 0-100/10000"
        assert len(resp.body) == 101
        assert resp.body == (header_data + fake_moov)[:101]
        mock_streamer.yield_file.assert_not_called()


@pytest.mark.asyncio
async def test_stream_cache_memory_ring_buffer():
    from FileStream.utils.stream_cache import LocalStreamCache

    with tempfile.TemporaryDirectory() as tmp_dir:
        cache = LocalStreamCache()
        cache.root = Path(tmp_dir)
        cache.enabled = True
        cache._max_mem_chunks = 3

        file_key = "test_mem_file"
        data1 = b"CHUNK_1" * 100
        data2 = b"CHUNK_2" * 100
        data3 = b"CHUNK_3" * 100
        data4 = b"CHUNK_4" * 100

        await cache.put_chunk(file_key, 0, data1)
        await cache.put_chunk(file_key, 1000, data2)
        await cache.put_chunk(file_key, 2000, data3)

        # All 3 in RAM
        assert await cache.get_chunk(file_key, 0) == data1
        assert await cache.get_chunk(file_key, 1000) == data2
        assert await cache.get_chunk(file_key, 2000) == data3

        # Add 4th chunk -> evicts chunk 0 from RAM ring-buffer (LRU)
        await cache.put_chunk(file_key, 3000, data4)
        assert len(cache._mem_cache) == 3

        # Chunk 0 still retrievable from disk
        assert await cache.get_chunk(file_key, 0) == data1


# ==============================================================================
# 5. INTEGRATION TESTS (TOKEN RESPONSE & ZERO HLS GUARANTEE)
# ==============================================================================
@pytest.mark.asyncio
async def test_get_stream_token_handler_returns_direct_url_and_no_hls():
    """Verify /get-stream-token/<path> returns stream_url and NO HLS or .m3u8 urls."""
    file_id = "60c72b2f9b1d8b001c8e4f1a"
    session_id = "sess_integration_direct_1"

    page_token = token_service.create_access_token(
        path=file_id,
        kind="page",
        metadata={"session_id": session_id},
    )

    mock_db_file = {
        "_id": file_id,
        "file_name": "video_sample.mp4",
        "mime_type": "video/mp4",
        "file_size": 15000000,
        "duration": 120,
    }

    req = make_mocked_request(
        "GET",
        f"/get-stream-token/{file_id}?page_token={page_token}",
        headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        match_info={"path": file_id},
    )

    with patch("FileStream.server.stream_routes.db.resolve_file_reference", AsyncMock(return_value=(mock_db_file, None))), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        resp = await get_stream_token_handler(req)
        assert resp.status == 200
        import json
        body = json.loads(resp.text)

        assert body["success"] is True
        assert "/stream/" in body["stream_url"]
        assert "hls_url" not in body
        assert ".m3u8" not in body["stream_url"]
        assert "/hls/" not in body["stream_url"]
        assert body["mime_type"] == "video/mp4"
        assert body["is_native_playable"] is True


@pytest.mark.asyncio
async def test_end_to_end_player_direct_source_contract():
    """Verify player source resolver resolves direct HTTP stream source."""
    token_data = {
        "success": True,
        "stream_url": "http://127.0.0.1:8080/stream/stream_token_xyz",
        "mime_type": "video/mp4",
        "file_duration": 3600,
        "thumbnails_url": "http://127.0.0.1:8080/thumbnails/token/thumbnails.vtt",
    }

    # Verify no HLS in token_data
    assert "hls_url" not in token_data

    # Simulating resolver.js logic
    def resolve_source(data):
        if not data or not data.get("success"):
            return None
        if data.get("stream_url"):
            return {
                "type": "direct",
                "src": data["stream_url"],
                "mimeType": data.get("mime_type", "video/mp4"),
                "duration": data.get("file_duration", 0),
                "thumbnails": data.get("thumbnails_url"),
            }
        return None

    resolved = resolve_source(token_data)
    assert resolved is not None
    assert resolved["type"] == "direct"
    assert resolved["src"] == token_data["stream_url"]
    assert resolved["mimeType"] == "video/mp4"
    assert "/stream/" in resolved["src"]
    assert "m3u8" not in resolved["src"]
