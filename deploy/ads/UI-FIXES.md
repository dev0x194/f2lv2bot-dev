# Test A UI fixes: loading, responsive banners and labels

Production has not been updated with these UI changes. No publisher configuration,
authorization, CSP, cookies, sessions, services or backend routes changed.

## Confirmed cause and implementation

The old top banner's synchronous provider script preceded `main`; a delayed
response stopped the HTML parser before the homepage heading existed. Deferred
local scripts also waited for parsing to finish. This was reproduced with a held
synthetic response, independently of provider approval or server-side connectivity.

Templates now parse the main content, footer and file/folder configuration first.
Local UI, Video.js and access controls initialize before banners. The theme select
binds on an idempotent content-ready event without waiting for DOMContentLoaded.
Banner scripts remain parser-inserted: each receives its own original `atOptions`
then its original invocation URL. They are neither async nor dynamically injected.
CSS grid presents the late top banner above main content; frames are not moved or
recreated. Inline ads appear below main content. Reading order remains content-first.

A hidden, same-origin attachment transfer frame handles downloads requested during
ongoing parsing; this avoids cancelling the initial page navigation. Existing URL
validation and token issuance still apply. Normal post-load downloads are unchanged.
This frame contains no advertising code. Playback and seeking initialize before ads.

The browser still waits for synchronous advertising scripts to finish before
DOMContentLoaded; a later ad can wait behind an earlier one. A configured head
Popunder remains blocking. A third-party script that monopolizes the main thread,
alters the DOM or navigates the page can still disrupt it. These fixes do not promise
availability independence from arbitrary same-origin advertising JavaScript.

## Layout and labels

The viewport remains `width=device-width, initial-scale=1`. Banner selection uses
767px; homepage columns use 850px. Chrome Desktop site deliberately changes the
CSS viewport and scales a desktop page on the phone. No user-agent overrides were
added. Real desktop columns are preserved.

At 320px, card borders previously reduced the iframe viewport to 318px; narrow
cards now omit side borders so 320px creatives retain their exact viewport.
Theme selection uses a readable mobile font with existing 44px minimum targets.

Each initialized banner has one 12px Advertisement label outside the creative,
referenced by its complementary landmark. Labels use themed muted text, no overlay
and no pointer interception. Sponsored Link stays separate and user-initiated.
Reservations include creative and label dimensions, use only active-device slots,
and do not exist when global advertising is disabled. Failed/blocked configured
ads retain their reserved dimensions and label rather than collapsing, repeatedly
loading, or prematurely deleting a delayed creative. Initial viewport selection is
stable until reload; resizing never generates additional provider requests.

## Controlled validation

Tests use actual V3 templates/assets, synthetic application data and intercepted
`app.test`, `provider.test`, `creative.test` origins. All other network traffic is
aborted. No paid advertisement requests or impressions are generated.

The eight-width matrix covers 320, 360, 375, 390, 430, 768, 1024 and 1440 CSS pixels
on homepage, file/player and folder pages, plus landscape-style resizing. It checks
overflow, actual iframe dimensions, labels, creative click delivery, themes,
44px controls, desktop columns and no resize-triggered ad requests. Download and
actual local Video.js playback/seeking are exercised while provider parsing is held.
Disabled, fast, delayed, HTTP 503 and blocked-provider cases are included. Existing
route, security, range, download and Compose tests remain in the suite.

A separate headless Chromium benchmark used three fresh contexts per case, widths
390 and 1280, before/after source, and 1200ms delay per provider tag (two tags).
Main visibility measures the first animation frame with a visible main heading;
FCP/LCP use browser PerformanceObservers. This is a controlled desktop-browser
measurement, not Android field data. No CPU/network throttling was applied beyond
the provider delay. Host scheduling introduces noise; FCP need not improve because
the old header already painted early.

| Viewport / response | Main visible before → after (ms) | FCP before → after (ms) | LCP before → after (ms) |
|---|---:|---:|---:|
| 390 / disabled | 80.2 → 78.3 | 108 → 104 | 108 → 104 |
| 390 / fast | 108.2 → 105 | 136 → 160 | 160 → 160 |
| 390 / slow | 1297.4 → 88.6 | 92 → 116 | 1320 → 116 |
| 390 / failed | 104.1 → 81.8 | 144 → 124 | 148 → 124 |
| 390 / blocked | 130.9 → 85.9 | 144 → 116 | 176 → 116 |
| 1280 / disabled | 97.9 → 81.5 | 172 → 152 | 172 → 152 |
| 1280 / fast | 182.2 → 100.9 | 204 → 188 | 272 → 188 |
| 1280 / slow | 1306.9 → 86.2 | 164 → 156 | 1392 → 156 |
| 1280 / failed | 158.9 → 133.6 | 192 → 288 | 240 → 288 |
| 1280 / blocked | 152.5 → 126.8 | 200 → 188 | 228 → 188 |

Slow-response DOMContentLoaded stayed approximately 2500ms in both versions.
Measured layout shift was 0 in these synthetic cases; real provider markup may differ.

Production was checked without executing advertising JavaScript: HTTPS `/health`
returned 200/healthy, MongoDB connected and five active clients. Container IDs,
image IDs, start times, mounts and private `.env` matched the pre-change snapshot.
Live Telegram media transfer was not repeated during this frontend task.

No real Adsterra delivery has been verified for this source revision. Android
acceptance still requires all four creatives, refresh/navigation/Chrome reopening,
normal mobile vs Desktop site, theme/navigation, playback/seeking, sharing and a
real Telegram download. Use ordinary manual browsing, no artificial reload traffic
or ad clicks. Verify publisher accounting separately. Backend Telegram transfers
are not simulated as real transfers by the local media fixture.

## Proposed deployment, only after approval

No `.env` changes are required. Keep the master switch and original zone fields;
leave currently inactive Popunder and Social Bar URLs empty. Keep Sponsored Link's
current deliberate-click behavior.

The current working UI baseline has a private snapshot at
`/home/ubuntu/deploy/filestream-rollbacks/ui-review-20261119T181627Z` containing
`.env`, two-service Compose and immutable recovery image references. The running
application's original image ID was absent from Docker's image catalogue; it was
preserved through a non-pausing container filesystem snapshot instead. This private
image includes runtime environment metadata and must never be pushed or shared.
Mounted sessions/cache/TLS remain external and are not included in that image.
Its independent Compose plus image override passes config validation; a live restore
has not been rehearsed because production must stay running.

Before an approved update, refresh the private snapshot if production changed and
verify all preserved images, environment and volume identities. Then:

```
cd /home/ubuntu/deploy/f2lv2bot-dev
sudo docker compose config --quiet
sudo docker compose up -d --build
curl --fail --silent --show-error https://tdrp.cc/health
sudo docker compose ps
```

Expect a brief application-container restart; no Telegram session reseeding is
needed. Check MongoDB and all five clients, then test real media and Android ads.
Traefik should not recreate when its configuration/image is unchanged.

If the approved UI update fails, restore the exact private environment and independent
image definition (these commands are documented, not executed):

```
cd /home/ubuntu/deploy/f2lv2bot-dev
sudo cp /home/ubuntu/deploy/filestream-rollbacks/ui-review-20261119T181627Z/.env .env
sudo chmod 600 .env
sudo docker compose -p f2lv2bot-dev \
  --env-file /home/ubuntu/deploy/filestream-rollbacks/ui-review-20261119T181627Z/.env \
  -f /home/ubuntu/deploy/filestream-rollbacks/ui-review-20261119T181627Z/docker-compose.yml \
  -f /home/ubuntu/deploy/filestream-rollbacks/ui-review-20261119T181627Z/rollback-images.json \
  up -d --no-build --no-deps filestreambot
curl --fail --silent --show-error https://tdrp.cc/health
```

Do not delete volumes, certificates, sessions, historical Test F snapshots or images.
The historical four-service Test F restore uses its own old Compose definition;
never layer that recovery onto this two-service model.

## Security limitation

Test A still runs third-party code with FileStream's document privileges, including
page-token access and credentialed same-origin requests. Labels, CSS, script order,
HttpOnly cookies and CSP do not eliminate that exposure. The retained direct-script
security regression tests document it; backend authentication remains unchanged.

## Final executed results and scoped file list

`RUN_COMPOSE_TESTS=1 /tmp/filestream-web-venv/bin/python -m pytest tests/ -q -rs`:
**106 passed, 0 failed, 0 skipped in 119.00s**. This includes the opt-in isolated
Docker build/TLS/update/state rehearsal. `git diff --check` and
`sudo docker compose config --quiet` also pass. No production deployment,
container restart, commit or push was performed.

Files changed for this UI task (earlier cleanup changes remain pending separately):

- `FileStream/web/static/css/app.css`
- `FileStream/web/static/js/direct-ads.js`
- `FileStream/web/static/js/player.js`
- `FileStream/web/static/js/theme.js`
- `FileStream/web/templates/base.html`
- `FileStream/web/templates/components/ad_reservation.html` (new)
- `FileStream/web/templates/components/ad_scripts.html`
- `FileStream/web/templates/components/ads.html`
- `FileStream/web/templates/folder.html`
- `FileStream/web/templates/landing.html`
- `FileStream/web/templates/play.html`
- `tests/test_direct_js.py`
- `tests/test_ui_ads.py` (new)
- `deploy/ads/README.md`
- `deploy/ads/UI-FIXES.md` (new)

No files were deleted for this UI task. Existing unrelated pending changes were
preserved, including previous source cleanup, configuration and deployment work.
