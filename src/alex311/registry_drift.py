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

Channel matters. Phone agents bypass the web wizard's rules, so agent- and
phone-entered records prove nothing about what the *web* form accepts. Only
rows we can positively tell are not agent/phone are used for the data checks.

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

#: Records from these channels never exercise the web wizard's validation.
NON_WEB_SOURCES = {"agent"}
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
    """True when the row is definitely not an agent/phone submission.

    Deliberately strict: a record we cannot classify is excluded rather than
    assumed to be web, because a false 'the wizard now allows this' is worse
    than a missed one.
    """
    return ((row.get("source") or "").strip().lower() not in NON_WEB_SOURCES
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
        """SELECT service_code, source, origin, raw_detail->'attributes' AS attrs
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
                       "values": set()})
            for v in attr.get("values") or []:
                answer = v.get("answer_value") or v.get("answer")
                if answer:
                    q["values"].add(answer)
    return out


def data_drift(rows: list[dict], registry: dict, min_rows: int = 5) -> list[Finding]:
    """Questions and answers real web submissions carry that the registry lacks.

    `min_rows` guards against thin data: one odd record for a rarely used
    service should not raise an alarm.
    """
    known = {s["service_code"]: s for s in registry["services"]}
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

            opts = {o["value"]: o for o in rq.get("options") or []}
            if not opts:          # free text: no vocabulary to compare
                continue
            for value in sorted(q["values"]):
                o = opts.get(value)
                if o is None:
                    out.append(Finding("option_added", code,
                                       f"{qcode}: answer {value!r} is not an option in the registry",
                                       {"question": qcode, "option": value}))
                elif (o.get("rule") or {}).get("type") == "hard_stop":
                    # The wizard used to reject this answer, yet a web
                    # submission carries it: the rule was relaxed.
                    out.append(Finding("rule_contradicted", code,
                                       f"{qcode}: {value!r} is marked a hard stop but appears in web data",
                                       {"question": qcode, "option": value,
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
              f"{stats['web_rows']} web records in the last {args.days} days")
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
