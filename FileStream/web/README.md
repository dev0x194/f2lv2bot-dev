# FileStream Web V2

Fresh aiohttp/Jinja frontend, with no SPA or frontend build required. The legacy
`FileStream/template` and `FileStream/static` text files are left untouched.
No old web archive was opened or reused. All assets live here.

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
# Supply a local, authorized MP4 to enable the real playback/seek check:
WEB_MEDIA_FIXTURE=/absolute/path/sample.mp4 /tmp/filestream-web-test/bin/python -m pytest -q tests/test_web_browser.py
```

Screenshots go to `/tmp/filestream-web-qa`, or `WEB_QA_DIR`. Browser tests parse
all first-party JavaScript with Chromium. No browser test contacts a real ad
provider. The optional playback test serves local MP4 bytes through the actual
media handler, replacing only the Telegram transport.

## Pages and contracts

- `/`: trusted-host bot branding, bot/group/updates links, steps, FAQ and SEO.
- `/gen/{public_id}`: exactly one file; supported browser containers get Video.js.
- `/gfolder/{public_id}`: numbered links in the stored `folder.files` order. Empty
  folders render an empty state. Deleted entries are skipped. The source has no
  folder-pagination API; rendering remains linear in folder size.
- `/legal`, `/privacy`: existing backend policy content; no ad or access gate.
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
`system` (default), `light`, or `dark`. System mode follows live OS changes;
storage events synchronize tabs. Storage failures are nonfatal. System fonts,
CSS variables, visible focus, skip navigation, large controls, reduced motion
and narrow-screen layouts are built in.

Video.js **8.24.2** is pinned and locally hosted (Apache-2.0, license files next to
assets). Sources: [maintained v8 repository](https://github.com/videojs/videojs-v8),
[setup/API guide](https://legacy.videojs.org/guides/setup/),
[upstream package](https://unpkg.com/video.js@8.24.2/).
Verify assets: `python FileStream/web/tools/vendor.py`; restore exact pinned
files: `python FileStream/web/tools/vendor.py --fetch`.

The player supports normal controls, browser-supported fullscreen/PiP, six speed
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

## Advertising

The existing Telegram `/admin → Configuration → Web Ads` callbacks and
`db.get_web_ads_status(bot=...)` / `update_web_ads_status(..., bot=...)` are retained.
Effective ads require global `WebAds.ENABLED`, the bot's configured enabled state,
and its Mongo setting. Trusted host/page file/folder context selects the bot;
visitor-supplied bot IDs are never used. Landing branding now also passes its
resolved bot to the shared ad context.

Existing configuration names are preserved:

| Placement | Environment prefix | Default dimensions |
| --- | --- | --- |
| Desktop top | `WEB_ADS_DESKTOP_TOP_BANNER_*` | 728×90 |
| Desktop inline | `WEB_ADS_DESKTOP_INLINE_BANNER_*` | 300×250 |
| Mobile top | `WEB_ADS_MOBILE_TOP_BANNER_*` | 320×50 |
| Mobile bottom | `WEB_ADS_MOBILE_BOTTOM_BANNER_*` | 300×250 |

Banner suffixes are `KEY`, `INVOKE_URL`, `WIDTH`, `HEIGHT`. The shared 300×250
key/URL is canonicalized to desktop-inline configuration when present, otherwise
to mobile-bottom configuration; both legacy environment names remain accepted.
Only one top and one shared unit initialize for the initial viewport. A resize
does not inject a second shared unit. Provider scripts run in opaque-origin
sandboxed frames, with reserved dimensions and Advertisement labels. Theme and
page tokens are inaccessible to these scripts. Use HTTPS provider URLs.

`WEB_ADS_DESKTOP_SOCIAL_BAR_URL` and `WEB_ADS_MOBILE_SOCIAL_BAR_URL` load only for
the selected viewport in an isolated labelled strip. Providers that require
parent DOM access may not work inside the sandbox; verify compatibility with
your provider. A script load is not evidence an advertisement was displayed.
Errors/timeouts show nonblocking feedback. Social layouts cannot escape their
frame. Third-party behavior has not been verified live.

`WEB_ADS_SMARTLINK_URL` and `WEB_ADS_POPUNDER_URL` remain existing configuration
fields, but V2 intentionally executes neither: no site policy authorizes
popunders, and extra windows are unnecessary to complete Play/Download. There
are no automatic ad redirects. Safety, bot, sharing, VLC, theme and navigation
never trigger ad windows. Ads OFF means no ad config/scripts/slots/detector.

Anti-adblock on file/folder pages checks only whether a local bait element is
hidden. A positive result pauses optional access actions and offers allowlist,
Retry and Continue anyway. Telegram, reporting, legal, privacy and theme remain
available. Script/network outages are handled separately as unknown ad-delivery
states and never prove blocking. Detection is bypassable and may be wrong; it is
not authorization, secure enforcement, or proof that an ad was seen. There is
no ad-click requirement or server ad-session flag.

Responses use no-referrer, private/no-store for nonstatic pages/media, nosniff,
and the existing compatible CSP plus framing/form restrictions. Default aiohttp
access logging is disabled to keep capability paths out of logs; explicit
stream logs fully redact tokens. Configure any external reverse-proxy/CDN logs
to redact `/stream/`, `/file/`, `/thumbnails/` and token/session query values too.
Third-party frames receive neither page URL referrers nor authorization values.

## Manual release checklist and validation limits

- [ ] With non-production services, start `python -m FileStream`; check dynamic
  primary and secondary bot branding and their separate admin ad toggles.
- [ ] Open a real uploaded MP4: play, seek, mute, speed, fullscreen and PiP;
  check resume and errors after expiry, revocation and media deletion.
- [ ] Test a PDF and MKV; ensure browser video actions are absent where unsupported.
- [ ] Download, cancel/retry, launch installed VLC on Android/iOS/desktop and
  copy its stream URL. Confirm external clients can seek with Range requests.
- [ ] Check folder source order, empty/deleted entries, return navigation and
  unusually large folders. Submit a test NSFW report and verify canonical scan IDs.
- [ ] Check 320/375/768/1440px, 200% zoom, keyboard focus, screen reader names,
  OS theme changes, storage disabled, and slow/missing poster/vendor assets.
- [ ] With real ad-provider test configuration, verify each active placement,
  shared-unit counts, sandbox compatibility, failures, local bait detection and
  Continue anyway. Ensure provider code cannot read tokens. Recheck Ads OFF.
- [ ] Check reverse-proxy logging and host routing, HTTPS cookies and CSP headers.

Implementation checks used isolated fixtures only. Full bot startup, live Mongo
persistence, production Telegram streaming, native VLC launch, physical mobile
browsers and real provider delivery are deployment QA items, not verified claims.
