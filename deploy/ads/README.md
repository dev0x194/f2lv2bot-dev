# Separate-origin banner documents

Android comparison: A/direct and B/regular iframe rendered real ads; C/sandboxed
URL, D/sandboxed srcdoc and E/minimal opaque document did not. F/a regular iframe
from tdrp.cc to ads.tdrp.cc rendered a real 320×50 ad, with parent DOM access false.
This demonstrates a compatible architecture; it does not identify the exact
sandbox restriction responsible. The owner has since reported real integrated cookie-fix creatives for all four
placements. See [the validation record](VALIDATION.md) for the precise real-device
coverage, automated tests and remaining physical checks. Do not report synthetic
fixtures as live creatives.

## Required ongoing infrastructure

- `tdrp.cc` stays on FileStream after an explicitly approved application cutover.
- `ads.tdrp.cc` requires its DNS A record, HTTPS certificate and this independent
  service for as long as banners are enabled. Keep DNS-only routing for the
  controlled initial validation. Provider acceptance of this origin is required.
- Keep Traefik's existing image, routers, certificate storage and volumes. The
  overlay adds one exclusive ad-host router and an independent container.
- Never run the diagnostic F router and production ad router for ads.tdrp.cc
  simultaneously. Likewise, never run Nginx's test router and FileStream's router
  for tdrp.cc simultaneously. Stopping a container does not delete its files or
  volumes. No `down -v` commands are necessary.

## Configuration/export

Existing KEY/INVOKE_URL/WIDTH/HEIGHT values and per-bot overrides remain authoritative.
Only the additional `WEB_ADS_DOCUMENT_ORIGIN` setting selects the document service;
an empty, non-HTTPS or malformed origin disables banners without executing an
alternative embed. Browser code also rejects same-origin documents. Do not use
the FileStream origin or a port-only variation of the same hostname: cookies are
not isolated by port. Use a distinct hostname and explicit HTTPS parent origins.

From the checked-out repository, using the application's Python environment:

```sh
WEB_ADS_DOCUMENT_ORIGIN=https://ads.tdrp.cc python -m FileStream.web.tools.export_ad_documents \
  --parent-origin https://tdrp.cc \
  --output /home/ubuntu/deploy/filestream-ad-public/release-1
```

Repeat `--parent-origin` for every trusted alternate application origin. Run with the same
process environment and ENV_FILE/.env as the target application; existing process
variables take precedence over dotenv values. The export uses the
same WebAds settings as the renderer, including per-bot keys/dimensions. It does
not start Telegram clients or access MongoDB. It produces only public HTML,
Nginx configuration, origin/count metadata and public `compose.env` settings. It
never exports the environment wholesale. The service does not receive `.env`.
Use a fresh versioned output directory so removed/disabled slots do not linger.
Do not commit generated bundles. Re-export and switch the service bundle whenever
banner configuration changes; do not hand-edit generated HTML. Content-addressed
slot URLs fail closed with 404 when application and service snapshots differ.

The global enable flag, bot enable flag and existing database/admin switch gate
new banner frames in FileStream. Admin OFF does not revoke an already loaded
third-party document or turn the public ad files into authenticated application
resources. The ad service intentionally has no database access. Global config
OFF plus re-export produces no banner documents. Social Bar retains its existing
separate opt-in sandbox; Smartlinks remain anchors and Popunder is never executed.

## Security boundary

A regular cross-origin iframe preserves parser-inserted provider scripts and
normal ad-origin Referer behavior. Parent navigation uses `referrerpolicy=origin`,
so private paths/query parameters are not sent. Documents contain public ad
configuration only; no media capabilities or application data are passed. Incoming
messages must match both the configured document origin and the exact frame
window. Content insertion remains `content-unverified`, never proof of fill.

All FileStream set_cookie calls have been audited. HTTPS page sessions now use
`__Host-fs_page_session`, Secure, HttpOnly, Path=/ and no Domain, protecting against
sibling-domain cookie tossing as well as cookie reads. Plain HTTP development
retains the old name; do not use it for public production. HTTPS pages opened
before the cookie-name migration need reloading; old media sessions may expire.
Do not reintroduce legacy-cookie fallback in HTTPS mode.

A sibling origin is not automatically credential-safe. Rejecting every Cookie
header caused a confirmed first-success/next-request-409 defect after legitimate
advertising created cookie state. The replacement map checks **names**, anywhere
in the full Cookie header (including duplicates), and returns 409 for the reserved
fs_/filestream_ families, protected-prefix variants, known credential aliases and
additional explicitly registered private names. Values are not used for decisions,
authentication, state, HTML or logging. It does not rely on `$cookie_name`, which
could miss a duplicate/shadowed name. Benign tracking/consent cookies are permitted
for both host-only and domain-wide scopes. Protected names stay rejected even if
a harmless ad cookie happens to use one: review the metadata instead of bypassing
the credential guard.

The source audit finds one application cookie writer, PAGE_SESSION_COOKIE. Tests
require new writers to undergo inventory review. Host-only `__Host-` cookie
protections and no legacy HTTPS fallback remain essential: an ad document cannot
read the app's private cookie or shadow it with Domain=.tdrp.cc. A malformed client
or domain-scoped legacy credential is rejected before third-party HTML is served.
The service is static: no cookies are set, no authentication/state is derived
from them, no request-cookie proxying exists, and outbound connections are denied.
The browser independently scopes provider HTTP cookies to the provider origin.

A Cookie request header does **not** contain its Domain/host-only flags, HttpOnly
flag, writer or purpose. Do not claim arbitrary unknown names are safe for other
applications. The default policy is reviewed against this FileStream deployment;
if other apps share the parent cookie domain, audit/register all their private
names with repeated `--sensitive-cookie-name NAME` options during export. Do not
pass values. If a complete inventory/migration cannot be ensured (including
cookies introduced while ads are already running), use a separate registrable
advertising domain instead of relying on a sibling subdomain or this name filter.
Never loosen credential prefixes to accommodate an unidentified cookie.

During preview validation, `/cookie-audit.html` is a provider-free local metadata
page. It shows cookie names and available domain/path/flags only, without values,
and does not upload them. Cookie Store cannot enumerate HttpOnly cookies; use
Chrome DevTools locally for that scope audit. The safe X-Ad-Protected-Cookie flag
indicates whether the server detected a protected name, including HttpOnly ones.
Do not export a raw Cookie header, storageState or unsanitized HAR. Exact names
on the user's Android profile have not yet been supplied; provider origin scripts
can create first-party cookies even though the ad server emits no Set-Cookie.

No Access-Control-Allow-Credentials response is added. The `/health` response is
public readiness data, not an authenticated endpoint. Generic runtime-error
messages are diagnostic and nonfatal; only explicit document/script-fetch failure
causes terminal cleanup. A loaded script or created iframe is still not proof of
real advertising fill.

The runtime image contains static Nginx and a small Linux seccomp launcher, no
application Python, database client, .env, sessions, Docker socket or writable
public files. It runs as UID 101, read-only, no capabilities, no-new-privileges and
bounded tmpfs/resources. A dedicated internal network separates it from the app.
The launcher additionally denies connect/sendto/sendmsg/sendmmsg (and io_uring bypasses) across exec/fork,
so the worker cannot reach app/internal services even by using Traefik as an
outbound proxy. The filter supports x86_64 and aarch64; fail startup on unsupported
architectures/filter failure. Do not override the image entrypoint. This stacks
with Docker's default seccomp policy. Operator `docker exec` is privileged
administrative access and is outside the worker boundary.

The service exposes only exact content-addressed banner paths and health, rejects
unknown slots, query strings and unsupported methods, and has no proxy/fetch
endpoint. Access logging is off; errors do not echo headers/credentials. CSP
restricts embedding to explicit HTTPS application origins, disables base/object
and form actions. Permissions Policy disables camera, microphone, location,
payment and USB. No restrictive script/frame host allowlist is invented for
unknown nested provider hosts. Provider JS executes with its own ad-origin
storage/network privileges, so this is still third-party code. Browser origin
rules protect the parent; the regular iframe does not impose an opaque sandbox.
Do not add app cookie Domain attributes or weaken app CORS. Do not click ads as
part of testing. Automatic redirects/popunders are not configured; browser
cross-origin navigation protections are not a guarantee against unwanted
behavior by every future provider creative.

## Prepared deployment procedure — requires explicit approval

These are reviewable instructions, not steps executed during implementation.
The approved preview switch is already complete: the hardened ad service is
active, F is stopped/preserved, and the internal network is attached to the
existing Traefik process without a restart. The Nginx homepage is unchanged.
The commands below describe setup on a new host; do not recreate already-existing
networks or start a competing F router on this host.
Use the freshly exported public `compose.env` with the existing application env:

```sh
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml config --quiet
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml build ad-documents
```

One-time network setup, after approval (inspect first; create/connect only if
missing). This live network attachment does not stop/recreate Traefik:

```sh
sudo docker network inspect filestreambot-ad-documents
sudo docker network create --internal filestreambot-ad-documents
sudo docker network connect filestreambot-ad-documents f2lv2bot-dev-traefik-1
```

The overlay declares this externally managed network so future Compose operations
preserve the intended Traefik attachment. The host-only test router is unchanged.
After approval, stop only the old F ad router, then start the new ad service:

```sh
sudo docker compose -f /tmp/filestream-test-f/docker-compose.yml stop ad-origin
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml up -d --no-deps ad-documents
```

Verify HTTPS, `/health`, all slot URLs, cookie rejection and actual origin headers.
Obtain the Android integrated-V3 result using a separately approved preview before
claiming the live application issue fixed. The production cookie/CSP hardening
must also pass that real-provider check; a successful diagnostic F alone does not
prove those additional restrictions compatible.

A full application cutover is a **separate explicit approval**: build the updated
filestreambot image, stop the original Nginx test (`test/ad-test`), and start only
filestreambot with the overlay/environment above using `--no-deps`. Do not run
`up` for the whole stack. Keep Traefik running. Existing application/certificate
volumes and databases are unchanged.

To roll back an ad-service change, stop only `ad-documents` and restart the F
container (never both at once). To roll back an application cutover, stop only
filestreambot before restarting the original Nginx test. Neither operation needs
volume deletion. The ad service can be maintained/stopped independently; the
application collapses missing/failed ads and streaming/downloads remain independent.

## Tests

```sh
sudo docker build -t filestream-ad-documents:test deploy/ads
AD_SERVICE_TEST_IMAGE=filestream-ad-documents:test \
AD_SERVICE_DOCKER='sudo -n docker' \
WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4 python -m pytest -q
```

Runtime tests use disposable unlabeled localhost containers, never public routers.
They test static documents, cookie/method/path rejection, nonroot startup and
kernel egress refusal. Browser fixtures use the actual V3 templates/renderer,
actual generated ad documents and all sizes, with synthetic provider JS. A
separate sibling-origin browser test covers real origin/referrer mechanics,
cookie-tossing protection and forged-message rejection. None proves live fill.


Current validation: see [VALIDATION.md](VALIDATION.md). The preview is published
with sample file data, and the hardened ad service is active. The production bot
and Nginx homepage have not been switched. Earlier 34-test and first-success-only
notes are superseded by the 54-test cookie-policy validation and the owner's
reported real Android results, including all four placements and two additional
navigation recordings. Physical Chrome process-restart coverage is not claimed.


Configuration updates: restart/recreate only the ad container after validating
its new versioned bundle; do not use `nginx -s reload` with this single-process
configuration. A diagnostic HUP reload caused HTTP 500/connection-pool errors
with few actual sockets; a container restart restored service. Do not bypass the
network filter or increase resource limits to mask that reload failure.


Cookie-policy regression validation uses `AD_COOKIE_PREVIEW_URL` pointing at the
approved HTTPS preview. The live-origin browser test uses a dedicated temporary
profile and a synthetic provider script: it exercises F, repeated reloads,
navigation, browser close/reopen, host-only ad cookies, harmless domain cookies,
protected cookies, nonfatal exceptions and all four sizes. No real user cookie
profile or values are exported. This proves cookie-policy mechanics, not provider
creative delivery; physical Android repeated-load sign-off remains required.


Cookie-policy fix validation (2026-11-17): **54 passed, no skips** with the Docker,
video and approved-preview integration options enabled. Android already proved a
real integrated creative before the blanket-cookie gate rejected a subsequent
request. The owner now confirms real creatives in all four placements and two
additional successful navigation recordings after the cookie fix, without visible
409. Git commit/push is authorized after final review; production deployment is
not authorized. No physical browser-restart success is inferred from that report.


## Application cutover after separate explicit authorization

Keep the current ad DNS record and existing Traefik certificates. From the dev
checkout, export a fresh non-diagnostic public bundle into a durable versioned
folder (not /tmp), using the same env/configuration as the application:

```sh
WEB_ADS_DOCUMENT_ORIGIN=https://ads.tdrp.cc python -m FileStream.web.tools.export_ad_documents \
  --parent-origin https://tdrp.cc \
  --output /home/ubuntu/deploy/filestream-ad-public/release-1
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml config --quiet
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml build filestreambot ad-documents
```

Review HTTPS mode (HAS_SSL=True), additional private cookie names for other apps,
parent origins and the existing external internal-network attachment first. After
explicit cutover approval, start/recreate only ad-documents with the new bundle
using the same Compose arguments and `up -d --no-deps ad-documents`. Check HTTPS
health and protected/benign cookie handling. Then stop only the root-domain test
router before starting the bot:

```sh
sudo docker compose -f test/docker-compose.yml --env-file test/.env stop ad-test
sudo docker compose --env-file .env \
  --env-file /home/ubuntu/deploy/filestream-ad-public/release-1/compose.env \
  -f docker-compose.yml -f docker-compose.ads.yml up -d --no-deps filestreambot
```

Do not invoke whole-stack `up`, restart Traefik, force-push, merge main, or delete
volumes/certificates. Refresh existing application tabs after the __Host- cookie
migration. Verify real file playback/downloads and each ad placement after cutover.
Rollback the app by stopping only filestreambot, then `start ad-test` with the test
Compose file. Roll back the ad bundle independently, ensuring only one ads-host
router is active. No commands in this section were executed for final validation.
