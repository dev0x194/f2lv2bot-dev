"""Mock functionality passes do NOT mean direct scripts are safe."""

import pytest
from web_fixtures import ROOT, web_ads_settings, environment
from FileStream.web.direct_ads import script_url


def settings(**extra):
    return web_ads_settings(
        {
            "WEB_ADS_ENABLED": "True",
            **{
                f"WEB_ADS_{p}_BANNER_KEY": key
                for p, key in [
                    ("MOBILE_TOP", "mobile"),
                    ("MOBILE_BOTTOM", "rectangle"),
                    ("DESKTOP_TOP", "desktop"),
                    ("DESKTOP_INLINE", "rectangle"),
                ]
            },
            **{
                f"WEB_ADS_{p}_BANNER_INVOKE_URL": "https://provider.test/22/" + key
                for p, key in [
                    ("MOBILE_TOP", "mobile"),
                    ("MOBILE_BOTTOM", "rectangle"),
                    ("DESKTOP_TOP", "desktop"),
                    ("DESKTOP_INLINE", "rectangle"),
                ]
            },
            **{
                f"WEB_ADS_{prefix}_BANNER_{field}": str(value)
                for prefix, size in (
                    ("MOBILE_TOP", (320, 50)),
                    ("MOBILE_BOTTOM", (300, 250)),
                    ("DESKTOP_TOP", (728, 90)),
                    ("DESKTOP_INLINE", (300, 250)),
                )
                for field, value in zip(("WIDTH", "HEIGHT"), size)
            },
            **extra,
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://provider.test/x.js",
        "https://evil.test/x.js",
        "https://provider.test.evil.test/x.js",
        "https://u:p@provider.test/x.js",
        "https://provider.test:8443/x.js",
        "https://provider.test/x.js#secret",
        "https://127.0.0.1/x.js",
    ],
)
def test_script_policy(url):
    assert not script_url(url, ("provider.test",))


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize(
    "width,keys,sizes",
    [
        (390, ["mobile", "rectangle"], [[320, 50], [300, 250]]),
        (1280, ["desktop", "rectangle"], [[728, 90], [300, 250]]),
    ],
)
async def test_parser_inserted_banners_original_order_and_no_resize_reload(
    width, keys, sizes, blocked
):
    from playwright.async_api import async_playwright

    e = environment()
    e.render["WebAds"] = settings()
    e.db.get_web_ads_status.return_value = True
    requests = []
    referrers = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": width, "height": 900})
        await page.route("**/*", lambda route: route.abort())

        async def app(route):
            path = route.request.url.split("https://app.test", 1)[1]
            if path == "/":
                await route.fulfill(
                    content_type="text/html",
                    body=await e.render["render_landing_page"](bot=e.bot),
                )
            elif path.startswith("/static/"):
                p = ROOT / "FileStream/web" / path.lstrip("/")
                (
                    await route.fulfill(path=p)
                    if p.is_file()
                    else await route.fulfill(body="")
                )
            else:
                await route.fulfill(status=404, body="")

        await page.route("https://app.test/**", app)

        async def provider(route):
            requests.append(route.request.url.rsplit("/", 1)[1])
            referrers.append((await route.request.all_headers()).get("referer"))
            if blocked:
                await route.abort()
                return
            await route.fulfill(
                content_type="application/javascript",
                body="""
              window.order=window.order||[];window.order.push({...atOptions});
              document.write('<iframe title="SYNTHETIC creative" src="about:blank" width="'+atOptions.width+'" height="'+atOptions.height+'"></iframe>');
            """,
            )

        await page.route("https://provider.test/**", provider)
        await page.goto("https://app.test/", wait_until="networkidle")
        assert requests == keys
        assert referrers == ["https://app.test/"] * 2
        if not blocked:
            order = await page.evaluate("window.order")
            assert [[x["width"], x["height"]] for x in order] == sizes
            assert all(x["format"] == "iframe" and x["params"] == {} for x in order)
        assert await page.locator(".ad-placement.loaded iframe").count() == (
            0 if blocked else 2
        )
        assert await page.locator("h1").is_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.set_viewport_size(
            {"width": 1280 if width == 390 else 390, "height": 900}
        )
        assert requests == keys
        assert (
            await page.evaluate(
                'window.FileStreamDirectAds.banner(JSON.parse(document.getElementById("ad-config").textContent),"top")'
            )
            is None
        )
        assert requests == keys
        await page.set_viewport_size({"width": width, "height": 900})
        await page.reload(wait_until="networkidle")
        assert requests == keys + keys
        await page.goto("https://app.test/", wait_until="networkidle")
        assert requests == keys + keys + keys
        await browser.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"WEB_ADS_MOBILE_TOP_BANNER_KEY": ""},
        {"WEB_ADS_MOBILE_TOP_BANNER_KEY": "wrong-zone"},
        {"WEB_ADS_MOBILE_TOP_BANNER_INVOKE_URL": "javascript:alert(1)"},
        {"WEB_ADS_MOBILE_TOP_BANNER_WIDTH": "321"},
        {"WEB_ADS_MOBILE_TOP_BANNER_HEIGHT": "0"},
    ],
)
def test_invalid_direct_zone_disables_only_that_placement(extra):
    c = settings(**extra).template_context()
    assert not c["mobile"]["top_banner"]["enabled"]
    assert c["mobile"]["bottom_banner"]["enabled"]
    assert c["desktop"]["top_banner"]["enabled"]
    assert c["desktop"]["inline_banner"]["enabled"]
    assert "document_url" not in c["mobile"]["top_banner"]


@pytest.mark.asyncio
async def test_blocking_provider_delays_domcontentloaded_not_essential_content():
    # Parser completion still waits, but essential DOM is available first.
    import asyncio
    from playwright.async_api import async_playwright

    e = environment()
    e.render["WebAds"] = settings()
    e.db.get_web_ads_status.return_value = True
    started, release = asyncio.Event(), asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.route("**/*", lambda route: route.abort())

        async def app(route):
            path = route.request.url.split("https://app.test", 1)[1]
            if path == "/":
                await route.fulfill(
                    content_type="text/html",
                    body=await e.render["render_landing_page"](bot=e.bot),
                )
            elif path == "/static/js/direct-ads.js":
                await route.fulfill(
                    path=ROOT / "FileStream/web/static/js/direct-ads.js"
                )
            else:
                await route.fulfill(body="", content_type="application/javascript")

        async def provider(route):
            started.set()
            await release.wait()
            await route.fulfill(body="", content_type="application/javascript")

        await page.route("https://app.test/**", app)
        await page.route("https://provider.test/**", provider)
        try:
            await page.goto("https://app.test/", wait_until="commit")
            await asyncio.wait_for(started.wait(), 5)
            assert await page.locator("h1").is_visible()
            assert await page.evaluate("document.readyState") == "loading"
            assert await page.evaluate('performance.getEntriesByName("filestream:content-ready").length') == 1
            release.set()
            await page.wait_for_load_state("domcontentloaded")
            assert await page.locator("h1").is_visible()
        finally:
            release.set()
            await browser.close()


@pytest.mark.asyncio
async def test_disabled_formats_do_not_load_either_advertising_runtime():
    e = environment()
    e.render["WebAds"] = settings(
        **{
            "WEB_ADS_" + prefix + "_BANNER_KEY": ""
            for prefix in (
                "MOBILE_TOP",
                "MOBILE_BOTTOM",
                "DESKTOP_TOP",
                "DESKTOP_INLINE",
            )
        }
    )
    e.db.get_web_ads_status.return_value = True
    html = await e.render["render_landing_page"](bot=e.bot)
    assert 'src="/static/js/direct-ads.js"' not in html
    assert 'src="/static/js/ads.js"' not in html
    assert "FileStreamDirectAds?.banner" not in html


@pytest.mark.asyncio
async def test_same_zone_is_not_requested_twice_with_different_invocation_queries():
    from playwright.async_api import async_playwright

    e = environment()
    e.render["WebAds"] = settings(
        WEB_ADS_DESKTOP_TOP_BANNER_KEY="rectangle",
        WEB_ADS_DESKTOP_TOP_BANNER_INVOKE_URL="https://provider.test/22/rectangle?placement=top",
    )
    e.db.get_web_ads_status.return_value = True
    calls = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.route("**/*", lambda r: r.abort())

        async def app(route):
            if route.request.url == "https://app.test/":
                await route.fulfill(
                    content_type="text/html",
                    body=await e.render["render_landing_page"](bot=e.bot),
                )
            elif route.request.url.endswith("/static/js/direct-ads.js"):
                await route.fulfill(
                    path=ROOT / "FileStream/web/static/js/direct-ads.js"
                )
            else:
                await route.fulfill(body="", content_type="application/javascript")

        async def provider(route):
            calls.append(True)
            await route.fulfill(
                body='document.write("<span>TEST</span>")',
                content_type="application/javascript",
            )

        await page.route("https://app.test/**", app)
        await page.route("https://provider.test/**", provider)
        await page.goto("https://app.test/", wait_until="networkidle")
        assert len(calls) == 1
        await browser.close()
