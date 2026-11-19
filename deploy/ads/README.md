# FileStream Test A advertising and two-service deployment

## Configuration

`.env` is the only runtime configuration file. Keep **WEB_ADS_ENABLED=True** to
permit advertising. Missing, empty or invalid values safely disable it. Only
`True`/`1` enables; `False`/`0` disables (case-insensitive, surrounding whitespace
ignored). False disables every banner, optional format, provider request and ad
initialization. Database/admin disable controls still suppress ads per client.

Each banner activates automatically from all four valid fields. No per-banner
switch, mode, acknowledgement, ad-origin or environment host-approval variable exists.

| Prefix | Width | Height |
|---|---:|---:|
| WEB_ADS_MOBILE_TOP_BANNER | 320 | 50 |
| WEB_ADS_MOBILE_BOTTOM_BANNER | 300 | 250 |
| WEB_ADS_DESKTOP_TOP_BANNER | 728 | 90 |
| WEB_ADS_DESKTOP_INLINE_BANNER | 300 | 250 |

Append `_KEY`, `_INVOKE_URL`, `_WIDTH`, `_HEIGHT` to each prefix. Copy the original
publisher values verbatim. Empty/partial/invalid groups do not load; partial groups
emit a once-per-placement warning containing no values. Keys, HTTPS URLs, exact
reviewed hosts and supported positive dimensions are validated. A `/22/<key>` URL
must match the key. These are script URLs, never iframe document URLs.
`MULTI_TOKEN<n>_WEB_ADS_...` may override zone fields without changing the master.
Internal script hosts live in `FileStream/web/direct_ads.py`; environment URLs
cannot approve arbitrary hosts. No publisher identifiers are built into defaults.

Optional formats also require the master switch plus an explicit valid URL:

- `WEB_ADS_POPUNDER_URL`: empty is off; may cause popups/navigation.
- `WEB_ADS_DESKTOP_SOCIAL_BAR_URL` and `WEB_ADS_MOBILE_SOCIAL_BAR_URL`: empty is off
  for that device. Exactly one tag is selected per initial viewport; overlays can
  interfere with interaction and need manual review.
- `WEB_ADS_SMARTLINK_URL`: empty is hidden; otherwise a labelled Sponsored Link
  requires a deliberate click. There are no automatic Smartlink redirects.

There are no optional-format enable switches. Before migrating, CLEAR URLs belonging
to previously disabled formats; populated old URLs would otherwise activate them.
The approved migration cleared previously inactive optional-format URLs while
preserving original banner settings. Preserve those effective choices on updates;
this source/Git review does not modify the live `.env`.

## Execution and known limitations

This is Test A: direct third-party JavaScript in the FileStream document. The
local loader emits parser-inserted `atOptions` then the blocking original provider
script at each body placement. This supports `document.write`. A zone initializes
once per document. Only initial-viewport placements load; resizing does not create
requests, and reloading selects a new viewport layout. Failure does not replace the
page or disable backend authorization. Banner scripts are emitted after the main content, footer, file configuration and
local UI/player initialization. CSS grid keeps the top banner visually above the
content without moving or reloading its iframe. Inert, viewport-specific space
reservations prevent a late banner from shifting content. A stalled provider still
delays DOMContentLoaded and later advertising tags; it no longer holds up the main
content or essential controls. An explicitly configured head Popunder can still
delay initial parsing. See [UI validation and rollout](UI-FIXES.md).

**KNOWN SECURITY DOWNGRADE:** provider scripts can read pageToken, metadata and
browser storage, request same-origin capabilities with the browser's credentials,
and read signed media URLs. HttpOnly protects cookie values, not fetch authority.
CSP, public-path restrictions and ordinary same-origin iframes do not isolate code.
Removing acknowledgement configuration does not mitigate any of these findings.
See the retained [security audit](DIRECT-INTEGRATION-AUDIT.md). Existing token/session
validation and host-only `__Host-` cookies remain enforced. No Telegram/MongoDB secret
exposure or arbitrary file enumeration is claimed by the synthetic audit.

## Installation and normal updates

Prerequisites: Docker Engine + Compose plugin, application DNS pointing to the
server, reachable ports 80/443, working ACME email, outbound Telegram/MongoDB/ACME.
The only services are `filestreambot` and `traefik`; no host Python or exports needed.

```
git clone <repository-url>
cd f2lv2bot-dev
git switch dev
cp example.env .env
nano .env
sudo docker compose up -d --build
```

Updates: `git pull --ff-only origin dev` then `sudo docker compose up -d --build`.
Application startup validates required credentials before client initialization,
without logging values. The original cache/TLS volume identities and proxy network
remain; multi-token Docker routing, ACME and HTTPS redirection are retained.
No file-provider/ad-public dependency remains. The old file provider was inspected:
it contained only the ad router, no non-ad middleware/TLS/application routing.

## Completed production migration and recovery record

The source cleanup did not alter production. The approved cutover was completed on
2026-11-19: only FileStream and Traefik now run, health is HTTP 200, MongoDB is
connected, and clients 0–4 are authorized. The primary authorization key was
preserved in the new session volume. Real playback/seek/ranges and a complete
434,274,407-byte Telegram-backed download passed. Android advertisements and
provider accounting remain NOT VERIFIED. Known same-origin and parser-delay risks
remain unresolved. Snapshot with the complete old
source, four-service Compose, private environment, tagged image set and mount/label
metadata: `/home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z`.
Its `source/rollback-images.json` was validated with its OWN old Compose definition.
Private directories are mode 700; credential files are 600. Do not upload snapshots.
Refresh the snapshot/candidate if live configuration changes after this review.
Before installing it, use `sudo cmp -s .env /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/source/.env`; stop if it differs so newer settings are not overwritten.

Historical approved one-time steps (already executed; do NOT reseed a running
client or rerun this migration for ordinary updates):

```
cd /home/ubuntu/deploy/f2lv2bot-dev
# Inspect any standalone router and stop only its verified project if running.
sudo docker ps --filter label=com.docker.compose.service=ad-test \
  --format '{{.Names}} project={{.Label "com.docker.compose.project"}}'
# If present: sudo docker compose -p VERIFIED_PROJECT -f test/docker-compose.yml stop ad-test
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/target.env .env
sudo docker compose config --quiet
# One-time migration: preserve the recorded container-local primary session.
# Refresh the read-only backup immediately before the approved interruption.
sudo docker compose build filestreambot
sudo docker compose create --no-build filestreambot
sudo docker cp \
  /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/approved-cutover-20261119T161322Z/primary-at-cutover.session \
  f2lv2bot-dev-filestreambot-1:/sessions/FileStream.session
sudo docker compose up -d --build
curl --fail https://tdrp.cc/health
curl --fail https://tdrp.cc/status
```

The one-time session-copy commands apply to this recorded `/app/FileStream.session`
installation; recheck the private session manifest if settings change. Keep the
checkout/image unchanged between create/copy/up. Normal fresh installs and updates
still use the single standard up command. The new named `filestream-sessions` volume stores primary authorization under
`/sessions` (image directory mode 700), so later normal updates retain it. This is
a non-advertising data volume, not another service; existing cache/TLS names remain
unchanged. Retain private backups before migrations.
The snapshot includes a consistent read-only SQLite copy with integrity checked;
secondary clients use in-memory sessions or strings preserved in private `.env`.

Expect application interruption and in-memory-token loss; Traefik may also recreate
because its old file-provider configuration is removed. A mode edit alone never
updates an image. Verify startup logs privately, healthy database/client counts,
unique hostname router, active computed banner eligibility and sample media actions.
Do not claim a real Telegram transfer from a health check alone.

The old services became orphans; `up` did not remove them. After the successful
checks, their recorded containers were stopped/removed with no volume or image
deletion. The following retirement commands are historical, not needed again. ONLY after new health,
media checks and approval to stop them, verify their recorded IDs/project labels then:

```
sudo docker stop f2lv2bot-dev-ad-documents-1
sudo docker rm f2lv2bot-dev-ad-documents-1 f2lv2bot-dev-ad-export-1
```

Never use blanket orphan cleanup or delete volumes. Retain the ad-public volume,
old images, advertising DNS and TLS data for recovery. New delivery does not require
the advertising hostname. DNS/certificate retirement is a separate future decision.

## Independent recovery

Do NOT combine the old image override with the NEW two-service model. Use the
complete OLD model and explicit ORIGINAL project name so volume identities match.
To restore the immediately preceding Test A deployment:

```
sudo docker compose -p f2lv2bot-dev \
  --env-file /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/source/.env \
  -f /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/source/docker-compose.yml \
  -f /home/ubuntu/deploy/filestream-rollbacks/test-a-cleanup-20261119T151702Z/source/rollback-images.json \
  up -d --no-build
```

Historical Test F remains independently recoverable using its own model/image set:

```
sudo docker compose -p f2lv2bot-dev \
  --env-file /home/ubuntu/deploy/filestream-rollbacks/test-f-20261119T122331Z/.env \
  -f /home/ubuntu/deploy/filestream-rollbacks/test-f-20261119T122331Z/docker-compose.yml \
  -f /home/ubuntu/deploy/filestream-rollbacks/test-f-20261119T122331Z/rollback-images.json \
  up -d --no-build
curl --fail https://tdrp.cc/health
```

Both manifests and original image tags are preflight-validated. Actual live rollback
was NOT executed. Recovery uses archived code/images, not removed source files.
After recovery keep using the old definition until a reviewed repair; the normal
new Compose command would reapply the new design. No volume deletion or pruning.

## Android acceptance and tests

On the approved production origin, use the same Chrome profile/mobile data/VPN/DNS
conditions. Check 320x50 and mobile 300x250; use a confirmed >=768px desktop viewport
and one normal reload for 728x90/inline 300x250. Check homepage/file/folder navigation,
play/seek, actual Telegram downloads, sharing/VLC, normal refresh, away/back and Chrome
reopening with cookies retained. Confirm no unwanted optional formats or redirects.
Global-off and missing-field behavior are automated with mocks; live switch changes
need an approved restart window. Do not automate paid impressions or click ads for
testing. Dashboard domain/zone attribution is a separate manual check.

Real integrated Android delivery/accounting remains NOT VERIFIED by this cleanup.
Mock tests do not establish it. Local tests: `python -m pytest tests/ -q -rs` with
Chromium installed; `RUN_COMPOSE_TESTS=1` adds a disposable loopback TLS rehearsal.
It uses the real application image with a synthetic HTTP command, not another bot,
no production Docker socket/credentials/volumes, and never requests advertising.

## Cleanup verification

Retired runtime files: ad document generator, provider iframe registry, old ads.js,
export/deployment entrypoints, Nginx/seccomp build files and mode-specific test cases.
Old uncommitted preview tools/assets and reports are retained in the complete private
source snapshot, not discarded. Synthetic MP4 moved to `tests/fixtures/sample.mp4`.
Only the vendor integrity tool remains under web/tools. Security exposure tests,
protected-cookie checks, media authorization/range tests and bot compatibility
facades remain. No unrelated core/bot/service compatibility file was removed.

Cache, TLS and the new primary-session state all survived the disposable update rehearsal.
Source/invocation validation does not prove paid creative delivery. Live Android
results and successful provider accounting remain explicit acceptance requirements.

## Cleanup-stage review results (before the UI fixes)

Final full suite: **91 passed, 0 failed, 0 skipped in 76.38 seconds** with
RUN_COMPOSE_TESTS=1. Retired mode-only tests are archived with old source; active
security, web, Range, player/seek, download, sharing and master-switch coverage ran.
The two-service rehearsal uses loopback TLS and synthetic app state, not production
volumes or a Docker socket. No real ad endpoints are contacted by automation.

The rebuilt review image passed runtime imports and Telegram/MongoDB constructor
checks with synthetic settings and networking disabled. This is NOT real Telegram
authorization or MongoDB connection validation in that image. Missing credentials
fail before client initialization. The private migration candidate produced four
eligible banners, no inactive optional scripts and no mode dependency. Session
backup SQLite integrity, independent rollback manifests and old image IDs passed
preflight. No live rollback was performed.

Current live health is HTTPS 200, database connected, five clients. This refers to
the existing Test A image. Live env, image/container identities, start times and
mounts were verified unchanged during source cleanup. The subsequent explicitly
approved production cutover is recorded above. Real integrated Android creatives,
Telegram file transfers and provider accounting remain NOT VERIFIED here.
No staging, commit, push, DNS change or volume deletion occurred. Production
restarts occurred only during the subsequently approved cutover.

The later [UI validation report](UI-FIXES.md) records the expanded suite and UI
changes. The following inventory records the earlier cleanup scope.

### Cleanup Git inventory

**Modified**

- `.dockerignore`
- `.gitignore`
- `Dockerfile`
- `FileStream/__main__.py`
- `FileStream/config.py`
- `FileStream/main.py`
- `FileStream/web/README.md`
- `FileStream/web/templates/base.html`
- `FileStream/web/templates/components/ad_scripts.html`
- `FileStream/web/templates/components/ads.html`
- `README.md`
- `deploy/ads/DIRECT-INTEGRATION-AUDIT.md`
- `deploy/ads/README.md`
- `docker-compose.yml`
- `example.env`
- `tests/test_compose_deployment.py`
- `tests/test_direct_ad_security.py`
- `tests/test_web.py`
- `tests/test_web_browser.py`
- `tests/web_fixtures.py`

**Created**

- `FileStream/web/direct_ads.py`
- `FileStream/web/static/js/direct-ads.js`
- `FileStream/web/templates/components/direct_ad.html`
- `tests/conftest.py`
- `tests/fixtures/sample.mp4`
- `tests/test_ads_master.py`
- `tests/test_direct_js.py`

**Deleted tracked files**

- `FileStream/web/ad_documents.py`
- `FileStream/web/provider_iframes.py`
- `FileStream/web/static/js/ads.js`
- `FileStream/web/tools/deployment.py`
- `FileStream/web/tools/export_ad_documents.py`
- `deploy/ads/Dockerfile`
- `deploy/ads/TEST-G.md`
- `deploy/ads/VALIDATION.md`
- `deploy/ads/nginx.conf`
- `deploy/ads/no-egress.c`
- `tests/test_ad_cookie_browser.py`
- `tests/test_ad_documents.py`
- `tests/test_ad_service_docker.py`
- `tests/test_provider_iframes.py`

Earlier untracked preview tools/assets, experiment documents and old preview tests
were also archived and removed from the active tree. Complete originals, including
uncommitted work, remain in the private source snapshot. No compatibility/core/bot
module was removed. The original ignored `test/` reference remains local, stopped,
and excluded from images. Generated Python caches are disposable and ignored.
