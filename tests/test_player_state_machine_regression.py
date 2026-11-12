"""
Regression test suite covering the root-cause failures for:
1. False UNSUPPORTED state on playable MKV / HEVC media.
2. Player state machine invalid transitions (e.g. UNSUPPORTED -> PLAYING).
3. Terminal failure states preservation.
4. Non-blocking initial HTML page navigation.
5. Content-Range correctness (bytes=0-, bytes=0-1023, bytes=1024-, bytes=-1024, HTTP 416).
6. Disconnect / cancellation cleanup during streaming.
"""

import asyncio
import os
import re
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from bson import ObjectId
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from FileStream.server.render_template import render_page, _TEMPLATE_CACHE
from FileStream.server.stream_routes import (
    get_stream_token_handler,
    stream_handler,
    _parse_range,
    PAGE_SESSION_COOKIE,
)
from FileStream.services.token_service import token_service
from tests.test_folder_mode import MockDatabase, MockBot


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


# ==============================================================================
# TEST A: MKV / HEVC stream does not enter UNSUPPORTED when stream is playable
# ==============================================================================
def test_a_mkv_hevc_stream_does_not_falsely_enter_unsupported():
    """
    Verify that:
    1. checkBrowserPlaybackSupport does NOT reject MKV upfront when canPlayType returns empty string.
    2. PlayerController / player.js does not unconditionally set UNSUPPORTED on any error for .mkv files.
    """
    root = os.path.dirname(os.path.dirname(__file__))
    player_js = os.path.join(root, "FileStream", "web", "static", "js", "player.js")
    controller_js = os.path.join(root, "FileStream", "web", "static", "js", "player", "controller.js")
    resolver_js = os.path.join(root, "FileStream", "web", "static", "js", "player", "resolver.js")

    # 1. Verify flawed check fileName.match(/\.(mkv|avi|wmv|flv)$/i) was removed from error handlers
    for file_path in [player_js, controller_js]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "this.config.fileName.match(/\\.(mkv|avi|wmv|flv)$/i)" not in content, (
            f"Flawed unconditional MKV error fallback found in {file_path}"
        )

    # 2. Verify MKV is marked supported with 'maybe' in capability detection
    for file_path in [player_js, resolver_js]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "isMkv" in content
        assert "supported: true" in content
        assert "canPlay: 'maybe'" in content

    # 3. Verify transient abort (errorCode === 1) is ignored rather than entering error state
    for file_path in [player_js, controller_js]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "errCode === 1" in content
        assert "MEDIA_ERR_ABORTED" in content


# ==============================================================================
# TEST B: State machine rejects invalid transitions (UNSUPPORTED -> PLAYING)
# ==============================================================================
def test_b_state_machine_rejects_invalid_transitions():
    """
    Verify that:
    1. PlayerState enum includes explicit intermediate and terminal failure states.
    2. _onCanPlay and _onPlaying guard against transitioning when already in terminal failure states.
    """
    root = os.path.dirname(os.path.dirname(__file__))
    player_js = os.path.join(root, "FileStream", "web", "static", "js", "player.js")
    controller_js = os.path.join(root, "FileStream", "web", "static", "js", "player", "controller.js")
    state_js = os.path.join(root, "FileStream", "web", "static", "js", "player", "state.js")

    required_states = [
        "AUTHENTICATING",
        "RESOLVING_SOURCE",
        "PREPARING_MEDIA",
        "LOADING",
        "READY",
        "PLAYING",
        "AUTH_FAILED",
        "TOKEN_EXPIRED",
        "SOURCE_NOT_FOUND",
        "STREAM_UNAVAILABLE",
        "MEDIA_UNSUPPORTED",
        "NETWORK_ERROR",
        "PLAYBACK_ERROR",
    ]

    for file_path in [player_js, controller_js, state_js]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        for state in required_states:
            assert state in content, f"Missing required state {state} in {file_path}"

    # Verify terminal state guards in _onCanPlay and _onPlaying
    for file_path in [player_js, controller_js]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "terminalStates" in content
        assert "terminalStates.includes(this.state)" in content


# ==============================================================================
# TEST C: Terminal error state is preserved on real failure
# ==============================================================================
def test_c_terminal_error_state_is_preserved_on_real_failure():
    """
    Verify that real fatal error codes map to distinct failure states:
    - Code 4 -> MEDIA_UNSUPPORTED
    - Code 2 -> NETWORK_ERROR
    - Code 3 -> PLAYBACK_ERROR
    And HTTP statuses map to AUTH_FAILED, TOKEN_EXPIRED, SOURCE_NOT_FOUND, STREAM_UNAVAILABLE.
    """
    root = os.path.dirname(os.path.dirname(__file__))
    for file_path in [
        os.path.join(root, "FileStream", "web", "static", "js", "player.js"),
        os.path.join(root, "FileStream", "web", "static", "js", "player", "controller.js"),
    ]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "PlayerState.MEDIA_UNSUPPORTED" in content
        assert "PlayerState.NETWORK_ERROR" in content
        assert "PlayerState.PLAYBACK_ERROR" in content
        assert "PlayerState.AUTH_FAILED" in content
        assert "PlayerState.TOKEN_EXPIRED" in content
        assert "PlayerState.SOURCE_NOT_FOUND" in content
        assert "PlayerState.STREAM_UNAVAILABLE" in content


# ==============================================================================
# TEST D: Non-blocking page navigation
# ==============================================================================
@pytest.mark.asyncio
async def test_d_non_blocking_page_navigation(mock_db, mock_bot):
    """
    Verify that render_page:
    1. Does NOT synchronously call ensure_flog_media_exists (which could block on Telegram MTProto).
    2. Caches compiled template in _TEMPLATE_CACHE.
    3. Renders loading overlay visibly immediately (no blank/dark page).
    4. Renders 'INITIALIZING' status pill rather than premature 'READY'.
    5. Shows 'Loading metadata…' when duration is not yet resolved.
    """
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "hevc_sample.mkv",
        "file_size": 150000000,
        "mime_type": "video/x-matroska",
        "duration": 0,  # Duration not yet extracted
    }
    await mock_db.file.insert_one(file_doc)
    public_link = {"public_id": "pub_mkv_test"}

    with patch("FileStream.server.render_template.db", mock_db):
        # Initial render
        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_sess_d",
        )

        # 1. Overlay is visible immediately (hidden removed)
        assert 'id="playerOverlay"' in html
        assert 'id="playerOverlaySpinner"' in html
        assert 'id="loadingBox"' in html
        # playerOverlay itself should not have 'hidden' in its class list
        overlay_match = re.search(r'<div id="playerOverlay"[^>]*class="([^"]*)"', html)
        assert overlay_match is not None
        assert "hidden" not in overlay_match.group(1).split()

        # 2. Status text is INITIALIZING, not READY
        assert '<span id="playerStateText">INITIALIZING</span>' in html

        # 3. Duration indicates loading metadata rather than '--:--' or '---'
        assert "Loading metadata…" in html

        # 4. Template caching is populated
        assert "play.html" in _TEMPLATE_CACHE


# ==============================================================================
# TEST E: Correct Content-Range handling for all standard ranges
# ==============================================================================
@pytest.mark.asyncio
async def test_e_correct_content_range_handling_for_standard_ranges(mock_db, mock_bot):
    """
    Verify HTTP 206 responses and Content-Range calculations for:
    - bytes=0-
    - bytes=0-1023
    - bytes=1024-
    - bytes=-1024
    and HTTP 416 for out-of-range requests.
    """
    total_size = 25000000

    # 1. Unit test range parser directly
    # bytes=0-
    p1 = _parse_range("bytes=0-", total_size)
    assert p1 == (0, total_size - 1)

    # bytes=0-1023
    p2 = _parse_range("bytes=0-1023", total_size)
    assert p2 == (0, 1023)

    # bytes=1024-
    p3 = _parse_range("bytes=1024-", total_size)
    assert p3 == (1024, total_size - 1)

    # bytes=-1024 (suffix range)
    p4 = _parse_range("bytes=-1024", total_size)
    assert p4 == (total_size - 1024, total_size - 1)

    # Out of bounds ranges -> False (results in HTTP 416)
    assert _parse_range("bytes=30000000-40000000", total_size) is False
    assert _parse_range("bytes=25000000-", total_size) is False
    assert _parse_range("bytes=5000-1000", total_size) is False

    # 2. Integration test via stream_handler with simulated ByteStreamer
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "range_test.mp4",
        "file_size": total_size,
        "mime_type": "video/mp4",
    }
    await mock_db.file.insert_one(file_doc)
    mock_db.resolve_file_reference = AsyncMock(return_value=(file_doc, {"public_id": "test_pub"}))
    session_id = "test_range_session"
    stok = token_service.create_access_token(
        str(fid),
        kind="stream",
        expires_in_seconds=1800,
        metadata={"session_id": session_id},
    )

    with patch("FileStream.server.stream_routes.db", mock_db), \
         patch("FileStream.server.stream_routes.FileStream", mock_bot), \
         patch("FileStream.server.stream_routes.multi_clients", {0: mock_bot}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {}), \
         patch("FileStream.server.stream_routes.utils.ByteStreamer") as mock_streamer_cls:

        mock_streamer = MagicMock()
        fake_props = MagicMock()
        fake_props.file_size = total_size
        fake_props.mime_type = "video/mp4"
        fake_props.file_name = "range_test.mp4"
        mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

        async def dummy_yield(*args, **kwargs):
            yield b"M" * 1024

        mock_streamer.yield_file = dummy_yield
        mock_streamer_cls.return_value = mock_streamer

        # Test A: bytes=0-1023 -> 206
        req_206 = make_mocked_request(
            "GET",
            f"/stream/{stok}",
            headers={"Range": "bytes=0-1023", "Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req_206.match_info["token"] = stok
        resp_206 = await stream_handler(req_206)
        assert resp_206.status == 206
        assert resp_206.headers["Content-Range"] == f"bytes 0-1023/{total_size}"
        assert resp_206.headers["Content-Length"] == "1024"
        assert resp_206.headers["Accept-Ranges"] == "bytes"

        # Test B: bytes=-1024 (suffix) -> 206
        req_suffix = make_mocked_request(
            "GET",
            f"/stream/{stok}",
            headers={"Range": "bytes=-1024", "Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req_suffix.match_info["token"] = stok
        resp_suffix = await stream_handler(req_suffix)
        assert resp_suffix.status == 206
        assert resp_suffix.headers["Content-Range"] == f"bytes {total_size - 1024}-{total_size - 1}/{total_size}"
        assert resp_suffix.headers["Content-Length"] == "1024"

        # Test C: bytes=40000000-50000000 (out of bounds) -> 416
        req_416 = make_mocked_request(
            "GET",
            f"/stream/{stok}",
            headers={"Range": "bytes=40000000-50000000", "Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req_416.match_info["token"] = stok
        resp_416 = await stream_handler(req_416)
        assert resp_416.status == 416
        assert resp_416.headers["Content-Range"] == f"bytes */{total_size}"
        assert resp_416.headers["Accept-Ranges"] == "bytes"


# ==============================================================================
# TEST F: Disconnect cleans up streaming resources
# ==============================================================================
@pytest.mark.asyncio
async def test_f_disconnect_cleans_up_streaming_resources(mock_db, mock_bot):
    """
    Simulate a client disconnect (asyncio.CancelledError / ConnectionResetError)
    during streaming and verify that resources are cleaned up without hanging tasks.
    """
    total_size = 50000000
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "disconnect_test.mp4",
        "file_size": total_size,
        "mime_type": "video/mp4",
    }
    await mock_db.file.insert_one(file_doc)
    mock_db.resolve_file_reference = AsyncMock(return_value=(file_doc, {"public_id": "test_pub"}))
    session_id = "test_disconnect_session"
    stok = token_service.create_access_token(
        str(fid),
        kind="stream",
        expires_in_seconds=1800,
        metadata={"session_id": session_id},
    )

    with patch("FileStream.server.stream_routes.db", mock_db), \
         patch("FileStream.server.stream_routes.FileStream", mock_bot), \
         patch("FileStream.server.stream_routes.multi_clients", {0: mock_bot}), \
         patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
         patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
         patch("FileStream.server.stream_routes.class_cache", {}), \
         patch("FileStream.server.stream_routes.utils.ByteStreamer") as mock_streamer_cls:

        mock_streamer = MagicMock()
        fake_props = MagicMock()
        fake_props.file_size = total_size
        fake_props.mime_type = "video/mp4"
        fake_props.file_name = "disconnect_test.mp4"
        mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

        cleaned_up = False

        async def disconnecting_yield(*args, **kwargs):
            nonlocal cleaned_up
            try:
                yield b"Chunk 1"
                # Client disconnected midway
                raise ConnectionResetError("Client disconnected")
            finally:
                cleaned_up = True

        mock_streamer.yield_file = disconnecting_yield
        mock_streamer_cls.return_value = mock_streamer

        req = make_mocked_request(
            "GET",
            f"/stream/{stok}",
            headers={"Range": "bytes=0-1048575", "Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req.match_info["token"] = stok

        resp = await stream_handler(req)
        # When response body generator is consumed, ConnectionResetError is raised and finally executes
        async_iter = getattr(resp.body, "_value", None) or resp.body
        with pytest.raises(ConnectionResetError):
            async for _ in async_iter:
                pass

        assert cleaned_up is True, "Stream generator finally block must execute on client disconnect"
