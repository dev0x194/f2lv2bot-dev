import os
import re
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from bson import ObjectId
from aiohttp import web

from FileStream.server.render_template import render_page, _sanitize_uploader, format_clock
from FileStream.core.media.file_properties import _extract_uploader, get_file_info
from FileStream.services.thumbnail_service import ThumbnailService
from FileStream.server.stream_routes import favicon_handler, get_stream_token_handler
from tests.test_folder_mode import MockDatabase, MockBot, MockMessage


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


def test_uploader_sanitization_edge_cases():
    """Verify uploader names with commas, punctuation, or spaces are sanitized and never result in stray commas."""
    assert _sanitize_uploader(", ") is None
    assert _sanitize_uploader(",") is None
    assert _sanitize_uploader("  ,  ") is None
    assert _sanitize_uploader(",,") is None
    assert _sanitize_uploader("John ,") == "John"
    assert _sanitize_uploader(", Alice") == "Alice"
    assert _sanitize_uploader("Bob, Jr.") == "Bob, Jr"
    assert _sanitize_uploader(None) is None
    assert _sanitize_uploader("") is None
    assert _sanitize_uploader("ValidUser") == "ValidUser"


def test_format_clock():
    """Verify format_clock correctly formats seconds into mm:ss or hh:mm:ss."""
    assert format_clock(0) == "--:--"
    assert format_clock(59) == "00:59"
    assert format_clock(65) == "01:05"
    assert format_clock(3665) == "01:01:05"
    assert format_clock(None) == "--:--"
    assert format_clock("invalid") == "--:--"


@pytest.mark.asyncio
async def test_play_page_no_stray_comma_badge(mock_db):
    """Verify that when uploader is empty or invalid, the badge doesn't render stray commas."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "clean_video.mp4",
            "file_size": 1048576,
            "mime_type": "video/mp4",
            "duration": 60,
            "uploader": ", ",
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "clean_123"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_session",
        )

        # Stray comma badge must NOT be present
        assert "👤 ," not in html
        assert "👤  ," not in html


def test_tvmaze_completely_removed_from_template():
    """Verify that TVMaze fallback scraper is completely absent from the web templates."""
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "FileStream", "web", "templates", "play.html"
    )
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "tvmaze.com" not in content.lower()
    assert "api.tvmaze" not in content.lower()


def test_css_touch_targets():
    """Verify that mobile touch targets on Vidstack player buttons meet accessibility standard (44x44px)."""
    css_path = os.path.join(
        os.path.dirname(__file__), "..", "FileStream", "web", "static", "player.css"
    )
    with open(css_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "min-width: 44px" in content
    assert "min-height: 44px" in content


@pytest.mark.asyncio
async def test_favicon_route():
    """Verify that /favicon.ico returns 200 with SVG content type and cache headers."""
    req = MagicMock()
    resp = await favicon_handler(req)
    assert resp.status == 200
    assert resp.content_type == "image/svg+xml"
    assert "public, max-age=" in resp.headers.get("Cache-Control", "")
    assert b"<svg" in resp.body


def test_thumbnail_service_poster_methods(tmp_path):
    """Verify ThumbnailService poster path, existence check, and generation logic."""
    service = ThumbnailService(cache_dir=str(tmp_path))
    fid = "test_file_id_789"
    # Initially poster does not exist
    assert service.get_poster_path(fid) is None
    assert service.has_poster(fid) is False

    # Simulate poster generated on disk
    poster_file = service._file_dir(fid) / "poster.jpg"
    poster_file.parent.mkdir(parents=True, exist_ok=True)
    poster_file.write_bytes(b"dummy_poster_bytes")

    poster_path = service.get_poster_path(fid)
    assert poster_path is not None
    assert poster_path.name == "poster.jpg"
    assert service.has_poster(fid) is True


@pytest.mark.asyncio
async def test_get_stream_token_handler_metadata(mock_db):
    """Verify get_stream_token_handler enriches response with codecs, resolution, duration, and file size."""
    fid = ObjectId()
    file_doc = {
        "_id": fid,
        "file_name": "movie.mp4",
        "file_size": 50000000,
        "duration": 3600,
        "resolution": "1080p Full HD",
        "video_codec": "h264",
        "audio_codec": "aac",
    }
    await mock_db.file.insert_one(file_doc)

    req = MagicMock()
    req.match_info = {"path": str(fid)}
    mock_db_client = MagicMock()
    mock_db_client.resolve_file_reference = AsyncMock(return_value=(file_doc, None))

    with patch("FileStream.server.stream_routes._validate_page_token", return_value=True), \
         patch("FileStream.server.stream_routes.db", mock_db_client), \
         patch("FileStream.server.stream_routes.ensure_flog_media_exists", AsyncMock(return_value=None)), \
         patch("FileStream.server.stream_routes.token_service.create_access_token", return_value="mock_token_abc"), \
         patch("FileStream.server.stream_routes.build_base_url", return_value="http://127.0.0.1:8080"):
        resp = await get_stream_token_handler(req)
        assert resp.status == 200
        import json
        data = json.loads(resp.text)
        assert data.get("stream_url") == "http://127.0.0.1:8080/stream/mock_token_abc"
        assert data.get("file_duration") == 3600
        assert "MiB" in data.get("file_size")
        assert data.get("resolution") == "1080p Full HD"
        assert data.get("video_codec") == "h264"
        assert data.get("audio_codec") == "aac"
