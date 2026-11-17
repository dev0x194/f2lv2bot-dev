"""Opt-in HTTPS preview check. Provider JS is a labelled fixture, never live-fill proof.

Uses an isolated temporary Chromium profile; no real user cookies or storage exports.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.async_api")


@pytest.mark.asyncio
async def test_preview_ad_cookies_survive_reload_navigation_and_browser_restart(
    tmp_path,
):
    preview = os.environ.get("AD_COOKIE_PREVIEW_URL")
    if not preview:
        pytest.skip("Set AD_COOKIE_PREVIEW_URL for the approved diagnostic preview")
    from urllib.parse import urlsplit

    parent = urlsplit(preview)
    parent_origin = parent.scheme + "://" + parent.netloc
    ad_origin = "https://ads.tdrp.cc"
    invocations = []
    statuses = []
    credential_seen = []
    tasks = []
    async with playwright.async_playwright() as pw:

        async def start():
            context = await pw.chromium.launch_persistent_context(
                str(tmp_path / "isolated-browser-profile"),
                args=["--no-sandbox"],
                viewport=dict(width=375, height=900),
            )

            async def provider(route):
                headers = await route.request.all_headers()
                # Public synthetic markers only. Never save or print Cookie headers.
                credential_seen.append(
                    "public-fs-cookie-probe" in headers.get("cookie", "")
                )
                invocations.append(route.request.url)
                await route.fulfill(
                    content_type="application/javascript",
                    body="""
                    document.cookie='ad_frequency=public-ad-fixture; Max-Age=3600; Secure; SameSite=Lax; Path=/';
                    document.write('<iframe srcdoc="<p>Synthetic ad fixture</p>"></iframe>');
                    setTimeout(()=>{throw new Error('Synthetic nonfatal provider telemetry error')},0);
                """,
                )

            await context.route("https://bellnewyork.org/22/**", provider)
            page = await context.new_page()

            async def record(req):
                if (
                    req.url.startswith(ad_origin + "/banner/")
                    or req.url == ad_origin + "/unit.html"
                ):
                    headers = await req.all_headers()
                    credential_seen.append(
                        "public-fs-cookie-probe" in headers.get("cookie", "")
                    )
                    # Only a boolean, no values, is retained for repeated-request assertions.
                    statuses.append(("cookie-present", bool(headers.get("cookie"))))

            async def response(res):
                if (
                    res.url.startswith(ad_origin + "/banner/")
                    or res.url == ad_origin + "/unit.html"
                ):
                    statuses.append(("status", res.status))

            page.on(
                "request", lambda req: tasks.append(asyncio.create_task(record(req)))
            )
            page.on(
                "response", lambda res: tasks.append(asyncio.create_task(response(res)))
            )
            return context, page

        async def wait_preview(page):
            await page.wait_for_function(
                "document.querySelector('[data-ad-placement=top]').dataset.adState==='content-unverified'"
            )
            assert await page.locator("[data-ad-placement=top] > iframe").is_visible()
            child = await (
                await page.locator("[data-ad-placement=top] > iframe").element_handle()
            ).content_frame()
            assert (
                await child.evaluate(
                    "(()=>{try{return !!parent.document.body}catch(e){return false}})()"
                )
                is False
            )
            assert (
                await child.evaluate(
                    "document.cookie.split(';').some(c=>c.trim().startsWith('__Host-fs_page_session='))"
                )
                is False
            )
            await page.wait_for_function(
                "document.querySelector('details pre').textContent.includes('runtime-error')"
            )
            # A window runtime exception must not be treated as document/network failure.
            assert (
                await page.locator("[data-ad-placement=top]").get_attribute(
                    "data-ad-state"
                )
                == "content-unverified"
            )

        context, page = await start()
        await page.goto(parent_origin + "/ad-test-f-320.html")
        await page.check("#baseline")
        await page.click("#start")
        await page.wait_for_function(
            "document.getElementById('details').textContent.includes('generatedIframes')"
        )
        for _ in range(2):
            await page.reload()
            await page.check("#baseline")
            await page.click("#start")
            await page.wait_for_function(
                "document.getElementById('details').textContent.includes('generatedIframes')"
            )
        # A harmless cookie created on the ad host persists, unlike a parent credential.
        cookie_metadata = [
            {"name": c["name"], "domain": c["domain"], "secure": c["secure"]}
            for c in await context.cookies(ad_origin)
        ]
        assert any(
            c["name"] == "ad_frequency" and c["domain"] == "ads.tdrp.cc"
            for c in cookie_metadata
        )
        await context.add_cookies(
            [
                dict(
                    name="__Host-fs_page_session",
                    value="public-fs-cookie-probe",
                    url=parent_origin + "/",
                    secure=True,
                    httpOnly=True,
                    expires=int(time.time()) + 3600,
                ),
                dict(
                    name="ad_consent",
                    value="public-consent-fixture",
                    domain=".tdrp.cc",
                    path="/",
                    secure=True,
                    expires=int(time.time()) + 3600,
                ),
            ]
        )
        await page.goto(preview)
        await wait_preview(page)
        for _ in range(3):
            await page.reload()
            await wait_preview(page)
        await page.goto(parent_origin + "/ad-test-f-320.html")
        await page.check("#baseline")
        await page.click("#start")
        await page.wait_for_function(
            "document.getElementById('details').textContent.includes('generatedIframes')"
        )
        await page.goto(preview)
        await wait_preview(page)
        await context.close()
        # Reopen the browser with its own native test-cookie storage, no JSON storage export.
        context, page = await start()
        await page.goto(preview)
        await wait_preview(page)
        for _ in range(2):
            await page.reload()
            await wait_preview(page)
        # Exercise all configured placements with the accumulated benign cookies.
        full = preview.rsplit("/", 1)[0] + "/cookie-fix.html"
        await page.goto(full)
        await page.wait_for_function(
            "document.querySelectorAll('[data-ad-state=content-unverified]').length===2"
        )
        for width, expected in [
            (375, [(320, 50), (300, 250)]),
            (1440, [(728, 90), (300, 250)]),
        ]:
            await page.set_viewport_size(dict(width=width, height=900))
            await page.wait_for_function(
                "device=>document.querySelector('[data-ad-placement=top]').dataset.adDevice===device",
                arg="mobile" if width < 768 else "desktop",
            )
            await page.wait_for_function(
                "document.querySelectorAll('[data-ad-state=content-unverified]').length===2"
            )
            dimensions = []
            for element in await page.locator(
                "[data-ad-placement] > iframe:visible"
            ).element_handles():
                child = await element.content_frame()
                dimensions.append(
                    await child.evaluate("[atOptions.width,atOptions.height]")
                )
            assert dimensions == [list(x) for x in expected]
        await asyncio.gather(*tasks)
        assert not any(credential_seen)
        assert any(kind == "cookie-present" and value for kind, value in statuses)
        assert all(value == 200 for kind, value in statuses if kind == "status")
        # Simulate a legacy domain-wide credential: it must still fail closed.
        await context.add_cookies(
            [
                dict(
                    name="fs_page_session",
                    value="public-fs-cookie-probe",
                    domain=".tdrp.cc",
                    path="/",
                    secure=True,
                    httpOnly=True,
                )
            ]
        )
        before = len(invocations)
        await page.goto(preview)
        await page.wait_for_function(
            "document.querySelector('details pre').textContent.includes('protected-cookie-name')"
        )
        assert await page.locator("[data-ad-placement=top] > iframe").count() == 0
        assert len(invocations) == before
        await context.close()
