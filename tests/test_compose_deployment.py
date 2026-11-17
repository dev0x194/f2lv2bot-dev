"""Opt-in isolated deployment rehearsal; never attaches to the live project.

RUN_COMPOSE_TESTS=1 uses sudo Docker. External Telegram/MongoDB are replaced
with a sleeping application command; exporter, Nginx, security and readiness
are the production implementation. No public ports, ACME or Docker socket.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    os.environ.get("RUN_COMPOSE_TESTS") != "1",
    reason="Opt-in isolated Docker rehearsal",
)
def test_single_compose_install_update_and_failure(tmp_path):
    stage = tmp_path / "checkout"
    shutil.copytree(
        ROOT,
        stage,
        ignore=shutil.ignore_patterns(
            ".git",
            ".env",
            "*.env",
            "__pycache__",
            ".pytest_cache",
            "test",
            ".agents",
            ".codex",
        ),
    )
    project = "fs-rehearsal-" + uuid.uuid4().hex[:10]
    env = (
        "API_ID=12345\nAPI_HASH=" + "a" * 32 + "\nBOT_TOKEN=12345:synthetic\n"
        "DATABASE_URL=mongodb://synthetic.invalid/test\nFQDN=app.invalid\n"
        "ACME_EMAIL=test@example.invalid\nWEB_ADS_DOCUMENT_ORIGIN=https://ads.invalid\n"
        "WEB_ADS_ENABLED=True\nWEB_ADS_MOBILE_BOTTOM_BANNER_KEY=syntheticBottom\n"
    )
    (stage / ".env").write_text(env)
    docker = ["sudo", "-n", "docker"]
    base = docker + ["compose", "-p", project]

    def run(args, check=True):
        return subprocess.run(
            base + args,
            cwd=stage,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
        )

    config = json.loads(run(["config", "--format", "json"]).stdout)
    services = config["services"]
    # Only substitute external processes, exposed resources and globally named tags.
    services["ad-documents"]["image"] = project + "-ads"
    services["ad-export"]["image"] = project + "-export"
    services["filestreambot"]["image"] = project + "-app"
    services["filestreambot"]["labels"] = {}
    services["filestreambot"]["build"]["target"] = "ad-export"
    services["filestreambot"]["entrypoint"] = [
        "python3",
        "-m",
        "FileStream.web.tools.deployment",
        "application",
    ]
    services["filestreambot"]["command"] = [
        "python3",
        "-c",
        "import time; time.sleep(3600)",
    ]
    traefik = services["traefik"]
    traefik["command"] = [
        "--entrypoints.web.address=:8088",
        "--providers.file.directory=/ad-public/traefik",
        "--providers.file.watch=true",
    ]
    traefik.pop("ports", None)
    traefik["volumes"] = [v for v in traefik["volumes"] if v["type"] == "volume"]
    for name in config["networks"]:
        config["networks"][name]["name"] = project + "-" + name
    for name in config["volumes"]:
        config["volumes"][name]["name"] = project + "-" + name
    # Keep env_file dynamic: `config` resolves it into environment by default.
    for name in ("ad-export", "filestreambot"):
        services[name]["env_file"] = [".env"]
        services[name]["environment"] = {"HAS_SSL": "True", "NO_PORT": "True"}
    (stage / "docker-compose.yml").write_text(json.dumps(config))
    try:
        (stage / ".env").write_text(
            env.replace("BOT_TOKEN=12345:synthetic", "BOT_TOKEN=")
        )
        assert run(["up", "-d", "--build"], check=False).returncode != 0
        assert not run(
            ["ps", "-q", "--status", "running", "ad-documents", "filestreambot"]
        ).stdout.strip()
        (stage / ".env").write_text(env)
        run(["up", "-d", "--build"])

        def state(service):
            cid = run(["ps", "-aq", service]).stdout.strip()
            return json.loads(subprocess.check_output(docker + ["inspect", cid]))[0]

        assert state("ad-export")["State"]["ExitCode"] == 0
        assert state("ad-documents")["State"]["Health"]["Status"] == "healthy"
        assert state("filestreambot")["State"]["Running"]
        ad = state("ad-documents")
        assert not any(
            "BOT_TOKEN=" in item or "DATABASE_URL=" in item
            for item in ad["Config"]["Env"]
        )
        assert ad["HostConfig"]["ReadonlyRootfs"]
        assert ad["Config"]["Entrypoint"] == ["/usr/local/bin/no-egress"]
        assert all(m["Type"] != "bind" for m in ad["Mounts"])
        # Inspect all public documents and exercise cookie handling without values in logs.
        listing = run(
            [
                "exec",
                "-T",
                "ad-documents",
                "sh",
                "-c",
                "ls /ad-public/current/html/banner/*.html",
            ]
        ).stdout.splitlines()
        assert len(listing) == 4
        for filename in listing:
            response = run(
                [
                    "exec",
                    "-T",
                    "ad-documents",
                    "wget",
                    "-qO-",
                    "http://127.0.0.1:8080/banner/" + Path(filename).name,
                ]
            ).stdout
            assert "atOptions" in response
        url = "http://127.0.0.1:8080/banner/" + Path(listing[0]).name
        assert (
            run(
                [
                    "exec",
                    "-T",
                    "ad-documents",
                    "wget",
                    "-qO-",
                    "--header=Cookie: harmless=synthetic",
                    url,
                ]
            ).returncode
            == 0
        )
        assert (
            run(
                [
                    "exec",
                    "-T",
                    "ad-documents",
                    "wget",
                    "-qO-",
                    "--header=Cookie: harmless=x; __Host-fs_page_session=synthetic",
                    url,
                ],
                check=False,
            ).returncode
            != 0
        )
        old_revision = run(
            ["exec", "-T", "ad-documents", "readlink", "/ad-public/current"]
        ).stdout.strip()
        traefik_id = state("traefik")["Id"]
        run(["up", "-d", "--build"])
        assert state("ad-export")["State"]["ExitCode"] == 0
        assert state("traefik")["Id"] == traefik_id
        changed = (
            env
            + "WEB_ADS_MOBILE_TOP_BANNER_KEY=updatedKey\nWEB_ADS_MOBILE_TOP_BANNER_WIDTH=321\n"
        )
        (stage / ".env").write_text(changed)
        run(["up", "-d", "--build"])
        new_revision = run(
            ["exec", "-T", "ad-documents", "readlink", "/ad-public/current"]
        ).stdout.strip()
        assert old_revision != new_revision
        assert state("ad-documents")["State"]["Health"]["Status"] == "healthy"
        assert state("filestreambot")["State"]["Running"]
        assert state("traefik")["Id"] == traefik_id
        # Returning to a previous configuration must retain intervening URLs.
        updated_urls = run(
            [
                "exec",
                "-T",
                "ad-documents",
                "sh",
                "-c",
                "ls /ad-public/current/html/banner/*.html",
            ]
        ).stdout.splitlines()
        (stage / ".env").write_text(env)
        run(["up", "-d", "--build"])
        for filename in updated_urls:
            assert (
                run(
                    [
                        "exec",
                        "-T",
                        "ad-documents",
                        "wget",
                        "-qO-",
                        "http://127.0.0.1:8080/banner/" + Path(filename).name,
                    ]
                ).returncode
                == 0
            )
        (stage / ".env").write_text(changed)
        run(["up", "-d", "--build"])
        # Source-only rebuild must publish a new bundle too.
        source = stage / "FileStream/web/ad_documents.py"
        source.write_text(source.read_text() + "\n# Synthetic update rehearsal\n")
        run(["up", "-d", "--build"])
        source_revision = run(
            ["exec", "-T", "ad-documents", "readlink", "/ad-public/current"]
        ).stdout.strip()
        assert source_revision != new_revision
        new_revision = source_revision
        assert state("traefik")["Id"] == traefik_id
        # An invalid export leaves the known-good current symlink untouched.
        (stage / ".env").write_text(
            changed + "WEB_ADS_MOBILE_TOP_BANNER_HEIGHT=invalid\n"
        )
        failed = run(["up", "-d", "--build"], check=False)
        assert failed.returncode != 0
        assert state("ad-export")["State"]["ExitCode"] != 0
        preserved = run(
            [
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "readlink",
                "ad-documents",
                "/ad-public/current",
            ]
        ).stdout
        assert new_revision in preserved
        (stage / ".env").write_text(changed)
        run(["up", "-d", "--build"])
        assert state("filestreambot")["State"]["Running"]
        (stage / ".env").write_text(
            changed.replace("BOT_TOKEN=12345:synthetic", "BOT_TOKEN=")
        )
        assert run(["up", "-d", "--build"], check=False).returncode != 0
        assert state("ad-export")["State"]["ExitCode"] != 0
        # Advertising disabled still publishes a valid empty current bundle.
        (stage / ".env").write_text(
            changed.replace("WEB_ADS_ENABLED=True", "WEB_ADS_ENABLED=False")
        )
        run(["up", "-d", "--build"])
        manifest = json.loads(
            run(
                ["exec", "-T", "ad-documents", "cat", "/ad-public/current/public.json"]
            ).stdout
        )
        assert manifest["documents"] == 0
        (stage / ".env").write_text(changed)
        run(["up", "-d", "--build"])

        # Rehearse a legacy host-bundle/two-file layout on this test project only.
        from web_fixtures import load_public_module, web_ads_settings

        exporter = load_public_module(
            "migration_exporter", "FileStream/web/tools/export_ad_documents.py"
        )
        legacy_bundle = stage / "legacy-public"
        exporter.export_documents(
            web_ads_settings(), legacy_bundle, ["https://app.invalid"]
        )
        before = {
            str(p.relative_to(legacy_bundle)): p.read_bytes()
            for p in legacy_bundle.rglob("*")
            if p.is_file()
        }
        legacy = json.loads(json.dumps(config))
        legacy["services"].pop("ad-export")
        for service in legacy["services"].values():
            service.pop("depends_on", None)
        legacy["services"]["filestreambot"]["entrypoint"] = [
            "python3",
            "-c",
            "import time; time.sleep(3600)",
        ]
        legacy["services"]["filestreambot"]["command"] = []
        old_ad = legacy["services"]["ad-documents"]
        old_ad.pop("healthcheck")
        old_ad["command"] = [
            "nginx",
            "-c",
            "/etc/nginx/ad-documents.conf",
            "-g",
            "daemon off;",
        ]
        old_ad["volumes"] = [
            str(legacy_bundle / "html") + ":/usr/share/nginx/html:ro",
            str(legacy_bundle / "nginx.conf") + ":/etc/nginx/ad-documents.conf:ro",
        ]
        old_ad["labels"] = {
            "traefik.enable": "true",
            "traefik.http.routers.ad-documents.rule": "Host(`ads.invalid`)",
        }
        legacy["services"]["traefik"]["command"] = ["--entrypoints.web.address=:8088"]
        (stage / "legacy.yml").write_text(json.dumps(legacy))
        (stage / "legacy-ads.yml").write_text(
            json.dumps({"services": {"ad-documents": {"restart": "unless-stopped"}}})
        )
        run(
            [
                "exec",
                "-T",
                "filestreambot",
                "python3",
                "-c",
                "from pathlib import Path; Path('/tmp/filestream_stream_cache/rehearsal-marker').write_text('synthetic-cache-marker')",
            ]
        )
        run(["down", "--remove-orphans"])
        run(["-f", "legacy.yml", "-f", "legacy-ads.yml", "up", "-d"])
        # Synthetic certificate marker demonstrates the named volume survives.
        run(
            [
                "exec",
                "-T",
                "traefik",
                "sh",
                "-c",
                "echo synthetic-certificate-marker > /letsencrypt/rehearsal-marker",
            ]
        )
        run(
            [
                "-f",
                "legacy.yml",
                "-f",
                "legacy-ads.yml",
                "stop",
                "ad-documents",
                "filestreambot",
            ]
        )
        run(["up", "-d", "--build"])
        assert state("ad-documents")["State"]["Health"]["Status"] == "healthy"
        assert state("filestreambot")["State"]["Running"]
        assert state("ad-documents")["Config"]["Labels"].get("traefik.enable") != "true"
        assert (
            "synthetic-certificate-marker"
            in run(
                ["exec", "-T", "traefik", "cat", "/letsencrypt/rehearsal-marker"]
            ).stdout
        )
        assert (
            "synthetic-cache-marker"
            in run(
                [
                    "exec",
                    "-T",
                    "filestreambot",
                    "cat",
                    "/tmp/filestream_stream_cache/rehearsal-marker",
                ]
            ).stdout
        )
        assert before == {
            str(p.relative_to(legacy_bundle)): p.read_bytes()
            for p in legacy_bundle.rglob("*")
            if p.is_file()
        }
    finally:
        run(["down", "-v", "--remove-orphans"], check=False)
