# Automatic isolated advertisement deployment

Use only the root `docker-compose.yml`. Configure `.env` and run
`sudo docker compose up -d --build` for installation and updates. Current Docker
Engine and Compose with successful-completion dependencies and dependency
restart support are required (rehearsal uses Compose 5.7.1).

`WEB_ADS_DOCUMENT_ORIGIN` supplies the ad hostname automatically; do not configure
`WEB_ADS_HOST`, `WEB_ADS_PUBLIC_DIR` or external network names. The origin must be
HTTPS and differ from every application hostname. Both application and ad DNS
records must resolve to this server. Port 80 must support HTTP ACME challenges;
port 443 must be publicly reachable. Set `ACME_EMAIL`, valid bot/database settings
and the four banner keys, dimensions and original invoke URLs in `.env`.

## Startup and updates

The exporter receives application settings but has no network. It validates
required configuration and generates public HTML, cookie-name rules, Nginx
configuration and a public Traefik route. A versioned named volume holds bundles;
a successful Nginx syntax check precedes atomic publication. Failed exports
leave the previous bundle selected and prevent dependent startup. Public banner
URLs from older bundles remain available for existing pages. Releases are retained
for rollback; monitor disk usage before any deliberate offline garbage collection.
No application secrets are written to that volume.

Source image changes and `.env` changes recreate the exporter. Compose also runs
an existing exited exporter during `up`. Dependency restart propagation restarts
Nginx after export; its health check compares the active configuration revision
with the published revision. FileStream checks its expected revision and banner
files before starting. Do not use `--no-deps` for normal updates. Nginx is restarted,
never hot-reloaded. Updates can briefly interrupt banner requests. Traefik watches
the public routing directory and ordinarily retains its container and certificates.

Nginx receives neither `.env` nor application secrets. It runs as UID 101 with a
read-only root and public volume, dropped capabilities, no-new-privileges, tmpfs,
resource limits and the existing seccomp launcher. The launcher validates Nginx
configuration before serving. Only Nginx and Traefik share the internal ad network;
the stacked filter blocks outbound connects/datagrams even toward Traefik.

Banners remain regular cross-origin documents. Parser order, four placements,
origin-only referrers, parent-window/origin message validation and benign ad
cookies are unchanged. Protected credential cookie names still return HTTP 409.
Host-only Secure/HttpOnly `__Host-` application sessions have no Domain attribute.
Inventory cookies from *all* other applications on the parent domain: register
additional private names in `WEB_ADS_SENSITIVE_COOKIE_NAMES` (comma separated,
no spaces). Unknown names cannot be classified from Cookie headers. Use a separate
registrable ad domain if a complete parent-domain cookie inventory is impossible.
The browser provider code still controls its own advertising behavior; the ad
service never forwards incoming cookies to providers. No additional advertising
formats are enabled by this deployment.

HTTPS production must retain `FILESTREAM_HAS_SSL=True`; this Compose setting
supplies the app's `HAS_SSL` regardless of the legacy `.env` `HAS_SSL` value.
Cookie names/flags and token validation code are unchanged. Browser cookies are
not cleared by Compose, but short-lived media capability tokens live in process
memory and are invalidated by an application restart. Refresh affected open pages
instead of assuming uninterrupted playback or token survival across migration.

## Existing two-file installation: planned migration

Do not apply this procedure until reviewed and authorized. Keep the same checkout
path, Compose project name, cache volume and `letsencrypt` volume. A one-time
Traefik recreation is necessary to add the file provider, public volume and new
internal network; normal updates do not require that change again.

1. Record the current commit, project name, image IDs, container IDs and named
   volumes. Save the old two Compose files and `.env` in a root-only backup outside
   the checkout. Keep the original exported host bundle intact. Do not print
   interpolated Compose configuration or copy secrets into Git.
2. Back up the certificate volume and cache using your established volume backup
   process. Do not remove volumes, certificates or the old external ad network.
3. Fetch/review the new `dev` code without starting it. Confirm `.env` has the
   distinct ad origin, required credentials, ACME email and correct provider URLs.
   Remove obsolete `COMPOSE_FILE` overrides from the deployment shell/environment.
4. In a maintenance window, stop the old advertisement container using its
   recorded container ID (`sudo docker stop <old-ad-container-id>`). This removes
   the old Docker-label router before the file-provider router appears. Stop the
   old app as well during this first migration, so it cannot reference an unready
   new service. This is a planned maintenance interruption.
5. Run `sudo docker compose up -d --build` from the original project directory.
   Do not use another project name, an overlay, `down`, or `--remove-orphans`.
   Check `sudo docker compose ps -a`: exporter exit 0, ad service healthy and the
   other three services running. Check HTTPS and Android ads before closing the
   maintenance window. Check playback/downloads with authorized sample media.
6. On failure, stop only the new application/ad service containers and restore
   the saved old checkout/Compose files and private `.env`. Use the same project
   name and original host bundle. First apply **only** the old Traefik definition:
   `sudo docker compose -f docker-compose.yml -f docker-compose.ads.yml up -d --no-deps traefik`.
   Confirm that Traefik no longer uses the file provider. Only then restore the
   old application/ad services:
   `sudo docker compose -f docker-compose.yml -f docker-compose.ads.yml up -d --no-deps ad-documents filestreambot`.
   Do not start the old Docker-label ad router while the new file-provider router
   is still active. Keep the new exited exporter/public volume for investigation;
   never use orphan cleanup or remove either public bundle during rollback.

A production migration has not been performed automatically. The isolated test
uses synthetic credentials, no public ports, no Docker socket and a replacement
external bot process; it does not prove Telegram connectivity, fresh ACME issuance
or real Android advertising delivery. Existing Android creative evidence is
recorded separately in [VALIDATION.md](VALIDATION.md).

## Regression checks

Run normal pytest for Python/browser regressions. Opt in to the isolated Docker
rehearsal with `RUN_COMPOSE_TESTS=1`; it uses only uniquely named test resources
and deletes only those test resources. `AD_SERVICE_TEST_IMAGE` and
`AD_SERVICE_DOCKER='sudo -n docker'` enable the independent seccomp/network tests.
Never supply production credentials to rehearsal tests.
