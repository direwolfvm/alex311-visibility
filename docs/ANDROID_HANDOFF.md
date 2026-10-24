# Android client handoff for the web/backend project

Snapshot: 2026-10-24. This document records the Android port's integration needs and proposed follow-up work; it does not mean the backend changes or cloud/provider configuration below have been implemented.

## References and checkout caveat

- Android repository: [direwolfvm/alex311-visibility-android](https://github.com/direwolfvm/alex311-visibility-android).
- Initial port and contributor CI: [Android PR #1](https://github.com/direwolfvm/alex311-visibility-android/pull/1), source commit `41dd46b83183fcc3cc9b0905ef092f0318b3553b`. Check the PR's current state before assuming it is merged.
- Android implementation/setup notes: `docs/ANDROID_SETUP.md`, `docs/CI.md`, and `CONTRIBUTING.md` in that repository.
- Local Android checkout: `/Users/jke/Github-local/alex311-visibility-android`.
- The local primary web checkout at `/Users/jke/Github-local/alex311-visibility` is still on initial commit `d1d0307` and contains only the repository shell. The backend is present in linked worktrees and local `origin/main` (`0d208fc`, 2026-10-23). Do not reset the primary checkout or alter another worktree merely to use this handoff. Start follow-up implementation from the actual current backend branch.
- Backend source pointers below were checked against the linked `feat/google-analytics` worktree (`7ca8a83`) and the local `origin/main` tree, rather than assumed from the empty primary checkout. Recheck them when beginning work.

The Android port was built against the existing APIs. No production backend, Firebase configuration, APNs sender, or deployment settings were changed for it. Earlier read-only production checks found `/.well-known/assetlinks.json` returning 404; that was a snapshot, not a guarantee about the site's current deployment.

## Android identifiers and behavior

| Item | Value |
| --- | --- |
| Android package / application ID | `com.herbertindustries.alex311` |
| Canonical HTTPS origin in the Android client | `https://alex311visibility.me` |
| Custom URI scheme | `com.herbertindustries.alex311-reborn` |
| Example case link | `com.herbertindustries.alex311-reborn://r/26-00039674` |
| Email handoff link | `com.herbertindustries.alex311-reborn://signin` plus the original sign-in query |
| Minimum / target Android API | 26 / 36 |
| Session cookie | `alex311_portal` |
| Sign-in methods | Email link or six-digit code; Google when an Android build is configured |
| Notifications today | Local notifications from WorkManager polling, approximately every three hours subject to Android scheduling |

The scheme matches the existing web page's “Open in the app” link. The Android app does **not** claim `alex311://`; that belongs to the City's app. Native Apple sign-in is not implemented on Android. APNs registration is not used by Android.

## Priority 1: Android HTTPS app links

### Web/backend work

Add a public `GET /.well-known/assetlinks.json` route in `dashboard/app.py`, alongside the existing Apple association route and before the static mount. Preserve the iOS route and its `APPLE_APP_IDS` configuration.

The Android statement needs the real Android package and the SHA-256 fingerprint of the certificate that signs the installed app:

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "com.herbertindustries.alex311",
      "sha256_cert_fingerprints": ["REPLACE_WITH_REAL_SHA256_SIGNING_CERTIFICATE_FINGERPRINT"]
    }
  }
]
```

The placeholder is documentation only and must never be served as a configured production association. With Google Play App Signing, use the **app-signing certificate**, rather than just the upload-key certificate. Debug fingerprints should be a deliberate development configuration, not automatically trusted in production. Certificate fingerprints are public configuration; private signing keys are not needed by the server.

Serve valid JSON over HTTPS without authentication, redirects, or HTML fallback. The existing `/.well-known/` middleware exemption in `dashboard/app.py` is a useful precedent. Consider a disabled/unconfigured 404 and bounded public caching consistent with the Apple route. Any Android association environment variables introduced for this route are **new proposed configuration**, not existing settings to assume are deployed.

### Android coordination required before enabling links

The current Android manifest has `android:pathPrefix="/submit"`, which claims more URLs than the Android router handles. Coordinate an Android change to narrow the intent filters to `/r/`, `/submit/login`, `/submit/my`, and the exact report-entry path `/submit` if that entry is retained. Do not broadly route all `/submit*` paths into the app.

Digest confirmation, unsubscribe, account/admin pages, and API paths must remain usable in a browser. The basic association statement above does not narrow paths; the Android manifest/router must do that work. Audit any supported trailing-slash variants as part of the same change. The existing iOS association tests explicitly keep digest actions in the browser; preserve that behavior.

The Android router currently recognizes only the canonical HTTPS host above. Some older web/push documentation names `alex311-reborn.com`; if that alias should open the Android app too, coordinate an additional manifest host, router allowlist entry, and association file served directly on that host. A redirect from an alias alone is not a completed app-link setup.

### Tests and verification

Add `tests/test_android_association.py`, following `tests/test_ios_association.py`, to verify public routing ahead of the static mount, JSON content type, the exact package/relationship/fingerprints, and behavior with missing or invalid configuration. Keep the Apple association tests green.

After deployment, check the canonical URL without following redirects and verify links on a signed Android build:

```sh
curl -i https://alex311visibility.me/.well-known/assetlinks.json
adb shell pm verify-app-links --re-verify com.herbertindustries.alex311
adb shell pm get-app-links com.herbertindustries.alex311
adb shell am start -a android.intent.action.VIEW \
  -d 'https://alex311visibility.me/r/26-00039674'
```

Also test email sign-in, My Issues, and the browser-only digest links. The custom scheme and pasted-link/code sign-in remain fallbacks while HTTPS verification is unavailable.

## Priority 2: Browser/email sign-in handoff for Android

The existing handoff is already close to usable: `dashboard/login.html` builds the correct custom-scheme link and preserves `location.search`. Its “app first” treatment currently checks only iPhone/iPad/iPod. `src/alex311/firebase_auth.py` also contains iPhone-specific email wording.

Proposed changes:

- Include Android in the mobile handoff presentation and use wording appropriate for both platforms. Keep the browser choice available, including when the native app is absent.
- Preserve the complete sign-in query, including `mode`, `oobCode`, `apiKey`, `continueUrl`, `lang`, and `tenantId`; preserve URL encoding rather than concatenating decoded values.
- Preserve the one-time-code behavior: merely rendering the landing page or offering “Open in the app” must not redeem the code. Keep the existing distinction between a browser that requested the link and one that merely received it.
- Retain email-code and pasted-link fallbacks. Do not promise an app is installed based on the user agent alone.
- Preserve server-side send throttling, truthful refusal messages, and `429` / `Retry-After` behavior.

Extend `tests/test_signin_handoff.py` for Android presentation, both browser/app choices, query preservation, and no unintended redemption. Preserve the existing iOS assertions. Relevant neighboring tests include `tests/test_signin_mail.py` and `tests/test_signin_code.py`.

The native app fetches `/submit/api/firebase-config`, redeems the link through Firebase REST in the configured tenant, and exchanges the resulting ID token at `/submit/api/session`. It then sends the returned cookie on authenticated submit APIs. The browser does not need to share its session cookie with the app.

## Priority 3: Google provider configuration

This is primarily Firebase/Google configuration and Android build configuration, rather than a new backend sign-in endpoint:

1. Register an Android OAuth client for `com.herbertindustries.alex311` and the applicable debug/release/Play signing certificates in the existing Firebase/Identity Platform project. Use the same tenant as the existing backend; do not invent a second tenant or weaken token verification.
2. Verify Google sign-in is enabled for that tenant. Check API-key/provider restrictions for the native REST exchange without breaking or weakening the shared web configuration.
3. Give the Android maintainer the project's **Web application OAuth client ID** to set as the Gradle property `googleWebClientId`. This is an audience/client ID, not an OAuth client secret. Do not reuse the iOS OAuth client ID.
4. Verify the Android Credential Manager → Google ID token → Firebase `signInWithIdp` with `tenantId` → existing `/submit/api/session` flow using a test account.

The button is hidden when that Android build property is unset. No Firebase JSON file or embedded client secret is needed for the implemented REST flow. This handoff does not contain the client ID, certificate fingerprints, or credentials; those remain configuration inputs to obtain and verify.

Backend reference points: `dashboard/submit.py`, `src/alex311/firebase_auth.py`, and `src/alex311/portal_auth.py`. Preserve tenant checks, revocation behavior, cookie security, and the forwarded HTTPS scheme behind the production proxy.

## Optional later work: FCM push

Android is functional with periodic polling; FCM is not a prerequisite for initial backend compatibility. The existing `src/alex311/push.py` and `/submit/api/devices` endpoints are APNs-specific. Registration currently accepts hex tokens and `production`/`sandbox`, and the sender emits an APNs payload. An FCM token cannot simply be sent there.

If immediate Android push is wanted later, plan a coordinated change to:

- Introduce provider-aware device storage/registration and an idempotent migration preserving current APNs rows and behavior.
- Validate FCM tokens separately; keep authenticated ownership, account switching, token refresh, device limits, and unregister/revocation behavior.
- Add an FCM sender and server-side credentials through the deployment's secret/identity mechanism; never put service-account keys in the app, repository, or CI for fork PRs.
- Include a case identifier/deep link in the Android payload and implement Android token registration/rotation and notification-tap routing.
- Define when polling/local notifications are suppressed to avoid duplicate announcements. Keep watches/digest semantics distinct from sent/followed case status changes.

References: `docs/push-notifications.md`, `src/alex311/push.py`, `src/alex311/schema.sql`, and `tests/test_push.py`. Preserve iOS/APNs support throughout. Sender tests should mock APNs/FCM; no live sends are necessary for automated tests.

## Shared API contract to preserve

| Area | Android dependency |
| --- | --- |
| Public browsing | `/api/meta`, `/api/requests`, `/api/points`, `/api/analytics`, `/api/request/{case}`, `/api/media/{id}` |
| Authentication | `/submit/api/firebase-config`, `/email-link`, `/email-code`, `/session`, `/whoami`; the latter abbreviated paths share the `/submit/api` prefix |
| Registry | Authenticated `/submit/api/registry/full`, schema `1`, version/ETag and `304` handling; cached per server origin |
| Location | `/submit/api/geocode`, `/reverse`, `/nearby`; coordinates use `lat` and `long` |
| Report submission | `/submit/api/validate` → `/precheck` → `/attempt/{id}/photos` → `/queue` → `/status/{id}` |
| User activity | Existing my/follow/link/feedback/watch/feed/profile/digest/logout/account endpoints |

Important details:

- Native submit requests advertise `Accept: application/json`; unauthenticated API calls must remain machine-readable `401`, rather than browser login redirects.
- Photos are raw `image/jpeg` request bodies, with a `name` query parameter, uploaded to a prepared attempt immediately before queueing. Android allows three and prepares orientation-correct JPEGs with a maximum 2560 px long side; retain the server's validation and metadata removal too.
- Queue fields are flattened: `attempt_id`, `first_name`, `last_name`, `email`, `phone`, `acknowledged`.
- The client saves the attempt ID before queueing and resumes `/status/{id}` after relaunch; preserve meaningful `prepared`, queued/filing, and terminal states, including case numbers and `photo_note`. Do not introduce a retry behavior that silently files a second case.
- Conditional questions, required answers, option rules, data-only exclusions, and date/time answer shapes must remain compatible. Native date/time answers use Alexandria's `America/New_York` zone. Server validation remains authoritative.
- Android unit tests replay the iOS fixture set: 30 real services and 180 answer cases. Coordinate registry changes and regenerated fixtures across backend, iOS, and Android.

## Acceptance and repository state

At handoff, the Android PR's nine GitHub checks passed: unit/lint/debug/release builds, API 26/36 device tests, CodeQL, secret/workflow checks, dependency audit/review, and the aggregate gate. This includes 19 Android unit tests, five instrumentation tests on each CI emulator, and six CI-helper tests. Device tests use a local MockWebServer. These results do **not** prove production account/provider configuration or end-to-end City filing.

Before release, coordinate testing of email code/link and configured Google sign-in; browser/app handoff and digest URLs; session revocation, account switching, follows/watches/feedback/digest; rotated/HEIC photos; and resuming a filing attempt after process death. A real City submission can dispatch staff and needs separate, deliberate authorization. Automated tests should never send reports or sign-in emails to real residents.

Android contributor setup includes the user-selected MIT license, contributor/security guidance, CODEOWNERS, templates, SHA-pinned read-only PR CI, grouped reviewed Dependabot updates, and live `main` protection. Owner PRs have a review-only exception; tests remain required. The app is not signed or published by CI. This records the Android repository's setup, not an instruction to overwrite the web repository's existing CI, license, or protection policy.

Deployment, provider-console changes, FCM credentials, signing, and store publishing are follow-up work. This handoff adds documentation only.
