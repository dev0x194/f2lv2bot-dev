"""Export public static ad files; the runtime ad container never reads .env.

Run from the repository root: python -m FileStream.web.tools.export_ad_documents
"""

import argparse
from pathlib import Path
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from FileStream.web.ad_documents import (
    banner_id,
    error_document,
    https_origin,
    render_document,
    sensitive_cookie_pattern,
)


def export_documents(
    settings,
    output,
    parent_origins,
    sensitive_cookie_names=(),
    document_root="/usr/share/nginx/html",
    release_id="ok",
):
    cookie_pattern = sensitive_cookie_pattern(sensitive_cookie_names)
    origin = https_origin(settings.DOCUMENT_ORIGIN)
    parents = sorted({https_origin(p) for p in parent_origins})
    if (
        not origin
        or not parents
        or "" in parents
        or any(urlsplit(origin).hostname == urlsplit(p).hostname for p in parents)
    ):
        raise ValueError(
            "Distinct HTTPS ad origin and explicit parent origins required"
        )
    output = Path(output).resolve()
    if any(ord(c) < 32 for c in str(output)):
        raise ValueError("Invalid output path")
    # Reject a nonempty destination: old public slots must not survive configuration changes.
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(
            "Export destination must be empty; use a new versioned directory"
        )
    documents = {}
    for index in sorted({0, *settings.BOT_OVERRIDES}):
        config = settings.template_context(bot=SimpleNamespace(client_index=index))
        for device, names in [
            ("desktop", ["top_banner", "inline_banner"]),
            ("mobile", ["top_banner", "bottom_banner"]),
        ]:
            for name in names:
                slot = config[device][name]
                if (
                    slot["enabled"]
                    and config.get("mode", "isolated_documents") == "isolated_documents"
                ):
                    documents[banner_id(slot)] = render_document(slot, parents)
    html_dir = output / "html" / "banner"
    html_dir.mkdir(parents=True)
    for identity, document in documents.items():
        (html_dir / (identity + ".html")).write_text(document)
    (output / "html" / "unavailable.html").write_text(error_document(parents))
    # Only public origin/count metadata; no environment dump, session or database values.
    (output / "public.json").write_text(
        json.dumps(
            dict(origin=origin, parents=parents, documents=len(documents)), indent=2
        )
    )
    config = (Path(__file__).resolve().parents[3] / "deploy/ads/nginx.conf").read_text()
    config = config.replace("/usr/share/nginx/html", document_root)
    config = config.replace('200 "ok\\n"', '200 "' + release_id + '\\n"')
    config = config.replace("__PARENT_ORIGINS__", " ".join(parents))
    config = config.replace("__SENSITIVE_COOKIE_PATTERN__", cookie_pattern)
    (output / "nginx.conf").write_text(config)
    (output / "compose.env").write_text(
        "WEB_ADS_DOCUMENT_ORIGIN="
        + origin
        + "\nWEB_ADS_HOST="
        + urlsplit(origin).hostname
        + "\nWEB_ADS_PUBLIC_DIR="
        + str(output)
        + "\n"
    )
    return len(documents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--parent-origin", action="append", required=True)
    parser.add_argument(
        "--sensitive-cookie-name",
        action="append",
        default=[],
        help="Additional private cookie NAME used by another app on the parent domain; never a value",
    )
    args = parser.parse_args()
    from FileStream.config import WebAds

    count = export_documents(
        WebAds, args.output, args.parent_origin, args.sensitive_cookie_name
    )
    print("Exported %s public banner documents; no credentials exported." % count)


if __name__ == "__main__":
    main()
