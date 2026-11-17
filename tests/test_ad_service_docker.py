"""Opt-in runtime security checks: AD_SERVICE_TEST_IMAGE=filestream-ad-documents:test."""

import os
import shlex
import subprocess
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pytest
from web_fixtures import load_public_module, web_ads_settings


def test_static_service_http_cookie_gate_and_kernel_egress_boundary(tmp_path):
    image = os.environ.get("AD_SERVICE_TEST_IMAGE")
    if not image:
        pytest.skip("Set AD_SERVICE_TEST_IMAGE after building deploy/ads")
    docker = shlex.split(os.environ.get("AD_SERVICE_DOCKER", "docker"))
    exporter = load_public_module(
        "ad_exporter_runtime", "FileStream/web/tools/export_ad_documents.py"
    )
    bundle = tmp_path / "public"
    exporter.export_documents(web_ads_settings(), bundle, ["https://app.test"])

    def run(*args, **kwargs):
        return subprocess.run(
            [*docker, *args], text=True, capture_output=True, **kwargs
        )

    result = run(
        "run",
        "-d",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "101:101",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m,uid=101,gid=101",
        "-p",
        "127.0.0.1::8080",
        "-v",
        str(bundle / "html") + ":/usr/share/nginx/html:ro",
        "-v",
        str(bundle / "nginx.conf") + ":/etc/nginx/ad-documents.conf:ro",
        image,
    )
    assert result.returncode == 0, result.stderr
    cid = result.stdout.strip()
    try:
        ports = run("port", cid, "8080").stdout.strip()
        assert ports, run("logs", cid).stderr
        port = ports.rsplit(":", 1)[1]
        base = "http://127.0.0.1:" + port
        for _ in range(30):
            try:
                with urlopen(base + "/health", timeout=1) as r:
                    assert r.read() == b"ok\n"
                break
            except (URLError, OSError):
                time.sleep(0.1)
        else:
            pytest.fail(run("logs", cid).stderr)
        slots = sorted((bundle / "html/banner").glob("*.html"))
        for slot in slots:
            with urlopen(base + "/banner/" + slot.name, timeout=2) as r:
                assert r.read() == slot.read_bytes()
                assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
                assert (
                    "frame-ancestors https://app.test"
                    in r.headers["Content-Security-Policy"]
                )
                assert r.headers.get("Set-Cookie") is None
        # Repeat the same document with harmless advertising cookies: no auth/state use.
        for cookie in (
            "ad_seen=public-test",
            "frequency=public-test; uuid=public-test",
            "ad_seen=fs_page_session",
        ):
            with urlopen(
                Request(base + "/banner/" + slots[0].name, headers={"Cookie": cookie}),
                timeout=2,
            ) as r:
                assert r.status == 200 and r.read() == slots[0].read_bytes()
                assert r.headers["X-Ad-Protected-Cookie"] == "0"
        for cookie in (
            "__Host-fs_page_session=public-test",
            "ad_seen=public-test; fs_page_session=public-test",
            "fs_page_session=public-test; fs_page_session=other",
            "access_token=public-test",
        ):
            with pytest.raises(HTTPError) as blocked_cookie:
                urlopen(
                    Request(
                        base + "/banner/" + slots[0].name, headers={"Cookie": cookie}
                    ),
                    timeout=2,
                )
            assert blocked_cookie.value.code == 409
            assert blocked_cookie.value.headers["X-Ad-Protected-Cookie"] == "1"
            assert b"public-test" not in blocked_cookie.value.read()
        for path, headers, code in [
            (
                "/banner/" + slots[0].name,
                {"Cookie": "fs_page_session=public-test"},
                409,
            ),
            ("/banner/" + slots[0].name + "?private=marker", {}, 404),
            ("/banner/not-a-slot.html", {}, 404),
            ("/.env", {}, 404),
            ("/etc/passwd", {}, 404),
        ]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(base + path, headers=headers), timeout=2)
            assert error.value.code == code
            assert b"invoke.js" not in error.value.read()
        status = run("exec", cid, "cat", "/proc/1/status").stdout
        assert "Uid:\t101\t101" in status and "Seccomp:\t2" in status
        # The same entrypoint filter runs before Nginx and survives exec/fork.
        blocked = run(
            "run",
            "--rm",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            image,
            "wget",
            "-T",
            "2",
            "-O",
            "-",
            "http://127.0.0.1:8080/health",
        )
        assert (
            blocked.returncode != 0 and "Operation not permitted" in blocked.stderr
        ), blocked.stderr
    finally:
        run("rm", "-f", cid)
