"""Test A contract. All provider responses are local mocks, never paid impressions."""

import pytest
from web_fixtures import web_ads_settings, environment, ROOT

FIELDS = dict(
    KEY="syntheticZone",
    INVOKE_URL="https://bellnewyork.org/22/syntheticZone",
    WIDTH="300",
    HEIGHT="250",
)


def only_bottom(**extra):
    return web_ads_settings(
        {
            "WEB_ADS_ENABLED": "True",
            **{"WEB_ADS_MOBILE_BOTTOM_BANNER_" + k: v for k, v in FIELDS.items()},
            **extra,
        }
    )


@pytest.mark.parametrize(
    "value", [None, "", "False", "false", "0", "yes", "on", "garbage", "2"]
)
def test_global_missing_invalid_or_false_disables_every_format(value):
    values = {"WEB_ADS_MOBILE_BOTTOM_BANNER_" + k: v for k, v in FIELDS.items()}
    values.update(
        WEB_ADS_POPUNDER_URL="https://accountut.com/pop.js",
        WEB_ADS_DESKTOP_SOCIAL_BAR_URL="https://bellnewyork.org/social.js",
        WEB_ADS_MOBILE_SOCIAL_BAR_URL="https://bellnewyork.org/social.js",
        WEB_ADS_SMARTLINK_URL="https://ardance.org/synthetic",
    )
    if value is not None:
        values["WEB_ADS_ENABLED"] = value
    c = web_ads_settings(values).template_context(enabled_override=True)
    assert not c["enabled"] and not c["has_any_banner"]
    assert (
        not c["popunder_url"] and not c["has_any_social_bar"] and not c["smartlink_url"]
    )


@pytest.mark.parametrize("value", ["True", "true", "TRUE", "1", " True "])
def test_master_true_and_only_bottom_complete(value):
    c = only_bottom(WEB_ADS_ENABLED=value).template_context()
    assert c["enabled"] and c["mobile"]["bottom_banner"]["enabled"]
    assert not c["mobile"]["top_banner"]["enabled"]
    assert (
        not c["desktop"]["top_banner"]["enabled"]
        and not c["desktop"]["inline_banner"]["enabled"]
    )
    assert (
        not c["popunder_url"] and not c["has_any_social_bar"] and not c["smartlink_url"]
    )


@pytest.mark.parametrize("field", list(FIELDS))
def test_each_required_field_missing_fails_closed(field, caplog):
    c = only_bottom(**{"WEB_ADS_MOBILE_BOTTOM_BANNER_" + field: ""}).template_context()
    assert not c["has_any_banner"]
    assert "incomplete or invalid configuration" in caplog.text
    assert FIELDS["KEY"] not in caplog.text and FIELDS["INVOKE_URL"] not in caplog.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("KEY", "</script><script>alert(1)</script>"),
        ("INVOKE_URL", "https://bellnewyork.org/22/other"),
        ("INVOKE_URL", "https://evil.test/22/syntheticZone"),
        ("INVOKE_URL", "javascript:alert(1)"),
        ("INVOKE_URL", 'https://bellnewyork.org/22/syntheticZone" onclick="alert(1)'),
        ("WIDTH", "invalid"),
        ("WIDTH", "0"),
        ("HEIGHT", "-1"),
        ("HEIGHT", "999999"),
    ],
)
def test_bad_field_disables_only_configured_placement(field, value):
    assert not only_bottom(
        **{"WEB_ADS_MOBILE_BOTTOM_BANNER_" + field: value}
    ).template_context()["has_any_banner"]


def test_no_mode_or_external_allowlist_can_change_policy():
    c = only_bottom(
        WEB_ADS_MODE="provider_iframe",
        WEB_ADS_SCRIPT_HOSTS="evil.test",
        WEB_ADS_MOBILE_BOTTOM_BANNER_INVOKE_URL="https://evil.test/22/syntheticZone",
    ).template_context()
    assert not c["has_any_banner"]
    assert "mode" not in c and "document_url" not in c["mobile"]["bottom_banner"]


def test_optional_presence_and_admin_disable():
    ads = only_bottom(
        WEB_ADS_POPUNDER_URL="https://accountut.com/pop.js",
        WEB_ADS_DESKTOP_SOCIAL_BAR_URL="https://bellnewyork.org/social.js",
        WEB_ADS_SMARTLINK_URL="https://ardance.org/synthetic",
    )
    c = ads.template_context()
    assert c["popunder_url"] and c["has_any_social_bar"] and c["smartlink_url"]
    assert not ads.template_context(enabled_override=False)["enabled"]
    ads.ENABLED = False
    assert not ads.template_context(enabled_override=True)["enabled"]


@pytest.mark.asyncio
async def test_global_off_and_empty_configuration_emit_no_ad_runtime_or_containers():
    e = environment(True)
    for settings in (
        web_ads_settings(),
        web_ads_settings({"WEB_ADS_ENABLED": "True"}),
        only_bottom(WEB_ADS_ENABLED="False"),
    ):
        e.render["WebAds"] = settings
        html = await e.render["render_landing_page"](bot=e.bot)
        assert "data-ad-placement=" not in html and "data-sponsored-link" not in html
        assert (
            'src="/static/js/direct-ads.js"' not in html
            and 'id="ad-config"' not in html
        )


def test_source_runtime_has_no_retired_imports_or_ad_options():
    forbidden = (
        "WEB_ADS_MODE",
        "WEB_ADS_DOCUMENT_ORIGIN",
        "WEB_ADS_SCRIPT_HOSTS",
        "WEB_ADS_DIRECT_JS_ACCEPT_RISKS",
        "WEB_ADS_POPUNDER_ENABLED",
        "WEB_ADS_SOCIAL_BAR_ENABLED",
        "WEB_ADS_SMARTLINK_ENABLED",
        "WEB_ADS_SENSITIVE_COOKIE_NAMES",
    )
    for name in [
        "FileStream/config.py",
        "example.env",
        ".env.example",
        "docker-compose.yml",
        "Dockerfile",
    ]:
        text = (ROOT / name).read_text()
        assert all(x not in text for x in forbidden)
    assert not (ROOT / "FileStream/web/ad_documents.py").exists()
    assert not (ROOT / "FileStream/web/provider_iframes.py").exists()
    assert "ad-export" not in (ROOT / "docker-compose.yml").read_text()


@pytest.mark.asyncio
async def test_actual_health_handler_reports_database_failures_without_details(
    monkeypatch,
):
    import sys, json, time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from web_fixtures import load_functions

    db = SimpleNamespace(ping=AsyncMock())
    monkeypatch.setitem(
        sys.modules,
        "FileStream.services.storage_service",
        SimpleNamespace(
            storage_service=SimpleNamespace(
                get_metrics=lambda: {"active_clients": 1, "storage_workers": 1}
            )
        ),
    )
    scope = load_functions(
        "FileStream/server/stream_routes.py",
        names={"health_route_handler"},
        db=db,
        multi_clients={0: object()},
        utils=SimpleNamespace(get_readable_time=lambda _: "synthetic uptime"),
        StartTime=time.time(),
        __version__="test",
    )
    response = await scope["health_route_handler"](None)
    assert response.status == 200 and json.loads(response.text)["database"]["connected"]
    db.ping.side_effect = RuntimeError("do not expose database credentials")
    response = await scope["health_route_handler"](None)
    assert response.status == 503 and "credentials" not in response.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("BOT_TOKEN", ""),
        ("API_HASH", "invalid-private-value"),
        ("DATABASE_URL", "https://private-value.invalid"),
        ("FQDN", "bad`host"),
        ("API_ID", "0"),
    ],
)
def test_startup_credential_checks_remain_redacted_before_client_initialization(
    field, value
):
    import re
    from web_fixtures import load_functions

    values = dict(
        API_ID="12345",
        API_HASH="a" * 32,
        BOT_TOKEN="12345:synthetic",
        DATABASE_URL="mongodb://db.invalid/test",
        FQDN="app.invalid",
    )
    values[field] = value
    check = load_functions(
        "FileStream/config.py", names={"validate_credentials"}, env=values, re=re
    )["validate_credentials"]
    with pytest.raises(ValueError) as error:
        check()
    assert field in str(error.value)
    assert "private-value" not in str(error.value)


def test_startup_valid_credentials_do_not_contact_external_services():
    import re
    from web_fixtures import load_functions

    check = load_functions(
        "FileStream/config.py",
        names={"validate_credentials"},
        env=dict(
            API_ID="12345",
            API_HASH="a" * 32,
            BOT_TOKEN="12345:synthetic",
            DATABASE_URL="mongodb://db.invalid/test",
            FQDN="app.invalid",
        ),
        re=re,
    )["validate_credentials"]
    assert check() is None


def test_multi_client_string_index_preserves_zone_overrides_and_master_priority():
    from types import SimpleNamespace

    ads = only_bottom()
    ads.BOT_OVERRIDES = {
        2: {
            "WEB_ADS_MOBILE_BOTTOM_BANNER_KEY": "botZone",
            "WEB_ADS_MOBILE_BOTTOM_BANNER_INVOKE_URL": "https://bellnewyork.org/22/botZone",
        }
    }
    c = ads.template_context(bot=SimpleNamespace(client_index="2"))
    assert c["mobile"]["bottom_banner"]["key"] == "botZone" and c["has_any_banner"]
    ads.ENABLED = False
    assert not ads.template_context(
        enabled_override=True, bot=SimpleNamespace(client_index="2")
    )["enabled"]
