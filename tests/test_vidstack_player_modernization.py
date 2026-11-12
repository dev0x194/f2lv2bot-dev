"""
Tests for Modernized Vidstack Media Player, Source Resolver, and Streaming Contract.
Validates Vidstack Web Components integration, pinned dependency, lifecycle state machine,
source resolution, mobile touch targets, security token scoping, and range streaming.
"""

import os
import re
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from bson import ObjectId
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from FileStream.server.render_template import render_page
from FileStream.server.stream_routes import (
    get_stream_token_handler,
    stream_handler,
    media_streamer,
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
# 1. VIDSTACK PINNED DEPENDENCY & ASSET CHECKS
# ==============================================================================

def test_vidstack_player_js_exists_and_is_valid():
    """Verify that player.js exists in static assets and contains core FSM abstractions."""
    root = os.path.dirname(os.path.dirname(__file__))
    player_js_path = os.path.join(root, "FileStream", "web", "static", "player.js")
    assert os.path.isfile(player_js_path), "FileStream/web/static/player.js must exist"
    assert os.path.getsize(player_js_path) > 2000, "player.js must contain substantial implementation"

    with open(player_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Core namespaces & classes
    assert "FSPlayer" in content
    assert "PlayerState" in content
    assert "resolveMediaSource" in content
    assert "PlayerController" in content

    # Lifecycle states
    for state in ["IDLE", "AUTHENTICATING", "LOADING", "READY", "PLAYING", "PAUSED", "BUFFERING", "ERROR", "ENDED"]:
        assert state in content

    # Watchdog & concurrency guards
    assert "failsafeTimer" in content
    assert "AbortController" in content
    assert "watchdog" in content.lower() or "failsafe" in content.lower()


def test_play_template_pinned_vidstack_version(mock_db, mock_bot):
    """Verify that play.html loads the pinned Vidstack v1.15.6 build instead of unpinned latest."""
    root = os.path.dirname(os.path.dirname(__file__))
    play_html_path = os.path.join(root, "FileStream", "web", "templates", "play.html")
    with open(play_html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Check pinned version
    assert "player@1.15.6" in content, "Vidstack must be pinned to 1.15.6"
    assert "cdn.vidstack.io/player" in content
    assert "/static/player.js" in content
    assert "/static/app.js" in content


# ==============================================================================
# 2. TEMPLATE RENDERING: VIDEO & AUDIO LAYOUTS
# ==============================================================================

@pytest.mark.asyncio
async def test_render_page_video_vidstack_components(mock_db, mock_bot):
    """Verify that render_page correctly generates Vidstack elements and excludes legacy Plyr."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "interstellar_trailer.mp4",
            "file_size": 45000000,
            "mime_type": "video/mp4",
            "duration": 180,
            "resolution": "1080p",
            "video_codec": "AVC / H.264",
            "audio_codec": "AAC",
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "vid_modern_1"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="session_vidstack_test",
        )

        assert "<!doctype html>" in html.lower()
        assert "<media-player" in html
        assert "<media-provider>" in html
        assert "<media-video-layout" in html
        assert "<media-poster" in html
        assert "/static/player.js" in html

        # Plyr must NOT be loaded
        assert "/static/plyr.js" not in html
        assert "/static/plyr.css" not in html


@pytest.mark.asyncio
async def test_render_page_audio_vidstack_components(mock_db, mock_bot):
    """Verify that audio media renders media-audio-layout and audio stylesheet."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "podcast_episode_1.mp3",
            "file_size": 20000000,
            "mime_type": "audio/mpeg",
            "duration": 1200,
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "aud_modern_1"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="session_audio_test",
        )

        assert "<media-player" in html
        assert "<media-audio-layout" in html
        assert "audio.css" in html
        assert "<media-video-layout" not in html


# ==============================================================================
# 3. BACKEND STREAM TOKEN CONTRACT & PERMISSION SCOPING
# ==============================================================================

@pytest.mark.asyncio
async def test_get_stream_token_valid_contract(mock_db):
    """Verify get_stream_token_handler returns structured payload with stream_url, mime_type, and codecs."""
    with patch("FileStream.server.stream_routes.db", mock_db):
        fid = ObjectId()
        session_id = "sess_stream_ok"
        file_doc = {
            "_id": fid,
            "file_name": "the_ship.mkv",
            "file_size": 250000000,
            "mime_type": "video/x-matroska",
            "duration": 2400,
            "video_codec": "HEVC / H.265",
            "audio_codec": "AAC",
            "resolution": "720p HD",
        }
        await mock_db.file.insert_one(file_doc)
        public_id = "ship_mkv_public"

        # Create valid page token bound to session
        page_token = token_service.create_access_token(
            public_id,
            kind="page",
            expires_in_seconds=1800,
            metadata={"session_id": session_id},
        )

        req = make_mocked_request(
            "GET",
            f"/get-stream-token/{public_id}?page_token={page_token}",
            headers={"Cookie": f"{PAGE_SESSION_COOKIE}={session_id}"},
        )
        req.match_info["path"] = public_id

        # Set resolve_file_reference on mock_db
        mock_db.resolve_file_reference = AsyncMock(return_value=(file_doc, {"public_id": public_id}))
        with patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock()):
            resp = await get_stream_token_handler(req)
            assert resp.status == 200
            import json
            body = json.loads(resp.text)
            assert body["success"] is True
            assert "/stream/" in body["stream_url"]
            assert "hls_url" not in body
            assert "/hls/" not in body["stream_url"]
            assert body.get("mime_type") == "video/x-matroska"
            assert "/thumbnails/" in body["thumbnails_url"]
            assert body["file_duration"] == 2400
            assert body["video_codec"] == "HEVC / H.265"
            assert body["is_native_playable"] is False  # MKV is not native


@pytest.mark.asyncio
async def test_get_stream_token_invalid_session_rejected(mock_db):
    """Verify get_stream_token_handler rejects requests where session cookie does not match page token."""
    with patch("FileStream.server.stream_routes.db", mock_db):
        public_id = "pub_unauth_stream"
        page_token = token_service.create_access_token(
            public_id,
            kind="page",
            expires_in_seconds=1800,
            metadata={"session_id": "legit_session"},
        )

        # Attacker provides different session cookie
        req = make_mocked_request(
            "GET",
            f"/get-stream-token/{public_id}?page_token={page_token}",
            headers={"Cookie": f"{PAGE_SESSION_COOKIE}=attacker_session"},
        )
        req.match_info["path"] = public_id

        with pytest.raises(web.HTTPForbidden):
            await get_stream_token_handler(req)


# ==============================================================================
# 4. HTTP RANGE REQUEST STREAMING CONTRACT
# ==============================================================================

def test_parse_range_semantics():
    """Verify RFC 7233 range parsing edge cases."""
    file_size = 10000

    # Normal range
    assert _parse_range("bytes=0-499", file_size) == (0, 499)
    assert _parse_range("bytes=500-999", file_size) == (500, 999)

    # Prefix range (from byte X to EOF)
    assert _parse_range("bytes=9000-", file_size) == (9000, 9999)

    # Suffix range (last N bytes)
    assert _parse_range("bytes=-500", file_size) == (9500, 9999)

    # Missing header returns None
    assert _parse_range("", file_size) is None
    assert _parse_range(None, file_size) is None

    # Invalid range headers return False (should trigger 416)
    assert _parse_range("invalid", file_size) is False
    assert _parse_range("bytes=", file_size) is False
    assert _parse_range("bytes=-", file_size) is False
    assert _parse_range("bytes=abc-def", file_size) is False


# ==============================================================================
# 5. SOURCE RESOLVER JAVASCRIPT COMPLIANCE TESTS
# ==============================================================================

def test_source_resolver_patterns_in_player_js():
    """Verify that player.js implements source resolution rules for direct HTTP range streaming."""
    root = os.path.dirname(os.path.dirname(__file__))
    player_js_path = os.path.join(root, "FileStream", "web", "static", "player.js")
    with open(player_js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Rule 1: Direct stream resolution
    assert "stream_url" in content
    assert "playerEl.src =" in content
    assert "data.hls_url" not in content
    assert "application/x-mpegurl" not in content
    # Rule 2: Storyboard scrubbing thumbnails
    assert "thumbnails_url" in content or "thumbnails" in content
    # Rule 3: Progress resume
    assert "FS.Progress" in content
    assert "getResumeSeconds" in content


# ==============================================================================
# 6. MOBILE UX & ACCESSIBILITY TESTS
# ==============================================================================

def test_player_css_accessibility_touch_targets():
    """Verify player.css satisfies mobile touch target requirements."""
    root = os.path.dirname(os.path.dirname(__file__))
    css_path = os.path.join(root, "FileStream", "web", "static", "player.css")
    with open(css_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Minimum button dimensions
    assert "min-height: 44px" in content or "min-width: 44px" in content
    # Vidstack video layout controls
    assert ".vds-button" in content
    # Responsive media query
    assert "@media (max-width: 600px)" in content
