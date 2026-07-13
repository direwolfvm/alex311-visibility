# Submission prototype (Option A) — gated beta + probe findings

A friendlier intake for 311 requests that **prepares** a submission and hands the
resident off to the official Alex311 portal to file it. Nothing here writes to
the City's system. Scoped to the **missed yard waste collection** scenario for
this first cut, and gated behind HTTP Basic auth so it is not publicly reachable
until the authorization question with the City is settled.

## Access (testing)

- URL: `https://alex311-dashboard-…/submit`
- Username: `alex311user`
- Password: stored in Secret Manager `alex311-submit-password`, wired to the
  Cloud Run service as `SUBMIT_PASSWORD` (env `SUBMIT_USER=alex311user`).
- Local dev: `SUBMIT_PASSWORD=localtest` in `.claude/launch.json`.

The gate is `dashboard/submit.py` (`secrets.compare_digest`, standard browser
Basic-auth dialog). All `/submit*` routes require it; the public dashboard is
unchanged.

## What the probe established

- **"Missed yard waste collection" maps to three real categories** (verified
  counts over the ingested year): Missed Collection (4,841), Bulk Yard Waste
  Pickup (1,250), Missed Leaf Collection (176). The form lets the resident pick
  in plain language and maps to the right one.
- **A submission needs little:** category + location (lat/long) + free-text
  description. Department, priority, and `source` are server-assigned (sampled
  records arrive with `source: Web`).
- **Duplicate detection works and is unique to us.** Because we hold the full
  history, `/submit/api/nearby` returns recent same-category requests within
  ~250m. Demonstrated in prod: a test point returned 8 prior Missed Collection
  reports (1 open). The official portal structurally cannot do this — it is the
  strongest reason for a resident to start here.
- **Full auto-prefill of the City's create form is UNVERIFIED.** The create page
  is a lazily-loaded Aura component; its prefill params (if any) are not in the
  page shell, so we can't confirm URL-driven prefill without a browser session on
  the create page itself. The handoff therefore **degrades gracefully**: it opens
  the known-good portal entry point and gives the resident a clean, copyable
  summary to paste. No feature depends on prefill working.

## Open items / next steps

1. **Verify create-form prefill in a real browser** (read-only: load the create
   page with candidate `c__`-prefixed params, observe whether fields populate).
   If supported, upgrade the handoff from copy-paste to one-click prefill.
2. **CopilotKit helper layer** (requested option). The deterministic form is the
   base; the copilot slots in on top using the project's existing
   `copilotkit-runtime` service — conversational intake, auto-categorization,
   and surfacing the dedupe result in chat. Not built in this cut.
3. **Authorization conversation with the City** — required before any *direct*
   programmatic submission (Option B/C). Until then this stays gated.

## Files

- `dashboard/submit.py` — gated router: auth, `/submit/api/scenarios`,
  `/submit/api/nearby` (bounding-box + haversine dedupe).
- `dashboard/submit.html` — the intake UI (scenario → map location → details →
  live duplicate check → review + handoff).
