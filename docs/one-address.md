# One address: alex311-reborn.com

Since 2026-10-06 the site's address is **https://alex311-reborn.com**. The
earlier address, `alex311visibility.me`, still resolves and still answers
API calls, but a page asked for there is sent to the new address (HTTP 308,
path and query kept). Session cookies are per host, so everyone signs in
once more.

## For the iOS app

- **Base URL:** `https://alex311-reborn.com`. The old host keeps answering
  `/api/*` and `/submit/api/*` without redirecting, so a build on the old
  base URL keeps working; it just has no reason to stay there.
- **Associated domains:** add `applinks:alex311-reborn.com` (keep
  `applinks:alex311visibility.me` for links already in people's inboxes).
  The association file is served, unredirected, on both hosts.
- **Sign-in links** now land on `https://alex311-reborn.com/submit/login?…`.
  Until a build with the new associated domain ships, iOS will not hand
  them to the app directly; the "Open in the app" button on that page
  (`com.herbertindustries.alex311-reborn://signin?…`) does. The six-digit
  code path is unchanged.
- **Push payloads** carry `url` on the new host.
- **Mail** comes from `no-reply@alex311-reborn.com`; support is
  `support@alex311-reborn.com` (the old address forwards too).

Nothing else in the API changed. The registry endpoints, versions and ETags
are the same on both hosts.
