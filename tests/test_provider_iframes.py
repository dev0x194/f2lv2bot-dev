"""Test G scaffold: synthetic contracts only, never real provider approvals."""

import json
import pytest
from web_fixtures import ROOT, environment, web_ads_settings, load_public_module
from FileStream.web import provider_iframes as provider


@pytest.fixture
def contracts(monkeypatch):
    entries = {}
    for width, height in ((320, 50), (300, 250), (728, 90)):
        url = f"https://provider.test/frame/{width}x{height}"
        entries[url] = dict(
            size=(width, height),
            publisher_origins=("https://app.test",),
            sandbox="allow-scripts allow-same-origin",
            referrer_policy="origin",
            evidence="SYNTHETIC TEST CONTRACT ONLY",
        )
    monkeypatch.setattr(provider, "APPROVED_CONTRACTS", entries)
    return entries


def settings(**extra):
    return web_ads_settings(
        {
            "WEB_ADS_MODE": "provider_iframe",
            "FQDN": "app.test",
            **{
                f"WEB_ADS_PROVIDER_IFRAME_{w}X{h}": f"https://provider.test/frame/{w}x{h}"
                for w, h in ((320, 50), (300, 250), (728, 90))
            },
            **extra,
        }
    )


def test_production_registry_is_empty_and_urls_cannot_self_authorize():
    assert not provider.APPROVED_CONTRACTS
    cfg = settings().template_context()
    assert cfg["mode_error"] == "provider-contract-unverified"
    assert not cfg["has_any_banner"] and not cfg["provider_iframe_contracts"]
    assert not cfg["has_any_social_bar"] and not cfg["popunder_url"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "javascript:alert(1)",
        "data:text/html,hi",
        "blob:https://provider.test/x",
        "http://provider.test/frame",
        "https://user:pass@provider.test/frame",
        "https://127.0.0.1/frame",
        "https://[::1]/frame",
        "https://localhost/frame",
        "https://ads.local/frame",
        "https://ads.internal/frame",
        "https://0x7f.0.0.1/frame",
        "https://app.test/frame",
        "https://ads.app.test/frame",
        "https://provider.test:8443/frame",
        "https://provider.test/frame#token",
        "https://provider.test/invoke.js",
        "https://bellnewyork.org/22/existing-key",
        "https://provider.test/frame?sid=private",
        "https://provider.test/frame?page_token=private",
        "https://provider.test/white space",
        "https://provider.test\\@app.test/frame",
    ],
)
def test_unsafe_urls_rejected(url, contracts):
    assert not provider.valid_url(url, "https://app.test")
    assert provider.approved_frame(url, 320, 50, "https://app.test") is None


def test_exact_endpoint_size_publisher_and_security_policy_required(contracts):
    good = "https://provider.test/frame/320x50"
    assert provider.approved_frame(good, 320, 50, "https://app.test")
    for url, w, h, parent in [
        (good + "?zone=other", 320, 50, "https://app.test"),
        ("https://unapproved.test/frame", 320, 50, "https://app.test"),
        (good, 728, 90, "https://app.test"),
        (good, 320, 50, "https://other.test"),
    ]:
        assert provider.approved_frame(url, w, h, parent) is None
    contracts[good]["sandbox"] = "allow-scripts allow-top-navigation"
    assert provider.approved_frame(good, 320, 50, "https://app.test") is None
    contracts[good]["sandbox"] = "allow-scripts"
    contracts[good]["referrer_policy"] = "unsafe-url"
    assert provider.approved_frame(good, 320, 50, "https://app.test") is None


def test_modes_four_slots_disabled_settings_and_no_export_of_provider_js(
    contracts, tmp_path
):
    cfg = settings().template_context()
    assert cfg["has_any_banner"] and len(cfg["provider_iframe_contracts"]) == 3
    assert [
        (cfg[d][s]["width"], cfg[d][s]["height"])
        for d, s in [
            ("mobile", "top_banner"),
            ("mobile", "bottom_banner"),
            ("desktop", "top_banner"),
            ("desktop", "inline_banner"),
        ]
    ] == [(320, 50), (300, 250), (728, 90), (300, 250)]
    assert not settings().template_context(enabled_override=False)["has_any_banner"]
    assert not settings(WEB_ADS_ENABLED="False").template_context()["has_any_banner"]
    optional = settings(
        WEB_ADS_SOCIAL_BAR_ENABLED="True",
        WEB_ADS_POPUNDER_URL="https://scripts.test/popup.js",
    ).template_context()
    assert not optional["has_any_social_bar"] and not optional["popunder_url"]
    missing = settings(WEB_ADS_PROVIDER_IFRAME_320X50="").template_context()
    assert not missing["mobile"]["top_banner"]["enabled"]
    assert missing["mobile"]["bottom_banner"]["enabled"]
    assert not settings(WEB_ADS_MODE="direct_scripts").template_context()[
        "has_any_banner"
    ]
    original = web_ads_settings().template_context()
    assert original["mode"] == "isolated_documents" and original["has_any_banner"]
    assert "/banner/" in original["mobile"]["top_banner"]["document_url"]
    rollback = settings(WEB_ADS_MODE="isolated_documents").template_context()
    assert rollback["provider_iframe_contracts"] == []
    for device, name in (
        ("mobile", "top_banner"),
        ("mobile", "bottom_banner"),
        ("desktop", "top_banner"),
        ("desktop", "inline_banner"),
    ):
        assert rollback[device][name] == original[device][name]
    exporter = load_public_module(
        "provider_export_test", "FileStream/web/tools/export_ad_documents.py"
    )
    assert (
        exporter.export_documents(settings(), tmp_path / "bundle", ["https://app.test"])
        == 0
    )
    assert not list((tmp_path / "bundle/html/banner").glob("*.html"))


@pytest.mark.asyncio
async def test_mock_provider_frames_responsive_isolated_retained_and_unknown_fill(
    contracts,
):
    pwmod = pytest.importorskip("playwright.async_api")
    e = environment(True)
    cfg = settings().template_context()
    e.render["WebAds"].template_context = lambda **kw: cfg
    html = await e.render["render_public_page"](
        "public-file", session_id="fixture-session"
    )
    requests = []
    iframe_script = """<script>
      let parentBlocked=false, navigationBlocked=false;
      try { parent.document.getElementById('file-config') } catch(e) { parentBlocked=true }
      try { top.location.href='https://escape.test/'; } catch(e) { navigationBlocked=true }
      window.audit={parentBlocked,navigationBlocked};
      fetch('https://app.test/private-fixture',{credentials:'include'}).then(r=>r.text()).then(()=>audit.readPrivate=true).catch(()=>audit.readPrivate=false);
    </script><p>Synthetic creative</p>"""
    async with pwmod.async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(viewport=dict(width=375, height=900))
        try:
            await context.add_cookies(
                [
                    dict(
                        name="__Host-fs_page_session",
                        value="fixture-private",
                        url="https://app.test/",
                        secure=True,
                        httpOnly=True,
                        sameSite="Lax",
                    )
                ]
            )
            await context.route("**/*", lambda route: route.abort())

            async def application(route):
                path = route.request.url.removeprefix("https://app.test")
                if path == "/gen/public-file":
                    await route.fulfill(
                        content_type="text/html",
                        body=html,
                        headers={
                            "Content-Security-Policy": "base-uri 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'"
                        },
                    )
                elif path == "/static/js/ads.js":
                    await route.fulfill(
                        content_type="application/javascript",
                        body=(ROOT / "FileStream/web/static/js/ads.js").read_text(),
                    )
                elif path == "/static/css/app.css":
                    await route.fulfill(
                        content_type="text/css",
                        body=(ROOT / "FileStream/web/static/css/app.css").read_text(),
                    )
                elif path == "/private-fixture":
                    await route.fulfill(
                        status=403,
                        body="fixture-private-response",
                        headers={"Access-Control-Allow-Origin": "https://app.test"},
                    )
                else:
                    await route.fulfill(content_type="application/javascript", body="")

            async def advertisement(route):
                requests.append(route.request)
                assert route.request.resource_type == "document"
                assert "__Host-fs_page_session" not in (
                    await route.request.all_headers()
                ).get("cookie", "")
                assert (await route.request.all_headers()).get(
                    "referer"
                ) == "https://app.test/"
                assert (
                    "public-file" not in route.request.url
                    and "pageToken" not in route.request.url
                )
                await route.fulfill(content_type="text/html", body=iframe_script)

            await context.route("https://app.test/**", application)
            await context.route("https://provider.test/**", advertisement)
            page = await context.new_page()
            await page.clock.install()
            await page.goto("https://app.test/gen/public-file")
            await page.wait_for_function(
                'document.querySelectorAll("aside.ad-placement > iframe").length===2'
            )
            await page.wait_for_function(
                '[...document.querySelectorAll("aside.ad-placement")].every(e=>e.dataset.adState==="loaded-unverified")'
            )
            assert len(requests) == 2
            for node in await page.locator("aside.ad-placement > iframe").all():
                assert await node.get_attribute("referrerpolicy") == "origin"
                assert "allow-top-navigation" not in await node.get_attribute("sandbox")
                assert "camera 'none'" in await node.get_attribute("allow")
                child = await (await node.element_handle()).content_frame()
                await child.wait_for_function(
                    "window.audit?.readPrivate===false", timeout=3000
                )
                assert await child.evaluate(
                    "audit.parentBlocked && audit.navigationBlocked"
                )
            assert page.url == "https://app.test/gen/public-file"
            assert (
                await page.locator(
                    "[data-ad-placement=top] > iframe:visible"
                ).get_attribute("width")
                == "320"
            )
            await page.set_viewport_size(dict(width=1440, height=900))
            await page.wait_for_function(
                'document.querySelector("[data-ad-placement=top]").dataset.adDevice==="desktop"'
            )
            assert (
                await page.locator(
                    "[data-ad-placement=top] > iframe:visible"
                ).get_attribute("width")
                == "728"
            )
            assert (
                await page.locator(
                    "[data-ad-placement=inline] > iframe:visible"
                ).get_attribute("width")
                == "300"
            )
            await page.wait_for_function(
                'document.querySelectorAll("aside.ad-placement > iframe").length===3'
            )
            await page.set_viewport_size(dict(width=375, height=900))
            await page.wait_for_function(
                'document.querySelector("[data-ad-placement=top]").dataset.adDevice==="mobile"'
            )
            await page.clock.run_for(75000)
            assert len(requests) == 3  # Shared 300x250 reused; no frame refresh loop.
            assert (
                await page.locator("aside.ad-placement > iframe:visible").count() == 2
            )
            assert (
                await page.locator('script[src^="https://provider.test"]').count() == 0
            )
            assert await page.evaluate("typeof atOptions") == "undefined"
            # A provider cannot send our monitor messages to claim fill or destroy a frame.
            await page.evaluate(
                """() => {const f=document.querySelector('aside iframe');window.dispatchEvent(new MessageEvent('message',{source:f.contentWindow,origin:'https://provider.test',data:{type:'filestream-ad',state:'error'}}));}"""
            )
            assert await page.locator("aside.ad-placement > iframe").count() == 3
            await page.locator(
                "[data-ad-placement=top] > iframe:visible"
            ).dispatch_event("error")
            await page.set_viewport_size(dict(width=1440, height=900))
            await page.set_viewport_size(dict(width=375, height=900))
            assert (
                len(requests) == 3
            )  # Explicit failed frames are not retried automatically.
            failures = []

            async def blocked(route):
                failures.append(route.request.url)
                await route.abort()

            await context.route("https://provider.test/**", blocked)
            failed_page = await context.new_page()
            await failed_page.clock.install()
            await failed_page.goto("https://app.test/gen/public-file")
            await failed_page.wait_for_load_state("networkidle")
            await failed_page.clock.run_for(75000)
            assert len(failures) == 2
            assert await failed_page.locator("#file-config").count() == 1
            assert await failed_page.locator('[data-action="download"]').count() == 1
            assert (
                await failed_page.locator(
                    '[data-ad-state="content-unverified"]'
                ).count()
                == 0
            )
        finally:
            await context.close()
            await browser.close()
