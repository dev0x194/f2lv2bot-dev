"""Optional Chromium suite: pip install playwright && playwright install chromium."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from web_fixtures import ROOT, environment, ads_config, load_functions, configure_ad_documents, route_ad_documents

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
        await route_ad_documents(context, ads_config(True))
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
            # Ordinary resizing within a breakpoint makes no provider requests.
            await page.set_viewport_size(dict(width=width + 10, height=900))
            await page.wait_for_timeout(100)
            assert ad_requests == before
            # Crossing the breakpoint selects the right unit once, keeping the
            # identical 300x250 document and retaining the old top document hidden.
            await page.set_viewport_size(dict(width=390 if width == 1440 else 1400, height=900))
            other = "mobile" if width == 1440 else "wide"
            await page.wait_for_function("expected => document.querySelector('[data-ad-placement=top]').dataset.adDevice === expected", arg="mobile" if width == 1440 else "desktop")
            top_frame = page.locator('[data-ad-placement="top"] > iframe:visible')
            await top_frame.wait_for()
            child = await (await top_frame.element_handle()).content_frame()
            assert await child.locator("[data-key]").get_attribute("data-key") == other
            assert sorted(ad_requests) == sorted(before + [f"https://ads.test/{other}.js"])
            assert await page.locator('[data-ad-placement] > iframe:visible').count() == 2
            switched = list(ad_requests)
            await page.set_viewport_size(dict(width=width, height=900))
            await page.wait_for_function("expected => document.querySelector('[data-ad-placement=top]').dataset.adDevice === expected", arg="desktop" if width == 1440 else "mobile")
            child = await (await top_frame.element_handle()).content_frame()
            assert await child.locator("[data-key]").get_attribute("data-key") == top
            assert ad_requests == switched
            assert await page.locator('[data-ad-placement] > iframe:visible').count() == 2
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
        await page.wait_for_timeout(100)
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


@pytest.mark.asyncio
async def test_ads_independent_units_across_breakpoints_and_inactive_failure():
    """A hidden unit's delayed failure must not hide the current device's ad."""
    config = ads_config(True)
    config['mobile']['bottom_banner'].update(
        key='mobile-bottom', invoke_url='https://ads.test/mobile-bottom.js')
    configure_ad_documents(config)
    requests = []
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        page = await browser.new_page(viewport=dict(width=1440, height=900))

        async def provider(route):
            requests.append(route.request.url)
            await route.fulfill(content_type='application/javascript', body="document.write('<p>'+atOptions.key+'</p>')")

        await page.route('https://ads.test/**', provider)
        await route_ad_documents(page, config)
        content = (
            '<style>[hidden]{display:none!important}.ad-placement{display:none}.ad-placement.loaded{display:block}</style>'
            '<aside class="ad-placement" data-ad-placement="top"></aside>'
            '<aside class="ad-placement" data-ad-placement="inline"></aside>'
            '<script id="ad-config" type="application/json">' + json.dumps(config) + '</script>')
        await page.route('https://app.test/', lambda r: r.fulfill(content_type='text/html', body=content))
        await page.goto('https://app.test/')
        await page.add_script_tag(path=str(ROOT / 'FileStream/web/static/js/ads.js'))
        await page.wait_for_function('document.querySelectorAll("[data-ad-state=content-unverified]").length === 2')
        assert sorted(requests) == ['https://ads.test/shared.js', 'https://ads.test/wide.js']
        await page.set_viewport_size(dict(width=375, height=900))
        await page.wait_for_function('document.querySelectorAll("[data-ad-device=mobile][data-ad-state=content-unverified]").length === 2')
        assert sorted(requests) == sorted('https://ads.test/' + name + '.js' for name in ['wide', 'shared', 'mobile', 'mobile-bottom'])
        top = page.locator('[data-ad-placement=top] > iframe:visible')
        mobile_frame = await (await top.element_handle()).content_frame()
        assert await mobile_frame.evaluate('atOptions.width') == 320
        assert await mobile_frame.evaluate('atOptions.height') == 50
        bottom = await (await page.locator('[data-ad-placement=inline] > iframe:visible').element_handle()).content_frame()
        assert await bottom.evaluate('atOptions.key') == 'mobile-bottom'
        await page.set_viewport_size(dict(width=1440, height=900))
        await page.wait_for_function('document.querySelectorAll("[data-ad-device=desktop][data-ad-state=content-unverified]").length === 2')
        await mobile_frame.evaluate("parent.postMessage({type:'filestream-ad',state:'error'},'*')")
        await page.wait_for_timeout(100)
        assert await page.locator('.ad-placement.loaded > iframe:visible').count() == 2
        desktop_frame = await (await top.element_handle()).content_frame()
        assert await desktop_frame.evaluate('atOptions.key') == 'wide'
        assert await desktop_frame.evaluate('atOptions.width') == 728
        assert len(requests) == 4
        await browser.close()


@pytest.mark.asyncio
async def test_smartlink_explicit_navigation_mobile_and_desktop():
    from web_fixtures import web_ads_settings
    e = environment(True)
    destination = 'https://ads.test/smart?campaign=public&placement=optional'
    navigation_requests = []

    async def render(request):
        mode = request.query.get('mode', 'on')
        values = {'WEB_ADS_ENABLED': 'False' if mode == 'global-off' else 'True',
                  'WEB_ADS_SMARTLINK_URL': '' if mode == 'missing' else 'javascript:alert(1)' if mode == 'unsafe' else destination}
        for prefix in ['DESKTOP_TOP', 'DESKTOP_INLINE', 'MOBILE_TOP', 'MOBILE_BOTTOM']:
            values['WEB_ADS_' + prefix + '_BANNER_INVOKE_URL'] = 'https://ads.test/banner/' + prefix + '.js'
        e.render['WebAds'] = web_ads_settings(values)
        e.db.get_web_ads_status.return_value = mode != 'admin-off'
        return web.Response(text=await e.render['render_public_page']('public-file', session_id='private-session'), content_type='text/html')

    app = web.Application()
    app.router.add_get('/gen/public-file', render)
    app.router.add_static('/static/', ROOT / 'FileStream/web/static')
    async with TestServer(app) as server, playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        for mobile in [True, False]:
            context = await browser.new_context(**(pw.devices['Pixel 7'] if mobile else dict(viewport=dict(width=1440, height=900))))
            async def provider(route):
                if route.request.url == destination:
                    navigation_requests.append(route.request)
                    await route.fulfill(content_type='text/html', body='<p>Mock destination reached</p>')
                else:
                    await route.fulfill(content_type='application/javascript', body="document.write('<div>Fixture</div>');")
            await context.route('https://ads.test/**', provider)
            page = await context.new_page()
            base = str(server.make_url('/gen/public-file'))
            for mode in ['missing', 'unsafe', 'admin-off', 'global-off']:
                await page.goto(base + '?mode=' + mode)
                assert await page.locator('[data-sponsored-link]').count() == 0
            await page.goto(base)
            link = page.get_by_role('link', name='Sponsored link')
            assert await link.get_attribute('href') == destination
            assert await link.get_attribute('target') is None
            assert await link.get_attribute('referrerpolicy') == 'no-referrer'
            assert set((await link.get_attribute('rel')).split()) >= {'sponsored', 'noreferrer', 'noopener'}
            await page.wait_for_timeout(200)
            assert len(navigation_requests) == int(not mobile)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert await page.locator('[data-action="download"]').count() == 1
            assert await page.locator('[data-action="play"]').count() == 1
            assert await page.locator('[data-action="vlc"]').count() == 1
            # A download error remains an independent file action, with no ad navigation.
            await page.route('**/get-download-token/**', lambda r: r.fulfill(status=403, body='Expired'))
            await page.click('[data-action="download"]')
            await page.wait_for_function("document.querySelector('#action-status').textContent.includes('expired')")
            assert len(navigation_requests) == int(not mobile)
            assert page.url == base
            await page.screenshot(path=f'/tmp/filestream-web-qa/smartlink-{mobile}.png', full_page=True)
            await link.focus()
            await page.keyboard.press('Enter')
            await page.wait_for_url(destination)
            assert len(context.pages) == 1
            request = navigation_requests[-1]
            assert not request.headers.get('referer')
            assert request.url == destination
            assert 'private-session' not in request.url
            await context.close()
        assert len(navigation_requests) == 2
        await browser.close()


@pytest.mark.asyncio
async def test_ads_delayed_creative_survives_empty_cleanup():
    """No-fill is provisional: a loaded provider may insert its creative later."""
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        page = await browser.new_page()
        await page.clock.install()
        await page.route('https://ads.test/**', lambda route: route.fulfill(
            content_type='application/javascript',
            body="setTimeout(()=>{const frame=document.createElement('iframe');frame.srcdoc='<p>Delayed creative fixture</p>';document.body.append(frame)},65000)"))
        config = ads_config(True)
        await route_ad_documents(page, config)
        content = ('<style>[hidden]{display:none!important}.ad-placement{display:none}.ad-placement.loaded{display:block}</style>'
            '<aside class="ad-placement" data-ad-placement="top"></aside>'
            '<script id="ad-config" type="application/json">' + json.dumps(config) + '</script>')
        await page.route('https://app.test/', lambda r: r.fulfill(content_type='text/html', body=content))
        await page.goto('https://app.test/')
        await page.add_script_tag(path=str(ROOT / 'FileStream/web/static/js/ads.js'))
        await page.wait_for_function("document.querySelector('iframe')?.contentWindow != null")
        # Ensure the provider has executed before advancing both documents' clocks.
        child = await (await page.locator('aside > iframe').element_handle()).content_frame()
        await child.wait_for_load_state()
        await page.clock.fast_forward(61000)
        await page.wait_for_function("document.querySelector('aside').dataset.adState === 'empty'")
        assert not await page.locator('aside').is_visible()
        await page.clock.fast_forward(5000)
        await page.wait_for_function("document.querySelector('aside').dataset.adState === 'content-unverified'", timeout=2000)
        assert await page.locator('aside > iframe').is_visible()
        assert await child.locator('iframe').count() == 1
        await browser.close()


@pytest.mark.asyncio
async def test_cross_origin_banners_referrers_credentials_and_message_authentication():
    """Exercise real V3 rendering and the real exported HTML on sibling HTTPS origins."""
    from FileStream.web.ad_documents import render_document
    e = environment(True)
    config = ads_config(True)
    for device in ('desktop', 'mobile'):
        for slot in config[device].values():
            if isinstance(slot, dict):
                slot['document_url'] = slot['document_url'].replace('ad-documents.test', 'ads.tdrp.test')
    e.render['WebAds'].template_context = lambda **kw: config
    html = await e.render['render_public_page']('public-file', session_id='private-session-marker')
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        context = await browser.new_context(viewport=dict(width=375, height=900))
        await context.add_cookies([dict(name='__Host-fs_page_session', value='private-cookie-marker',
                                       url='https://tdrp.test/', secure=True, httpOnly=True)])
        seen = []
        async def route(handler):
            req = handler.request
            headers = await req.all_headers()
            if req.url.startswith('https://ads.tdrp.test/'):
                seen.append((req.url, headers))
                assert 'private-cookie-marker' not in headers.get('cookie', '')
                slot = next(s for d in ('desktop','mobile') for s in config[d].values()
                            if isinstance(s,dict) and s['document_url']==req.url)
                await handler.fulfill(content_type='text/html', body=render_document(slot,['https://tdrp.test']),
                                      headers={'Referrer-Policy':'strict-origin-when-cross-origin',
                                               'Content-Security-Policy':"frame-ancestors https://tdrp.test"})
            elif req.url.startswith('https://ads.test/'):
                seen.append((req.url, headers))
                await handler.fulfill(content_type='application/javascript', body="""
                    if(document.readyState!=='loading'||!atOptions.key)throw Error('Order');
                    window.privateAccess=false;try{window.privateAccess=!!parent.document.body}catch(e){}
                    document.cookie='__Host-fs_page_session=attacker; Domain=tdrp.test; Path=/; Secure';
                    document.write('<iframe srcdoc="<p>Creative fixture</p>"></iframe>');
                """)
            elif '/static/' in req.url:
                path = req.url.split('/static/',1)[1]
                await handler.fulfill(path=str(ROOT/'FileStream/web/static'/path))
            else:
                await handler.fulfill(content_type='text/html', body=html,
                                      headers={'Referrer-Policy':'no-referrer'})
        await context.route('**/*', route)
        page = await context.new_page()
        await page.goto('https://tdrp.test/gen/public-file?private=parent-url-marker')
        await page.wait_for_function("document.querySelectorAll('[data-ad-state=content-unverified]').length===2")
        frames = page.locator('[data-ad-placement] > iframe')
        for i in range(2):
            frame = frames.nth(i)
            assert await frame.get_attribute('sandbox') is None
            assert await frame.get_attribute('srcdoc') is None
            assert await frame.get_attribute('referrerpolicy') == 'origin'
            child = await (await frame.element_handle()).content_frame()
            assert await child.evaluate('document.referrer') == 'https://tdrp.test/'
            assert await child.evaluate('window.privateAccess') is False
        for url, headers in seen:
            assert 'private-session-marker' not in url and 'parent-url-marker' not in str(headers)
            assert 'private-cookie-marker' not in str(headers)
            assert headers.get('referer') == ('https://tdrp.test/' if 'ads.tdrp.test' in url else 'https://ads.tdrp.test/')
        cookie = next(c for c in await context.cookies('https://tdrp.test/') if c['name']=='__Host-fs_page_session')
        assert cookie['value']=='private-cookie-marker' and cookie['domain']=='tdrp.test'
        await page.evaluate("""() => {const f=document.querySelector('[data-ad-placement] > iframe');window.dispatchEvent(new MessageEvent('message',{source:f.contentWindow,origin:'https://evil.test',data:{type:'filestream-ad',state:'error'}}))}""")
        assert await frames.count()==2
        assert await page.locator('[data-ad-state=content-unverified]').count()==2
        await browser.close()


@pytest.mark.asyncio
async def test_banner_documents_fail_closed_for_missing_or_same_host_settings():
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        for url in ['', 'https://app.test/banner/'+'a'*64+'.html',
                    'https://app.test:8443/banner/'+'a'*64+'.html',
                    'https://ads.test/invoke.js', 'https://ads.test/banner/'+'a'*64+'.html?token=bad',
                    'https://ads.test/banner/'+'a'*64+'.html#bad']:
            page = await browser.new_page()
            cfg = ads_config(True)
            cfg['desktop']['top_banner']['document_url'] = url
            content = '<aside data-ad-placement="top"></aside><script id="ad-config" type="application/json">'+json.dumps(cfg)+'</script>'
            await page.route('https://app.test/', lambda r: r.fulfill(content_type='text/html',body=content))
            await page.goto('https://app.test/')
            await page.add_script_tag(path=str(ROOT/'FileStream/web/static/js/ads.js'))
            assert await page.locator('iframe').count()==0
            assert await page.locator('[data-ad-placement]').get_attribute('data-ad-state')=='disabled'
            await page.close()
        await browser.close()
