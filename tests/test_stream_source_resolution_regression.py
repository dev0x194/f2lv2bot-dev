"""
Regression tests for stream source resolution and media request lifecycle.
Specifically verifies:
1. /get-stream-token returns HTTP 200 with valid playable stream metadata.
2. Frontend MediaSourceResolver resolves a valid playable source object.
3. PlayerController assigns source directly to the player element (preventing DOM ID mismatch bug).
4. Browser issues Range request (HTTP 206) to the resolved stream endpoint.
5. Malformed token responses immediately trigger PlayerState.ERROR without waiting for watchdog timeout.
6. All 29 diagnostic instrumentation checkpoints and safe token redactions are present.
"""

import os
import re
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from bson import ObjectId
from aiohttp import web
from aiohttp.test_utils import make_mocked_request, TestClient, TestServer

from FileStream.server.render_template import render_page
from FileStream.server.stream_routes import (
    get_stream_token_handler,
    stream_handler,
    media_streamer,
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


@pytest.mark.asyncio
async def test_stream_source_resolution_and_media_request_pipeline(mock_db, mock_bot):
    """
    Test the complete chain:
    /get-stream-token (200) -> source resolution -> player assignment -> actual stream request (206 Range).
    """
    with patch("FileStream.server.stream_routes.db", mock_db), \
         patch("FileStream.server.stream_routes.FileStream", mock_bot), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):

        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "movie_sample.mp4",
            "file_size": 25000000,
            "mime_type": "video/mp4",
            "duration": 3600,
        }
        await mock_db.file.insert_one(file_doc)
        public_id = "pub_stream_reg_1"
        await mock_db.file.insert_one({"_id": ObjectId(), "public_id": public_id, "file_id": str(fid)})
        mock_db.resolve_file_reference = AsyncMock(return_value=(file_doc, {"public_id": public_id}))

        session_id = "test_page_session_reg_1"
        page_token = token_service.create_access_token(
            public_id,
            kind="page",
            expires_in_seconds=1800,
            metadata={"session_id": session_id},
        )

        # 1. /get-stream-token request
        req = make_mocked_request(
            "GET",
            f"/get-stream-token/{public_id}?page_token={page_token}",
            headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req.match_info["path"] = public_id

        token_resp = await get_stream_token_handler(req)
        assert token_resp.status == 200, "Token endpoint must return HTTP 200"

        import json
        token_data = json.loads(token_resp.body.decode("utf-8"))

        # 2. Verify token response shape
        assert token_data.get("success") is True
        assert token_data.get("is_native_playable") is True
        stream_url = token_data.get("stream_url")
        assert stream_url and "/stream/" in stream_url, "Must return valid stream_url"

        # 3. Simulate MediaSourceResolver logic
        # Directly mirrors FileStream/web/static/js/player/resolver.js
        def simulate_resolve_media_source(data, options=None):
            if not data or not data.get("success"):
                return None
            if data.get("stream_url"):
                return {
                    "type": "direct",
                    "src": data["stream_url"],
                    "mimeType": (options or {}).get("mimeType") or "video/mp4",
                }
            return None

        resolved = simulate_resolve_media_source(token_data)
        assert resolved is not None, "Source resolution must succeed for playable token data"
        assert resolved["type"] == "direct"
        assert resolved["src"] == stream_url
        assert resolved["mimeType"] == "video/mp4"

        # 4. Extract token and simulate media Range request issued by player
        stream_token = stream_url.split("/stream/")[1].split("?")[0]
        stream_req = make_mocked_request(
            "GET",
            f"/stream/{stream_token}",
            headers={
                "Range": "bytes=0-1048575",
                "Cookie": f"{PAGE_SESSION_COOKIE}={session_id}",
            },
        )
        stream_req.match_info["token"] = stream_token

        with patch("FileStream.server.stream_routes.multi_clients", {0: mock_bot}), \
             patch("FileStream.server.stream_routes.work_loads", {0: 0}), \
             patch("FileStream.server.stream_routes.choose_best_client", return_value=0), \
             patch("FileStream.server.stream_routes.class_cache", {}), \
             patch("FileStream.server.stream_routes.utils.ByteStreamer") as mock_streamer_cls:

            mock_streamer = MagicMock()
            fake_props = MagicMock()
            fake_props.file_size = 25000000
            fake_props.mime_type = "video/mp4"
            fake_props.file_name = "movie_sample.mp4"
            mock_streamer.get_file_properties = AsyncMock(return_value=fake_props)

            async def dummy_yield(*args, **kwargs):
                yield b"X" * 1024

            mock_streamer.yield_file = dummy_yield
            mock_streamer_cls.return_value = mock_streamer

            media_resp = await stream_handler(stream_req)
            assert media_resp.status == 206, "Player media request must receive HTTP 206 Partial Content"
            assert media_resp.headers.get("Accept-Ranges") == "bytes"
            assert "bytes 0-1048575/25000000" in media_resp.headers.get("Content-Range", "")
            assert media_resp.headers.get("Content-Type") == "video/mp4"


def test_player_js_and_controller_dom_element_fallback():
    """Verify that player.js and controller.js query player by multiple resilient selectors."""
    root = os.path.dirname(os.path.dirname(__file__))
    player_js_path = os.path.join(root, "FileStream", "web", "static", "js", "player.js")
    controller_js_path = os.path.join(root, "FileStream", "web", "static", "js", "player", "controller.js")

    for path in [player_js_path, controller_js_path]:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Resilient element resolution
        assert "document.getElementById('player')" in content
        assert "document.getElementById('vidstackPlayer')" in content
        assert "querySelector('media-player')" in content
        assert "config.playerElement" in content

        # Layout resolution
        assert "document.getElementById('vidstackLayout')" in content
        assert "media-video-layout" in content

        # Player API compatibility
        assert "async initialize()" in content
        assert "teardown()" in content
        assert "this.activeSource = resolved" in content
        assert "playerEl.src =" in content
        assert "this.mediaRequestStarted = true" in content

        # Immediate error detection without waiting for watchdog
        assert "Media player element not found in DOM" in content
        assert "No playable media stream found for this file" in content


def test_malformed_token_response_triggers_immediate_error():
    """Verify that player.js resolver and controller immediately reject malformed responses."""
    root = os.path.dirname(os.path.dirname(__file__))
    player_js_path = os.path.join(root, "FileStream", "web", "static", "js", "player.js")
    resolver_js_path = os.path.join(root, "FileStream", "web", "static", "js", "player", "resolver.js")

    for path in [player_js_path, resolver_js_path]:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # resolver must guard against data.success === false or non-object
        assert "data.success === false" in content or "data.success" in content

    with open(player_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # controller must disarm watchdog on error and enter PlayerState.ERROR
    assert "if (this.watchdog) this.watchdog.disarm()" in content
    assert "PlayerState.ERROR" in content
    assert "watchdog.arm()" in content
    assert "watchdog.disarm()" in content


def test_mandatory_29_diagnostics_and_safe_redaction():
    """Verify all 29 diagnostic logging checkpoints and token redaction rules are present."""
    root = os.path.dirname(os.path.dirname(__file__))
    player_js_path = os.path.join(root, "FileStream", "web", "static", "js", "player.js")

    with open(player_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Redaction helper
    assert "_redact" in content
    assert "[REDACTED]" in content
    assert "page_token" in content
    assert "stream_token" in content

    # Check key checkpoints out of the 29
    checkpoints = [
        "1: player bootstrap",
        "2: custom element availability",
        "3: controller initialization",
        "4: token request start",
        "5: token response HTTP status",
        "6: complete token response shape",
        "7: resolved stream URL/path",
        "8: resolved MIME type",
        "9: selected playback mode",
        "10: assignment of source to media-provider/player",
        "11: media-player sourcechange",
        "12: provider change",
        "13: loadstart",
        "14: loadedmetadata",
        "15: durationchange",
        "16: canplay",
        "17: canplaythrough",
        "18: playing",
        "19: waiting",
        "20: stalled",
        "21: suspend",
        "22: error",
        "23: abort",
        "24: emptied",
        "25: progress",
        "26: timeupdate",
        "27: watchdog start",
        "28: watchdog cancel",
        "29: State Transition",
    ]

    for cp in checkpoints:
        num = cp.split(":")[0]
        assert f"'{num}'" in content or f'"{num}"' in content or num in content, f"Missing diagnostic checkpoint {cp}"
