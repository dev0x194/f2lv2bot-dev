"""Optional Chromium suite: pip install playwright && playwright install chromium."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from web_fixtures import ROOT, environment, ads_config, load_functions

playwright = pytest.importorskip("playwright.async_api")


@pytest.mark.asyncio
async def test_browser_layout_themes_actions_and_ads():
    e = environment()
    ad_requests = []
    errors = []
    policy = load_functions(
        "FileStream/bot/helpers/legal.py", names={"build_policy_page_context"},
        Telegram=type("Settings", (), {"UPDATES_CHANNEL": ""})(),
        build_updates_channel_url=lambda: None,
        build_policy_url=lambda p, **kw: "/" + p,
        POLICY_LAST_UPDATED="2026-04-19")
    e.render["build_policy_page_context"] = policy["build_policy_page_context"]

    async def page_handler(request):
        enabled = request.query.get("ads") == "on"
        e.render["WebAds"].ENABLED = enabled
        e.db.get_web_ads_status.return_value = enabled
        path = request.path
        if path == "/":
            html = await e.render["render_landing_page"](bot=e.bot)
        elif path in {"/legal", "/privacy"}:
            html = await e.render["render_policy_page"](path[1:], bot=e.bot)
        elif path == "/error":
            html = await e.render["render_public_error_page"]("Unavailable", "Ask the sender for a new link.")
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
    for path in ["/legal", "/privacy", "/error"]:
        app.router.add_get(path, page_handler)
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
                body="window.invocationParent=document.currentScript.parentElement.tagName;document.write('<div data-key=\"'+atOptions.key+'\" data-size=\"'+atOptions.width+'x'+atOptions.height+'\">Fixture advertisement</div>');",
            )

        await context.route("https://ads.test/**", ad)
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        base = str(server.make_url("")).rstrip("/")
        output = Path(os.environ.get("WEB_QA_DIR", "/tmp/filestream-web-qa"))
        output.mkdir(exist_ok=True, parents=True)
        for width in [320, 375, 768, 1440]:
            await page.set_viewport_size(dict(width=width, height=900))
            for path in ["/", "/gen/public-file", "/gfolder/folder", "/legal", "/privacy", "/error"]:
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
            frames = page.locator("[data-ad-placement] > iframe")
            keys = []
            sizes = []
            for index in range(2):
                child = await (await frames.nth(index).element_handle()).content_frame()
                keys.append(await child.locator("[data-key]").get_attribute("data-key"))
                sizes.append(await child.locator("[data-key]").get_attribute("data-size"))
                assert await child.evaluate("window.invocationParent") == "BODY"
                assert await child.evaluate("atOptions.format") == "iframe"
                assert await child.evaluate("JSON.stringify(atOptions.params)") == "{}"
                assert await child.evaluate("(() => {try {return !!parent.document.body;} catch (_) {return false;}})()") is False
            assert keys == [top, "shared"]
            assert sizes == ["728x90" if width == 1440 else "320x50", "300x250"]
            await page.wait_for_function('Array.from(document.querySelectorAll("[data-ad-placement]")).every(el => el.dataset.adState === "content-unverified")')
            before = list(ad_requests)
            await page.add_script_tag(url=base + "/static/js/ads.js")
            await page.set_viewport_size(dict(width=390 if width == 1440 else 1400, height=900))
            await page.wait_for_timeout(100)
            assert ad_requests == before
            await page.set_viewport_size(dict(width=width, height=900))
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
            'Array.from(document.querySelectorAll("[data-ad-placement]")).some(el => el.dataset.adState === "error")'
        )
        assert await page.evaluate("window.filestreamAccessAllowed()")
        assert not await page.locator("#adblock-notice").is_visible()
        assert await page.locator("[data-ad-placement] > iframe").count() == 0
        # A successful, empty script is not a filled advertisement.
        await context.route("https://ads.test/**", lambda route: route.fulfill(content_type="application/javascript", body=""))
        await page.clock.install()
        await page.goto(base + "/gen/public-file?ads=on")
        await page.wait_for_timeout(100)
        await page.clock.fast_forward(61000)
        await page.wait_for_function('Array.from(document.querySelectorAll("[data-ad-placement]")).every(el => el.dataset.adState === "empty")')
        assert await page.locator(".ad-placement.loaded").count() == 0
        # A hanging request collapses after the grace period without retrying.
        await context.route("https://ads.test/**", lambda route: None)
        await page.goto(base + "/gen/public-file?ads=on", wait_until="domcontentloaded")
        await page.clock.fast_forward(31000)
        assert await page.locator(".ad-placement.loaded").count() == 0
        assert await page.locator('[data-ad-state="slow-unverified"]').count() == 2
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
        await page.click('[data-action="vlc"]')
        await page.wait_for_function('document.getElementById("external-url").value.includes("/stream/")')
        assert (await page.locator('#launch-vlc').get_attribute('href')).startswith('vlc://http')
        duration = await page.evaluate('videojs("player").duration()')
        if duration > 12:
            await page.evaluate('videojs("player").currentTime(6); videojs("player").pause()')
            await page.reload()
            await page.click('[data-action="play"]')
            await page.wait_for_function('videojs("player").currentTime() >= 6')
        await browser.close()


@pytest.mark.asyncio
async def test_v3_folder_composed_controls_drawer_and_sharing():
    from web_fixtures import mixed_folder_files
    e = environment()
    e.db.get_file.side_effect = mixed_folder_files(e.file)
    html = await e.render['render_folder'](
        'folder', folder_doc=dict(files=list(range(6))),
        public_link=dict(public_id='folder-public'))
    app = web.Application()
    app.router.add_get('/gfolder/folder-public', lambda request: web.Response(text=html, content_type='text/html'))
    app.router.add_static('/static/', ROOT / 'FileStream/web/static')
    async with TestServer(app) as server, playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        page = await browser.new_page(viewport=dict(width=390, height=844))
        await page.add_init_script("""
          Object.defineProperty(navigator, 'share', {value: undefined});
          Object.defineProperty(navigator, 'clipboard', {value:{writeText: async text => {window.copied=text;}}});
        """)
        await page.goto(str(server.make_url('/gfolder/folder-public')))
        assert await page.locator('html').get_attribute('data-theme') == 'dark'
        await page.select_option('#file-sort', 'large')
        assert 'Bundle.zip' in await page.locator('.file-card').first.inner_text()
        await page.fill('#file-search', 'ALPHA')
        assert await page.locator('.file-card:visible').count() == 1
        await page.click('[data-filter="video"]')
        assert await page.locator('#empty-results').is_visible()
        await page.click('#reset-files')
        await page.click('[data-filter="documents"]')
        assert await page.locator('.file-card:visible').count() == 1
        await page.locator('.file-card:visible a').focus()
        await page.keyboard.press('Enter')
        assert await page.locator('#file-details').is_visible()
        assert await page.locator('#detail-name').inner_text() == 'Guide.pdf'
        assert not await page.locator('#detail-play').is_visible()
        assert await page.locator('#detail-download').get_attribute('href') == '/gen/public-file'
        await page.click('#detail-copy')
        assert (await page.evaluate('window.copied')).endswith('/gen/public-file')
        assert 'copied' in await page.locator('#toast').inner_text()
        await page.keyboard.press('Escape')
        assert not await page.locator('#file-details').is_visible()
        assert await page.evaluate('document.activeElement.matches("[data-folder-file]")')
        await page.click('[data-filter="other"]')
        await page.locator('.file-card:visible a').click()
        assert '<img src=x' in await page.locator('#detail-name').inner_text()
        assert await page.locator('#detail-name img').count() == 0
        await page.screenshot(path='/tmp/filestream-web-qa/390-drawer.png', full_page=True)
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.keyboard.press('Escape')
        for key in ['all','video','audio','images','documents','archive','other']:
            await page.click(f'[data-filter="{key}"]')
            assert await page.locator('.file-card:visible').count() == (6 if key == 'all' else 1)
        await page.click('[data-filter="all"]')
        await page.select_option('#file-sort', 'small')
        assert 'Guide.pdf' in await page.locator('.file-card').last.inner_text()
        await page.click('[data-share-url]')
        assert (await page.evaluate('window.copied')).endswith('/gfolder/folder-public')
        await browser.close()
