# Option C proof-of-concept — direct submission via browser automation

Context: the City indicated no official API/partnership (Option B) is available,
so this explores **Option C** — submitting a request through the portal's own
guest channel. This is an experimental PoC, kept gated, never public.

**Safety posture:** `src/alex311/submit_browser.py` is **dry-run by default** and
stops before the final submit, so it never creates a work order. A real
submission requires BOTH `live=True` AND env `ALEX311_ALLOW_LIVE_SUBMIT=1` — a
deliberate double gate, because every real submit dispatches City staff. No real
submission has been made; the last-mile is left as an explicit, human-triggered
step.

> **Authorization caveat, restated:** "no Option B" is not the same as the City
> authorizing scripted use of the guest endpoint. This PoC proves *technical*
> feasibility; the go/no-go on actually operating it is a decision to make with
> the City, not a technical question.

## How guest submission actually works (verified by driving the live site)

There is **no simple write API to replay.** The read path (`getServiceRequest*`)
is a clean guest Aura call, but submission is a multi-step UI flow:

1. Home → click a category tile. Tiles carry real metadata, e.g. **Missed
   Collection** = `data-service-code="TESMISCO"`, `data-category-code="RECYTRASH"`
   (Incap311 `webhomenew`/`webpopularnew` components).
2. "Request This Service" → opens a **5-step wizard** (verified for Missed
   Collection):
   - **Step 1 – File Upload** (optional attachments)
   - **Step 2 – Location** — an **Esri GIS map**; Continue stays *disabled*
     (`title="Please select location"`) until a point/parcel is selected
   - **Steps 3–5** – details/description, contact (optional for guests), review → **Submit**
3. **No login is required** for this flow (the `create-service-request` deep-link
   *does* force login, but the tile-driven guest flow does not).

Backend action seen in the bundle: `createServiceRequest`
(`aura://ServiceAutomationFamilyController/ACTION$createServiceRequest`). The site
also loads **reCAPTCHA v3** (site keys present; tied to Salesforce messaging
config). No captcha challenge fired through Step 2; whether it scores the final
submit is still unverified (only a real submit would show it).

**Conclusion:** replaying a bare HTTP POST is brittle/blocked (multi-step wizard,
Esri location requirement, probable captcha scoring). The robust path is **browser
automation driving the real flow** — the Playwright fallback the project always
reserved, now the submission mechanism.

## What the PoC harness does

`prepare_submission(scenario, description, address, dry_run=True, live=False)`:
- launches headless Chromium, selects the category tile, enters the wizard,
  walks steps filling recognized fields, and records each step;
- **dry-run:** stops at the first gate it can't pass or at the Submit step, with a
  screenshot — never submits;
- **live (double-gated):** completes Submit and extracts the `NN-NNNNNNNN` case number.

CLI: `python -m alex311.submit_browser --scenario missed --description "…" --address "…" [--screenshot out.png] [--headed]`

Verified run (dry): drives Home → Missed Collection → "Request This Service" →
Step 1 (File Upload, passes) → **Step 2 (Location) — stops cleanly** (`stage=blocked_at_location`),
because automated Esri point-selection needs selector tuning.

## Remaining work (each explicitly gated / a human's call)

1. **Automate the Esri location pick** (Step 2) — type in the map's search box and
   click a suggestion, or click a parcel. This is the main remaining automation
   hurdle; selectors are the least stable part (third-party widget).
2. **Walk steps 3–5** and confirm the field selectors for description/contact.
3. **One real end-to-end test submit** (`--live` + `ALEX311_ALLOW_LIVE_SUBMIT=1`),
   clearly marked as a test, to confirm the case number is returned and to see
   whether reCAPTCHA gates the final submit. This creates a real record — **your call.**
4. **Deployment:** do NOT bake Playwright/Chromium into the public dashboard image.
   Production would run this as a separate Cloud Run **Job** (Playwright base
   image), triggered from the gated intake UI via a queue — never inline in the
   web service.

## Files
- `src/alex311/submit_browser.py` — the dry-run-safe, double-gated Playwright harness + CLI.
- Builds on the gated intake UI from the Option A prototype (`dashboard/submit.*`).
