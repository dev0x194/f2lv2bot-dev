# Advertising validation record — 2026-11-17

## Physical Android, reported by the owner

The owner reports real creatives in all four placements across Android tests:
mobile top 320x50, mobile bottom 300x250, desktop top 728x90 and desktop inline
300x250. Two additional Android recordings are reported to show real top and
300x250 creatives after navigation to the cookie-fix preview, without visible
HTTP 409. Earlier successful captures reported HTTP 200, protected-cookie
indicator 0 and completion of the cookie metadata audit. These are owner-reported
physical-device observations, not synthetic results or inferred advertising fill.

Screenshots, recordings and diagnostic JSON files were described in conversation
but are not exposed as files/images in this workspace. No claim is made that the
agent inspected their raw contents or knows the exact Android cookie names.
No private diagnostic files, cookie values or session tokens are committed.

| Scenario | Physical Android evidence reported by the owner |
| --- | --- |
| Mobile top 320x50 | Real creative confirmed |
| Desktop top 728x90 | Real creative confirmed |
| Desktop inline 300x250 | Real creative confirmed |
| Mobile bottom 300x250 | Owner explicitly confirms all four placements displayed real ads |
| Navigation to preview on two additional recordings | Real top and 300x250 creatives; no visible HTTP 409 |
| Every reload step without cookie clearing | Complete physical sequence not explicitly confirmed; automated sequence passed |
| Closing/reopening Android Chrome without cookie clearing | Physical scenario not explicitly confirmed; automated persistent-profile sequence passed |

The physical-navigation evidence supports the corrected integration. It does not
establish an unobserved Chrome process-restart scenario, exhaustive reload count,
unknown cookie scope, or future provider inventory. Those limits remain explicit.

## Automated checks

The final full-suite rerun completed with **54 passed, no skips**:

```sh
AD_SERVICE_TEST_IMAGE=filestream-ad-documents:test \
AD_SERVICE_DOCKER='sudo -n docker' \
WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4 \
AD_COOKIE_PREVIEW_URL=https://tdrp.cc/ad-v3-preview/cookie-fix-320.html \
python -m pytest -q
```

The opt-in HTTPS preview test is synthetic **only at the provider script**. It
uses the deployed ad service's actual HTTPS responses and cookie policy, in an
isolated temporary Chromium profile. It checks clean F, reloads, navigation,
V3 reloads, browser process close/reopen with persisted benign cookies, all four
placements, host-only app-cookie exclusion, domain-wide benign cookies, duplicate
protected cookies and deliberate unsafe-cookie HTTP 409 before provider execution.
Its synthetic provider creates an ad-origin cookie, inserts labelled fixture
content and throws a nonfatal telemetry error; the frame remains functional.
It is not proof of real provider fill on Android or other physical browsers.

The source/browser suites also cover responsive switching/reuse, independent
keys, parser ordering, missing/unsafe settings, disabled global/admin controls,
provider errors/empty/delayed responses, late content after collapse, authenticated
postMessages, cross-origin parent-DOM isolation, cookie prefix shadowing, Video.js
playback, byte ranges and secure stream/download token issuance/revocation.

A built disposable Docker container verifies nonroot/read-only startup, public
static documents only, protected-cookie rejection, harmless-cookie admission,
unknown paths/queries, no application credentials/mounts and kernel refusal of
outbound connections. The active preview service security recheck also passed.

## Real-network mobile viewport attempt

Server Chromium with Pixel 7 emulation (412 CSS px) selected 320x50 top and
300x250 bottom, with the expected keys, ads.tdrp.cc origin, origin-only referrer
and parent DOM access false. Actual provider invocations returned HTTP 200 with
zero-byte bodies in this environment; no generated creative was observed.
Therefore this attempt confirms mobile selection/isolation, not real creative
rendering. It does not invalidate the owner's successful Android observations.

## Cookie security and remaining risks

The former `if ($http_cookie != "")` rejected benign ad cookie state and caused
first-success/next-request HTTP 409. The replacement checks protected names
throughout the header, including duplicates. The current FileStream inventory is
host-only PAGE_SESSION_COOKIE; HTTPS uses __Host-fs_page_session, Secure,
HttpOnly, Path=/ and no Domain or legacy HTTPS fallback. No values are used as
ad-service authentication/state, reflected or logged; the service has no upstream
proxy and cannot make outbound connections. Parent DOM/media tokens remain
inaccessible from the ad origin.

Unknown cookies are not automatically safe merely because their names are outside
the guard. The guard is reviewed against this FileStream codebase. Other apps on
the same parent cookie domain require an inventory and additional private names
registered during export; cookies written while an ad is already open matter too.
Use a separate registrable ad domain when that inventory cannot be guaranteed.
The exact historical cookie writer/scope on the phone remains unattributed here.

Generic runtime-error events accompanied successful real creatives. They remain
nonterminal diagnostics. Their precise real exception is not identified by the
redacted generic event; preview-only origin/line/column metadata can help locate
it without logging messages/stacks or private data. Only explicit document or
script-fetch errors are terminal. No untested runtime-error suppression is added.

The regular iframe grants the ad's own origin normal script/storage/network
privileges. Origin separation, not an opaque sandbox, protects the app. Actual
creative clicks, provider redirects, other physical browsers and production
Telegram/MongoDB streaming are not claimed validated by these preview checks.
DNS/TLS and the dedicated ad service remain required for ongoing operation.

## Previous advertising release state (historical)

The following describes the earlier preview release, not the current live inventory.

The production FileStream bot has not been deployed or started. The Nginx test
homepage, Traefik process/certificates and persistent volumes remain unchanged.
The owner explicitly authorized committing and pushing the validated changes to
origin/dev after the final test/security review. That authorization does not
permit merging main or deploying the production bot. The controlled preview
remains available; unobserved physical restart/reload cases remain limitations,
not claims of successful real-device tests.


## Single-Compose deployment validation — 2026-11-17

The new canonical root Compose deployment remains **uncommitted and unpushed**
pending owner approval. No live container, certificate, volume or DNS migration
was performed. Existing production containers were left running throughout.

Full-suite command (test environment only):

```sh
RUN_COMPOSE_TESTS=1 \
AD_SERVICE_TEST_IMAGE=filestream-ad-documents:local \
AD_SERVICE_DOCKER='sudo -n docker' \
WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4 \
python -m pytest -q
```

Result: **54 passed, 1 skipped in 205.18 seconds**. The skipped test requires an
explicitly selected approved HTTPS diagnostic preview (`AD_COOKIE_PREVIEW_URL`);
it was not pointed at the current production application.

The isolated Compose 5.7.1 rehearsal executes `up -d --build` using one canonical
file, unique project/resources, no public ports and no Docker socket. It covers:

- Fresh missing-credential failure blocking the app and ad service, then successful
  one-shot export and healthy startup with synthetic credentials.
- All four configured placement documents, named volume mounts, no application
  credentials in ad runtime configuration, benign-cookie success and protected
  credential-cookie rejection.
- Repeat deployment, key/size changes, source rebuilds, return to earlier settings
  with intervening banner URLs retained, disabled ads and recovery after failures.
- Failed export preserving the selected known-good bundle; unchanged Traefik
  container identity during ordinary updates.
- Migration from a synthetic two-file/host-bundle layout, removal of the old
  Docker-label ad router, preservation of certificate/cache volume markers and
  byte-for-byte preservation of the old host bundle.

Existing regression tests passed for responsive frames, cookie names/duplicates/
shadowing, secure playback/downloads, Video.js, anti-adblock and the real Docker
kernel egress restriction. The real application image built successfully; a
separate network-disabled container check confirmed that its Python 3.11 startup
guard accepts the exporter image's public bundle. That check executed only a
harmless confirmation command, never the Telegram bot. `git diff --check` and a
scan comparing modified/new sources against configured private credential values
also passed without printing those values.

This does not establish a fresh public ACME certificate, production Telegram/
MongoDB connectivity, an actual EC2 migration or new Android creative delivery.
The earlier physical Android evidence remains as recorded above; frontend ad
rendering, cookie policy and provider invocation code were not changed by this
orchestration work. Existing unknown parent-domain cookie and unobserved physical
browser-restart limitations remain. Migration instructions are in README.md.

### Final single-Compose review

The review rerun completed with **54 passed, 1 skipped in 207.71 seconds** using
`-rs` and the same opt-in Docker/browser suite settings above (ad security image:
`filestream-ad-documents:review`). The skipped case is
`tests/test_ad_cookie_browser.py::test_preview_ad_cookies_survive_reload_navigation_and_browser_restart`:
no approved `AD_COOKIE_PREVIEW_URL` was supplied. It was not silently replaced by
a production URL. Physical Android delivery/restart coverage is not extended by
this rerun.

The real application-image bundle compatibility check passed again with no
network and a harmless command instead of bot startup. Compose configuration,
Python formatting, diff whitespace and configured-credential scans passed.
Both untracked files were reviewed: the deployment module and Compose rehearsal
test. No private environment, session file or temporary diagnostic is included.
Before/after IDs and start timestamps of all three existing live containers
matched exactly. No live services, volumes, DNS or certificates were changed.

Documentation now makes rollback ordering explicit: restore only the old Traefik
configuration before returning the old Docker-label advertisement router. It also
corrects a historical assertion that whole-stack startup never changes Traefik.
Keep `FILESTREAM_HAS_SSL=True`. Cookie security and authentication code are
unchanged, but short-lived access tokens reside in process memory and do not
survive an application restart; affected pages need refreshing. Forward migration
was rehearsed on isolated synthetic infrastructure; live TLS/router cutover and
the newly clarified rollback command sequence were not executed on production.
Changes remain uncommitted and unpushed pending owner review.
