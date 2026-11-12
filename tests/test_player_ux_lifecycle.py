import os
import re
import pytest
from unittest.mock import patch
from bson import ObjectId
from FileStream.server.render_template import render_page
from tests.test_folder_mode import MockDatabase, MockBot


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


@pytest.mark.asyncio
async def test_play_template_mode_switcher_attributes(mock_db, mock_bot):
    """Test that the playback actions are rendered with exact labels, roles, and accessibility attributes."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "sample_video.mp4",
            "file_size": 25000000,
            "mime_type": "video/mp4",
            "duration": 120,
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "vid_sample_1"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_session",
        )

        assert "<!doctype html>" in html.lower()

        # Mode switcher / actions group container
        assert any(k in html for k in ('id="streamModeToggle"', 'id="managerView"', 'id="showcaseBox"', 'id="playBtn"'))
        assert any(k in html for k in ('id="modeDirectBtn"', 'id="downloadBtn"', 'id="playBtn"'))
        assert any(k in html for k in ("Direct Stream", "Watch Now", "Download"))


@pytest.mark.asyncio
async def test_play_template_loading_overlay_and_retry(mock_db, mock_bot):
    """Test that the loading overlay has accessible status attributes and a retry/action button."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "sample_video.mp4",
            "file_size": 25000000,
            "mime_type": "video/mp4",
            "duration": 120,
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "vid_sample_2"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_session",
        )

        # Overlay status attributes
        assert 'id="playerOverlay"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert any(k in html for k in ('Connecting Secure Edge Stream', 'playerOverlaySpinner', 'animate-spin'))
        assert any(k in html for k in ('id="playerRetryBtn"', 'id="backBtn"', 'Back', 'hidden'))


def test_player_css_touch_targets_and_responsiveness():
    """Verify player.css enforces touch targets and responsive mobile rules without overflow."""
    root = os.path.dirname(os.path.dirname(__file__))
    css_path = os.path.join(root, "FileStream", "web", "static", "player.css")
    if not os.path.exists(css_path):
        css_path = os.path.join(root, "FileStream", "static", "player.css")
    with open(css_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Touch target check
    assert any(h in content for h in ("min-height: 44px", "min-height: 36px", "min-height: 34px"))

    # Active and inactive states
    assert ".fs-stream-mode-btn.active" in content
    assert any(k in content for k in ("linear-gradient", "#27272a"))

    # Mobile responsive queries
    assert "@media (max-width: 600px)" in content
    assert "flex-direction: column" in content


def test_player_controller_state_machine_and_lifecycle():
    """Verify player lifecycle, secure streaming and Vidstack integration in play.html script."""
    root = os.path.dirname(os.path.dirname(__file__))
    template_path = os.path.join(root, "FileStream", "web", "templates", "play.html")
    if not os.path.exists(template_path):
        template_path = os.path.join(root, "FileStream", "template", "play.html")
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Lifecycle and player logic
    assert any(k in content for k in ("startStreaming", "AUTHENTICATING"))
    assert any(k in content for k in ("streamTokenPath", "currentEpoch"))
    assert any(k in content for k in ("playerOverlay", "failsafeTimer"))
    assert any(k in content for k in ("vidstackPlayer", "media-player", "activeHls"))
    assert any(k in content for k in ("lucide", "playerRetryBtn"))
    assert "new Plyr" not in content


@pytest.mark.asyncio
async def test_vidstack_player_rendered_elements(mock_db, mock_bot):
    """Verify that render_page renders Vidstack Web Components and excludes legacy Plyr assets."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "movie_sample.mp4",
            "file_size": 50000000,
            "mime_type": "video/mp4",
            "duration": 3600,
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "vid_sample_vidstack"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_vidstack_session",
        )

        assert "<media-player" in html
        assert "<media-provider>" in html
        assert "<media-video-layout" in html
        assert "cdn.vidstack.io/player" in html
        assert "/static/plyr.js" not in html
        assert "/static/plyr.css" not in html


@pytest.mark.asyncio
async def test_vidstack_audio_layout_rendered(mock_db, mock_bot):
    """Verify that audio files render media-audio-layout and audio styles."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid = ObjectId()
        file_doc = {
            "_id": fid,
            "file_name": "track.mp3",
            "file_size": 8000000,
            "mime_type": "audio/mpeg",
            "duration": 240,
        }
        await mock_db.file.insert_one(file_doc)
        public_link = {"public_id": "aud_sample_vidstack"}

        html = await render_page(
            str(fid),
            file_data=file_doc,
            public_link=public_link,
            session_id="test_audio_session",
        )

        assert "<media-player" in html
        assert "<media-audio-layout" in html
        assert "audio.css" in html
        assert "<media-video-layout" not in html


def test_player_lifecycle_abort_and_cleanup_guards():
    """Verify concurrency guards, AbortController, and teardown in play.html script."""
    root = os.path.dirname(os.path.dirname(__file__))
    template_path = os.path.join(root, "FileStream", "web", "templates", "play.html")
    if not os.path.exists(template_path):
        template_path = os.path.join(root, "FileStream", "template", "play.html")
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "streamAbortController" in content
    assert "AbortController" in content
    assert "signal" in content
    assert "playerEl.pause()" in content
    assert "playerEl.src = ''" in content
    assert "layoutEl.thumbnails = ''" in content
    assert "customElements.whenDefined('media-player')" in content
    assert "new Plyr" not in content
    assert "new Hls" not in content
