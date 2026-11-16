"""Verify or restore the checked-in, version-pinned Video.js distribution."""

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

BASE = Path(__file__).resolve().parents[1] / "static/vendor/videojs"
MANIFEST = BASE / "manifest.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch", action="store_true", help="restore exact pinned assets"
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    for name, info in manifest["files"].items():
        path = BASE / name
        data = (
            urllib.request.urlopen(info["url"], timeout=30).read()
            if args.fetch
            else path.read_bytes()
        )
        actual = hashlib.sha256(data).hexdigest()
        if actual != info["sha256"]:
            raise SystemExit(f"Checksum mismatch: {name}")
        if args.fetch:
            path.write_bytes(data)
        print(f"OK {name}")


if __name__ == "__main__":
    main()
