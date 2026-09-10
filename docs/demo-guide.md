# Demo and evaluation guide

> Also published as a shareable page:
> https://claude.ai/code/artifact/01924090-9b0e-473c-b7da-9bb0a06d369d

A walkthrough of what has been built, in the order that makes the argument, with
the exact commands to run each part. Written for a session with the City, and
for anyone repeating the evaluation afterwards.

**Two things to be clear about before you start.**

1. **Nothing here submits anything to the City.** The rule-discovery probe
   asserts in code that it never presses Submit. The submission form *prepares
   and checks* a request and then hands off to the official portal. A real
   submission needs two separate gates (`live=True` **and**
   `ALEX311_ALLOW_LIVE_SUBMIT=1`) and has never been used.
2. **This is not an official City of Alexandria service**, and "no Option B" is
   not the same as the City authorising scripted use of the guest channel. The
   work proves feasibility; whether to operate it is a decision to make with the
   City, not a technical question.

---

## 0. Setup

```bash
docker run -d --name alex311-pg -e POSTGRES_PASSWORD=alex311 \
    -e POSTGRES_DB=alex311 -p 54329:5432 postgres:16
export DATABASE_URL=postgresql://postgres:alex311@localhost:54329/alex311
export MEDIA_DIR=./media_store
uv sync --group dev
uv run python -m alex311.ingest init-db
uv run python -m alex311.ingest incremental --days 14 --max-details 200 --max-media 50
```

Run the whole test suite first. It is the fastest honest summary of what is
covered:

```bash
uv run pytest -q
```

Expect **154 passed**. Without `DATABASE_URL` set, 11 identity flow tests skip
and the rest still run.

Start the app:

```bash
uv run uvicorn dashboard.app:app --port 8311
```

The public dashboard is at `http://localhost:8311`. The gated prototype is at
`http://localhost:8311/submit`, which now shows a **login page**.

Two ways in, on purpose:

- **People** sign in with an account. Seed the first admin with
  `python -m alex311.portal_auth seed --email you@example.com`, then add others
  at `/submit/users`.
- **Scripts** keep using HTTP Basic with `alex311user` and `SUBMIT_PASSWORD`
  (in production, Secret Manager `alex311-submit-password`). Every `curl`
  example below uses that path.

---

## 1. The public dashboard — the part that already works

Live: **https://alex311visibility.me**

(The Cloud Run URL still answers, but the mapped domain is the one to put in front of anyone.)

This is a read-only mirror of the City's own public data. It never calls the
city portal in response to a visitor, so public traffic cannot generate load on
the municipal site.

**Worth showing:** filter to a category and watch the map, then open a row to
see the full record with photos, then the Analytics tab for time-to-close by
type. Everything is deep-linked back to the official record.

**The point to make:** the City's portal shows a resident *their own* request.
It cannot show anyone the pattern — how long this category takes to close, what
else is happening on the street, whether the issue is already reported. That
gap is what the whole project exists to fill.

**Mobile matters here.** Of the last 90 days of requests, 42% arrived from a
phone and 63% of self-service submissions did. Open the dashboard on a phone, or
at a 375px window, and check the map, the collapsed filter panel and the
analytics charts.

---

## 2. What the City's own form actually asks

```bash
uv run python spike/wizard_rules_report.py   # regenerates docs/wizard-rules.md
open docs/wizard-rules.md
```

Every one of the **111 catalog services** has been walked read-only through the
real wizard, recording each question, each option, and what the portal does when
you pick it: **39 hard stops** (the answer is rejected), **44 advisories**, and
the skip-logic that decides which question appears next.

**Worth showing:** a service with a redirect, for example *Tree Inspection
Request*, where answering "the tree is blocking the road" ejects the resident
with a phone number after they have already answered other questions.

**The point to make:** none of this is discoverable in advance on the City's
form. A resident meets it one question at a time, three questions in.

---

## 3. The same questions, all at once

Open **`/submit`** and pick a request type.

Every question for that type is on one page, with the City's own rules explained
inline next to the option that triggers them, instead of surprising the resident
mid-way. Try:

- **Tree Inspection Request** — the rejecting answers are marked before you pick
  one, with the City's actual wording.
- **Street Pavement Markings** — a "select all applicable" question that the
  City's form renders as a custom dropdown.
- Any type, then **Check my answers** — validation runs server-side against the
  same registry, so nothing the wizard would reject can be prepared.

The page ends with a review summary, a copy button and a link to the official
portal. It does not file anything.

---

## 4. Setting a location, and duplicate detection

There are three ways to place the request, because each one fails for someone:

- **Use my location** — browser geolocation, then the address box fills itself
  from the nearest address the City has used near that point.
- **Type an address** — matched against the City's own records as you type.
- **Tap the map** — always available, and the fallback when neither of the above
  works.

**Worth showing:** type `1437 Janneys Lane` without the apostrophe. It matches
`1437 JANNEY'S LN` and reports 99 past requests, because the City writes that
street two ways and we fold them into one place.

**The point to make:** we geocode against **16,103 distinct Alexandria addresses
that the City itself geocoded** when it logged a request there. No external
geocoder, so the resident's address never leaves our infrastructure, there is no
rate limit, and a match returns the coordinates the City already uses for that
address — the request lands where they expect it. The cost is coverage: an
address with no 311 history is not in there, which is why tapping the map is
never taken away.

Recent same-category requests within 250 metres then appear before you finish.

**The point to make:** there were **837 same-address, same-category re-files
within 7 days** in one 90-day window, across 562 addresses. The City's form
cannot warn about these because it does not show a resident anyone else's
requests. We hold the full history, so we can.

---

## 5. Anti-abuse — the part to spend the most time on

This is the answer to the obvious objection: *if you relay requests, do you
become a spam pipe?*

### 5.1 Show the policy running against the City's real history

```bash
uv run python spike/backtest_abuse.py --days 90
```

This replays **8,187 real requests** through the same function the endpoint
calls. Expect:

| Outcome | Result |
|---|---|
| Held for review | 66 of 8,187 (0.8%), across 12 of 5,327 addresses |
| Duplicate notices, shown but never blocking | 127 (1.6%) |

And on the four cases the research doc names:

| Address | What it is | Result |
|---|---|---|
| 80 S Earley St | 52 complaints in one day, one voice | 50 of 53 held, first at request #4 |
| 493 N Armistead St | persistent single target | 1 held, 1 notice |
| 400 King St | legitimately busy commercial block | **0 held**, 3 notices |
| 1437 Janney's Ln | recurring defect, many reporters | **0 held**, 1 notice |

**The point to make:** the burst campaign is caught on its fourth request. The
busy commercial block and the genuine recurring defect are never held. That
separation is the whole design, and it is measured against the City's own data
rather than asserted.

### 5.2 Show it live

```bash
A=alex311user:$SUBMIT_PASSWORD
J=/tmp/demo-cookies.txt; rm -f $J

# a first report is simply allowed
curl -s -u $A -c $J -b $J -X POST localhost:8311/submit/api/precheck \
  -H 'content-type: application/json' \
  -d '{"service_code":"TESNOISE","service_name":"Noise Issues",
       "address":"77 DEMO ST","description":"loud trucks overnight"}'

# repeat it three more times and watch allow -> notice -> notice -> review
```

Then the queue and the audit trail:

```bash
curl -s -u $A localhost:8311/submit/api/review-queue
ID=$(curl -s -u $A localhost:8311/submit/api/review-queue \
     | python3 -c "import json,sys; print(json.load(sys.stdin)['queue'][0]['attempt_id'])")
curl -s -u $A -X POST "localhost:8311/submit/api/review/$ID?action=approve&reason=legitimate"
```

Every evaluation is recorded, not only the refusals, because the rate limits
count real attempts and an audit trail of nothing but rejections explains
nothing. Moderation is append-only: a reversal is a new row, so the trail of who
decided what survives.

A filled honeypot field blocks outright:

```bash
curl -s -u $A -X POST localhost:8311/submit/api/precheck \
  -H 'content-type: application/json' \
  -d '{"service_code":"TESNOISE","address":"1 A ST","website":"http://spam"}'
```

**The point to make:** only bot signals block. Everything that looks human goes
to a queue, because the cost of a false positive is a resident whose real
problem goes unreported.

### 5.3 Identity

```bash
curl -s -u $A -c $J -b $J -X POST localhost:8311/submit/api/auth/start \
  -H 'content-type: application/json' -d '{"email":"Resident.Demo+311@Gmail.com"}'
# the code is printed in the server log by the console sender
curl -s -u $A -c $J -b $J -X POST localhost:8311/submit/api/auth/confirm \
  -H 'content-type: application/json' \
  -d '{"email":"resident.demo@gmail.com","code":"THE-CODE"}'
curl -s -u $A -b $J localhost:8311/submit/api/auth/me
```

Note the second command uses a **different spelling of the same mailbox** and
gets the same submitter. Plus-tags and (for providers that ignore them) dots are
folded, because otherwise every per-submitter limit is one keystroke from being
defeated.

**The point to make:** the City's own channel had no identity lever at all when
the data behind this was collected — the contact step was optional. As of
2026-09-10 that has changed for some services: *Missed Collection* now requires
name, email and phone, while *Fire Department Comments* does not. Ours requires a
verified mailbox, which is *more* friction than the official flow, deliberately. It is the only thing that makes
per-submitter limits and accountability real, and residents who will not
identify can still be sent to the official portal.

---

## 6. Keeping it honest over time

The registry describes a form the City controls, so it starts going stale the
day it is built.

```bash
uv run python -m alex311.registry_drift --days 30
```

Runs weekly as a Cloud Run job. It watches the live service catalog and the
answers residents actually submit, and fails when a service is added or renamed,
a question changes, an unknown answer appears, or an answer we believe is
rejected turns up in a real web submission. The wizard walk itself needs a
browser, so it runs from a workstation:

```bash
scripts/weekly_drift.sh --no-crawl   # the browser-free half
scripts/weekly_drift.sh              # both, about an hour, read-only
```

**The point to make:** this is a commitment to not silently drift out of sync
with the City's form, and it is also how the City would learn we noticed a
change they made.

---

## 7. What to ask the City

The technical questions are answered. These are not:

1. **Authorisation.** Is the City willing to have a third party relay resident
   requests through the guest channel, and under what conditions? Everything
   above is built; none of it should run against the live portal without this.
2. **A real integration.** If relaying is acceptable in principle, a supported
   endpoint is better than browser automation for both sides — more reliable for
   us, more controllable for them.
3. **What the City wants from the abuse layer.** The thresholds are calibrated
   against their data, but they are our choices. The review queue, the caps and
   the identity requirement are all adjustable, and the City is better placed to
   say what "too many" means.
4. **A supervised first submission.** One real request per category family, with
   the City informed, before anything opens wider.

## 8. Honest limits

- **Branching is first-order.** Each option's immediate follow-up question is
  recorded; deeper chains are not.
- **Three hard stops have no explanation**, because the City's form shows none.
  Verified by re-probing with a four-second wait; the form says so plainly.
- **Free-text validation is shallow.** One plausible value per field type was
  tried, not the full format rules.
- **The identity rules are not backtested.** Every historical record is
  anonymous, so per-submitter limits and targeting detection have no history to
  replay against. They are covered by unit tests instead. This is also why the
  persistent-single-target case barely trips on address rules alone.
- **The demo emails nothing.** Verification codes are written to the log by the
  console sender; a real deployment needs an SMTP provider.
- **No reviewer interface.** The review queue is an API, not a screen.
- **Contact requirements are not in the registry.** They are per service and
  the probe never reaches that step, so the harness discovers them at run time.
- **Address lookup only covers addresses with 311 history** — 16,103 of them.
  Anywhere else needs a map tap or the device's location.
- **Contact and review steps of the City's wizard were not re-walked.**

---

## 9. Filing a real request (when you decide to)

Live submission is enabled but stays shut behind two gates that must both be
open in the same run:

```bash
# 1. rehearse. Same code path, stops with Submit in view, sends nothing.
uv run python -m alex311.submit_browser \
  --service TESMISCO --address "2307 Russell Rd" \
  --description "what is actually wrong" \
  --answers '{"01PL-MISSEDTYP": "Recycling", "01DT-MISSEDTIM": {"date": "2026-09-08", "time": "18:45"}}' \
  --first-name "..." --last-name "..." --email "..." --phone "..." \
  --screenshot /tmp/review.png
```

Read `/tmp/review.png`. It is the City's own review step, and what it shows is
exactly what will be filed. Then, and only then:

```bash
# 2. file it. Both gates, one command, one request.
ALEX311_ALLOW_LIVE_SUBMIT=1 uv run python -m alex311.submit_browser ... --live
```

Things worth knowing before you do:

- **Some services require contact details.** *Missed Collection* demands name,
  email and phone; the run stops with `needs_contact` rather than inventing any.
  Those details go to the City with the request.
- **The address must be one the City services.** It is matched against the
  City's own gazetteer on the location step; an unrecognised one stops the run
  with `address_not_serviceable` rather than filing at the wrong place.
- **A live run will not invent an answer.** Anything the wizard asks that you
  did not supply stops the run. A dry run is allowed to make something up so the
  walk can continue, which is why the rehearsal is not proof that your answers
  are complete — check the review screenshot.
- **It is recorded.** With `DATABASE_URL` set, the attempt is written to
  `submission_attempts` before the click and the case number written back after.
- **Through the hosted job, approving is the live action.** A resident queuing a
  request changes nothing; a reviewer releasing it is what creates a real City
  record, and nothing recalls it afterwards.
- **One request per run.** There is no batch mode and there should not be.
