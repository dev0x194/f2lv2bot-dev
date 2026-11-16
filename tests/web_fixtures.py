"""Offline dependency boundary: execute real source functions with safe adapters.

No bot/config imports, dotenv loading, Telegram clients or Mongo connections.
"""

import ast
import asyncio
import contextlib
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
import traceback
from types import SimpleNamespace
import urllib.parse
from unittest.mock import AsyncMock

import jinja2
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]


def load_functions(relative, names=None, **bindings):
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (names is None or node.name in names)
    ]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(globals(), __file__=str(path), **bindings)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class Missing(Exception):
    pass


def ads_config(enabled=False):
    def slot(key, w, h):
        return dict(
            enabled=enabled,
            key=key,
            width=w,
            height=h,
            invoke_url=f"https://ads.test/{key}.js",
        )

    return dict(
        enabled=enabled,
        desktop=dict(
            top_banner=slot("wide", 728, 90),
            inline_banner=slot("shared", 300, 250),
            social_bar_url="",
        ),
        mobile=dict(
            top_banner=slot("mobile", 320, 50),
            bottom_banner=slot("shared", 300, 250),
            social_bar_url="",
        ),
        smartlink_url="https://ads.test/smart",
        popunder_url="",
    )


def environment(enabled=False):
    token_ns = {}
    exec((ROOT / "FileStream/services/token_service.py").read_text(), token_ns)
    tokens = token_ns["TokenService"]()
    bot = SimpleNamespace(username="FixtureBot", name="Fixture", client_index=0)
    file = dict(
        _id="a" * 24,
        file_name="A very long <script>alert(1)</script> episode 02.mp4",
        file_size=1024,
        mime_type="video/mp4",
        duration=10,
    )
    db = SimpleNamespace(
        resolve_public_file=AsyncMock(
            return_value=(file, dict(public_id="public-file"))
        ),
        get_web_ads_status=AsyncMock(return_value=enabled),
        get_file=AsyncMock(return_value=file),
        ensure_public_link_for_file=AsyncMock(
            return_value=dict(public_id="public-file")
        ),
    )
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "FileStream/web/templates"),
        autoescape=True,
    )
    render = load_functions(
        "FileStream/server/render_template.py",
        env=env,
        db=db,
        token_service=tokens,
        FileStream=bot,
        FileNotFound=Missing,
        Server=SimpleNamespace(
            THUMBNAILS_ENABLED=False, url_for_bot=lambda b: "https://example.test"
        ),
        WebAds=SimpleNamespace(
            ENABLED=enabled,
            template_context=lambda **kw: ads_config(kw["enabled_override"]),
        ),
        get_bot_username=lambda b: b.username,
        get_bot_name=lambda b: b.name,
        build_policy_url=lambda p, **kw: "/" + p,
        build_updates_channel_url=lambda: None,
        build_public_file_url=lambda p, **kw: "/gen/" + p,
        build_public_folder_url=lambda p, **kw: "/gfolder/" + p,
        build_public_bot_open_link=lambda p, b: "https://t.me/FixtureBot?start=open_"
        + p,
        build_start_link=lambda p, b: "https://t.me/FixtureBot?start=" + p,
        build_public_download_token_path=lambda p: "/get-download-token/" + p,
        build_public_stream_token_path=lambda p: "/get-stream-token/" + p,
        resolve_bot_client=lambda **kw: bot,
        humanbytes=lambda n: str(n) + " B",
        detect_category=lambda **kw: "video",
        __version__="test",
        Telegram=SimpleNamespace(bot_media=lambda *a: None),
        build_add_to_group_link=lambda b: "https://t.me/FixtureBot?startgroup=true",
        _TEMPLATE_CACHE={},
    )
    messages = SimpleNamespace(
        SECURE_LINK_EXPIRED="Expired",
        STREAM_EXPIRED="Expired",
        DOWNLOAD_EXPIRED="Expired",
        DOWNLOAD_REQUEST_INVALID="Invalid",
    )
    routes = load_functions(
        "FileStream/server/stream_routes.py",
        names={
            "_extract_page_token",
            "_current_page_session_id",
            "_current_download_session_id",
            "_issue_page_session_cookie",
            "_validate_page_token",
            "gen_handler",
            "gfolder_handler",
            "dl_handler",
            "get_download_token_handler",
            "get_stream_token_handler",
            "file_handler",
            "stream_handler",
            "_assert_access_link",
            "_validate_stream_token",
            "_canonical_mime_type",
            "_parse_range",
            "_sanitize_filename",
            "media_streamer",
        },
        db=db,
        token_service=tokens,
        FileStream=bot,
        FileNotFound=Missing,
        InvalidHash=Missing,
        BadStatusLine=type("BadStatusLine", (Exception,), {}),
        WebMessages=messages,
        PAGE_SESSION_COOKIE="fs_page_session",
        PAGE_SESSION_TTL=1800,
        Server=SimpleNamespace(HAS_SSL=False, THUMBNAILS_ENABLED=False),
        Telegram=SimpleNamespace(MULTI_CLIENT=False),
        resolve_request_bot=lambda host: bot,
        render_public_page=render["render_public_page"],
        render_public_folder=render["render_public_folder"],
        render_public_error_page=render["render_public_error_page"],
        ensure_flog_media_exists=AsyncMock(),
        build_base_url=lambda **kw: "http://127.0.0.1",
        humanbytes=lambda n: str(n),
        MIME_MAP={".mp4": "video/mp4"},
        _OBJECT_ID_RE=re.compile("[a-f0-9]{24}"),
    )
    return SimpleNamespace(
        render=render, routes=routes, db=db, tokens=tokens, file=file, bot=bot
    )


def mixed_folder_files(base):
    """Real renderer inputs covering type, unknown size, and hostile names."""
    return [
        dict(base, file_name='Zebra.mp4', mime_type='video/mp4', file_size=8000),
        dict(base, file_name='Alpha.mp3', mime_type='audio/mpeg', file_size=2000),
        dict(base, file_name='Photo.png', mime_type='image/png', file_size=500),
        dict(base, file_name='Guide.pdf', mime_type='application/pdf', file_size=None),
        dict(base, file_name='Bundle.zip', mime_type='application/zip', file_size=10000),
        dict(base, file_name='<img src=x onerror=alert(1)>.bin', mime_type='', file_size=0),
    ]
