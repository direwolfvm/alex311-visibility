# Accounts on top of the submission system — a plan

*2026-09-15. A design, not code. Written against the codebase as it stands at
the merge of #40.*

## The one decision to make first

**Use Firebase Authentication for sign-in. Keep the account data in Cloud SQL,
and use Postgres row-level security to protect it.**

The premise behind "Firebase for storage because we can use RLS" needs one
correction. Row-level security is a Postgres feature: the database itself
refuses to return rows the current user does not own, whatever the application
asks for. Firebase's equivalent is Firestore Security Rules, which do a similar
job for a *client* talking to Firestore directly. Both are real protections.
The question is which store the data should live in, and the answer follows
from what the four features actually do:

| feature | what it joins against |
|---|---|
| "My requests" | `submission_attempts` (ours) and `service_requests` (the mirror) — for status, case number, the record page |
| Follow | `service_requests` — status changes come from the mirror's ingest |
| "Was it addressed?" | `service_requests.status` and `closed_datetime` — the whole point is comparing the City's view with the resident's |
| Public disclosure | the dashboard, which reads Postgres and nothing else |

Every one of them is a join against data that is in Postgres and is not going
to move. Putting the account rows in Firestore means every dashboard query that
uses a resident rating bridges two stores, and the analytics this is for become
the hardest thing on the site to write. Postgres RLS gives the guarantee you
want — the database, not the application, decides whose rows come back — in
the database that holds everything else.

Firebase Auth is a different matter and a good fit. It is already enabled in
this project (`identitytoolkit.googleapis.com` is on, along with Firestore, so
the Firestore route stays open if you want it later). It replaces two things we
hand-rolled and would rather not maintain: the password accounts in
`portal_auth.py` and the emailed one-time codes in `identity.py`. And it lets us
keep **no email address at all** on our side, which is the strongest form of
"we don't really want to store personal information."

Everything below assumes that decision. The data model is the same either way;
only the "where it lives" and "how it is protected" sections would change.

## Principles

1. **Pointers, not copies.** A "my request" is a case number. The City holds
   the request; we mirror the public record. We never hold a second copy.
2. **No personal information we do not need.** Our tables key on the Firebase
   `uid`, an opaque string. Email lives in Firebase. Name, phone and address go
   to the City at filing time and are purged from our side afterwards.
3. **Consent at the point of use.** Signing up accepts that ratings and notes
   may be used in aggregate. Showing any individual rating or note publicly is
   a separate, per-item, off-by-default choice.
4. **The database enforces ownership.** Not only the endpoints.

## What exists, and what changes

| today | after |
|---|---|
| `portal_users` — email, scrypt password hash, role | `accounts` — `uid`, role. No email, no password |
| `portal_sessions` — our httponly cookie, hashed token | kept, keyed by `uid`. The login step changes; the cookie does not |
| `identity.py` — emailed six-digit codes, `submitters`, `submitter_sessions`, `submitter_verifications` | retired. Firebase does this, better, and holds the email instead of us |
| `submission_attempts.submitter_id` — optional, usually null (`identity_enforced: false`) | always the `uid`. Per-submitter abuse limits become real |
| `submission_attempts.contact` — name, email, phone, kept forever | purged once the request is `filed`. The City has it; a `failed` row keeps it only until retried or given up |
| `approved_by` — the actor's email | the `uid` |
| shared Basic credential (`SUBMIT_USER`/`SUBMIT_PASSWORD`) for scripts and admin recovery | kept for scripts only; admin bootstrap moves to a CLI grant by `uid` |

Nothing about the filing path changes. The worker, the queue, the advisory lock
and the City's form are untouched.

## Data model

Four tables, all in Cloud SQL, all keyed on the Firebase `uid`.

```sql
CREATE TABLE accounts (
    uid             TEXT PRIMARY KEY,               -- Firebase uid; opaque
    role            TEXT NOT NULL DEFAULT 'user',   -- admin | user
    policy_version  TEXT NOT NULL,                  -- the data policy they accepted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at     TIMESTAMPTZ
);

-- "Mine" and "following" are one relation with two flavours. A row is a
-- pointer: the case number and nothing the City already holds.
CREATE TABLE request_links (
    uid                 TEXT NOT NULL REFERENCES accounts (uid) ON DELETE CASCADE,
    service_request_id  TEXT NOT NULL,              -- the City's case number
    relation            TEXT NOT NULL,              -- mine | following
    attempt_id          BIGINT REFERENCES submission_attempts (attempt_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, service_request_id)
);

-- The resident's own verdict. One per person per request, editable.
CREATE TABLE feedback (
    uid                 TEXT NOT NULL REFERENCES accounts (uid) ON DELETE CASCADE,
    service_request_id  TEXT NOT NULL,
    relation            TEXT NOT NULL,              -- mine | following, at the time
    score               SMALLINT CHECK (score BETWEEN 1 AND 5),  -- NULL = "not sure"
    note                TEXT,
    status_at_rating    TEXT,                       -- open | closed, per the mirror
    share_score         BOOLEAN NOT NULL DEFAULT false,
    share_note          BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    hidden_at           TIMESTAMPTZ,                -- moderation: withdrawn from public view
    hidden_by           TEXT,
    PRIMARY KEY (uid, service_request_id)
);
```

`portal_sessions` stays as it is, with `user_id` becoming the `uid`.
`moderation_actions` gains an optional `feedback_uid`/`feedback_case` pair so a
hidden note has the same audit trail a blocked submitter does.

### Row-level security

```sql
ALTER TABLE request_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_links FORCE  ROW LEVEL SECURITY;
CREATE POLICY own ON request_links
    USING (uid = current_setting('app.uid', true));
CREATE POLICY admin ON request_links
    USING (current_setting('app.role', true) = 'admin');
-- the same pair on feedback, plus:
CREATE POLICY shared ON feedback FOR SELECT
    USING (share_score AND hidden_at IS NULL);
```

The application opens each request's transaction with
`SET LOCAL app.uid = %s; SET LOCAL app.role = %s;` after verifying the session.
A handler that forgets to filter by user then returns nothing rather than
everything.

One thing to know: **RLS does not apply to the table's owner**, and today the
service connects as the owner. Phase 1 therefore creates an `alex311_app` role
that owns nothing, and the service connects as that. `FORCE` above is belt and
braces for the same reason. Without this step the policies are decoration.

## The four features

### 1. The gateway

**Client.** The Firebase JS SDK from the CDN — the compat build needs no
bundler, which matters because this site has no build step. Sign-in by
**email link** (passwordless; no password to reset, nothing for us to store)
with **Google** as a second option. On success the SDK yields an ID token.

**Server.** `POST /submit/api/session` takes the ID token, verifies it with
`google.oauth2.id_token.verify_firebase_token` — `google-auth` is already in the
production image as a dependency of the storage client, so no new package —
and mints the same httponly cookie `portal_sessions` issues today. From there
`gate()` and `admin_only()` work exactly as they do now, reading `accounts`
instead of `portal_users`.

**First sign-in** creates the `accounts` row with `role = 'user'` and records
the `policy_version` shown on the sign-in page. **Admin bootstrap** is a CLI:
`python -m alex311.accounts grant-admin <uid>`, run once against the first
uid; after that the Users panel does it. The current four accounts are
re-created by signing in and being granted.

**Retired:** the password path, the email-code path, and their five tables.
The login page keeps its explainer and loses its password field.

### 2. My requests

A pointer, written at the one moment we can vouch for it: **when the worker
files the request and gets a case number back**, it inserts
`(uid, case, 'mine', attempt_id)`. That is the only path to a `mine` row — a
request filed on the City's own site cannot be claimed as "mine" here, because
the City's public feed carries no reporter identity and we would be recording
an assertion we cannot check. Such a request can be *followed* instead, which
is honest about what we know.

`GET /submit/my` — a page listing the resident's links with the mirror's current
status, last update and the case number, each row linking to `/r/{case}`. The
status is as fresh as the last ingest, four times a day; the page says so.

### 3. Follow

A button on `/r/{case}` when signed in; `POST /submit/api/follow/{case}` and
its `DELETE`. Following is the general case of "I care about this" and needs no
proof. The My requests page shows `following` rows beside `mine`, labelled.

"Track" means, in the first version, **seeing the current status on the next
visit**. Notifying by email when the status changes is a later phase and the
only place email would come back into play — and even then the Admin SDK can
look an address up by `uid` at send time, so we still would not store it. It
also needs an SMTP provider, which the project does not yet have.

### 4. Was it actually addressed?

This is the feature with real analytical value. The severity work in September
found that City closure often does not end the problem — 131 right-of-way
complaints were re-filed after an earlier one at the same address had already
been closed. That was inferred from re-reports. A direct signal is better.

**The question**, asked on `/r/{case}` to anyone with a link to it:

> Was this issue actually addressed? **1** not at all · **2** · **3** partly ·
> **4** · **5** completely · *not sure*

with an optional note. Stored with `relation` (the submitter's verdict and a
neighbor's are both useful, and different) and `status_at_rating`, because
"rated unresolved while the City still says open" and "rated unresolved after
the City closed it" are different findings.

One rating per person per request, editable. Five points rather than a yes/no,
because "partly" is the most common real answer to this question and a binary
forces it into a lie.

## The data policy

Three separate things, kept separate on purpose.

**Analytics use — accepted at sign-up.** The sign-in page states, in the same
plain register as the current explainer: *ratings and notes you leave may be
used in aggregate on this site, for example to report how often residents say
a closed request was actually fixed.* The `policy_version` accepted is stored on
the account. Changing the policy means a new version and a re-acceptance on
next sign-in.

What "aggregate" means concretely: extend `/api/analytics` with
resident-reported resolution — the score distribution for closed requests, by
category and by department, and the rate of "closed but rated 1 or 2". Any
cell with fewer than **five** ratings is suppressed. Notes are never in
aggregates; they are read by a person.

**Public disclosure — per item, off by default.** Two checkboxes on the rating,
both unticked:

- *Show my score on this request's public page* → `share_score`
- *Show my note too* → `share_note` (only offered if the score is shared)

When on, `/r/{case}` shows *"2 residents rated this: 1 and 2 out of 5"* and,
where shared, the notes — with no name, no uid, nothing that identifies the
rater beyond what the note itself says. The dashboard's record rows can carry
the same aggregate.

A shared note is public text about a specific location, so it gets the
treatment the rest of the site gives such things: a *report* link, an admin
*hide* action recorded in `moderation_actions`, and the Status board listing
notes shared in the last 30 days for a look. Hidden notes vanish from public
view and stay in analytics.

**Deletion.** `DELETE /submit/api/account` removes the account row, and the
cascade removes every link and every rating, shared or not. Sign-in
credentials are deleted in Firebase, either by the resident from the same page
(the client SDK can delete its own user) or by an admin. Because we store no
email, there is nothing else to find and remove.

## Phases

Each phase ships on its own and is useful on its own.

**Phase 1 — sign in with Firebase.** The `accounts` table and `alex311_app`
role; the session endpoint; the login page rewired; the CLI grant; the Users
panel reading `accounts`; `submitter_id` and `approved_by` become the `uid`;
`contact` purged on `filed`; the password and email-code code and their tables
removed. *Done when:* a tester signs in by email link and files a request; the
Users panel shows uids and roles; `identity_enforced` is true on every
precheck; a filed attempt has no contact details on our side.

**Phase 2 — mine and following.** `request_links`, RLS, the worker writing
`mine` on filing, the follow button, the My requests page. *Done when:* a
resident who filed through us sees the case on their page with its current
status, and can follow any record from `/r/{case}`.

**Phase 3 — the rating, privately.** `feedback`, the question on `/r/{case}`,
policy acceptance at sign-in, the analytics extension with suppression. *Done
when:* the Analytics tab shows resident-reported resolution by category, and no
individual rating is visible anywhere public.

**Phase 4 — disclosure and moderation.** The two share flags, the public
rendering on `/r/{case}`, the report link, the admin hide, the Status board
listing. *Done when:* a shared note appears on its record page and an admin can
withdraw it with a recorded reason.

**Later, if wanted:** email notification on status change for followed
requests. Needs an SMTP provider first.

## Decisions I need from you

1. **Postgres or Firestore for the account rows.** My recommendation is above.
   If you want Firestore anyway, phases 2–4 still work; the analytics join
   becomes an export job rather than a query.
2. **Sign-in methods.** Email link alone is the least we can hold. Google
   sign-in is a convenience and costs nothing. Phone would mean SMS costs and a
   number on file at Firebase; I would leave it out.
3. **Five-point scale or yes/no.** Five, with "not sure", is my recommendation
   for the reason given.
4. **Who may rate.** Submitters only, or anyone following? I would allow both
   and record which, since a neighbor's "still broken" is evidence too.
5. **Public notes at all.** The alternative is scores public, notes
   analytics-only. That removes the moderation work in phase 4 entirely. Worth
   considering if moderation capacity is the constraint it was last week.
6. **The suppression threshold.** Five is conventional. With the current
   volume of filings it will suppress almost everything for months; that is
   correct, not a bug.
7. **Purging contact details after filing.** I have assumed yes. It is the
   single largest reduction in personal information the site holds.

## Risks worth naming

- **RLS that does not bite.** If the service keeps connecting as the table
  owner, every policy is silently ignored. The `alex311_app` role is not
  optional and phase 1 includes a test that proves a handler without a filter
  returns nothing.
- **The rating is an allegation, not a finding.** The analytics should say
  "residents rated" and never "was not fixed". The same discipline the
  severity work needed.
- **Re-identification through notes.** A note reading "the cord outside my
  house at 1 W Oak" identifies its author to their neighbors whatever we
  strip. The sign-in page should say that plainly beside the share checkbox.
- **Firebase is a dependency the City's data is not.** If it goes away, sign-in
  goes away; nothing about the mirror or the filing path depends on it, and the
  session cookie keeps signed-in users working through an outage.
