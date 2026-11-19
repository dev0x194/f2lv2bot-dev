"""Actual V3 UI with synthetic script/creative origins; never paid ad requests."""

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
import jinja2
import pytest
from web_fixtures import ROOT, environment
from test_direct_js import settings
from playwright.async_api import async_playwright

METRICS = """() => {
 if (window !== top) return;
 window.uiMetrics = {mainVisible:null, lcp:null, shifts:0, clicks:0};
 new PerformanceObserver(list => { for(const e of list.getEntries()) uiMetrics.lcp=e.startTime; }).observe({type:'largest-contentful-paint',buffered:true});
 new PerformanceObserver(list => { for(const e of list.getEntries()) if(!e.hadRecentInput) uiMetrics.shifts+=e.value; }).observe({type:'layout-shift',buffered:true});
 const check=()=>{const el=document.querySelector('main h1');if(el && el.getBoundingClientRect().height && getComputedStyle(el).visibility!=='hidden')uiMetrics.mainVisible=performance.now();else requestAnimationFrame(check)};requestAnimationFrame(check);
 addEventListener('message',e=>{if(e.origin==='https://creative.test' && e.data==='test-click')uiMetrics.clicks++});
}"""


async def setup(
    context, *, enabled=True, source=None, behavior="fast", release=None, started=None
):
    e = environment()
    e.render["WebAds"] = settings(
        WEB_ADS_ENABLED="True" if enabled else "False",
        WEB_ADS_SMARTLINK_URL="https://ads.test/optional",
    )
    e.db.get_web_ads_status.return_value = True
    source = Path(source or ROOT / "FileStream/web")
    e.render["env"] = jinja2.Environment(
        loader=jinja2.FileSystemLoader(source / "templates"), autoescape=True
    )
    e.render["_resolve_template_path"] = lambda name: str(source / "templates" / name)
    requests = []
    await context.add_init_script("(" + METRICS + ")()")
    await context.route("**/*", lambda r: r.abort())

    async def app(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/static/"):
            p = source / path.lstrip("/")
            if p.is_file():
                await route.fulfill(path=p)
            else:
                await route.fulfill(status=404, body="")
        elif path == "/":
            await route.fulfill(
                content_type="text/html",
                body=await e.render["render_landing_page"](bot=e.bot),
            )
        elif path == "/gen/public-file":
            await route.fulfill(
                content_type="text/html",
                body=await e.render["render_public_page"](
                    "public-file", session_id="synthetic"
                ),
            )
        elif path == "/gfolder/folder":
            e.db.get_file.return_value = e.file
            await route.fulfill(
                content_type="text/html",
                body=await e.render["render_folder"](
                    "folder",
                    folder_doc=dict(files=["one"]),
                    public_link=dict(public_id="folder"),
                ),
            )
        elif path.startswith("/get-download-token/"):
            await route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    dict(success=True, download_url="https://app.test/file/synthetic")
                ),
            )
        elif path.startswith("/get-stream-token/"):
            await route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    dict(
                        success=True,
                        stream_url="https://app.test/stream/synthetic",
                        mime_type="video/mp4",
                    )
                ),
            )
        elif path == "/stream/synthetic":
            data = (ROOT / "tests/fixtures/sample.mp4").read_bytes()
            header = route.request.headers.get("range", "")
            match = re.match(r"bytes=(\d+)-(\d*)", header)
            if match:
                start = int(match[1])
                end = min(int(match[2]) if match[2] else len(data) - 1, len(data) - 1)
                await route.fulfill(
                    status=206,
                    body=data[start : end + 1],
                    content_type="video/mp4",
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                        "Accept-Ranges": "bytes",
                    },
                )
            else:
                await route.fulfill(body=data, content_type="video/mp4")
        elif path == "/file/synthetic":
            await route.fulfill(
                body=b"synthetic download",
                headers={"Content-Disposition": 'attachment; filename="synthetic.txt"'},
            )
        elif path.startswith("/dl/"):
            await route.fulfill(
                status=403, body="Legacy download route remains protected"
            )
        else:
            await route.fulfill(status=404, body="")

    async def provider(route):
        requests.append(route.request.url)
        if started:
            started.set()
        if release:
            await release.wait()
        if behavior == "slow":
            await asyncio.sleep(1.2)
        if behavior == "blocked":
            await route.abort()
            return
        if behavior == "failed":
            await route.fulfill(status=503, body="")
            return
        await route.fulfill(
            content_type="application/javascript",
            body='document.write(\'<iframe title="Synthetic creative" width="\'+atOptions.width+\'" height="\'+atOptions.height+\'" src="https://creative.test/frame"></iframe>\')',
        )

    await context.route("https://app.test/**", app)
    await context.route("https://provider.test/**", provider)
    await context.route(
        "https://creative.test/**",
        lambda r: r.fulfill(
            content_type="text/html",
            body="<style>html,body{margin:0;width:100%;height:100%}button{width:100%;height:100%;border:0;background:#cce8df;color:#142a22}</style><button onclick=\"parent.postMessage('test-click','https://app.test')\">SYNTHETIC TEST creative</button>",
        ),
    )
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [320, 360, 375, 390, 430, 768, 1024, 1440])
async def test_viewports_banners_labels_and_controls(width):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": width, "height": 900})
        requests = await setup(context)
        page = await context.new_page()
        try:
            for path in ["/", "/gen/public-file", "/gfolder/folder"]:
                await page.goto("https://app.test" + path, wait_until="networkidle")
                assert await page.evaluate(
                    "document.documentElement.scrollWidth<=innerWidth"
                )
                assert (
                    await page.locator("meta[name=viewport]").get_attribute("content")
                    == "width=device-width, initial-scale=1"
                )
                assert await page.locator(".ad-label:visible").count() == 2
                assert (
                    await page.get_by_role(
                        "complementary", name="Advertisement", exact=True
                    ).count()
                    == 2
                )
                for label in await page.locator(".ad-label").all():
                    style = await label.evaluate(
                        "(e)=>({size:parseFloat(getComputedStyle(e).fontSize),events:getComputedStyle(e).pointerEvents})"
                    )
                    assert style["size"] >= 12 and style["events"] == "none"
                frames = page.locator(".ad-placement iframe")
                assert await frames.count() == 2
                assert (await frames.nth(0).bounding_box())["width"] == (
                    320 if width < 768 else 728
                )
                assert (await frames.nth(1).bounding_box())["width"] == 300
                label_box = await page.locator(".ad-label").first.bounding_box()
                frame_box = await frames.first.bounding_box()
                assert label_box["y"] + label_box["height"] <= frame_box["y"]
                await frames.first.scroll_into_view_if_needed()
                box = await frames.first.bounding_box()
                # The creative, never a label/overlay, receives center clicks.
                assert (
                    await page.evaluate(
                        "(p)=>document.elementFromPoint(p.x,p.y).tagName",
                        {
                            "x": box["x"] + box["width"] / 2,
                            "y": box["y"] + box["height"] / 2,
                        },
                    )
                    == "IFRAME"
                )
                frame = await (await frames.first.element_handle()).content_frame()
                await frame.get_by_role("button").click()
                await page.wait_for_function("() => uiMetrics.clicks===1")
                await page.locator("#theme").select_option("light")
                assert await page.locator("html").get_attribute("data-theme") == "light"
                assert (await page.locator("#theme").bounding_box())["height"] >= 44
                if path == "/":
                    columns = await page.locator(".split").first.evaluate(
                        '(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length'
                    )
                    assert columns == (2 if width > 850 else 1)
                if path.startswith("/gen/"):
                    for button in await page.locator(
                        ".file-page .actions > button"
                    ).all():
                        assert (await button.bounding_box())["height"] >= 44
            before = len(requests)
            await page.set_viewport_size(
                {"width": 900 if width < 768 else 390, "height": 430}
            )
            assert len(requests) == before
            assert await page.evaluate(
                "document.documentElement.scrollWidth<=innerWidth"
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,behavior",
    [
        (False, "slow"),
        (True, "fast"),
        (True, "slow"),
        (True, "failed"),
        (True, "blocked"),
    ],
)
async def test_stalled_provider_cannot_hide_content_or_block_download_controls(
    enabled, behavior
):
    started, release = asyncio.Event(), asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        requests = await setup(
            context,
            enabled=enabled,
            behavior=behavior,
            started=started,
            release=release if enabled else None,
        )
        page = await context.new_page()
        try:
            navigation = asyncio.create_task(
                page.goto(
                    "https://app.test/gen/public-file", wait_until="domcontentloaded"
                )
            )
            if enabled:
                await asyncio.wait_for(started.wait(), 10)
            else:
                await navigation
            assert await page.locator("main h1").is_visible()
            assert await page.locator("#file-config").count() == 1
            await page.locator("#theme").select_option("light")
            assert await page.locator("html").get_attribute("data-theme") == "light"
            if enabled:
                assert await page.evaluate("document.readyState") == "loading"
            async with page.expect_download() as transfer:
                await page.locator("[data-action=download]").click()
            download = await transfer.value
            assert await download.failure() is None
            assert Path(await download.path()).read_bytes() == b"synthetic download"
            if not enabled:
                assert (
                    not requests
                    and not await page.locator(
                        ".ad-reservation,.ad-placement,.ad-label"
                    ).count()
                )
            release.set()
            await navigation
            if enabled:
                assert (
                    len(requests) == 2 and await page.locator(".ad-label").count() == 2
                )
        finally:
            release.set()
            await browser.close()


@pytest.mark.asyncio
async def test_labels_and_reservations_do_not_appear_for_inactive_device():
    e = environment()
    e.render["WebAds"] = settings(
        WEB_ADS_MOBILE_TOP_BANNER_KEY="", WEB_ADS_MOBILE_BOTTOM_BANNER_KEY=""
    )
    e.db.get_web_ads_status.return_value = True
    html = await e.render["render_landing_page"](bot=e.bot)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        requests = await setup(context)
        await context.route(
            "https://app.test/",
            lambda r: r.fulfill(content_type="text/html", body=html),
        )
        page = await context.new_page()
        await page.goto("https://app.test/", wait_until="networkidle")
        assert (
            not requests
            and not await page.locator(
                ".ad-label:visible,.ad-reservation:visible,.ad-placement:visible"
            ).count()
        )
        await browser.close()


@pytest.mark.asyncio
async def test_videojs_playback_and_seeking_work_before_ad_parser_finishes():
    started, release = asyncio.Event(), asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await setup(context, started=started, release=release)
        page = await context.new_page()
        navigation = asyncio.create_task(
            page.goto("https://app.test/gen/public-file", wait_until="domcontentloaded")
        )
        try:
            await asyncio.wait_for(started.wait(), 10)
            await page.locator("[data-action=play]").click()
            await page.wait_for_function(
                '() => window.videojs && videojs("player").currentTime()>.2'
            )
            await page.evaluate('() => videojs("player").currentTime(3)')
            await page.wait_for_function(
                '() => videojs("player").currentTime()>3.2 && videojs("player").readyState()>=2'
            )
            assert await page.evaluate("document.readyState") == "loading"
            release.set()
            await navigation
        finally:
            release.set()
            await browser.close()
