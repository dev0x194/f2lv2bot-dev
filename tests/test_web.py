import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from web_fixtures import ROOT, Missing, environment, load_functions, ads_config


@pytest.mark.asyncio
async def test_real_render_autoescape_and_file_isolation():
    e = environment()
    html = await e.render["render_public_page"]("public-file", session_id="session")
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    assert html.count('data-action="download"') == 1
    assert "video.min.js" in html
    assert (
        "ad-config" not in html
        and "adblock.js" not in html
        and "data-ad-placement" not in html
    )
    assert "report_file_public-file" in html
    assert (
        "filestream-theme" in (ROOT / "FileStream/web/static/js/theme.js").read_text()
    )


@pytest.mark.asyncio
async def test_folder_order_empty_and_public_links():
    e = environment()
    files = [
        dict(e.file, _id=str(i), file_name=name)
        for i, name in enumerate(["Episode 10.mp4", "Episode 02.mp4", "Episode 01.mp4"])
    ]
    e.db.get_file.side_effect = files
    e.db.ensure_public_link_for_file.side_effect = [
        dict(public_id="public-" + str(i)) for i in range(3)
    ]
    html = await e.render["render_folder"](
        "folder",
        folder_doc=dict(_id="folder", files=["0", "1", "2"]),
        public_link=dict(public_id="folder-public"),
    )
    assert (
        html.index("Episode 10") < html.index("Episode 02") < html.index("Episode 01")
    )
    assert all(f"/gen/public-{i}" in html for i in range(3))
    assert "01." in html and "03." in html and "<video" not in html
    assert not e.tokens._access_tokens
    html = await e.render["render_folder"](
        "folder",
        folder_doc=dict(_id="folder", files=[]),
        public_link=dict(public_id="folder-public"),
    )
    assert "no available files" in html


@pytest.mark.asyncio
async def test_ads_global_and_bot_setting():
    e = environment(True)
    html = await e.render["render_public_page"]("public-file", session_id="session")
    assert "ad-config" in html and "adblock.js" in html
    original_context = e.render["WebAds"].template_context
    e.render["WebAds"].template_context = lambda **kw: {**ads_config(kw["enabled_override"]), "adblock_enabled": False}
    html = await e.render["render_public_page"]("public-file", session_id="session")
    assert "ads.js" in html and "adblock.js" not in html and 'id="adblock-notice"' not in html
    e.render["WebAds"].template_context = original_context
    e.db.get_web_ads_status.return_value = False
    html = await e.render["render_public_page"]("public-file", session_id="session")
    assert "ad-config" not in html and "adblock" not in html
    e.db.get_web_ads_status.return_value = True
    e.render["WebAds"].ENABLED = False
    assert not (await e.render["_template_context"]())["ad_config"]["enabled"]
    assert e.db.get_web_ads_status.call_args.kwargs["bot"] is e.bot


@pytest.mark.asyncio
async def test_page_session_issuance_revocation_and_external_stream():
    e = environment()

    async def transport(request, path, **kwargs):
        return web.Response(text="fixture media")

    e.routes["media_streamer"] = transport
    e.routes["build_base_url"] = lambda **kw: str(client.make_url("")).rstrip("/")
    app = web.Application()
    for path, name in [
        ("/gen/{public_id}", "gen_handler"),
        ("/get-stream-token/{path}", "get_stream_token_handler"),
        ("/get-download-token/{path}", "get_download_token_handler"),
        ("/stream/{token}", "stream_handler"),
        ("/file/{token}", "file_handler"),
        ("/dl/{path}", "dl_handler"),
    ]:
        app.router.add_get(path, e.routes[name])
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/dl/" + "a" * 24)).status == 403
        assert (await client.get("/get-stream-token/public-file")).status == 403
        page = await client.get("/gen/public-file")
        assert page.status == 200
        html = await page.text()
        import re

        cfg = json.loads(
            re.search(r'id="file-config">(.*?)</script>', html, re.S).group(1)
        )
        headers = {"X-Page-Token": cfg["pageToken"]}
        bad = await client.get("/get-stream-token/another", headers=headers)
        assert bad.status == 403
        response = await client.get("/get-stream-token/public-file", headers=headers)
        assert response.status == 200
        data = await response.json()
        assert "/dl/" not in json.dumps(data) and data["success"] is True
        download = await (
            await client.get("/get-download-token/public-file", headers=headers)
        ).json()
        assert "/file/" in download["download_url"]
        assert (await client.session.get(download["download_url"])).status == 200
        # VLC is a purpose-limited bearer stream, usable without the page cookie.
        client.session.cookie_jar.clear()
        assert (await client.session.get(data["stream_url"])).status == 200
        assert (
            await client.get("/get-stream-token/public-file", headers=headers)
        ).status == 403
        e.db.resolve_public_file.side_effect = Missing("revoked")
        assert (await client.session.get(data["stream_url"])).status == 403
        assert (await client.session.get(download["download_url"])).status == 403


@pytest.mark.asyncio
async def test_public_resolution_rechecks_expiry_even_when_cached():
    tree = ast.parse((ROOT / "FileStream/database/database.py").read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Database"
    )
    methods = [
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name in {"resolve_public_file", "_assert_public_link_active"}
    ]
    namespace = dict(
        FileNotFound=Missing,
        time=__import__("time"),
        get_cached_file_reference=lambda p: ({"_id": "old"}, {}),
        cache_file_reference=lambda *a: None,
    )
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(
                    body=[
                        ast.ClassDef(
                            name="FixtureDB",
                            bases=[],
                            keywords=[],
                            body=methods,
                            decorator_list=[],
                        )
                    ],
                    type_ignores=[],
                )
            ),
            "database.py",
            "exec",
        ),
        namespace,
    )
    db = namespace["FixtureDB"]()
    db.get_public_link = AsyncMock(
        return_value=dict(type="file", target_id="file", expires_at=0)
    )
    db.get_file = AsyncMock(return_value=dict(_id="file"))
    with pytest.raises(Missing):
        await db.resolve_public_file("public")
    db.get_public_link.return_value = dict(type="file", target_id="file", revoked=True)
    with pytest.raises(Missing):
        await db.resolve_public_file("public")
    db.get_file.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "range_value,status,expected",
    [
        (None, 200, b"0123456789"),
        ("bytes=2-5", 206, b"2345"),
        ("bytes=-3", 206, b"789"),
        ("bytes=30-", 416, None),
        ("bytes=5-2", 416, None),
    ],
)
async def test_real_media_range_transport(range_value, status, expected):
    e = environment()
    e.db.resolve_file_reference = AsyncMock(return_value=(e.file, {}))
    payload = b"0123456789"

    class ByteStreamer:
        async def get_file_properties(self, *args):
            return SimpleNamespace(file_size=10, mime_type="video/mp4")

        async def yield_file(self, *args):
            # The actual streamer computes cuts; substitute Telegram transport only.
            _, _, _, offset, first, last, count, size = args
            yield payload[offset + first : offset + (count - 1) * size + last]

    client_id = object()
    e.routes.update(
        work_loads={0: 0},
        multi_clients={0: client_id},
        class_cache={client_id: ByteStreamer()},
        choose_best_client=lambda x: 0,
        _eligible_client_indexes=lambda f: [0],
        TELEGRAM_GETFILE_LIMIT=1048576,
        Server=SimpleNamespace(
            STREAM_CHUNK_SIZE_MB=1,
            MP4_FAST_SCRUB_ENABLED=False,
            THUMBNAILS_ENABLED=False,
        ),
        utils=SimpleNamespace(get_name=lambda f: "sample.mp4"),
    )

    async def handler(request):
        return await e.routes["media_streamer"](request, "fixture")

    app = web.Application()
    app.router.add_get("/media", handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/media", headers={"Range": range_value} if range_value else {}
        )
        assert response.status == status
        if expected is not None:
            assert await response.read() == expected
        if status == 206:
            assert response.headers["Content-Range"].endswith("/10")
        if status == 416:
            assert response.headers["Content-Range"] == "bytes */10"


def test_all_templates_compile_and_python_syntax():
    e = environment()
    for name in e.render["env"].list_templates():
        e.render["env"].get_template(name)
    for name in [
        "FileStream/server/render_template.py",
        "FileStream/server/stream_routes.py",
        "FileStream/server/__init__.py",
        "FileStream/services/token_service.py",
        "FileStream/database/database.py",
        "FileStream/bot/handlers/system/nsfw_report.py",
        "FileStream/__main__.py",
    ]:
        compile((ROOT / name).read_text(), name, "exec")


def test_ad_configuration_global_switch_and_independent_slots():
    tree = ast.parse((ROOT / "FileStream/config.py").read_text())
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WebAds"
    )
    ns = dict(
        env={},
        _BOT_WEB_AD_OVERRIDES={},
        _bool_env=lambda key, default=False: default,
        _int_env=lambda key, default=0: default,
        _str_env=lambda key, default="": default,
    )
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "config.py", "exec"), ns)
    ads = ns["WebAds"]
    bot = SimpleNamespace(client_index=2)
    ads.BOT_OVERRIDES = {
        2: {"WEB_ADS_MOBILE_BOTTOM_BANNER_KEY": "other", "WEB_ADS_ENABLED": "true"}
    }
    config = ads.template_context(enabled_override=True, bot=bot)
    assert config["mobile"]["bottom_banner"]["key"] == "other"
    assert config["desktop"]["inline_banner"]["key"] != "other"
    assert not config["desktop"]["social_bar_url"]
    assert not config["mobile"]["social_bar_url"]
    for device, name, dimensions in [("desktop", "top_banner", (728,90)), ("desktop", "inline_banner", (300,250)), ("mobile", "top_banner", (320,50)), ("mobile", "bottom_banner", (300,250))]:
        slot = config[device][name]
        assert (slot["width"], slot["height"]) == dimensions
        assert slot["enabled"]
    for url in ["javascript:alert(1)", "http://ads.test/script", "https://user:pass@ads.test/script", ""]:
        assert not ads._banner_slot("mobile", "key", 320, 50, url)["enabled"]
    assert not ads._banner_slot("mobile", "", 320, 50, "https://ads.test/script")["enabled"]
    ads.BOT_OVERRIDES[2]["WEB_ADS_ENABLED"] = "false"
    assert not ads.template_context(enabled_override=True, bot=bot)["enabled"]
    ads.ENABLED = False
    assert not ads.template_context(enabled_override=True, bot=bot)["enabled"]


@pytest.mark.asyncio
async def test_privacy_headers_preserve_existing_csp():
    ns = load_functions(
        "FileStream/server/__init__.py", names={"web_privacy_middleware"}
    )
    middleware = web.middleware(ns["web_privacy_middleware"])

    async def handler(request):
        return web.Response(
            text="fixture", headers={"Content-Security-Policy": "default-src 'self';"}
        )

    app = web.Application(middlewares=[middleware])
    app.router.add_get("/page", handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/page")
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        assert (await client.get("/missing")).status == 404


@pytest.mark.asyncio
async def test_secondary_landing_and_policy_pages_render():
    e = environment(True)
    secondary = SimpleNamespace(
        username="SecondaryBot", name="Secondary", client_index=2
    )
    page = await e.render["render_landing_page"](bot=secondary)
    assert "SecondaryBot" in page and "application/ld+json" in page
    assert e.db.get_web_ads_status.call_args.kwargs["bot"] is secondary
    policy = load_functions(
        "FileStream/bot/helpers/legal.py",
        names={"build_policy_page_context"},
        Telegram=SimpleNamespace(UPDATES_CHANNEL=""),
        build_updates_channel_url=lambda: None,
        build_policy_url=lambda p, **kw: "/" + p,
        POLICY_LAST_UPDATED="2026-04-19",
    )
    e.render["build_policy_page_context"] = policy["build_policy_page_context"]
    for name in ["legal", "privacy"]:
        page = await e.render["render_policy_page"](name, bot=secondary)
        assert "What We Store" in page and "ad-config" not in page
    page = await e.render["render_public_error_page"](
        "Expired", "<unsafe>", bot=secondary
    )
    assert "&lt;unsafe&gt;" in page and "Go to home" in page


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_report_uses_canonical_ids_for_scanning(kind):
    import datetime, html

    pending = []
    canonical = dict(_id="a" * 24, file_name="fixture.mp4", title="Fixture", user_id=1)
    db = SimpleNamespace(
        get_file=AsyncMock(side_effect=Missing()),
        get_folder=AsyncMock(side_effect=Missing()),
        resolve_public_file=AsyncMock(return_value=(canonical, {})),
        resolve_public_folder=AsyncMock(return_value=(canonical, {})),
        add_nsfw_report=AsyncMock(),
        update_nsfw_report=AsyncMock(),
    )
    scanner = AsyncMock(return_value=(False, "clean"))
    report = SimpleNamespace(id=10, edit_text=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=report))
    message = SimpleNamespace(from_user=SimpleNamespace(id=2), reply_text=AsyncMock())
    safety = SimpleNamespace(
        channel_report_text=lambda **kw: "Reviewing...",
        REPORT_RECEIVED="received",
        REPORT_CLEAN="clean",
    )
    ns = load_functions(
        "FileStream/bot/handlers/system/nsfw_report.py",
        names={"process_report"},
        Client=object,
        Message=object,
        Telegram=SimpleNamespace(NUDENET_CHANNEL=-1),
        db=db,
        _report_id=lambda: "report-fixture",
        datetime=datetime.datetime,
        html=html,
        asyncio=SimpleNamespace(create_task=lambda c: pending.append(c)),
        SafetyMessages=safety,
        ParseMode=SimpleNamespace(HTML="html"),
        _scan_file_by_id=scanner,
        _scan_folder_by_id=scanner,
    )
    await ns["process_report"](bot, message, kind, "public-ref")
    assert db.add_nsfw_report.call_args.args[0]["target_id"] == canonical["_id"]
    for task in pending:
        await task
    scanner.assert_awaited_once_with(bot, canonical["_id"])


@pytest.mark.asyncio
async def test_legacy_redirects_never_reactivate_links():
    db = SimpleNamespace(
        get_public_link=AsyncMock(side_effect=Missing("Public link not found")),
        get_active_public_link_for_target=AsyncMock(return_value=None),
        resolve_public_file=AsyncMock(
            return_value=({"_id": "file"}, {"public_id": "public"})
        ),
    )
    ns = load_functions(
        "FileStream/server/stream_routes.py",
        names={"_resolve_active_legacy_reference"},
        db=db,
        FileNotFound=Missing,
    )
    with pytest.raises(Missing):
        await ns["_resolve_active_legacy_reference"]("internal", "file")
    db.get_active_public_link_for_target.return_value = {"public_id": "public"}
    result = await ns["_resolve_active_legacy_reference"]("internal", "file")
    assert result[1]["public_id"] == "public"
    db.get_public_link.side_effect = None
    db.get_public_link.return_value = {"public_id": "revoked"}
    db.resolve_public_file.side_effect = Missing("revoked")
    db.get_active_public_link_for_target.reset_mock()
    with pytest.raises(Missing):
        await ns["_resolve_active_legacy_reference"]("revoked", "file")
    db.get_active_public_link_for_target.assert_not_called()


@pytest.mark.asyncio
async def test_v3_folder_metadata_and_safe_error_states():
    from web_fixtures import mixed_folder_files
    import re

    e = environment()
    files = mixed_folder_files(e.file)
    e.db.get_file.side_effect = files
    html = await e.render['render_folder'](
        'folder', folder_doc=dict(files=list(range(len(files)))),
        public_link=dict(public_id='folder-public'))
    config = json.loads(re.search(r'id="folder-config">(.*?)</script>', html, re.S).group(1))
    assert [f['size_bytes'] for f in config] == [8000, 2000, 500, None, 10000, 0]
    assert '<img src=x' not in html
    assert 'a' * 24 not in html
    assert 'pageToken' not in html
    for reason, expected in [('This link has expired', 'Link expired'),
                             ('This link has been revoked', 'Link revoked'),
                             ('Public link not found', 'Link not found'),
                             ('secret database error', 'Unavailable')]:
        page = await e.render['render_public_error_page']('Unavailable', 'Ask the sender.', reason=reason)
        assert expected in page
        assert 'secret database error' not in page
