# FileStream Web V3

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
- `/gfolder/{public_id}`: searchable file cards, initially in stored `folder.files` order. Empty
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

Banner suffixes are `KEY`, `INVOKE_URL`, `WIDTH`, `HEIGHT`. Each slot keeps its
own key, URL and dimensions, including independent desktop-inline/mobile-bottom
settings. Only the active viewport's top and secondary units initialize. Resizing
within the same breakpoint scales existing units without new provider requests.
Crossing 767px selects the new device's units and hides the old documents. Each
unique key/URL/dimension tuple initializes at most once per placement per page;
returning to a previous breakpoint reuses its attached document. Identical 300×250
settings share one document across breakpoints, while independent configurations
remain independent. Failed units are retained as failed records and are not retried.
Hidden units' delayed messages cannot change the active unit's visibility/state.

Banners now navigate regular cross-origin iframes to fixed documents on
`WEB_ADS_DOCUMENT_ORIGIN` (for production, `https://ads.tdrp.cc`). That origin must
be distinct from FileStream and have ongoing DNS, TLS and a separately operated
static ad service. There is no same-origin/srcdoc fallback for banners. Each
minimal document defines its own `atOptions` before a parser-inserted invocation,
with normal origin referrers and support for `document.write`. Only the public
slot configuration is exported; the service receives no application credentials.
App-to-ad referrers are reduced to origin, and postMessages require exact origin
and frame identity. HTTPS session cookies use the `__Host-` prefix with no Domain.
The ad endpoint rejects protected credential-cookie names, including legacy and
domain-wide FileStream cookies, while permitting ordinary advertising cookies.
Cookie values are neither authenticated, reflected nor forwarded by the service.

Use the single root Compose file for installation and updates; export is automatic.
See [ad-service operations and security](../../deploy/ads/README.md) for the export,
isolated Docker service, egress filter, deployment/rollback and real-browser
release gate. The template/admin flow remains unchanged. The confirmed Android
F result established the architecture; the owner now reports real integrated
cookie-fix creatives for all four placements and successful navigation in two
additional recordings. See the precise coverage and remaining
physical checks in [the validation record](../../deploy/ads/VALIDATION.md). Historical investigation
notes below describe earlier code and are not the current deployment recipe.

Social bars require `WEB_ADS_SOCIAL_BAR_ENABLED=True` (default False) in addition
to an active device URL and the global/admin switches. A URL alone does not enable
them. Social layouts remain confined to their frame. Popunders are never executed.
`WEB_ADS_ADBLOCK_ENABLED=False` disables the optional detector and warning;
otherwise Retry and Continue anyway retain their existing behavior.

A script download or iframe load does not prove advertising fill. Console messages
prefixed `[FileStream ads]` and `data-ad-state` on each placement expose delivery
states without logging URLs or credentials: `error`, `slow-unverified`, `empty`,
and `content-unverified`. Script errors collapse immediately. After 30 seconds a
pending placement hides without retrying, allowing late content to reveal it.
A successfully loaded script that inserts no visible text/media/frame within 60
seconds collapses provisionally, retaining the isolated document and message
listener so later content can reveal it. Only a script error destroys the frame.
Nested cross-origin iframe fill cannot be inspected: an empty
creative inside such a frame remains unknown and is not reported as success.

### Blank-banner investigation (2026-11-17)

#### Working Nginx reference follow-up

`test/ad-test.html` is the reference for the original invocation order and URLs.
The user's successful browser run establishes that real inventory can be served
on this domain. Earlier empty responses below do not invalidate that result and
do not establish an account/provider fault.

The follow-up Chromium comparison used fresh contexts, the same HTTPS origin
(`https://tdrp.cc/`), the three reference keys/URLs, and no Popunder execution.
Browser-local HTML substitution compared the original banner HTML, rendered V3
with the current loader, and original banners plus Social Bar, without changing
Traefik or deploying the application. All invocation responses in that run were
HTTP 200 with zero-byte bodies; neither implementation produced a real creative.
Original requests sent the origin referrer; V3 requests sent none. V3 already
uses parser-inserted scripts in BODY inside opaque sandboxed `srcdoc`; its outer
frame is created dynamically. No script execution error was observed. This run
cannot establish whether opaque origin, referrer suppression or frame restrictions
explain the user's difference. Do not remove those protections on this evidence.

A separate deterministic browser regression proved that the 60-second empty
cleanup destroyed a provider document before a creative inserted at 65 seconds.
Cleanup now collapses an empty placement while preserving its document, observer
and listener; late content restores it. Network errors still destroy the frame.
The test fixture is synthetic and is not evidence of live advertising fill.
No change grants advertising scripts access to page DOM, cookies or media tokens.

Configuration inspection matched the four production banner keys, dimensions and
invocation URLs to the reference. No per-bot advertising override fields were
present. `load_dotenv` preserves existing process environment values; effective
configuration also requires the global flag and persisted admin switch. The
production FileStream container was stopped, so its effective runtime/admin state
was not verified. Only Nginx and Traefik were running during this investigation.

The remote had only `main`, with no local or remote `dev`. Until an authorized
`dev` branch exists and live creative validation succeeds, do not describe this
partial lifecycle correction as the verified production fix or push to `main`.

To restore FileStream later, after reviewing/building the desired code, stop the
test router before starting the application (run from the repository root):

```sh
sudo docker compose -f test/docker-compose.yml --env-file test/.env stop ad-test
sudo docker compose up -d --build
```

This is a historical test-router restoration example. For an existing two-file
installation, use the reviewed [migration procedure](../../deploy/ads/README.md)
first: whole-stack startup recreates Traefik once to add the file provider.
Preserve certificate/cache volumes; never run `down -v`. No restoration or
migration was executed as part of this review.

Regression validation: `WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4
/tmp/filestream-web-venv/bin/python -m pytest -q` passes all 21 tests. Advertising
fixtures exercise per-frame options/order, parent isolation, duplicate execution,
resize request counts, empty scripts, blocked requests, timeouts, global/admin
switches and detector disabling. Media fixtures cover the existing secure routes
and actual Chromium MP4 playback; these are local fixtures, not production services.

All three supplied `bellnewyork.org/22/<key>` URLs returned HTTP 200 with **zero
response bytes** in direct GET probes. Chromium also received a zero-byte mobile
script and inserted no creative or nested iframe in the existing sandbox. Thus
this environment reproduced blank frames from an empty invocation response;
there is no evidence here that sandbox privileges caused that response. The old
loader incorrectly accepted script load as success and retained the empty frame.
Other corrected defects were desktop-inline configuration overwriting the mobile
bottom slot, implicit social-bar execution, and narrow-screen frame clipping.
Provider eligibility, account/zone status, geography and live fill still require
provider-side investigation; local fixtures cannot prove live advertising fill.
Physical Android Chrome/Telegram were not available for verification.

### Production follow-up (2026-11-17)

The deployment at `tdrp.cc` was inspected read-only. Its 20 `WEB_ADS_*` values
matched the deployment `.env`; global ads were True. Rendered homepage configuration
confirmed the admin gate allowed banners, social bars were disabled, and mobile
selected only the 320×50 mobile key plus one 300×250 bottom key. The desktop path
requested only its 728×90 key plus one inline key. No keys or provider were changed.

The deployed `/static/js/ads.js` SHA-256 matched commit 88741b5 and its running
container: `edd0441e9b8330b03d178b9187f57219ecd4f3a8adf3d61e3aee73fdaceb6486`.
Thus stale source was not reproduced for this hostname. The reported three areas
and old “Advertisement could not load” text were not present on the tested live
homepage; another affected share page/host or an old browser cache remains possible.
No private file URL was available, so live secure file pages were not probed.

GET results for all three invocation endpoints, without referrer and with
`Referer: https://tdrp.cc/`:

| Endpoint suffix / placements | Host curl | Running container urllib | Chromium loader |
| --- | --- | --- | --- |
| `d76c947d5bce357e40ad06c5443da535` / mobile top | 200, 0 bytes | 403 | 200, 0 bytes |
| `360f27a067037e47107b2b725150e7b5` / desktop inline, mobile bottom | 200, 0 bytes | 403 | 200, 0 bytes |
| `f0c271d2355a57b29b6b278209294341` / desktop top | 200, 0 bytes | 403 | 200, 0 bytes |

Host responses were `application/javascript`, `Content-Length: 0`, zero redirects,
`Server: cloudflare`, `CF-Cache-Status: DYNAMIC`, `Vary: Accept-Encoding` and
`Vary: Origin`. Container probes varied: a browser-UA request returned a zero-byte
JavaScript 403; subsequent standard urllib requests returned 17-byte
`text/plain; charset=UTF-8` bodies containing `error code: 1010`, with zero redirects.
[Cloudflare documents error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)
as browser-signature blocking; it accounts for those urllib refusals, but does
not explain Chromium's successful empty responses or establish inventory status. Banner requests are made by visitors' browsers, not by the Python server.

The exact supplied original mobile snippet was tested first in BODY, without a
sandbox, using a browser-local intercepted HTML document at the `tdrp.cc` origin.
No diagnostic page was published, no existing app document or authorization was
included, and no provider request was mocked. It sent the normal origin referrer,
received 200/0 bytes and rendered nothing. The isolated loader, with no referrer,
also received empty responses for all three URLs. Neither comparison logged a
JavaScript/CSP/sandbox error. No real advertisement was observed.

The actual production homepage was observed for 65 seconds in Chromium's Pixel 7
emulation: two mobile invocations, no social bar, no horizontal overflow. At 32
seconds both placements were hidden (`slow-unverified`); at 65 seconds both frames
had been removed (`empty`). Thus permanent empty-frame cleanup already works for
these empty responses. Empty **cross-origin creative frames** remain uninspectable
and are never claimed to be filled merely because a frame exists. Physical Android
Chrome/Telegram, other geographies and provider callbacks were not available.

One separate application defect was demonstrated with a local provider fixture:
the loader invoked scripts in HEAD. The [official Adsterra banner guide](https://adsterra.com/blog/how-banner-ads-make-money/)
requires BODY placement. The frame now explicitly opens BODY before the setup and
invocation scripts, with a regression asserting `document.currentScript`'s parent.
This corrects the documented integration pattern; it does not explain the empty
response to the original snippet. Sandboxing privileges remain unchanged.
Text detection now walks text nodes excluding script/style/template/noscript;
`innerText` can otherwise count script source when a document is hidden. Empty
response, error, timeout and duplicate-request regressions still pass.

The supplied `atOptions` pattern is consistent with Adsterra, but ownership of the
invocation domain and account approval were not authenticated. The official guide
requires approved website/banner status and copying current code from GET CODE
for the registered domain. It does not document compatibility with opaque sandbox
frames. Verify the exact `tdrp.cc` domain, each unit's status/size, current embed URL,
and request/impression statistics in the actual provider dashboard. Give support
the endpoint, timestamp and empty-response evidence; do not send bot credentials,
cookies or media capabilities. No evidence identifies approval, domain association,
geography or no inventory as the specific upstream reason.

Changed files for this follow-up: `static/js/ads.js` (BODY invocation and text
inspection), `tests/test_web_browser.py` (BODY regression), and this report.
Full local suite with the MP4 fixture: **21 passed**. These mocked integration and
media tests do not establish live provider fill. No deployment, commit or push was
performed for this follow-up.

### Latest live recheck and responsive correction (2026-11-17)

The deployed source, container and HTTPS `/static/js/ads.js` all matched commit
50d84fe's loader SHA-256:
`d567ce02ad8a974f42b214b2000e68b9186e9b6c9306710e3e859705c9029b76`.
The 20 advertising environment fields again matched `.env`; ads were True,
no per-bot ad overrides were configured, and social bars remained off by default.
Effective enablement is an AND: global setting, per-bot configured enablement,
and the persisted admin Web Ads status must all permit ads. The admin switch
can turn configured ads off, but cannot override global False. Global banner
fields supply defaults; `MULTI_TOKEN<n>_WEB_ADS_*` supplies per-bot overrides.
Existing process variables take precedence over python-dotenv-loaded files.
The live homepage's rendered ad config confirms its admin gate permits banners.

A randomly named temporary HTML file was served under the existing site's static
route, containing only the original supplied 320×50 code in BODY and viewport
metadata. Chromium loaded it from `https://tdrp.cc`; the document and provider
script were real HTTP requests, not intercepted/mocked. It returned HTTP 200 with
zero script bytes, no redirect, no creative and no console/script error. The file
was removed immediately after the diagnostic run. No permanent diagnostic route,
application authorization data, or credential was published.

Fresh GETs of **all three** unique endpoints again returned HTTP 200, zero bytes,
`application/javascript`, and zero redirects from host curl and Chromium.
Host referrer/no-referrer comparisons were unchanged. Python urllib inside the
running container received HTTP 403, 17 bytes, `text/plain; charset=UTF-8`, no
redirects. These responses contain Cloudflare error 1010; it identifies client
signature blocking for those server probes, not the reason browsers receive empty
scripts. No executable provider code was delivered to the tested browser requests.

Live Pixel 7 emulation measured mobile top at **320×50 rendered pixels**, with
`atOptions` key `d76c947d5bce357e40ad06c5443da535`, width 320 and height 50.
Mobile bottom rendered at **300×250**, using
`360f27a067037e47107b2b725150e7b5`. Desktop rendered **728×90** with
`f0c271d2355a57b29b6b278209294341`, plus a single 300×250 inline banner.
No wrong-device units were requested at initial load. The live mobile homepage
hid both placements at the grace timeout and removed the empty frames by 65
seconds. No indefinitely visible empty panel was reproduced for these responses.
Cross-origin frames that exist but are unfilled remain unknown; their contents
are never inspected or claimed to be filled.

A separate lifecycle defect was reproduced locally: opening at desktop width
then resizing to 375px retained the desktop key and 728×90 dimensions. The loader
now switches units on media-query changes, retaining documents in place to avoid
iframe reloads when returning to a prior viewport. Identical secondary settings
are reused; distinct mobile/desktop settings stay independent. Event handlers
track each unit's state so delayed failure/content messages from a hidden unit
cannot hide/reveal the current device's placement. Ordinary within-breakpoint
resizing and repeated script initialization make no extra provider requests.

The patched loader was also tested against the real homepage and provider using
a browser-local replacement of only `/static/js/ads.js` (not a server deployment).
Desktop → mobile → desktop → mobile measured the correct 728×90/320×50 top unit
and 300×250 secondary unit. Only three unique provider requests occurred; shared
secondary and previous top documents were reused. All three responses were still
200/0 bytes with no redirects or script errors. After the grace period, both active
placements had state `empty`, display `none`, and zero remaining frames. This is
live request/cleanup verification, not successful ad fill.

Changed files: `static/js/ads.js`, `tests/test_web_browser.py`, and this README.
Validation: `WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4
/tmp/filestream-web-venv/bin/python -m pytest -q` completed with **22 passed**.
The regression adds independent-unit breakpoint switching and an inactive unit's
late failure, alongside existing keys/options, sandbox, failure/empty/timeout,
Anti-AdBlock, layouts, token security, downloads and MP4 playback checks. Local
fixtures establish integration behavior; they do not establish real provider fill.

Publisher action is required to investigate live delivery. In the actual network's
publisher dashboard, select the **tdrp.cc** website (or correct the registration if
that is not the approved domain), verify website and each banner are approved and
active, and open GET CODE for 320×50, 300×250 and 728×90. Compare the full current
script URLs, keys and dimensions to this site's four configured slots; update only
fields demonstrably different from the provider's current code. If approval is
pending/rejected, resolve the stated dashboard reason before expecting delivery.
If domain, status and code match, provide support with the empty original-snippet
response evidence and ask them to identify disabled zones, delivery restrictions,
or inventory eligibility and restore executable script delivery. Check their
request/impression counters for these units during a real visit. We cannot infer
which dashboard item is wrong from an empty response, nor change the account
without access. No keys, provider, iframe privileges, CSP, popunder policy, media
authorization or frontend design were changed.

### Deployment diagnostics

`config.py` loads `ENV_FILE` when supplied, otherwise the repository `.env`;
existing process variables take precedence. Compose already injects `.env` using
`env_file`; its `environment` section does not override ad variables. The working source folder contains only `.env.example`; the separate deployment
checkout's advertising fields were compared to the running container during the
follow-up above. No private credentials or unrelated environment were printed.
Global enable, per-bot `MULTI_TOKEN<n>_WEB_ADS_*` overrides and the persisted
`/webads` or admin toggle must all permit banners. Keys and invocation URLs are
public provider configuration, never bot/database/media credentials.

Apply source/env changes with `sudo docker compose up -d --build` (a restart alone does not reload container environment).
Inspect `docker compose logs --tail=100 filestreambot` without dumping environment
variables. In browser DevTools use Network to inspect invocation status, response
body, blocked requests and subsequent creative requests; inspect Console for CSP,
sandbox and script errors. Compare ordinary Chrome with Telegram on the same
network and inspect each ad frame separately. Empty HTTP 200 is not proof of an
ad blocker. Do not copy media capability URLs or cookies into provider support logs.

### Optional Smartlink / sponsored navigation

`WEB_ADS_SMARTLINK_URL` is now rendered as one **Sponsored link** beside the
secondary banner on landing, file and folder pages. It is an ordinary accessible
anchor, opens the configured HTTPS destination in the same tab only when selected,
and works without JavaScript. An “ADVERTISEMENT · OPTIONAL” label and explanatory
copy separate it from Play, Download, VLC and folder controls. Banner failure does
not remove this independent control. Legal/error pages have no sponsored action.

Root cause: configuration already loaded and reached `ad-config`, but the old
frontend deliberately never consumed it. No Smartlink anchor/event existed.
The old `.env.example` comment described an obsolete ad gate. There is no current
ad gate or separate Smartlink enable setting, and none has been introduced.

Configuration and precedence:

1. Compose injects `.env` into the container. Existing process variables take
   precedence over python-dotenv file loading (`ENV_FILE`, then repository `.env`).
2. `WEB_ADS_SMARTLINK_URL` is the global destination; the default is now empty,
   removing the old hardcoded fallback. Missing/blank means no sponsored link.
3. `MULTI_TOKEN<n>_WEB_ADS_SMARTLINK_URL` overrides it for that bot. An explicitly
   blank or invalid override disables Smartlink for that bot without falling back.
4. Global `WEB_ADS_ENABLED`, per-bot configured enablement and the persisted admin
   `/webads` switch must all permit advertising. Admin ON cannot override global
   OFF. This applies to Smartlink as well as banners.
5. Backend validation rejects non-HTTPS, relative, credential-bearing, malformed
   host/port, whitespace/control-character and backslash destinations. Invalid URLs
   become empty before reaching JSON or Jinja. Valid values are autoescaped in HTML.

The URL remains administrator-controlled public advertising configuration. It is
never fetched by the production backend, executed as JavaScript, embedded as an
iframe, or augmented with file/session/stream tokens. The anchor uses
`rel="sponsored nofollow noopener noreferrer"` and `referrerpolicy="no-referrer"`.
No popups, automatic navigation, download/play interception, required clicks,
media-gate changes or third-party main-document scripts were added. Popunder
scripts remain inactive. Configuring Smartlink does not enable social bars.

Live verification (2026-11-17): the deployment `.env`, running container and live
homepage JSON all contained
`https://ardance.org/4/8f1a3362d3c4d7c7e94598462b3d7fb7`; the old live HTML had
**zero matching anchors** despite ads enabled. There were no per-bot Smartlink
overrides. Therefore the missing control was an application integration gap,
not a missing Docker variable. The source change must be rebuilt into the image
before the live site renders the new anchor; it is server-rendered and has no
JavaScript-cache dependency. Existing dynamic responses remain private/no-store.

Provider type/approval could **not** be authenticated. A real GET of this endpoint
returned HTTP 200, `text/html`, **zero bytes**, no redirects. A fresh Chromium
session using the actual rendered landing template made no destination request
before interaction; clicking Sponsored link navigated in the same tab, sent no
referrer, and also received HTTP 200 with an empty HTML body and no redirect.
This proves user-initiated navigation, not advertising delivery or approval.
The endpoint was not observed delivering JavaScript, but an empty response cannot
prove its ad-format identity. Its path must not be automatically interpreted as
banner, Social Bar or Popunder code.

For Adsterra, the [official Direct Link guide](https://adsterra.com/blog/guide-for-working-with-direct-links/)
uses a URL in an anchor. Confirm the actual publisher's Smartlinks/Direct Links
entry is **Active**, its registered website is correct if domain-bound, and GET
CODE provides this exact URL as an `href` destination. If it provides a script
`src` instead, obtain an actual Direct Link; leave other formats in their separate
settings. If the destination/code/status match but it still returns empty HTML,
ask provider support to investigate delivery restrictions/account eligibility.
No provider, keys or production `.env` values were changed. Banner no-fill is a
separate unresolved provider issue.

Validation: **25 tests passed** with the local MP4 fixture. New tests execute the
actual configuration parsers and ad class; cover global/admin/per-bot precedence,
missing/unsafe URLs, page placement, no unrelated format enablement, keyboard
navigation, no automatic requests/popups/referrer/token appending, independent
Download errors and Android Chrome emulation plus desktop. Existing streaming,
VLC preparation, Video.js playback, themes, Anti-AdBlock and banner regressions
remain covered. UI screenshots were checked in Chromium; physical Android and
Telegram were not tested. Mock destination navigation is distinct from the real
empty provider response above. No application deployment, commit or push was
performed for this change.

Apply the source changes from the deployment checkout:

```bash
cd ~/deploy/f2lv2bot-dev
sudo docker compose up -d --build
sudo docker compose logs --tail=100 filestreambot
```

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

## V3 interface

The existing templates now use a black/zinc design with a 32px grid, restrained
radial glow, locally authored SVG icons, white primary actions and responsive
cards. Light and System remain available. No fonts, icon CDN, build tool, or new
runtime dependencies are required; Geist/Inter/JetBrains Mono use local fonts
when installed and system fallbacks otherwise.

Folder search, type chips and stable name/size sorting compose together. Exact
`size_bytes` is additive renderer metadata; missing sizes sort last in either
direction. Original folder order remains the initial view. Dates are not exposed
reliably, so no chronological sort is offered. The native details dialog supports
Escape, backdrop dismissal, focus restoration and a mobile bottom sheet. Download
and playback links open the existing authorized file page before issuing tokens;
folder pages never mint file capabilities. Share and copy use public URLs only.

Known expired/revoked/missing/wrong-kind backend link errors have public messages;
unknown errors retain generic text. No exception details are rendered. Advertising
continues in isolated frames; anti-adblock is a nonmodal keyboard-accessible
warning with a session-persisted Continue override. It cannot trap navigation.

V3 screenshots and browser regression artifacts are written to
`/tmp/filestream-web-qa`. Production Telegram/MongoDB, native VLC launching,
physical devices and live advertisement providers still require release QA.

### Verified in this workspace

`WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4 /tmp/filestream-web-venv/bin/python -m pytest -q`
completed with **21 passed**. These tests use the actual Jinja templates, renderer,
route functions and Range handler, with fixture service adapters. Chromium covers
320/375/768/1440px landing, file, folder, legal, privacy and error layouts; a 390px
mixed-file folder covers filters, combined search, sorting, dialog keyboard/focus,
clipboard sharing and hostile filename rendering. Other checks cover themes,
ad placement isolation and disabling, detector override, token/session enforcement,
revocation, byte ranges, local MP4 playback/seeking/retry, resume where the fixture
is long enough, and preparation of VLC URLs. Native VLC launching is not tested.

`python FileStream/web/tools/vendor.py` verified all four pinned Video.js assets.
First-party JavaScript parsing and Python/template compilation are included in
the regression suite. No production bot, database, provider or deployment was used.
