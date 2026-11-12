import os
import json
import jinja2
import pytest
from unittest.mock import patch, MagicMock
from bson import ObjectId
from aiohttp import web
from FileStream.server.render_template import (
    render_landing_page,
    render_policy_page,
    render_public_error_page,
    render_page,
    render_folder,
    _template_context,
)
from FileStream.core.resilience.load_shedding import create_security_middleware
from tests.test_folder_mode import MockDatabase, MockBot


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


@pytest.mark.asyncio
async def test_static_files_exist():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(current_dir)
    static_dir = os.path.join(repo_root, "FileStream", "web", "static")
    if not os.path.isdir(static_dir):
        static_dir = os.path.join(repo_root, "FileStream", "static")

    required_files = [
        "tokens.css",
        "main.css",
        "player.css",
        "app.js",
        "player.js",
    ]
    for filename in required_files:
        path = os.path.join(static_dir, filename)
        assert os.path.isfile(path), f"Missing static file: {filename}"
        assert os.path.getsize(path) > 100, f"Static file {filename} appears empty"


@pytest.mark.asyncio
async def test_render_landing_page(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        html = await render_landing_page(bot=mock_bot)
        assert "<!doctype html>" in html.lower()
        assert "tokens.css" in html
        assert "main.css" in html
        assert "app.js" in html
        assert "File Delivery" in html or "File Sharing" in html
        assert "How It Works" in html or "Workflow" in html
        assert "FAQ" in html


@pytest.mark.asyncio
async def test_render_policy_pages(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        legal = await render_policy_page("legal", bot=mock_bot)
        assert "<!doctype html>" in legal.lower()
        assert "Legal &amp; Terms" in legal or "Legal & Terms" in legal
        assert "tokens.css" in legal

        privacy = await render_policy_page("privacy", bot=mock_bot)
        assert "<!doctype html>" in privacy.lower()
        assert "Privacy Policy" in privacy
        assert "tokens.css" in privacy


@pytest.mark.asyncio
async def test_render_error_page(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        html = await render_public_error_page(
            "File unavailable",
            "This file link has expired.",
            bot=mock_bot,
        )
        assert "<!doctype html>" in html.lower()
        assert "File unavailable" in html
        assert "This file link has expired." in html
        assert "tokens.css" in html


@pytest.mark.asyncio
async def test_render_public_file_page(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        file_id = ObjectId()
        file_data = {
            "_id": file_id,
            "file_name": "sample_video.mp4",
            "file_size": 10485760,
            "mime_type": "video/mp4",
        }
        public_link = {
            "public_id": "pub_sample_123",
            "access_token": "token_abc",
        }
        html = await render_page(
            str(file_id),
            file_data=file_data,
            public_link=public_link,
            session_id="test_session_1",
        )
        assert "<!doctype html>" in html.lower()
        assert "sample video.mp4" in html or "sample_video.mp4" in html
        assert "plyr.css" in html or "player.css" in html
        assert "tokens.css" in html or "tailwindcss" in html
        assert "playBtn" in html or "modeDirectBtn" in html
        assert "player" in html or "Adaptive HLS" in html
        # Ensure adGate blocking dialog was removed
        assert 'id="adGate"' not in html


@pytest.mark.asyncio
async def test_render_download_page(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(current_dir)
        template_file = os.path.join(repo_root, "FileStream", "web", "templates", "dl.html")
        if not os.path.isfile(template_file):
            template_file = os.path.join(repo_root, "FileStream", "template", "dl.html")
        with open(template_file, "r", encoding="utf-8") as f:
            template = jinja2.Environment().from_string(f.read())

        context = await _template_context(
            _bot_client=mock_bot,
            file_name="test_download.zip",
            file_size="15.2 MB",
            file_url="https://domain.com/file/abc",
            category="Archive",
            mime_type="application/zip",
            timestamp="Just now",
        )
        html = template.render(**context)
        assert "<!doctype html>" in html.lower()
        assert "test_download.zip" in html
        assert '<meta name="robots" content="noindex, nofollow, noarchive"' in html
        assert 'id="adGate"' not in html
        assert "downloadBtn" in html


@pytest.mark.asyncio
async def test_render_folder_page(mock_db, mock_bot):
    with patch("FileStream.server.render_template.db", mock_db):
        fid1 = ObjectId()
        fid2 = ObjectId()
        f1 = {
            "_id": fid1,
            "file_name": "movie.mp4",
            "file_size": 50000000,
            "mime_type": "video/mp4",
        }
        f2 = {
            "_id": fid2,
            "file_name": "song.mp3",
            "file_size": 5000000,
            "mime_type": "audio/mp3",
        }
        await mock_db.file.insert_one(f1)
        await mock_db.file.insert_one(f2)

        folder_id = ObjectId()
        folder_doc = {
            "_id": folder_id,
            "title": "My Media Folder",
            "files": [str(fid1), str(fid2)],
        }
        await mock_db.folders.insert_one(folder_doc)
        public_link = {"public_id": "gfolder_123"}

        html = await render_folder(
            str(folder_id),
            title="My Media Folder",
            folder_doc=folder_doc,
            public_link=public_link,
            session_id="session_test",
        )
        assert "<!doctype html>" in html.lower()
        assert "My Media Folder" in html
        assert "movie.mp4" in html
        assert "song.mp3" in html
        assert "tokens.css" in html
        assert 'id="adGate"' not in html


@pytest.mark.asyncio
async def test_folder_xss_protection(mock_db, mock_bot):
    """Verify that file names with script tags cannot break out of JSON script in folder.html."""
    with patch("FileStream.server.render_template.db", mock_db):
        fid1 = ObjectId()
        xss_name = '</script><script>alert("xss")</script>.mp4'
        f1 = {
            "_id": fid1,
            "file_name": xss_name,
            "file_size": 1000,
            "mime_type": "video/mp4",
        }
        await mock_db.file.insert_one(f1)
        folder_id = ObjectId()
        folder_doc = {
            "_id": folder_id,
            "title": "XSS Test Folder",
            "files": [str(fid1)],
        }
        await mock_db.folders.insert_one(folder_doc)
        public_link = {"public_id": "gfolder_xss"}

        html = await render_folder(
            str(folder_id),
            title="XSS Test Folder",
            folder_doc=folder_doc,
            public_link=public_link,
            session_id="session_test",
        )
        # Ensure </script><script> is escaped and cannot terminate the enclosing script tag
        assert "</script><script>alert" not in html
        assert "\\u003c/script\\u003e" in html or "\\u003c" in html or "&lt;" in html


@pytest.mark.asyncio
async def test_security_middleware_headers():
    middleware = create_security_middleware()

    async def dummy_html_handler(request):
        return web.Response(text="<h1>Hello</h1>", content_type="text/html")

    async def dummy_json_handler(request):
        return web.json_response({"status": "ok"})

    req = MagicMock()
    req.path = "/"
    req.remote = "127.0.0.1"

    resp_html = await middleware(req, dummy_html_handler)
    assert resp_html.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp_html.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp_html.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert "camera=()" in resp_html.headers.get("Permissions-Policy", "")
    assert "default-src 'self'" in resp_html.headers.get("Content-Security-Policy", "")

    req.path = "/api/status"
    resp_json = await middleware(req, dummy_json_handler)
    assert resp_json.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp_json.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    # Test blocked suspicious path (early return)
    req.path = "/.env"
    resp_suspicious = await middleware(req, dummy_html_handler)
    assert resp_suspicious.status == 404
    assert resp_suspicious.headers.get("X-Content-Type-Options") == "nosniff"

    # Test path traversal path (blocked by suspicious path or sanitizer)
    req.path = "/dl/../../etc/passwd"
    resp_traversal = await middleware(req, dummy_html_handler)
    assert resp_traversal.status in (400, 404)
    assert resp_traversal.headers.get("X-Content-Type-Options") == "nosniff"
