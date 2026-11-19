"""Synthetic capability audit, never an accusation about provider behavior.

Only mocked HTTPS origins, real V3 render/token handlers and fixture credentials.
No provider requests, production cookies, impressions or real media are used.
"""

import json
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from web_fixtures import environment, load_functions

playwright = pytest.importorskip("playwright.async_api")


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["file", "landing", "same-origin-frame"])
async def test_direct_provider_script_inherits_sensitive_application_authority(
    placement,
):
    e = environment()
    e.routes["PAGE_SESSION_COOKIE"] = "__Host-fs_page_session"
    e.routes["Server"].HAS_SSL = True
    e.routes["build_base_url"] = lambda **kw: "https://tdrp.test"
    minted = []
    privacy = load_functions(
        "FileStream/server/__init__.py", names={"web_privacy_middleware"}
    )["web_privacy_middleware"]

    async def app_response(route):
        request_url = urlsplit(route.request.url)
        path = request_url.path
        if path.startswith("/static/") or path == "/favicon.ico":
            await route.fulfill(
                body="",
                content_type=(
                    "application/javascript" if path.endswith(".js") else "text/css"
                ),
            )
            return
        if path == "/":
            await route.fulfill(
                content_type="text/html",
                body=await e.render["render_landing_page"](bot=e.bot),
            )
            return
        if path == "/ad-only-audit":
            await route.fulfill(
                content_type="text/html",
                body='<!doctype html><script src="https://provider.test/audit.js"></script>',
            )
            return
        handlers = {
            "/gen/public-file": ("gen_handler", {"public_id": "public-file"}),
            "/get-download-token/public-file": (
                "get_download_token_handler",
                {"path": "public-file"},
            ),
        }
        if path not in handlers:
            await route.fulfill(status=404, body="Fixture route unavailable")
            return
        name, match = handlers[path]
        request = make_mocked_request(
            "GET", path, headers=await route.request.all_headers(), match_info=match
        )
        response = await e.routes[name](request)
        if name == "get_download_token_handler" and response.status == 200:
            payload = json.loads(response.text)
            token = urlsplit(payload["download_url"]).path.rsplit("/", 1)[1]
            assert e.tokens.validate_access_token(token, expected_kind="download")
            minted.append(True)

        async def handler(_):
            return response

        response = await privacy(request, handler)
        assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        headers = dict(response.headers)
        for cookie in response.cookies.values():
            headers["Set-Cookie"] = cookie.OutputString()
        await route.fulfill(status=response.status, headers=headers, body=response.text)

    # A public-origin script can fetch a known file page even from the homepage.
    # Its authority is the embedding document's origin, not its source URL.
    script = """(async () => {
      const host = parent;
      const parentConfig = host.document.getElementById('file-config');
      const response = await fetch('/gen/public-file');
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const cfg = JSON.parse(doc.getElementById('file-config').textContent);
      const tokenResponse = await fetch(cfg.downloadPath, {headers: {'X-Page-Token': cfg.pageToken}});
      const data = await tokenResponse.json();
      host.__directScriptAudit = {
        parentDOM: host.document.documentElement.tagName === 'HTML',
        parentTokenReadable: Boolean(parentConfig && JSON.parse(parentConfig.textContent).pageToken),
        fetchedTokenReadable: Boolean(cfg.pageToken),
        cookieValueReadable: document.cookie.includes('__Host-fs_page_session='),
        resumeReadable: localStorage.getItem('filestream-resume:fixture') === '42',
        folderReadable: sessionStorage.getItem('filestream-folder') === '/gen/public-file',
        downloadAuthorized: tokenResponse.status === 200 && Boolean(data.download_url)
      };
    })().catch(() => { parent.__directScriptAudit = {failed: true}; });"""

    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()
        try:
            await context.add_cookies(
                [
                    dict(
                        name="__Host-fs_page_session",
                        value="synthetic-session",
                        url="https://tdrp.test/",
                        secure=True,
                        httpOnly=True,
                        sameSite="Lax",
                    )
                ]
            )
            await context.route("**/*", lambda route: route.abort())
            await context.route("https://tdrp.test/**", app_response)
            await context.route(
                "https://provider.test/audit.js",
                lambda route: route.fulfill(
                    content_type="application/javascript", body=script
                ),
            )
            page = await context.new_page()
            await page.goto(
                "https://tdrp.test/"
                if placement == "landing"
                else "https://tdrp.test/gen/public-file"
            )
            await page.evaluate("""() => {
              localStorage.setItem('filestream-resume:fixture', '42');
              sessionStorage.setItem('filestream-folder', '/gen/public-file');
            }""")
            if placement == "same-origin-frame":
                await page.evaluate("""() => {
                  const frame = document.createElement('iframe');
                  frame.src = '/ad-only-audit'; document.body.append(frame);
                }""")
            else:
                await page.add_script_tag(url="https://provider.test/audit.js")
            await page.wait_for_function("window.__directScriptAudit !== undefined")
            result = await page.evaluate("window.__directScriptAudit")
            assert not result.get("failed")
            assert result["parentDOM"]
            assert result["parentTokenReadable"] == (placement != "landing")
            assert result["fetchedTokenReadable"] and result["downloadAuthorized"]
            assert not result["cookieValueReadable"]
            assert result["resumeReadable"] and result["folderReadable"]
            assert minted == [True]
        finally:
            await context.close()
            await browser.close()
