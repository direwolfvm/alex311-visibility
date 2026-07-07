# Phase 0 spike — findings (2026-07-07)

**Result: PASSED, fully headless.** Pure stdlib Python (`urllib`), no browser, no Playwright.
One run proved: bootstrap → `getServiceRequestList_v2` (1-week window, 50 records) →
`getServiceRequest` detail → server-side photo download (9.7 MB JPEG). Run it yourself:
`python3 spike/phase0_spike.py`.

## Open question #1 — guest `aura.token`: RESOLVED

There is **no guest token to extract.** The bootstrap page embeds a JSON literal
`var auraConfig = {...}` in an inline script. Its token mechanism:

- `auraConfig["eikoocnekot"]` ("tokencookie" reversed) would name a cookie carrying the
  CSRF token — **absent for guest sessions** (no such key, no such Set-Cookie).
- The page JS then takes the else-branch: `auraConfig.token == null` → sets
  `auraConfig.csrfV2 = true`. Guests operate tokenless.
- Sending `aura.token=null` (the literal string) with the guest cookie jar → HTTP 200,
  `state=SUCCESS`.

So the bootstrap parse is just:

1. `GET /customer/s/service-requests?...` with a cookie jar (sets `CookieConsentPolicy`,
   `LSKey-c$CookieConsentPolicy`, `renderCtx`).
2. `re.search(r"var auraConfig = ", html)` → `json.JSONDecoder().raw_decode` at that offset.
3. `auraConfig.context` gives `fwuid`, `app`, `loaded` — pass a minimal
   `{mode, fwuid, app, loaded, dn:[], globals:{}, uad:false}` as `aura.context`.

**Stability note:** the parse depends only on the `var auraConfig = ` marker and standard
Aura framework behavior, not on Incap311 specifics. Client must still re-bootstrap on
framework-mismatch errors (fwuid rotates on redeploys) and *should* handle the
token-cookie branch (`eikoocnekot`) in case Salesforce ever turns it on for guests —
`phase0_spike.py` already implements both branches.

## Open question #2 — private photos: PARTIALLY RESOLVED

The downloaded photo had `private: false` and fetched fine server-side with the guest jar
(no CORS, `Content-Type: image/jpeg`, valid JPEG magic `ffd8ffe0`). All 39 media entries
on the sampled page were `private: false`. **No `private: true` sample was encountered**,
so behavior for those is still unverified — Phase 1 media downloader should treat a 401/403
on a `private: true` URL as expected-and-skip, not as a client failure.

## Other confirmations

- List call returns 50 records for `page_size: 50`, 1-week window — no volume error.
- Detail `contact` is `{}` for guests — no reporter PII, as expected.
- List record fields match the reference model, plus `service_versionId`.
- Detail fields match, plus extras: `additional_details`, `address_details`, `address_id`,
  `cancel_reason`, `language`, `multi_assets_geometry`, `multi_assets_layer_id`,
  `reported_location`, `service_level_agreement`, `service_notice`,
  `service_secondary_contacts`.
- Aura responses may carry a `*/` anti-hijack prefix; strip before `json.loads`
  (handled in the spike).
- `robots.txt` is empty — no crawl restrictions published.

## Consequence for the plan

The Playwright fallback is **not needed for the current portal configuration**. Keep it as
a documented contingency (if Salesforce enables guest CSRF tokens via the `eikoocnekot`
cookie path, the cookie arrives in Set-Cookie headers and is still parseable without a
browser — a real browser would only be needed if token issuance ever moves into executed JS).
