# The history of the report form

The questions this site asks are the City's, and the City changes them. What
the form asked on a given day is part of the record of every report made that
day, so **every version of the form this site has used is kept, whole, for
good.** The database enforces it: a trigger on each of the three history
tables refuses DELETE, TRUNCATE and any rewrite, for every role — including the
owner the jobs run as. Removing history takes dropping that trigger, which is a
deliberate act and not something a query can do by accident.

## What is kept

| Where | What | Changed after it is written? |
|---|---|---|
| `registry_versions` | Every registry this site has used or been offered: the complete registry (`registry`, every request type with its questions, options and rules), the list of differences from the one in use when it was built (`changes`), and its current state. | The registry itself never (trigger). Only its state (`proposed` → `active` → `retired`, or `dismissed`) and who last decided. |
| `registry_events` | An append-only log. One row each time a version was **proposed**, **adopted**, **replaced**, **dismissed** or **withdrawn**, with when and by whom. A `proposed` row carries the differences as they stood then. | Never (trigger). The web service's role is also granted only INSERT and SELECT. |
| `wizard_walks` | Every walk of every request type by the nightly job: exactly what the City's form rendered and what each answer did, with what changed against the walk in use. | Never. |
| `submission_attempts.registry_version` | For each report, the version of the form it was written against. | Never. |

The registry that shipped with the site, in use before anything was adopted,
is stored as the first row (event `baseline`) the first time a version is
offered or adopted — so the history starts at the beginning, not at the first change.
Versions before that live in git (`docs/data/form-registry.json`).

A version is named by a 16-character fingerprint of its content. The same
content always has the same name, and it is the `version` the registry
endpoints and the iOS app already use.

## Questions it answers

**What did the form ask on a given day?**

```sql
-- the version in use at a moment
SELECT version FROM registry_events
 WHERE at <= '2026-11-02 14:00-05' AND event IN ('baseline', 'adopted')
 ORDER BY event_id DESC LIMIT 1;
-- and the form itself
SELECT registry FROM registry_versions WHERE version = '<that version>';
```

In code: `registry_store.in_use_at(conn, when)` and `registry_store.get(conn, version)`.
A version put back with "Use again" has more than one period in use; the log
records each.

**What was this report asked?**

```sql
SELECT a.attempt_id, a.answers, s AS service_as_asked
  FROM submission_attempts a
  JOIN registry_versions v ON v.version = a.registry_version,
       jsonb_array_elements(v.registry->'services') s
 WHERE a.attempt_id = 123 AND s->>'service_code' = a.service_code;
```

Reports made before 2026-10-05 have no `registry_version`; use the date query
above for those (or git, before the first adoption).

**When did the City change a question, and what exactly changed?**

```sql
SELECT at, version, by, detail->'changes' FROM registry_events
 WHERE event = 'proposed' ORDER BY at;                      -- as offered
SELECT walked_at, changes FROM wizard_walks
 WHERE service_code = 'TESSEWER' AND changes <> '[]' ORDER BY walked_at;   -- as observed
```

**Who decided?** `registry_events.by` — an administrator's account, or `walk`
for a request type the City retired, which is removed without waiting.

## Where to see it

Admin page → **Registry** → **Versions**. Each version opens the whole registry
as it was (`GET /submit/api/admin/registry/version/{version}`); "Full log" is
the event log (`GET /submit/api/admin/registry/history`). Both are for
administrators.

## Rules for anyone changing this code

- Do not add a DELETE or a pruning job for `registry_versions`,
  `registry_events` or `wizard_walks`, and do not drop the `kept` /
  `kept_whole` triggers (`registry_history_is_kept()` in `schema.sql`). The
  tables are small (a registry is about
  360 KB; a year of nightly walks is tens of megabytes).
- Do not UPDATE `registry_versions.registry`. A different registry is a
  different version.
- These tables hold nothing about a person, so account deletion does not and
  should not touch them.
- Database backups are the Cloud SQL instance's; this history has no second
  copy outside it.

`tests/test_registry_store.py` pins all of this: the triggers, the app role's
grants, the baseline, and the "in use on a given day" query.
