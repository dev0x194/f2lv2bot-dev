"""Optional Chromium suite: pip install playwright && playwright install chromium."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from web_fixtures import ROOT, environment, ads_config

playwright = pytest.importorskip("playwright.async_api")


@pytest.mark.asyncio
async def test_browser_layout_themes_actions_and_ads():
    e = environment()
    ad_requests = []
    errors = []

    async def page_handler(request):
        enabled = request.query.get("ads") == "on"
        e.render["WebAds"].ENABLED = enabled
        e.db.get_web_ads_status.return_value = enabled
        path = request.path
        if path == "/":
            html = await e.render["render_landing_page"](bot=e.bot)
        elif path.startswith("/gfolder/"):
            e.db.get_file.side_effect = None
            e.db.get_file.return_value = e.file
            html = await e.render["render_folder"](
                "folder",
                title="A folder for the weekend",
                folder_doc=dict(files=["a", "b", "c"]),
                public_link=dict(public_id="folder"),
            )
        else:
            html = await e.render["render_public_page"](
                "public-file", session_id="fixture-session"
            )
        response = web.Response(text=html, content_type="text/html")
        response.set_cookie("fs_page_session", "fixture-session")
        return response

    app = web.Application()
    app.router.add_get("/", page_handler)
    app.router.add_get("/gen/{id}", page_handler)
    app.router.add_get("/gfolder/{id}", page_handler)
    app.router.add_static("/static/", ROOT / "FileStream/web/static")
    async with TestServer(app) as server, playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()

        async def ad(route):
            ad_requests.append(route.request.url)
            await route.fulfill(
                content_type="application/javascript",
                body="document.write('Fixture advertisement');",
            )

        await context.route("https://ads.test/**", ad)
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        base = str(server.make_url("")).rstrip("/")
        output = Path(os.environ.get("WEB_QA_DIR", "/tmp/filestream-web-qa"))
        output.mkdir(exist_ok=True, parents=True)
        for width in [320, 375, 768, 1440]:
            await page.set_viewport_size(dict(width=width, height=900))
            for path in ["/", "/gen/public-file", "/gfolder/folder"]:
                await page.goto(base + path)
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), (width, path)
                assert await page.locator("#theme").count() == 1
                await page.screenshot(
                    path=str(output / f'{width}-{path.split("/")[1] or "landing"}.png'),
                    full_page=True,
                )
        for script in (ROOT / "FileStream/web/static/js").glob("*.js"):
            await page.evaluate("(source)=>{new Function(source);}", script.read_text())
        await page.goto(base + "/")
        await page.select_option("#theme", "dark")
        await page.goto(base + "/gen/public-file")
        assert await page.locator("html").get_attribute("data-theme") == "dark"
        await page.select_option("#theme", "system")
        await page.emulate_media(color_scheme="light")
        assert await page.locator("html").get_attribute("data-theme") == "light"
        await page.emulate_media(color_scheme="dark")
        await page.wait_for_function(
            "document.documentElement.dataset.theme === 'dark'"
        )
        assert (
            await page.evaluate('localStorage.getItem("filestream-theme")') == "system"
        )
        # Keyboard skip link and theme selector are reachable.
        await page.reload()
        await page.keyboard.press("Tab")
        assert (
            await page.evaluate("document.activeElement.textContent")
            == "Skip to content"
        )
        for width, top in [(1440, "wide"), (375, "mobile"), (320, "mobile")]:
            await page.set_viewport_size(dict(width=width, height=900))
            ad_requests.clear()
            await page.goto(base + "/gen/public-file?ads=on")
            await page.wait_for_function(
                'document.querySelectorAll(".ad-placement iframe").length === 2'
            )
            await page.wait_for_timeout(300)
            assert sorted(ad_requests) == sorted(
                [f"https://ads.test/{top}.js", "https://ads.test/shared.js"]
            )
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            # Positive bait hiding gates optional actions only; an override remains available.
            await page.add_style_tag(content=".adsbox {display:none !important}")
            (
                await page.click("#adblock-retry", force=True)
                if await page.locator("#adblock-retry").is_visible()
                else await page.evaluate(
                    'document.getElementById("adblock-retry").click()'
                )
            )
            await page.wait_for_timeout(250)
            assert await page.locator("#adblock-notice").is_visible()
            await page.click('[data-action="download"]')
            assert "notice" in await page.locator("#action-status").inner_text()
            assert await page.locator(".safety a").count() == 2
            await page.click("#adblock-continue")
            assert await page.evaluate("window.filestreamAccessAllowed()")
            await page.screenshot(path=str(output / f"{width}-ads.png"), full_page=True)
        for path in ["/", "/gfolder/folder"]:
            await page.goto(base + path + "?ads=on")
            await page.wait_for_function(
                'document.querySelectorAll(".ad-placement iframe").length === 2'
            )
            assert await page.locator("#adblock-notice").count() == (
                0 if path == "/" else 1
            )

        async def fail_provider(route):
            await route.abort()

        await context.route("https://ads.test/**", fail_provider)
        await page.goto(base + "/gen/public-file?ads=on")
        await page.wait_for_function(
            'Array.from(document.querySelectorAll(".ad-status")).some(el => !el.hidden)'
        )
        assert await page.evaluate("window.filestreamAccessAllowed()")
        assert not await page.locator("#adblock-notice").is_visible()
        ad_requests.clear()
        await page.goto(base + "/gen/public-file")
        assert not ad_requests and await page.locator(".ad-placement").count() == 0
        # Network errors must produce an actionable state, never a success message.
        await page.route(
            "**/get-download-token/**",
            lambda route: route.fulfill(status=403, body="Expired"),
        )
        await page.click('[data-action="download"]')
        await page.wait_for_function(
            'document.getElementById("action-status").textContent.includes("expired")'
        )
        assert await page.locator("#retry-action").is_visible()
        assert not errors, errors
        await browser.close()


@pytest.mark.asyncio
async def test_videojs_mp4_playback_seek_and_expired_url_refresh():
    fixture = os.environ.get("WEB_MEDIA_FIXTURE")
    if not fixture or not Path(fixture).is_file():
        pytest.skip("Set WEB_MEDIA_FIXTURE to a local MP4 fixture")
    e = environment()
    data = Path(fixture).read_bytes()
    e.file.update(file_name="Test trailer.mp4", file_size=len(data))
    e.routes.update(
        Server=type(
            "Settings",
            (),
            dict(
                HAS_SSL=False,
                THUMBNAILS_ENABLED=False,
                STREAM_CHUNK_SIZE_MB=1,
                MP4_FAST_SCRUB_ENABLED=False,
            ),
        )(),
        work_loads={0: 0},
        multi_clients={0: "fixture"},
        choose_best_client=lambda x: 0,
        _eligible_client_indexes=lambda f: [0],
        TELEGRAM_GETFILE_LIMIT=1048576,
        utils=type("Utils", (), {"get_name": staticmethod(lambda f: "trailer.mp4")})(),
    )

    class Transport:
        async def get_file_properties(self, *args):
            return type("Media", (), dict(file_size=len(data), mime_type="video/mp4"))()

        async def yield_file(self, *args):
            _, _, _, offset, first, last, count, size = args
            yield data[offset + first : offset + (count - 1) * size + last]

    e.routes["class_cache"] = {"fixture": Transport()}
    e.db.resolve_file_reference = AsyncMock(return_value=(e.file, {}))
    app = web.Application()
    for path, name in [
        ("/gen/{public_id}", "gen_handler"),
        ("/get-stream-token/{path}", "get_stream_token_handler"),
        ("/stream/{token}", "stream_handler"),
    ]:
        app.router.add_get(path, e.routes[name])
    app.router.add_static("/static/", ROOT / "FileStream/web/static")
    async with TestServer(app) as server, playwright.async_playwright() as pw:
        e.routes["build_base_url"] = lambda **kw: str(server.make_url("")).rstrip("/")
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"]
        )
        page = await browser.new_page()
        await page.goto(server.make_url("/gen/public-file").human_repr())
        await page.click('[data-action="play"]')
        await page.wait_for_function(
            'window.videojs && videojs("player").currentTime() > 0.1', timeout=20000
        )
        await page.evaluate('videojs("player").currentTime(10)')
        await page.wait_for_function('videojs("player").currentTime() >= 10')
        assert await page.evaluate('videojs("player").playbackRates()') == [
            0.5,
            1,
            1.25,
            1.5,
            2,
        ]
        await page.evaluate('videojs("player").trigger("error")')
        await page.wait_for_timeout(1200)
        assert len(e.tokens._access_tokens) >= 3
        await browser.close()
