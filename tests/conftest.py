"""Synthetic hosts for offline provider tests; never an environment authorization."""

import pytest
from web_fixtures import load_public_module
import sys


@pytest.fixture(autouse=True)
def synthetic_provider_policy(monkeypatch):
    policy = sys.modules["FileStream.web.direct_ads"]
    monkeypatch.setattr(
        policy, "SCRIPT_HOSTS", policy.SCRIPT_HOSTS | {"provider.test", "ads.test"}
    )
    monkeypatch.setattr(
        policy,
        "SMARTLINK_HOSTS",
        policy.SMARTLINK_HOSTS | {"provider.test", "ads.test"},
    )
