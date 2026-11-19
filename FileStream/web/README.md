# FileStream Web V3

Aiohttp/Jinja frontend with no SPA or frontend build step. Assets live here;
legacy `FileStream/template` and `FileStream/static` pointers remain for compatibility.

## Run and verify

Run the application with the repository's existing Python environment and safe,
non-production bot/database configuration: `python -m FileStream`. The existing
server factory discovers `web/static`; the renderer discovers `web/templates`.
Do not start the bot against production credentials for frontend tests.

Offline regression tests do not import bot/config initializers, load dotenv, or
connect to Telegram/MongoDB. They compile and execute actual source functions
with service adapters; HTTP tests use aiohttp TestServer and the actual Range
response implementation with a fake byte transport.

```sh
python -m venv /tmp/filestream-web-test
/tmp/filestream-web-test/bin/pip install aiohttp jinja2 pytest pytest-asyncio
/tmp/filestream-web-test/bin/python -m pytest -q tests/test_web.py
```

Optional Chromium review (Linux may need `playwright install-deps chromium`):

```sh
/tmp/filestream-web-test/bin/pip install playwright
/tmp/filestream-web-test/bin/python -m playwright install chromium
/tmp/filestream-web-test/bin/python -m pytest -q tests/test_web_browser.py
# The synthetic repository fixture runs by default; optionally use another local MP4:
WEB_MEDIA_FIXTURE=/absolute/path/sample.mp4 /tmp/filestream-web-test/bin/python -m pytest -q tests/test_web_browser.py
```

Screenshots go to `/tmp/filestream-web-qa`, or `WEB_QA_DIR`. Browser tests parse
all first-party JavaScript with Chromium. No browser test contacts a real ad
provider. The optional playback test serves local MP4 bytes through the actual
media handler, replacing only the Telegram transport.

## Pages and contracts

- `/`: trusted-host bot branding, bot/group/updates links, steps, FAQ and SEO.
- `/gen/{public_id}`: exactly one file; supported browser containers get Video.js.
- `/gfolder/{public_id}`: searchable file cards, initially in stored `folder.files` order. Empty
  folders render an empty state. Deleted entries are skipped. The source has no
  folder-pagination API; rendering remains linear in folder size.
- `/legal`, `/privacy`: existing policy content; optional formats follow the master switch.
- Missing, revoked and expired public pages use the shared error template.
- `/watch`, `/folder`, `/folderm`: existing redirect routes remain registered.
  Legacy IDs redirect only through an existing active public mapping. They never
  mint replacement links for revoked/expired mappings; such requests return 404.
- `/dl/{path}`: retained but always **403**. Internal database IDs are no longer
  access capabilities. Existing raw download links must be replaced with public
  share pages.
- Both token issuers require `X-Page-Token` plus the matching HttpOnly page cookie,
  and resolve the active public file link before issuance.
- Stream-token JSON now returns `download_token_path`, replacing the broken
  `download_url` pointing at `/dl`. Download clicks call the separate authorized
  download-token endpoint and use its `/file/{token}` response.
- Issued media and storyboard tokens record their originating public ID. Each
  use rechecks activity and target identity, including expiry/revocation. Public
  file resolution no longer trusts cached authorization decisions.
- `/thumb/{path}` now resolves active public IDs; internal thumbnail IDs are no
  longer accepted. Posters are optional and failures do not stop playback.
- Reporting deep links retain `report_file_<public_id>` /
  `report_folder_<public_id>`; moderation resolves these to canonical internal IDs
  before storing reports, scanning or deleting media.

Templates retain the backend's `play.html` name. Jinja includes/inheritance use
an explicit loader with autoescaping; JSON uses `tojson`. Full filenames remain
in the DOM for assistive technology and in title attributes, with visual
ellipsis. A same-origin folder return link is remembered in sessionStorage.

## Themes and player

`theme.js` runs synchronously in the head before styles. `filestream-theme` stores
`dark` (default), `system`, or `light`. System mode follows live OS changes;
storage events synchronize tabs. Storage failures are nonfatal. System fonts,
CSS variables, visible focus, skip navigation, large controls, reduced motion
and narrow-screen layouts are built in.

Video.js **8.24.2** is pinned and locally hosted (Apache-2.0, license files next to
assets). Sources: [maintained v8 repository](https://github.com/videojs/videojs-v8),
[setup/API guide](https://legacy.videojs.org/guides/setup/),
[upstream package](https://unpkg.com/video.js@8.24.2/).
Verify assets: `python FileStream/web/tools/vendor.py`; restore exact pinned
files: `python FileStream/web/tools/vendor.py --fetch`.

The player supports normal controls, browser-supported fullscreen/PiP, five speed
options, safe per-public-ID resume positions, optional authorized WebVTT/WebP
scrub previews and a retry/one-time URL refresh after errors. MP4/M4V/WebM and
common audio containers are offered for browser playback; codecs still depend
on the browser. MKV/AVI are offered via download/VLC, without a playback promise.
There is no HLS or transcoding. The source currently supplies no subtitle-track
list; no subtitle files are invented.

VLC preparation issues a 30-minute stream-only bearer capability. A separate
explicit Launch VLC link uses `vlc://https://…`; the authorized URL is also
selectable/copyable for VLC's Open Network Stream. It works without browser
cookies; mismatching supplied sessions are rejected. Native scheme handling is
platform dependent and was not tested against a VLC installation. Keep bearer
URLs private. Downloads retain the backend's six-hour token lifetime and session
binding. Page tokens last 30 minutes; reload after page authorization expires.
Tokens remain in memory: process restarts invalidate them and multiple workers
need sticky routing or a future shared token store.

## Advertising and deployment

Test A direct JavaScript is the only runtime integration. There is one master
`WEB_ADS_ENABLED` switch, default disabled. Four explicit valid fields activate a
banner; optional formats activate only from explicit valid URLs. There is no mode,
external host-approval setting, exporter, provider-iframe registry or ad service.
The internal reviewed hostname allowlist is in `direct_ads.py`.

Parser-inserted tags preserve `atOptions` ordering and `document.write` semantics.
Only the initial viewport's placements load. Resize does not request more ads;
reload at the intended viewport to switch layouts. A stalled provider can delay
parsing. Same-origin code can access page tokens and credentialed APIs; neither
CSP nor HttpOnly makes this integration isolated. Backend authorization is unchanged.
See [the final advertising/migration guide](../../deploy/ads/README.md) and retained
[security audit](../../deploy/ads/DIRECT-INTEGRATION-AUDIT.md).

The single Compose model has FileStream plus Traefik. Existing cache/TLS volumes,
Docker routing, multi-token domains and the proxy network are preserved. Historical
rollback lives in independent private snapshots, not runtime compatibility modes.
Do not start a second hostname-competing standalone test router.

## Tests

`tests/fixtures/sample.mp4` is intentional synthetic media, excluded from production
images. Tests use real rendering, token and Range handlers with fake Telegram/DB
adapters and mocked provider responses. Browser tests never contact real advertising
endpoints. `RUN_COMPOSE_TESTS=1` enables a disposable two-service Docker rehearsal;
it uses loopback TLS, a synthetic HTTP app, no Docker socket and no production data.
Actual Telegram authorization and database connection in the new image require
manual/live startup approval; mock constructor or health tests do not establish them.

Independent historical sources/tests remain in private rollback snapshots. Retain
compatibility facades and legacy static paths until external callers are reviewed.
