# Deploying to GCP

One container image serves three roles: the dashboard (Cloud Run service), the
ingest job, and the health check (both Cloud Run jobs on Cloud Scheduler).

## Current deployment (2026-07-07)

Live in project **permitting-ai-helper** (us-east4):

| Thing | Value |
|---|---|
| Dashboard | https://alex311-dashboard-650621702399.us-east4.run.app |
| Image | `us-east4-docker.pkg.dev/permitting-ai-helper/cloud-run-source-deploy/alex311-portal:latest` |
| Database | `alex311` on Cloud SQL `metabase-sql` (user `alex311`) |
| DB credentials | Secret Manager `alex311-database-url` |
| Media bucket | `gs://permitting-ai-helper-alex311-media` (prefix `alex311-media/`) |
| Ingest schedule | 03:20 / 09:20 / 15:20 / 21:20 America/New_York (`alex311-ingest-schedule`) |
| Health schedule | hourly at :45 (`alex311-health-schedule`) |
| Drift schedule | Mondays 06:30 America/New_York (`alex311-drift-schedule`, §6 — not yet created) |
| Alerting | policy "Alex311 job failures" → email channel (jke314@outlook.com) |

Redeploy after a code change:

```bash
gcloud builds submit --tag us-east4-docker.pkg.dev/permitting-ai-helper/cloud-run-source-deploy/alex311-portal:latest .
gcloud run deploy alex311-dashboard --region=us-east4 \
    --image=us-east4-docker.pkg.dev/permitting-ai-helper/cloud-run-source-deploy/alex311-portal:latest
# jobs pick up :latest automatically on their next execution
```

Note: Cloud Run jobs in this project hit an API quirk where per-execution
`--args` overrides fail (`Unknown name "priorityTier"`); to run a one-off
command, `gcloud run jobs update alex311-ingest --args=...`, execute, then
restore the args (running executions keep their snapshot). Two more gcloud
quirks: the `--args` list rejects duplicate values (use distinct numbers for
the two caps), and bare `/healthz` never reaches the container on run.app
domains.

**Backfill history:** 2025-07-01 → present launched 2026-07-07 (execution
`alex311-ingest-924mx`, ~1 year ≈ 30k records, runs many hours). If it hits
the 24h timeout or dies, just re-run it — the list pass is fast and
enrichment/media resume from where they stopped; scheduled incrementals also
drain the same queues (500 details + 500 media per run).

Set these once:

```bash
export PROJECT=<your-project> REGION=us-east4
export SQL_INSTANCE=$PROJECT:$REGION:<cloudsql-instance>
export IMAGE=$REGION-docker.pkg.dev/$PROJECT/alex311/portal:latest
export BUCKET=<media-bucket>
# DATABASE_URL for Cloud Run (unix socket path form):
export DB_URL='postgresql://alex311:<password>@/alex311?host=/cloudsql/'$SQL_INSTANCE
```

## 1. Database & bucket (one-time)

```bash
gcloud sql databases create alex311 --instance=<cloudsql-instance>
gcloud sql users create alex311 --instance=<cloudsql-instance> --password=<password>
gcloud storage buckets create gs://$BUCKET --location=$REGION \
    --uniform-bucket-level-access
```

Apply the schema (from any machine that can reach the instance, e.g. via
`cloud-sql-proxy`): `DATABASE_URL=... python -m alex311.ingest init-db`

## 2. Build & push

```bash
gcloud artifacts repositories create alex311 --repository-format=docker --location=$REGION
gcloud builds submit --tag $IMAGE .
```

## 3. Ingest job (every 6 hours, off-peak-friendly)

```bash
gcloud run jobs create alex311-ingest --image=$IMAGE --region=$REGION \
    --set-cloudsql-instances=$SQL_INSTANCE \
    --set-env-vars="DATABASE_URL=$DB_URL,MEDIA_BUCKET=$BUCKET" \
    --task-timeout=3600 --max-retries=1 \
    --command=python --args="-m,alex311.ingest,incremental,--days,14"

gcloud scheduler jobs create http alex311-ingest-schedule \
    --location=$REGION --schedule="20 3,9,15,21 * * *" --time-zone="America/New_York" \
    --uri="https://run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/alex311-ingest:run" \
    --http-method=POST --oauth-service-account-email=<scheduler-sa>@$PROJECT.iam.gserviceaccount.com
```

One-time backfill (run manually; ~8k records / 3 months, be patient and polite):

```bash
gcloud run jobs execute alex311-ingest --region=$REGION \
    --args="-m,alex311.ingest,backfill,--start,2025-01-01,--max-details,10000,--max-media,10000"
```

Re-run the same backfill command safely at any time — upserts are idempotent
and enrichment/media resume where they left off.

## 4. Health check job (hourly)

```bash
gcloud run jobs create alex311-health --image=$IMAGE --region=$REGION \
    --set-cloudsql-instances=$SQL_INSTANCE \
    --set-env-vars="DATABASE_URL=$DB_URL" \
    --task-timeout=300 --max-retries=0 \
    --command=python --args="-m,alex311.health"

gcloud scheduler jobs create http alex311-health-schedule \
    --location=$REGION --schedule="45 * * * *" \
    --uri="https://run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/alex311-health:run" \
    --http-method=POST --oauth-service-account-email=<scheduler-sa>@$PROJECT.iam.gserviceaccount.com
```

The health check exits non-zero when the bootstrap parse, list schema, detail
schema, or the guest-privacy invariant breaks — i.e. exactly the things a
Salesforce redeploy can silently change — **or when our data has actually gone
stale** (no successful ingest in 24h, `STALE_AFTER` in `alex311/ingest.py`).

### Weekend "volume too large" episodes — expected, and not an alert

The portal rations queries most weekends, returning *"Data volume for selected
duration is too large to show"* even for tiny windows. Between 2026-07-12 and
2026-08-08 this failed 21 ingest runs, **every one of them on a Sat/Sun/Mon and
none Tue–Fri**, and every one self-healed with no data loss.

The jobs now treat it as the transient portal-side condition it is:

- `fetch_range` retries a capped floor window, then **skips and reports** it
  instead of aborting the run; after 3 consecutive floor caps it abandons the
  rest of the run rather than hammering a portal that is clearly refusing.
- Skipped windows are never silent: logged at WARNING and counted in
  `ingest_runs.windows_incomplete`.
- Coverage repairs itself, because every incremental run re-reads a 15-day
  lookback and upserts idempotently.
- A run that reads *nothing at all* only fails if the last successful ingest is
  older than `STALE_AFTER`; otherwise it warns and exits 0.
- The health canary reports `degraded` (exit 0) when the portal is rationing —
  a volume response still proves bootstrap, the Aura envelope and our parsing
  all work, which is what the canary is for.

Net effect: weekend rationing is silent and self-healing; a portal outage that
genuinely stops ingestion for a day still pages. To see what a run skipped:

```sql
SELECT started_at, records_seen, windows_incomplete, error
FROM ingest_runs WHERE windows_incomplete > 0 ORDER BY started_at DESC;
```

## 5. Dashboard service

```bash
gcloud run deploy alex311-dashboard --image=$IMAGE --region=$REGION \
    --set-cloudsql-instances=$SQL_INSTANCE \
    --set-env-vars="DATABASE_URL=$DB_URL,MEDIA_BUCKET=$BUCKET" \
    --allow-unauthenticated --min-instances=0
```

(The dashboard only reads Postgres/GCS — it never calls the city portal, so
public traffic can't generate load on the municipal site.)

## 6. Registry drift check (weekly)

`docs/data/form-registry.json` describes a form the City controls. This job asks
whether that description is still true, using the two sources that need no
browser — the live catalog and the answers residents actually submitted:

```bash
gcloud run jobs create alex311-drift --image=$IMAGE --region=$REGION \
    --set-cloudsql-instances=$SQL_INSTANCE \
    --set-env-vars="DATABASE_URL=$DB_URL" \
    --task-timeout=600 --max-retries=0 \
    --command=python --args="-m,alex311.registry_drift,--days,30,--record"

gcloud scheduler jobs create http alex311-drift-schedule \
    --location=$REGION --schedule="30 6 * * 1" --time-zone="America/New_York" \
    --uri="https://run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/alex311-drift:run" \
    --http-method=POST --oauth-service-account-email=<scheduler-sa>@$PROJECT.iam.gserviceaccount.com
```

It exits non-zero — so the same "job failed" alert below covers it — when a
service is added, removed or renamed, when a question code, wording or datatype
changes, when an answer value appears that the registry does not list, or when
an answer the registry marks a **hard stop** shows up in a real web submission
(meaning the City relaxed that rule). Agent- and phone-entered records are
excluded throughout: they bypass the wizard's validation and prove nothing about
the web form. `--min-rows` (default 5) keeps a thinly used service from tripping
the alarm on one odd record. `--record` writes the outcome to `ingest_runs`
(`kind='drift'`) alongside ingest and health.

The third source, the wizard walk itself, needs Playwright and deliberately
does **not** ship in this image. Run it from a workstation instead:

```bash
scripts/weekly_drift.sh          # both halves; --no-crawl for the browser-free one
```

That re-walks all 111 services read-only (about an hour, sequential and polite,
it never presses Submit) and diffs the result against the committed rules with
`spike/rules_diff.py`, which reports new or reworded questions, changed widgets,
added or removed options, and any rule that flipped. When drift is real, adopt
it by rebuilding the registry and committing the regenerated data.

## 7. Alerting

Alert on failed executions of any job (this catches ingest breakage, the health
check's deliberate non-zero exits, and the weekly registry drift check):

```bash
gcloud alpha monitoring policies create --display-name="alex311 job failures" \
    --condition-display-name="Cloud Run job execution failed" \
    --condition-filter='resource.type="cloud_run_job" AND metric.type="run.googleapis.com/job/completed_task_attempt_count" AND metric.labels.result="failed"' \
    --condition-threshold-value=0 --condition-threshold-comparison=COMPARISON_GT \
    --condition-threshold-duration=0s \
    --notification-channels=<channel-id>
```

`ingest_runs` in Postgres keeps a queryable history of every run (kind,
range, counts, error text) — the dashboard header shows the latest one, and
`SELECT * FROM ingest_runs WHERE NOT ok ORDER BY started_at DESC` is the
first thing to check when an alert fires.

## When the portal redeploys

Expected failure mode. The client already re-bootstraps automatically on
fwuid rotation; if Salesforce changes the page layout or response schema, the
health job starts failing with a precise error (`BootstrapError: no
'var auraConfig'`, `list schema drift, missing keys: [...]`). All parsing
lives in `src/alex311/client.py` and `src/alex311/models.py` — fix there,
rebuild, redeploy.
