"""Two-service rehearsal: isolated loopback TLS and a synthetic application.

No production Docker socket, certificates, credentials, networks or volumes.
The real application image is built; Telegram/MongoDB startup is not simulated as
successful authorization. The HTTP stub tests topology/readiness/update only.
"""

import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
import urllib.request
import uuid
import pytest
from web_fixtures import ROOT


def test_two_service_source_and_persistent_names():
    text = (ROOT / "docker-compose.yml").read_text()
    assert (
        "ad-export" not in text
        and "ad-documents" not in text
        and "ad-public" not in text
    )
    assert "--providers.file." not in text
    assert "filestream-cache" in text and "letsencrypt" in text
    assert "filestreambot-proxy" in text and "MULTI_TOKEN20_FQDN" in text
    assert "deployment" not in (ROOT / "Dockerfile").read_text()


@pytest.mark.skipif(
    os.environ.get("RUN_COMPOSE_TESTS") != "1",
    reason="Opt-in isolated Docker rehearsal",
)
def test_two_services_fresh_build_https_update_and_state(tmp_path):
    stage = tmp_path / "checkout"
    shutil.copytree(
        ROOT,
        stage,
        ignore=shutil.ignore_patterns(
            ".git",
            ".env*",
            "*.env",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            "venv",
            "test",
            ".agents",
            ".codex",
        ),
    )
    (stage / ".env").write_text(
        "FQDN=app.invalid\nACME_EMAIL=test@example.invalid\nWEB_ADS_ENABLED=False\n"
    )
    project = "fs-final-" + uuid.uuid4().hex[:10]

    def run(args, check=True):
        return subprocess.run(
            ["sudo", "-n", "docker", "compose", "-p", project, *args],
            cwd=stage,
            text=True,
            capture_output=True,
            check=check,
        )

    model = json.loads(run(["config", "--format", "json"]).stdout)
    assert set(model["services"]) == {"filestreambot", "traefik"}
    assert set(model["volumes"]) == {
        "filestream-cache",
        "letsencrypt",
        "filestream-sessions",
    }
    assert model["networks"]["proxy"]["name"] == "filestreambot-proxy"
    app = model["services"]["filestreambot"]
    traefik = model["services"]["traefik"]
    app["image"] = project + "-app"
    app["labels"] = {}
    app["command"] = [
        "python",
        "-u",
        "-c",
        "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write(b'{\"status\":\"healthy\"}')\n def log_message(self,*args):pass\nHTTPServer(('0.0.0.0',8080),H).serve_forever()",
    ]
    # Test-only bind state instead of creating/deleting named Docker volumes.
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    cache = tmp_path / "cache"
    tls = tmp_path / "tls"
    cache.mkdir()
    tls.mkdir()
    app["volumes"] = [
        dict(type="bind", source=str(cache), target="/tmp/filestream_stream_cache"),
        dict(type="bind", source=str(sessions), target="/sessions"),
    ]
    for path in ("cert.pem", "key.pem"):
        assert not (tmp_path / path).exists()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=app.invalid",
            "-addext",
            "subjectAltName=DNS:app.invalid",
            "-keyout",
            str(tmp_path / "key.pem"),
            "-out",
            str(tmp_path / "cert.pem"),
        ],
        check=True,
        capture_output=True,
    )
    (tmp_path / "key.pem").chmod(0o600)
    routing = {
        "http": {
            "routers": {
                "app": {
                    "rule": "Host(`app.invalid`)",
                    "entryPoints": ["websecure"],
                    "tls": {},
                    "service": "app",
                }
            },
            "services": {
                "app": {
                    "loadBalancer": {"servers": [{"url": "http://filestreambot:8080"}]}
                }
            },
        },
        "tls": {
            "certificates": [
                {"certFile": "/fixture/cert.pem", "keyFile": "/fixture/key.pem"}
            ]
        },
    }
    (tmp_path / "routing.yml").write_text(json.dumps(routing))
    traefik["command"] = [
        "--entrypoints.websecure.address=:8443",
        "--providers.file.filename=/fixture/routing.yml",
    ]
    traefik["ports"] = [
        dict(target=8443, published="0", host_ip="127.0.0.1", protocol="tcp")
    ]
    traefik["volumes"] = [
        dict(type="bind", source=str(tmp_path), target="/fixture", read_only=True),
        dict(type="bind", source=str(tls), target="/letsencrypt"),
    ]
    model["volumes"] = {}
    model["networks"]["proxy"]["name"] = project + "-proxy"
    (stage / "docker-compose.yml").write_text(json.dumps(model))
    try:
        run(["config", "--quiet"])
        run(
            ["up", "-d", "--build"]
        )  # Exact normal deployment command, disposable project.
        address = run(["port", "traefik", "8443"]).stdout.strip()
        request = urllib.request.Request(
            "https://" + address + "/health", headers={"Host": "app.invalid"}
        )
        for attempt in range(40):
            try:
                with urllib.request.urlopen(
                    request, context=ssl._create_unverified_context(), timeout=3
                ) as response:
                    assert json.load(response)["status"] == "healthy"
                break
            except Exception:
                if attempt == 39:
                    raise
                time.sleep(0.5)
        (cache / "marker").write_text("synthetic-cache")
        (sessions / "marker").write_text("synthetic-session")
        (tls / "marker").write_text("synthetic-tls")
        before = run(["ps", "-q", "traefik"]).stdout
        model["services"]["filestreambot"]["environment"]["TEST_REVISION"] = "two"
        (stage / "docker-compose.yml").write_text(json.dumps(model))
        run(["up", "-d", "--build"])
        assert before == run(["ps", "-q", "traefik"]).stdout
        assert (cache / "marker").read_text() == "synthetic-cache"
        assert (sessions / "marker").read_text() == "synthetic-session"
        assert (tls / "marker").read_text() == "synthetic-tls"
        assert (
            run(
                [
                    "exec",
                    "-T",
                    "filestreambot",
                    "python",
                    "-c",
                    'from FileStream.config import WebAds; assert not WebAds.ENABLED; from FileStream.web.direct_ads import banner; from pathlib import Path; assert not Path("/app/.env").exists(); assert not Path("/app/tests").exists()',
                ]
            ).returncode
            == 0
        )
    finally:
        # Only this random test project; no -v and no production volumes exist here.
        run(["down"], check=False)
