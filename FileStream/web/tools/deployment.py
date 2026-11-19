"""Container-only deployment: validate, publish atomically, and check readiness.

Only public output enters /ad-public. Never serialize configuration or exceptions.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from FileStream.web.ad_documents import banner_id, https_origin
from FileStream.web.tools.export_ad_documents import export_documents

ROOT = Path("/ad-public")


def configuration():
    required = ("API_ID", "API_HASH", "BOT_TOKEN", "DATABASE_URL", "FQDN")
    for name in required:
        if not os.environ.get(name, "").strip():
            raise ValueError("Missing " + name)
    if not os.environ["API_ID"].isdigit() or int(os.environ["API_ID"]) <= 0:
        raise ValueError("Invalid API_ID")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", os.environ["API_HASH"]):
        raise ValueError("Invalid API_HASH")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", os.environ["BOT_TOKEN"]):
        raise ValueError("Invalid BOT_TOKEN")
    database = urlsplit(os.environ["DATABASE_URL"])
    if database.scheme not in ("mongodb", "mongodb+srv") or not database.hostname:
        raise ValueError("Invalid DATABASE_URL")
    parents = []
    for name in ("FQDN", *(f"MULTI_TOKEN{i}_FQDN" for i in range(1, 21))):
        host = os.environ.get(name, "")
        if host:
            origin = https_origin("https://" + host)
            if not origin or urlsplit(origin).netloc != host.lower():
                raise ValueError("Invalid application hostname")
            parents.append(origin)
    from FileStream.config import WebAds

    # Invalid explicitly configured dimensions must not silently disable a unit.
    for name, value in os.environ.items():
        if re.match(r"(?:MULTI_TOKEN\d+_)?WEB_ADS_", name) and name.endswith(
            ("_WIDTH", "_HEIGHT")
        ):
            if not value.isdigit() or not 0 < int(value) <= 4096:
                raise ValueError("Invalid banner dimensions")
    return WebAds, sorted(set(parents))


def slots(settings):
    if getattr(settings, "MODE", "isolated_documents") != "isolated_documents":
        return
    from types import SimpleNamespace

    for index in sorted({0, *settings.BOT_OVERRIDES}):
        config = settings.template_context(bot=SimpleNamespace(client_index=index))
        for device, names in [
            ("desktop", ("top_banner", "inline_banner")),
            ("mobile", ("top_banner", "bottom_banner")),
        ]:
            for name in names:
                slot = config[device][name]
                if config["enabled"] and slot.get("key") and not slot["enabled"]:
                    raise ValueError("Invalid enabled banner configuration")
                if slot["enabled"]:
                    yield slot


def revision(settings, parents):
    digest = hashlib.sha256(
        json.dumps(
            [
                settings.DOCUMENT_ORIGIN,
                getattr(settings, "MODE", "isolated_documents"),
                parents,
                list(slots(settings)),
                os.environ.get("WEB_ADS_SENSITIVE_COOKIE_NAMES", ""),
            ],
            sort_keys=True,
        ).encode()
    )
    for filename in (
        "FileStream/config.py",
        "FileStream/web/ad_documents.py",
        "FileStream/web/provider_iframes.py",
        "FileStream/web/tools/export_ad_documents.py",
        "FileStream/web/tools/deployment.py",
        "deploy/ads/nginx.conf",
    ):
        digest.update(Path("/app", filename).read_bytes())
    return digest.hexdigest()


def publish(settings, parents):
    ROOT.mkdir(exist_ok=True)
    with (ROOT / ".export.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = revision(settings, parents)
        releases = ROOT / "releases"
        releases.mkdir(exist_ok=True)
        final = releases / identity
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
        try:
            stage.chmod(0o755)
            export_documents(
                settings,
                stage,
                parents,
                filter(
                    None,
                    os.environ.get("WEB_ADS_SENSITIVE_COOKIE_NAMES", "").split(","),
                ),
                document_root=str(final / "html"),
                release_id=identity,
            )
            (stage / "compose.env").unlink()
            # Retain content-addressed URLs used by existing pages during updates.
            previous = ROOT / "current/html/banner"
            if previous.is_dir():
                for document in previous.glob("*.html"):
                    target = stage / "html/banner" / document.name
                    if not target.exists():
                        shutil.copyfile(document, target)
            for slot in slots(settings):
                document = (
                    stage / "html/banner" / (banner_id(slot) + ".html")
                ).read_text()
                if "<script" not in document or slot["invoke_url"] not in document:
                    raise ValueError("Invalid public document")
            subprocess.run(
                ["nginx", "-t", "-c", str(stage / "nginx.conf")],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if final.exists():
                # Returning to a prior configuration must retain URLs used by
                # pages from the intervening release. Add only missing files;
                # never overwrite an existing content-addressed document.
                if (final / "nginx.conf").read_bytes() != (
                    stage / "nginx.conf"
                ).read_bytes():
                    raise ValueError("Existing bundle configuration is corrupt")
                for document in (stage / "html/banner").glob("*.html"):
                    target = final / "html/banner" / document.name
                    if target.exists():
                        if target.read_bytes() != document.read_bytes():
                            raise ValueError("Existing public document is corrupt")
                    else:
                        pending_document = target.with_suffix(".pending")
                        shutil.copyfile(document, pending_document)
                        pending_document.replace(target)
                shutil.rmtree(stage)
            else:
                stage.rename(final)
            route = ROOT / "traefik"
            route.mkdir(exist_ok=True)
            resolver = os.environ.get("TRAEFIK_CERT_RESOLVER", "letsencrypt")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", resolver):
                raise ValueError("Invalid certificate resolver")
            origin = https_origin(settings.DOCUMENT_ORIGIN)
            routing = {
                "http": {
                    "routers": {
                        "ad-documents": {
                            "rule": "Host(`" + urlsplit(origin).hostname + "`)",
                            "entryPoints": ["websecure"],
                            "tls": {"certResolver": resolver},
                            "service": "ad-documents",
                        }
                    },
                    "services": {
                        "ad-documents": {
                            "loadBalancer": {
                                "servers": [{"url": "http://ad-documents:8080"}]
                            }
                        }
                    },
                }
            }
            content = json.dumps(routing, sort_keys=True)
            destination = route / "ad-documents.yml"
            if not destination.exists() or destination.read_text() != content:
                pending = route / ".pending"
                pending.write_text(content)
                pending.replace(destination)
            pending = ROOT / ".current"
            pending.unlink(missing_ok=True)
            pending.symlink_to("releases/" + identity)
            pending.replace(ROOT / "current")
            print("Public advertisement bundle ready: " + identity)
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def main():
    try:
        settings, parents = configuration()
        if sys.argv[1] == "export":
            publish(settings, parents)
        elif sys.argv[1] == "application":
            if (ROOT / "current").resolve().name != revision(settings, parents):
                raise ValueError(
                    "Public advertisement bundle does not match application"
                )
            for slot in slots(settings):
                if not (
                    ROOT / "current/html/banner" / (banner_id(slot) + ".html")
                ).is_file():
                    raise ValueError("Missing advertisement document")
            os.execvp(sys.argv[2], sys.argv[2:])
        else:
            raise ValueError("Unknown deployment mode")
    except Exception:
        # Configuration exceptions can contain credentials. Never print them.
        print(
            "Deployment validation failed; check required .env settings and public bundle.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
