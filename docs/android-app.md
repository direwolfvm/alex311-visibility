# The Android app: what the website provides

Answers to `ANDROID_HANDOFF.md`, for the maintainer of
[alex311-visibility-android](https://github.com/direwolfvm/alex311-visibility-android).
Written 2026-10-24.

## Hosts — read this first

The site's address changed on 2026-10-06. **`https://alex311-reborn.com` is
the primary**; `alex311visibility.me` still resolves and still answers every
API path, the association files and the health check without redirecting,
but a *page* asked for there (`/r/…`, `/submit/login`, `/submit/my`) is sent
to `alex311-reborn.com` with a 308. So:

- Set the client's canonical origin to `https://alex311-reborn.com`.
- Register **both** hosts in the manifest's intent filters and the router's
  allowlist: sign-in emails and digests now carry `alex311-reborn.com`
  links, while links already in people's inboxes carry the old host.
- The association file is served, unredirected, on both hosts.

## Priority 1: App Links (`/.well-known/assetlinks.json`)

Served by `dashboard/app.py`, public, JSON, no redirect, cached an hour.
Configured by two environment variables on the Cloud Run service — **not
yet set** in production, so the URL answers 404 until they are:

| variable | value |
|---|---|
| `ANDROID_PACKAGE` | `com.herbertindustries.alex311` |
| `ANDROID_SHA256_CERT_FINGERPRINTS` | comma-separated SHA-256 fingerprints of the signing certificate(s): with Play App Signing the **app-signing** certificate; a debug certificate only as a deliberate choice |

Send the fingerprint(s) to the web side (they are public configuration) and
they go on with `gcloud run services update alex311-dashboard
--update-env-vars=…` (never `--set-env-vars`). The statement grants
`handle_all_urls`; **the manifest must narrow the paths** to `/r/`,
`/submit/login`, `/submit/my` and the exact `/submit` entry. The digest's
`/submit/digest/confirm` and `/submit/digest/unsubscribe`, the account and
admin pages, and everything under `/api` and `/submit/api` stay in the
browser — the iOS association lists exactly those three paths for the same
reason.

Verify after the variables are set:

```sh
curl -i https://alex311-reborn.com/.well-known/assetlinks.json
curl -i https://alex311visibility.me/.well-known/assetlinks.json   # same body, no redirect
adb shell pm verify-app-links --re-verify com.herbertindustries.alex311
adb shell pm get-app-links com.herbertindustries.alex311
```

## Priority 2: the sign-in handoff

Done on the web side. `dashboard/login.html` now treats Android like an
iPhone: when a sign-in link opens in a browser that did not ask for it, the
page offers "Open in the app" first on any phone and "Sign in here" second,
both everywhere, and consumes nothing until one is chosen. The app link is
`com.herbertindustries.alex311-reborn://signin` + the original query string,
passed through still encoded (`mode`, `oobCode`, `apiKey`, `continueUrl`,
`lang`, `tenantId`). The sign-in email's wording says "on your phone"
rather than "on an iPhone". The six-digit code and pasted-link paths are
unchanged, as are the send limits (5 an hour per address, 60 s apart, 20 an
hour per caller, `429` with `Retry-After`).

## Priority 3: Google sign-in

Identity Platform project `permitting-ai-helper`, tenant **`alex311-qfnem`**
(the only tenant for this site; never a second one). Google is enabled on
the tenant. The **web application OAuth client id** the tenant's Google
provider uses — the audience for `googleWebClientId` — is

```
650621702399-f23j3l0mv6uv63b19d1chst41pv4shem.apps.googleusercontent.com
```

It is an identifier, not a secret (it is in every web sign-in page). Do not
reuse the iOS OAuth client id. What is still to do, and needs the signing
certificate fingerprint(s) first: registering an **Android OAuth client**
for `com.herbertindustries.alex311` in the same project (Google Cloud
console → APIs & Services → Credentials → OAuth client → Android, one per
certificate: debug, upload, Play app-signing). Then the Credential Manager
→ Google ID token → Firebase `signInWithIdp` with `tenantId` →
`POST /submit/api/session` flow can be tried with a test account.

The API keys in the project carry no referrer or app restrictions today.

## Push

Not for Android yet. `/submit/api/devices` and `alex311.push` are APNs
only (hex tokens, `production`/`sandbox`, an APNs payload). Polling with
local notifications is the Android answer until an FCM sender is built; the
handoff's plan for that is sound and is not scheduled.

## The contract

Everything in the handoff's "Shared API contract" table is as described
and is covered by this repository's tests; nothing in it changed for
Android. Two additions since the handoff was written: `GET
/api/request/{case}` now carries `support: {support, oppose}` (community
support counts; `PUT /submit/api/stance/{case}` behind the sign-in), and
`/api/threads/{case}` (related requests; `PUT /submit/api/threads/{case}/{other}`
for votes). Neither is required for the app.
