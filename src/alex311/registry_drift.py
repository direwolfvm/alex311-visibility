"""Registry drift: has the City's form changed under our form registry?

`docs/data/form-registry.json` is a snapshot of a vendor-controlled UI. The
City (or Incap311) can add a question, rename one, or relax a rule at any time,
and nothing tells us — the gated `/submit` form would simply start asking the
wrong things. This module answers "is the registry still true?" from two
sources that need **no browser**, so it can run in the deployed image:

  catalog   live `getServiceTypes`      services added, removed or renamed
  data      ingested `raw_detail`       questions and answers actually being
                                        submitted that the registry does not know

The third source — the wizard walk itself — needs Playwright and stays out of
the web image; `spike/rules_diff.py` diffs a fresh crawl against the committed
rules instead. Everything here is read-only: one catalog call and some SELECTs.

Channel matters, and more than it first appears. About a third of records reach
the City some way other than the wizard — staff typing a phone call, an emailed
or tweeted report transcribed by staff, a third-party app with its own form —
and in all of those a human keys in answers the wizard would have refused. The
data checks therefore use an **allowlist** of wizard channels (`WEB_SOURCES`),
so an unrecognised channel is excluded rather than mistaken for the web form.

Usage: python -m alex311.registry_drift [--days 30] [--min-rows 5]
Exits non-zero when drift is found — wire it to a weekly Cloud Run job so the
existing "job failed" alert fires.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .client import Alex311Client

log = logging.getLogger("alex311.registry_drift")

#: Sources that ARE the web wizard we walked. Everything else reaches the City
#: another way — staff typing a phone call (Agent/Phone), an emailed or tweeted
#: report transcribed by staff (origin Email/Facebook/Twitter/Sprout Social), or
#: a third-party app with its own form (snap311) — and none of those exercise
#: the wizard's validation, so they say nothing about what the web form accepts.
#: An allowlist, not a denylist: a channel we do not recognise is excluded
#: rather than assumed to be the wizard.
WEB_SOURCES = {"web", "ios", "ios browser", "android", "android browser"}
NON_WEB_ORIGINS = {"phone"}

REGISTRY_NAME = "docs/data/form-registry.json"


@dataclass
class Finding:
    """One way the registry no longer matches reality."""
    kind: str
    service_code: str
    summary: str
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.kind}] {self.service_code}: {self.summary}"


def registry_path(path: str | os.PathLike | None = None) -> Path:
    """Locate form-registry.json.

    `alex311` is pip-installed in the container, so a path relative to this
    file only works in a source checkout. The image copies `docs/data` next to
    the WORKDIR instead, so try that too, and let FORM_REGISTRY override both.
    """
    tried = []
    for cand in (path, os.environ.get("FORM_REGISTRY"),
                 Path.cwd() / REGISTRY_NAME,
                 Path(__file__).resolve().parents[2] / REGISTRY_NAME):
        if not cand:
            continue
        p = Path(cand)
        if p.is_file():
            return p
        tried.append(str(p))
    raise FileNotFoundError(f"{REGISTRY_NAME} not found; tried: {', '.join(tried)}")


def load_registry(path: str | os.PathLike | None = None) -> dict:
    return json.loads(registry_path(path).read_text())


def norm(t: str | None) -> str:
    """Compare question text the way build_registry.py does: markup and
    punctuation removed, so a vendor reflow is not reported as a rename."""
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"<[^>]+>", " ", t or "").lower()).strip()


def is_web(row: dict) -> bool:
    """True only when the record came through the web wizard this registry describes.

    Strict by design: a false "the wizard now allows this" is worse than a
    missed one, and roughly a third of records arrive by phone, email or social
    media, where staff key in answers the wizard would have refused.
    """
    return ((row.get("source") or "").strip().lower() in WEB_SOURCES
            and (row.get("origin") or "").strip().lower() not in NON_WEB_ORIGINS)


# --------------------------------------------------------------- catalog side

def catalog_drift(live: list[dict], registry: dict) -> list[Finding]:
    """Services added, removed or renamed since the registry was built."""
    known = {s["service_code"]: s for s in registry["services"]}
    seen = {t["service_code"]: t for t in live if t.get("service_code")}
    out: list[Finding] = []

    for code in sorted(seen.keys() - known.keys()):
        out.append(Finding("service_added", code,
                           f"new service {seen[code].get('service_name')!r} is not in the registry",
                           {"service_name": seen[code].get("service_name")}))
    for code in sorted(known.keys() - seen.keys()):
        out.append(Finding("service_removed", code,
                           f"registry service {known[code]['service_name']!r} is gone from the catalog",
                           {"service_name": known[code]["service_name"]}))
    for code in sorted(seen.keys() & known.keys()):
        was, now = known[code]["service_name"], seen[code].get("service_name")
        if norm(was) != norm(now):
            out.append(Finding("service_renamed", code, f"{was!r} -> {now!r}",
                               {"was": was, "now": now}))
    return out


# ------------------------------------------------------------------ data side

def fetch_attribute_rows(conn, days: int) -> list[dict]:
    """Recent enriched records that carry answered questions."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return conn.execute(
        """SELECT service_code, source, origin, requested_datetime,
                  raw_detail->'attributes' AS attrs
             FROM service_requests
            WHERE requested_datetime >= %s
              AND jsonb_array_length(coalesce(raw_detail->'attributes', '[]'::jsonb)) > 0""",
        (since,),
    ).fetchall()


def observed(rows: list[dict]) -> dict[str, dict]:
    """service_code -> {rows, questions{code: {text, datatype, values}}} for web rows."""
    out: dict[str, dict] = {}
    for row in rows:
        if not is_web(row):
            continue
        svc = out.setdefault(row["service_code"], {"rows": 0, "questions": {}})
        svc["rows"] += 1
        for attr in row["attrs"] or []:
            code = attr.get("code")
            if not code:
                continue
            q = svc["questions"].setdefault(
                code, {"text": attr.get("description"), "datatype": attr.get("datatype"),
                       "values": {}})
            for v in attr.get("values") or []:
                # the portal pads some stored answers (" Construction "), which
                # is not drift — compare on the trimmed value
                answer = (v.get("answer_value") or v.get("answer") or "").strip()
                if answer:
                    # keep the newest sighting: a rule can only have been
                    # relaxed by a record submitted after we crawled it
                    at = row.get("requested_datetime")
                    if at and (q["values"].get(answer) is None or at > q["values"][answer]):
                        q["values"][answer] = at
                    else:
                        q["values"].setdefault(answer, at)
    return out


def built_at(registry: dict) -> datetime | None:
    """When the registry was generated, as an aware UTC datetime."""
    try:
        return datetime.strptime(registry["generated"], "%Y-%m-%d %H:%M UTC").replace(
            tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None


def data_drift(rows: list[dict], registry: dict, min_rows: int = 5) -> list[Finding]:
    """Questions and answers real web submissions carry that the registry lacks.

    `min_rows` guards against thin data: one odd record for a rarely used
    service should not raise an alarm.
    """
    known = {s["service_code"]: s for s in registry["services"]}
    crawled = built_at(registry)
    out: list[Finding] = []

    for code, svc in sorted(observed(rows).items()):
        reg = known.get(code)
        if reg is None:      # catalog_drift already reports unknown services
            continue
        if svc["rows"] < min_rows:
            continue
        by_code = {q["code"]: q for q in reg["questions"] if q.get("code")}

        for qcode, q in sorted(svc["questions"].items()):
            rq = by_code.get(qcode)
            if rq is None:
                out.append(Finding("question_added", code,
                                   f"question {qcode} {(q['text'] or '')[:60]!r} is not in the registry",
                                   {"question": qcode, "text": q["text"], "rows": svc["rows"]}))
                continue
            if q["text"] and norm(q["text"]) != norm(rq.get("text")):
                out.append(Finding("question_text_changed", code,
                                   f"{qcode}: {rq.get('text', '')[:50]!r} -> {q['text'][:50]!r}",
                                   {"question": qcode, "was": rq.get("text"), "now": q["text"]}))
            if q["datatype"] and rq.get("datatype") and q["datatype"] != rq["datatype"]:
                out.append(Finding("datatype_changed", code,
                                   f"{qcode}: {rq['datatype']} -> {q['datatype']}",
                                   {"question": qcode, "was": rq["datatype"], "now": q["datatype"]}))

            if rq.get("source") == "data-only":
                # We never saw this question rendered, so its option list is a
                # historical vocabulary, not a claim about what the form offers.
                # Comparing answers against it only produces noise.
                continue
            opts = {o["value"].strip(): o for o in rq.get("options") or []}
            if not opts:          # free text: no vocabulary to compare
                continue
            for value, last_seen in sorted(q["values"].items()):
                o = opts.get(value)
                if o is None:
                    out.append(Finding("option_added", code,
                                       f"{qcode}: answer {value!r} is not an option in the registry",
                                       {"question": qcode, "option": value}))
                elif (o.get("rule") or {}).get("type") == "hard_stop":
                    # The wizard rejects this answer, yet a wizard submission
                    # carries it. Only records submitted *after* the crawl can
                    # mean the rule was relaxed; older ones predate what we
                    # measured and are already reflected in `seen_in_data`.
                    if crawled and last_seen and last_seen <= crawled:
                        continue
                    out.append(Finding("rule_contradicted", code,
                                       f"{qcode}: {value!r} is marked a hard stop but was submitted "
                                       f"through the web form on {last_seen:%Y-%m-%d}"
                                       if last_seen else
                                       f"{qcode}: {value!r} is marked a hard stop but appears in web data",
                                       {"question": qcode, "option": value,
                                        "last_seen": last_seen.isoformat() if last_seen else None,
                                        "message": (o.get("rule") or {}).get("message", "")}))
    return out


# ----------------------------------------------------------------------- glue

def check(registry: dict, *, client: Alex311Client | None = None, conn=None,
          days: int = 30, min_rows: int = 5) -> tuple[list[Finding], dict]:
    """Run every enabled check. Returns (findings, stats)."""
    findings: list[Finding] = []
    stats = {"services_in_registry": len(registry["services"]),
             "generated": registry.get("generated"),
             "catalog_checked": False, "data_rows": 0, "web_rows": 0}

    if client is not None:
        live = client.get_service_types()
        stats["catalog_checked"] = True
        stats["services_live"] = len(live)
        findings += catalog_drift(live, registry)

    if conn is not None:
        rows = fetch_attribute_rows(conn, days)
        stats["data_rows"] = len(rows)
        stats["web_rows"] = sum(1 for r in rows if is_web(r))
        # Surface the channels we excluded. If the City renames the wizard's
        # source value, the web sample silently drops to zero — this is how we
        # would notice, so it belongs in the job output rather than a comment.
        skipped: dict[str, int] = {}
        for r in rows:
            if not is_web(r):
                key = f"{r.get('source') or '(none)'}/{r.get('origin') or '(none)'}"
                skipped[key] = skipped.get(key, 0) + 1
        stats["skipped_channels"] = dict(sorted(skipped.items(), key=lambda kv: -kv[1])[:8])
        findings += data_drift(rows, registry, min_rows=min_rows)

    return findings, stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(
        prog="alex311.registry_drift",
        description="Check docs/data/form-registry.json against the live catalog "
                    "and recently ingested answers. Read-only; never submits.")
    p.add_argument("--days", type=int, default=30, help="how far back to read answers")
    p.add_argument("--min-rows", type=int, default=5,
                   help="minimum recent web records before judging a service")
    p.add_argument("--registry", help="path to form-registry.json")
    p.add_argument("--skip-catalog", action="store_true", help="don't call the portal")
    p.add_argument("--skip-db", action="store_true", help="don't read Postgres")
    p.add_argument("--json", action="store_true", help="emit findings as JSON")
    p.add_argument("--record", action="store_true",
                   help="record the outcome in ingest_runs (kind='drift')")
    args = p.parse_args(argv)

    registry = load_registry(args.registry)
    conn = run_id = None
    if not args.skip_db:
        conn = db.connect()
        if args.record:
            run_id = db.start_run(conn, "drift", None, None)

    client_cm = Alex311Client() if not args.skip_catalog else None
    try:
        if client_cm is not None:
            with client_cm as client:
                findings, stats = check(registry, client=client, conn=conn,
                                        days=args.days, min_rows=args.min_rows)
        else:
            findings, stats = check(registry, conn=conn, days=args.days,
                                    min_rows=args.min_rows)
    except Exception as e:
        log.error("drift check FAILED to run: %s", e)
        if conn and run_id:
            db.finish_run(conn, run_id, ok=False, error=f"{type(e).__name__}: {e}")
        return 2

    if args.json:
        print(json.dumps({"stats": stats, "findings": [asdict(f) for f in findings]}, indent=1))
    else:
        print(f"registry {stats.get('generated')} — {stats['services_in_registry']} services; "
              f"catalog {'checked' if stats['catalog_checked'] else 'skipped'}; "
              f"{stats['web_rows']} of {stats['data_rows']} records in the last {args.days} days "
              f"came through the web form")
        if stats.get("skipped_channels"):
            print("  other channels (not the wizard): "
                  + ", ".join(f"{k} {v}" for k, v in stats["skipped_channels"].items()))
        for f in findings:
            print(f"  {f}")

    if conn and run_id:
        db.finish_run(conn, run_id, ok=not findings, records_seen=stats["web_rows"],
                      error="; ".join(str(f) for f in findings)[:2000] or None)

    if findings:
        log.error("registry drift: %d finding(s) — re-crawl with "
                  "spike/wizard_rules.py and rebuild the registry", len(findings))
        return 1
    log.info("registry drift: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
