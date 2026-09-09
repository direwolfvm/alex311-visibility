# A generic submission form on top of Alex311 — research findings & proposed design

*Research branch `research/generic-form-layer`, 2026-09-09. All portal probing was read-only; no request was submitted.*

## TL;DR

**Feasible, and the hard part is already done.** Residents' two complaints — *you can't see all the questions* and *validation is baffling* — are both direct consequences of how the vendor wizard works, and both go away with a single-page form driven by a per-category question schema. We now have that schema for the 45 categories people actually use (mined from 32k real records), the full 111-service catalog from the portal itself, and a verified way through the wizard's one mechanical hurdle (the map). The genuinely new work is (1) capturing the *branching and redirect rules* the wizard enforces but the data can't show, and (2) an anti-abuse layer — which our dataset makes unusually strong, because we can see targeting patterns the official portal can't.

## 1. Why the current interface frustrates people — what the wizard actually does

Verified by driving the guest flow headlessly for *Tree Inspection Request* (`spike/wizard_walk.py`, screenshots in `docs/img/`). The flow is Home → category tile → "Request This Service" → a **5-step modal wizard**: File Upload → Location → Details → Contact → Review.

| What residents experience | Mechanism we observed |
|---|---|
| **"I can't see all the questions"** | Step 3 uses *progressive disclosure*: only question 1 is visible; answering it reveals question 2 beneath it, and so on. Each question gets its own **Next** button (`data-que-number`). Five questions means five reveals. |
| **"I don't know how much is left"** | The only progress text is a banner, *"There is 1 mandatory question unanswered"* — and it **always says 1**, because only one unanswered question is ever on screen. It cannot tell you five are coming. |
| **"The button just won't work"** | **Continue and Next are silently disabled** until the current question is answered. No inline message, no field highlighting. `required` is never set in the DOM — it appears only inside an aria-label (*"Required Question 5 Please describe…"*), invisible to sighted users. |
| **"It threw me out for no reason"** | Some answers trigger a modal **Validation Alert** that is a **hard stop**: "Is the tree blocking the road? → Yes" shows *"Please call the Police non-emergency line at 703.746.4444"*, and after OK the answer is rejected and the flow does not advance. "Tree Removal" fires an advisory alert too. These rules are not announced beforehand. |
| Location step | An Esri map inside `iframe.map-loc-mobile`; Continue is disabled until a point is picked ("Select a location"). Address search alone does not satisfy it. |
| Contact step | First/Last name, Email, Phone — **all optional**. Continue enables with nothing filled. Guest submission is fully anonymous. |
| Review step | Cancel / Previous / **Submit Request**. |

Also on that page: two permanent banners ("If this is an immediate danger please call 911", "Please provide the details of your request in the Additional Information box below") plus an optional free-text *Additional Information* box on every category.

## 2. Where a generic form's schema comes from

**The portal exposes a catalog but not the questions.** The Aura wrapper method `getServiceTypes` (`[{"language":"EN"}]`) returns **111 services** in Open311 shape — `service_code`, `service_name`, `description`, `keywords`, `group`, `metadata`, `type`, `definitions` — but `definitions` holds only departments and category groupings. Every `getServiceDefinition`-style method we tried is "Unknown method". Snapshot: `docs/data/service-catalog.json`.

**The questions come from our own data.** Every enriched record carries an Open311-format `attributes` array — `code`, `order`, `datatype`, `description` (the question text), `values` (the answer) — and 32,237 of our 34,074 enriched records have one. Mined with `scripts/mine_question_schema.sql` into `docs/data/question-schema.mined.json`:

- **142 distinct questions across 45 categories**; median 3 per category, max 8 (*Flooding Concerns*, *Traffic Signals and Street Lights*).
- **80% are `singlevaluelist`** (113), plus `string` 16, `multivaluelist` 6, `datetime` 4, `text` 2, `date` 1. List questions have a median of **2 options**, max 9 — this is overwhelmingly a form of short radio groups, ideal for a single page.
- **Presence rate is a usable proxy for "required"**: 107 questions appear on ≥95% of their category's records; 23 appear on <50% and are plainly conditional (e.g. *Sidewalk*'s four bike-rack questions at 0.2%, shown only when the request's nature is a bike rack).

**What the data cannot tell you — and why it matters:**

1. **Branching and redirect rules live only in the wizard.** The data shows *"Is the tree blocking the road?"* answered *Yes* on some records, yet the web wizard hard-stops that answer. Those records came in by phone (agents bypass web validation; ~30% of all requests are `Phone/Agent`). Likewise *"public or private property?"* shows **only "Public"** in 3k+ records — "Private" is almost certainly a redirect we never see. A generic form must replicate these rules, and they can only be learned by exercising the wizard per category (or from the vendor's config, which we don't have).
2. **Coverage is 45 of 111 services.** The other 66 have no enriched web submissions in our data; their questions need the same wizard walk.
3. Presence % is a proxy. True required-ness for the long tail needs confirmation.

## 3. Spam and targeting — what our data shows (last 90 days, 8,195 requests / 5,398 addresses)

Repeat activity at one address is common and mostly legitimate; **bursts are rare and stark**:

| Pattern | Example | Signature |
|---|---|---|
| **Burst campaign** | **80 S Earley St: 52 noise complaints in one day**, only 2 distinct descriptions, all `API/Web` — a business's truck loading being hammered | many/day, near-identical text, one category |
| **Persistent single target** | 493 N Armistead St: 16 noise complaints over 14 days, 16 different write-ups, vs. a downstairs neighbour | same address + category, spread over weeks, one submitter's voice |
| **Legitimately busy** | 400 King St: 19 requests, **9 categories, 18 distinct descriptions** | many categories, many voices, commercial |
| **Genuine recurring defect** | 1437 Janney's La: 10 traffic-signal reports from different people | one category, many reporters, infrastructure |

Only **3 addresses** exceeded 5 requests in a single day all quarter, so a per-address daily cap catches the burst case with essentially no collateral. Meanwhile **837 same-address, same-category re-files within 7 days** (562 addresses) show heavy duplicate pressure that a "this is already reported — add to it instead" flow would absorb. And because the official contact step is optional, **the portal itself offers no identity lever at all** — everything above was possible anonymously.

## 4. Proposed architecture

```
 resident ──▶ [ Single-page form ]──▶ [ Validation + rules ]──▶ [ Abuse controls ]──▶ [ Submit queue ]──▶ Playwright harness ──▶ Alex311
                  ▲                          ▲                        ▲                                   (Option C, dry-run default)
             schema registry           same schema              our 31k-record history
```

**Schema registry** — one JSON per category, merged from three sources: the catalog (name, code, description, department), the mined schema (questions, datatypes, options, presence), and a **hand-curated rules file** (`required`, `show_if` conditionals, `redirect_if` rules with their message). Versioned in the repo; drift detected by re-mining and diffing.

**Single-page renderer** — every question for the chosen category visible at once, grouped (Location · Details · Photos · Contact), with a real progress indicator, inline per-field validation on blur, required markers, and conditionals shown/hidden live. Redirect rules render as **inline guidance next to the option** ("Blocking the road? Call 703-746-4444 — this form can't dispatch police") instead of a modal that ejects you. The existing gated intake (`dashboard/submit.*`) is the seed; the location picker reuses our Leaflet map and existing nearby-duplicate check.

**Validation** — the same schema enforces datatype/options/required on both client and server; the server is authoritative, so the harness never drives the wizard with an answer the wizard would reject.

**Abuse controls** (see §5) — sit *between* validation and the queue so a blocked submission never reaches the portal.

**Submission** — the Option C harness (restored in PR #14), now with the map hurdle solved (click inside `iframe.map-loc-mobile`) and made schema-driven: for each question, locate the radio group by its attribute code (`input[name="01PL-RPCTREERD"]`) — the codes are stable identifiers shared by the wizard and the data. Runs as a separate Cloud Run **Job** with a Playwright image, fed by a queue, never inline in the web service; dry-run stays the default and live submission keeps its double gate.

Optional on top: the CopilotKit helper assessed earlier (conversational intake → this same schema → the same validation and abuse layer).

## 5. Anti-abuse policy, with thresholds grounded in §3

1. **Identity.** Our layer requires an account with a verified email (or an equivalent verified channel). This is *more* friction than the official anonymous flow — deliberately: it is the only lever that makes per-submitter limits mean anything. Residents who won't identify can still be handed off to the official portal.
2. **Per-submitter rate limits.** e.g. 5 submissions/day, 15/week, with exponential cooldown; plus a hard cap per submitter per **address** (3/week) and per **address + category** (1/week unless the earlier one is closed).
3. **Per-address caps across all submitters.** 3/day and 8/week from our layer, regardless of who; over that, submissions go to a **review queue** rather than being dropped. Calibrated so 80 S Earley St trips it on request #4 while 400 King St (~1.5/week) never does.
4. **Dedupe-into-existing.** Before submit, show recent same-category requests within ~150 m (we already do this); offer "add a note / I'm also affected" instead of a new ticket. This absorbs most of the 837 re-files and is the single biggest spam *and* UX win.
5. **Targeting detector.** Flag when one submitter's activity concentrates on one address (share of their submissions ≥ 60% at a single address with ≥ 3 total), or when an address accumulates ≥ 5 requests in 7 days with low description diversity. Flags route to review, not auto-block; reviewers see the address's history from our data.
6. **Content + bot hygiene.** Near-duplicate text detection per submitter, CAPTCHA/Turnstile on the form, honeypot fields, and a full audit log (who, when, what, decision). Everything is auditable because *we* are the sender of record for every request we relay.
7. **Never more anonymous than the city.** We attach nothing to the portal request the resident didn't provide, but we keep the submitter identity on our side for accountability.

## 6. Open questions and risks

- **Authorization.** Unchanged from the Option C assessment: "no Option B" is not the City authorizing scripted use of the guest channel. Relaying residents' requests at scale should be agreed with the City, and this design's abuse controls are a strong part of that conversation — we would be *reducing* their spam, not adding to it.
- **Rule discovery per category** is real work: ~2–3 wizard walks per category to enumerate redirect and conditional rules (111 services). Automatable with `spike/wizard_walk.py` plus per-option probing, but it must be read-only and polite.
- **Schema drift.** The vendor can change questions at any time. Re-mine weekly and diff; the health canary should fail on an unknown attribute code.
- **Phone-channel skew.** Mined answer vocabularies include agent-entered values the web wizard forbids; treat the wizard, not the data, as authoritative for what a *web* submission may contain.
- **Photos.** Step 1 accepts uploads (10 MB); the harness would need to drive the file input. Not yet exercised.

## 7. Recommended next steps

1. **Rule discovery spike** (read-only): extend `wizard_walk.py` to probe every option of every list question for the top 15 categories by volume and record redirects/conditionals into the rules file.
2. **Schema registry + renderer**: merge catalog + mined schema + rules; build the single-page form on the existing gated `/submit` for those 15 categories; keep it behind the login.
3. **Abuse layer**: accounts, limits, per-address caps, review queue, audit log — all testable against our historical data before any live traffic.
4. **Harness**: land PR #14, add the iframe map click and schema-driven fills, run it as a Cloud Run Job, dry-run only.
5. **One supervised live submission** per category family, with the City informed, before opening the gate any wider.

## Artifacts

| File | What |
|---|---|
| `docs/data/service-catalog.json` | `getServiceTypes` snapshot — 111 services |
| `docs/data/question-schema.mined.json` | per-category questions, datatypes, options, presence % |
| `scripts/mine_question_schema.sql` | regenerates the above from Postgres |
| `spike/wizard_walk.py` | read-only wizard walker (shadow-DOM-aware) |
| `docs/img/wizard-*.png` | step 3 fully revealed, contact, review |
