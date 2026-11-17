"""Public ad export, deployment boundary and HTTPS-origin configuration tests."""

import json
from pathlib import Path
import pytest
from web_fixtures import ROOT, load_public_module, web_ads_settings
from FileStream.web.ad_documents import (
    banner_id,
    banner_options,
    document_url,
    https_origin,
    render_document,
)

exporter = load_public_module(
    "ad_exporter", "FileStream/web/tools/export_ad_documents.py"
)


def test_ad_origin_and_slot_validation():
    settings = web_ads_settings()
    for origin in [
        "",
        "https://user:secret@ads.test",
        "http://ads.test",
        "https://ads.test/path",
        "https://ads.test?x=1",
        "https://ads.test/#x",
        "https://ads.test:bad",
        "https://ads.test/\n",
        "https://ads.test\\@evil.test",
    ]:
        assert not https_origin(origin)
    assert https_origin("https://ADS.test:443/") == "https://ads.test"
    for values in [
        {"WEB_ADS_DOCUMENT_ORIGIN": ""},
        {"WEB_ADS_DOCUMENT_ORIGIN": "http://ads.test"},
    ]:
        cfg = web_ads_settings(values).template_context()
        assert not cfg["has_any_banner"]
    slot = settings.template_context()["mobile"]["top_banner"]
    for changes in [
        dict(key="</script>"),
        dict(width=0),
        dict(height=9000),
        dict(width="320"),
        dict(invoke_url="javascript:alert(1)"),
        dict(invoke_url="https://user:pass@ads.test/invoke"),
        dict(invoke_url="https://ads.test/white space"),
    ]:
        with pytest.raises(ValueError):
            banner_options({**slot, **changes})
    assert document_url("https://ad-documents.test", slot).endswith(
        banner_id(slot) + ".html"
    )


def test_public_export_exact_slots_and_no_credentials(tmp_path):
    settings = web_ads_settings(
        {
            "DATABASE_URL": "private-database-marker",
            "BOT_TOKEN": "private-token-marker",
            "WEB_ADS_MOBILE_TOP_BANNER_KEY": "mobile",
            "WEB_ADS_MOBILE_TOP_BANNER_INVOKE_URL": "https://provider.test/mobile.js",
            "WEB_ADS_MOBILE_BOTTOM_BANNER_KEY": "bottom",
            "WEB_ADS_MOBILE_BOTTOM_BANNER_INVOKE_URL": "https://provider.test/bottom.js",
        },
        {2: {"WEB_ADS_MOBILE_BOTTOM_BANNER_KEY": "bot-bottom"}},
    )
    out = tmp_path / "bundle"
    count = exporter.export_documents(settings, out, ["https://app.test"])
    cfg = settings.template_context()
    assert count == 5  # four independent defaults plus one bot override
    mobile = cfg["mobile"]["top_banner"]
    document = (out / "html/banner" / (banner_id(mobile) + ".html")).read_text()
    assert document.index("var atOptions=") < document.index(
        'src="https://provider.test/mobile.js"'
    )
    assert "async" not in document and "defer" not in document
    assert "<body><script>" in document and "sandbox=" not in document
    assert "postMessage({type:'filestream-ad',state},target)" in document
    exported = "\n".join(p.read_text() for p in out.rglob("*") if p.is_file())
    for secret in (
        "private-database-marker",
        "private-token-marker",
        "smartlink",
        "popunder",
        "social_bar",
    ):
        assert secret not in exported
    assert "__PARENT_ORIGINS__" not in exported
    assert "frame-ancestors https://app.test" in exported
    with pytest.raises(ValueError, match="empty"):
        exporter.export_documents(settings, out, ["https://app.test"])
    with pytest.raises(ValueError, match="Distinct"):
        exporter.export_documents(
            settings, tmp_path / "bad", ["https://ad-documents.test"]
        )
    same_host = web_ads_settings({"WEB_ADS_DOCUMENT_ORIGIN": "https://app.test:8443"})
    with pytest.raises(ValueError, match="Distinct"):
        exporter.export_documents(
            same_host, tmp_path / "port-only", ["https://app.test"]
        )
    off = web_ads_settings({"WEB_ADS_ENABLED": "False"})
    assert (
        exporter.export_documents(off, tmp_path / "disabled", ["https://app.test"]) == 0
    )
    assert not list((tmp_path / "disabled/html/banner").glob("*.html"))


def test_ad_service_has_no_application_or_proxy_network_mounts():
    text = (ROOT / "docker-compose.yml").read_text()
    service = text.split("\n  ad-documents:\n", 1)[1].split("\n  traefik:", 1)[0]
    assert "env_file:" not in service and "environment:" not in service
    assert "docker.sock" not in service and "/app" not in service
    assert "sessions" not in service and "database" not in service
    assert "      - proxy" not in service and "internal: true" in text
    assert "security_opt:" in service
    assert "connect" in (ROOT / "deploy/ads/no-egress.c").read_text()
    assert "cap_drop: [ALL]" in service and "read_only: true" in service
    nginx = (ROOT / "deploy/ads/nginx.conf").read_text()
    assert "proxy_pass" not in nginx and "fastcgi_pass" not in nginx
    assert "if ($ad_sensitive_cookie)" in nginx and "return 409" in nginx
    assert '$http_cookie != ""' not in nginx
    assert "access_log off" in nginx


@pytest.mark.parametrize(
    "header",
    [
        "",
        "ad_seen=public",
        "uuid=public; frequency=public",
        "_ga=public",
        "ad_seen=fs_page_session",
        "ad_seen=auth",
    ],
)
def test_benign_cookie_names_do_not_block_ads(header):
    import re
    from FileStream.web.ad_documents import sensitive_cookie_pattern

    assert re.search(sensitive_cookie_pattern(), header, re.I) is None


@pytest.mark.parametrize(
    "header",
    [
        "fs_page_session=public",
        "__Host-fs_page_session=public",
        "__Host-Http-fs_page_session=public",
        "__Secure-filestream_auth=public",
        "fs_future_auth=public",
        "ad_seen=public; fs_page_session=public",
        "fs_page_session=public; fs_page_session=other",
        "ad_seen=public; __Host-fs_page_session=public; ad_seen=other",
        "PAGE_TOKEN=public",
        "CF_Authorization=public",
        "access_token=public",
    ],
)
def test_protected_cookie_names_block_even_duplicates_and_shadowing(header):
    import re
    from FileStream.web.ad_documents import sensitive_cookie_pattern

    assert re.search(sensitive_cookie_pattern(), header, re.I) is not None


def test_additional_private_cookie_inventory_is_validated():
    import re
    from FileStream.web.ad_documents import sensitive_cookie_pattern

    assert re.search(
        sensitive_cookie_pattern(["other_app_identity"]),
        "other_app_identity=public",
        re.I,
    )
    with pytest.raises(ValueError):
        sensitive_cookie_pattern(["invalid;name=value"])


def test_every_filestream_cookie_writer_stays_in_the_protected_inventory():
    import ast
    import re
    from types import SimpleNamespace
    from FileStream.web.ad_documents import sensitive_cookie_pattern

    source = ast.parse((ROOT / "FileStream/server/stream_routes.py").read_text())
    assignment = next(
        n
        for n in source.body
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "PAGE_SESSION_COOKIE" for t in n.targets
        )
    )
    for secure in (False, True):
        ns = {"Server": SimpleNamespace(HAS_SSL=secure)}
        exec(
            compile(
                ast.Module(body=[assignment], type_ignores=[]),
                "cookie-inventory",
                "exec",
            ),
            ns,
        )
        assert re.search(
            sensitive_cookie_pattern(), ns["PAGE_SESSION_COOKIE"] + "=public", re.I
        )
    for path in (ROOT / "FileStream").rglob("*.py"):
        for call in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "set_cookie"
            ):
                assert (
                    call.args
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == "PAGE_SESSION_COOKIE"
                ), "Review any new cookie writer against the ad-service credential inventory"
                assert not any(k.arg == "domain" for k in call.keywords)
