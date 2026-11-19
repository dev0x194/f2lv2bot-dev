> Historical security evidence, retained intentionally. The current source is Test A
> only, controlled by WEB_ADS_ENABLED with safe default disabled. Old mode/gate
> descriptions below record earlier stages, not current configuration. The same
> origin risks remain; see [current migration and recovery guidance](README.md).

# Direct Adsterra integration security audit — 2026-11-18

Decision: **do not enable direct advertising JavaScript on the current FileStream
application origin**. Retain Test F. This is a browser authority conflict, not an
ad availability/account approval finding. No evidence here alleges malicious
behavior by the provider. Runtime code, configuration and deployment are unchanged.

## Official installation and supplied configuration

The [official HTML installation guide](https://help-publishers.adsterra.com/en/articles/5210780-adding-ads-to-a-static-html-site),
linked from the supplied [code snippets collection](https://help-publishers.adsterra.com/en/collections/2274770-ad-units-and-code-snippets),
documents Popunder before closing head, Social Bar before closing body, banner
snippets in the body and Smartlinks as navigation links. These placement rules do
not promise security isolation from the publisher's document.

The local public configuration was checked without displaying private settings:
Popunder uses accountut.com; desktop/mobile Social Bar and the three banner sizes
use bellnewyork.org; Smartlink uses ardance.org. All four banner placements retain
their configured keys, dimensions and invocation URLs. The 300x250 placements can
share a zone while retaining independent configuration. No provider URL was
invented, rewritten, fetched as an ad, proxied or mirrored during this audit.
The original test/ad-test.html is the existing reference implementation.

## Sensitive pages and browser state

| Surface | Browser-visible authority/data | Direct script decision |
| --- | --- | --- |
| `/gen/{public_id}` / play.html | `#file-config` contains pageToken, public ID, stream/download token paths and share URL; page includes filename, size, codecs and optional uploader | Block |
| `/watch/{path}` | Legacy link resolves/redirects to a public file page | Block |
| `/gfolder/{public_id}` / folder.html | File names, public IDs/share URLs, MIME/size/category, optional uploader and per-file token endpoint paths; folder/browser navigation state | Block |
| `/folder/{folder_id}`, `/folderm/{folder_id}` | Legacy resolution/redirect to folder flow | Block |
| `/get-stream-token/{path}`, `/get-download-token/{path}` | JSON responses issue media capabilities after page-token/session checks | Never inject HTML/scripts |
| `/stream/{token}`, `/file/{token}`, `/thumbnails/{token}/...` | Media capability paths; download URL can include session ID for external clients | Never inject HTML/scripts |
| `/`, `/legal`, `/privacy`, rendered error pages | Usually no page token in their own HTML; still share application origin, storage and credentialed fetch authority | Do not treat as a safe advertising origin |
| `/status`, `/health`, `/metrics` | Operational JSON/text responses, not advertising HTML; existing access policy unchanged | Never inject HTML/scripts |
| Browser storage | Theme preference, per-file resume position keyed by public ID, folder/file navigation identifiers in sessionStorage | Readable by allowed same-origin script |

FileStream-issued media capabilities are distinct from raw Telegram bot/API or
MongoDB credentials. The audit does not establish that server `.env` values or
Telegram's internal file access hashes are exposed in these templates.

`FileStream/web/templates/dl.html` contains a legacy external unpkg icon script.
The current `/dl/{path}` handler returns HTTP 403, and no active server renderer
reference to that template was found. This does not establish a live leak, but the
template must undergo a fresh audit before any future reactivation.

## Confirmed exposure mechanism

`play.html` serializes `pageToken` in JSON in the DOM. The real token handlers
validate that token and its session binding, then return authorized URLs. A script
loaded directly from another host executes with the embedding page's authority:
its source host does not constrain its DOM access or same-origin fetches.

A direct script can read the page token or obtain a fresh page response for a known
file URL, supply `X-Page-Token`, and receive the authorized download response. A
provider script on the homepage can do the same for a file identifier available
from browser state or known to that script. This does not demonstrate arbitrary
file enumeration or a server authentication bypass; it demonstrates why giving
an untrusted script the application's browser authority defeats client-side
separation. Scripts can also modify controls, observe future clicks/DOM changes,
or intercept fetches and media URLs after initial parsing.

Secure, HttpOnly, host-only `__Host-fs_page_session` remains essential: it prevents
cookie-value reads and sibling-domain shadowing. It does **not** stop an executing
same-origin script from sending that cookie through normal fetch requests. The
cookie's SameSite setting is not a boundary between scripts executing within the
same document origin. An unsandboxed same-origin iframe can read the parent DOM
and storage and make the same requests. Placing direct scripts only on public
routes does not remove that authority.

The real privacy middleware's CSP restricts base/object, framing ancestors and
form actions; it does not create a sandbox for permitted JavaScript. Even adding
a nonce or provider script allowlist would authorize that script, not reduce its
privileges. HttpOnly, no-referrer, no-store, minification, closures and late script
placement cannot provide complete isolation. An opaque sandbox changes the
execution model and previously failed Android creative delivery. Reintroducing
it is not the requested direct integration and is not newly validated here.

## Automated evidence

`tests/test_direct_ad_security.py` uses mocked HTTPS publisher/provider origins,
fixture credentials, real V3 rendering/token handlers and the real privacy
middleware on file/token responses. It never contacts production or advertising
endpoints and emits only boolean capability results.

Three Chromium scenarios demonstrate the security conflict:

1. Direct script on a file page reads its DOM token and obtains an authorized
   download capability while the HttpOnly cookie value remains unreadable.
2. Direct script on the homepage obtains a file page and an authorized capability
   through same-origin requests; it can read local/session storage.
3. Script in an ordinary same-origin ad-only iframe reads the parent DOM and
   storage and obtains an authorized capability.

These are synthetic capability tests, not proof of provider misconduct, live ad
fill, physical Android behavior or counted impressions. Existing tests continue
to cover isolated banner sizes/breakpoints, ordering, duplicate prevention,
delayed/failing creatives, optional formats, Smartlink clicks, cookie rules,
playback/seeking and downloads. No direct banner scheduling implementation is
claimed tested because the direct mode did not pass the security gate.

## Implementation and deployment decision

Only this report and the synthetic security test are added. Do not put direct
banner, Social Bar or Popunder scripts in base.html, play.html, folder.html or
public routes. The optional labeled Smartlink remains user-initiated navigation;
it is not a script or replacement iframe endpoint.

There is no direct-mode rollout to approve yet. Retaining private streaming on
`tdrp.cc` requires keeping a compatible different-origin execution boundary, such
as the current ad-document service, or obtaining official provider-hosted isolated
HTML tags. A broader architecture moving privileged file pages to another origin
would also retain an additional public origin and falls outside this request.
Neither provides the requested unrestricted-script, one-origin privacy guarantee.

Test F is unchanged, so no rollback operation is necessary. Do not remove
ad-export, ad-documents, their public volume/network, DNS or existing certificates.
No production deployment, commit or push is authorized by this audit.

## Physical Android check procedure (after a safe replacement is approved)

Use the same Android Chrome/device and mobile-data network, keeping VPN,
ad-blocking and DNS-filtering settings fixed. Avoid private browsing or clearing
cookies between steps. On an approved sample-file preview:

1. Observe top 320x50 and bottom 300x250; record visible creatives and timing.
2. Use an approved desktop viewport to verify 728x90 and inline 300x250 separately;
   desktop-site mode does not itself guarantee a specific CSS viewport width.
3. Reload normally, navigate away/back, close/reopen Chrome using the same profile,
   and revisit; record each result without inferring unobserved scenarios.
4. Play and seek sample media, download a sample file, and confirm layout/control
   access during loading/failure. Leave Popunder and Social Bar disabled initially.
5. Inspect approved preview diagnostics or remote DevTools for statuses, console
   failures and frame origin; never export raw cookies, media URLs/tokens or HARs
   containing credentials. Use sanitized screenshots/boolean summaries.
6. Confirm publisher-domain/zone attribution and accepted impressions through the
   provider's reporting/support. Referrer headers, iframe load and visible content
   alone do not prove accounting. Do not click ads or loop automated reloads to
   manufacture impressions. Request provider-approved test traffic where available.

No new real Android creative, reload, browser-restart or accounting validation
was performed for this audit. VPS responses do not determine Android delivery.


## Final validation results

The three focused audit scenarios passed. Full regression rerun:
**57 passed, 1 skipped in 212.33 seconds**, using:

```sh
RUN_COMPOSE_TESTS=1 \
AD_SERVICE_TEST_IMAGE=filestream-ad-documents:test-g-regression \
AD_SERVICE_DOCKER='sudo -n docker' \
WEB_MEDIA_FIXTURE=/tmp/filestream-test-trailer.mp4 \
python -m pytest -q -rs
```

The skipped case is the approved HTTPS preview reload/navigation/browser-restart
test (`tests/test_ad_cookie_browser.py`); `AD_COOKIE_PREVIEW_URL` was not supplied.
The isolated Compose rehearsal and Docker seccomp/cookie tests ran successfully.
This rerun does not add real Android or impression-accounting evidence. Python
formatting, whitespace and configured-credential scans passed. Existing live
FileStream, advertisement and Traefik container IDs/start times were unchanged.
Only this report and `tests/test_direct_ad_security.py` are new; no application,
template, environment, Compose, Dockerfile or advertising URL was modified.

## Disabled experimental implementation (2026-11-19)

The optional `direct_js` implementation is now documented in [the current guide](README.md).
It requires an exact risk acknowledgement and script-host allowlist, remains off by
default and was not deployed. The additional `direct-mode` browser scenario runs
the actual parser-inserted loader and confirms the same token, storage and
credentialed-download exposures. This finding does not reverse the audit's
recommendation to retain cross-origin isolation. Test assertion success represents
successful reproduction of the exposure, not satisfaction of privacy requirements.
