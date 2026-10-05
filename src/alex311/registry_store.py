"""The registry as data: which one is in use, and what has been offered.

The form registry describes a form the City controls. It used to be a file
in the image, so following the City meant a walk on someone's laptop, a
commit and two deploys. Now the registry in use is a row
(`registry_versions`, status 'active'), the nightly walk (`alex311.walk`)
offers a new one when the City's form has moved, and an administrator adopts
it from the admin page. The web service, the filing worker and the drift
check all read the same row.

The file shipped in the image (`docs/data/form-registry.json`) is what is
used until something has been adopted, and whenever the database cannot be
read — a registry a day old is better than no report form.

Nothing is thrown away. Every version ever used or offered stays in
`registry_versions`, whole; `registry_events` is an append-only log of when
each was offered, adopted, replaced or dismissed and by whom; and every
report records the version it was written against. See
docs/registry-history.md.

Two rules about what happens without a person:

  * A request type the City has retired is removed at once. Offering it
    would only collect reports that cannot be filed.
  * Anything else — a new question, a reworded option, a rule that changed —
    waits as a proposal, because it changes what residents are asked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from psycopg.types.json import Jsonb

log = logging.getLogger("alex311.registry_store")

REGISTRY_NAME = "docs/data/form-registry.json"
RULES_NAME = "docs/data/wizard-rules.json"
MINED_NAME = "docs/data/question-schema.mined.json"


def data_file(name: str, override: str | os.PathLike | None = None) -> Path:
    """A file under docs/data: next to the working directory in the images,
    relative to the source tree in a checkout."""
    tried = []
    for cand in (override, Path.cwd() / name, Path(__file__).resolve().parents[2] / name):
        if not cand:
            continue
        p = Path(cand)
        if p.is_file():
            return p
        tried.append(str(p))
    raise FileNotFoundError(f"{name} not found; tried: {', '.join(tried)}")


_bundled_cache: dict[tuple[str, float], dict] = {}


def bundled() -> dict:
    """The registry shipped in the image, with its fingerprint."""
    p = data_file(REGISTRY_NAME, os.environ.get("FORM_REGISTRY"))
    key = (str(p), p.stat().st_mtime)
    if key not in _bundled_cache:
        raw = p.read_bytes()
        _bundled_cache.clear()
        _bundled_cache[key] = {"version": hashlib.sha256(raw).hexdigest()[:16],
                               "registry": json.loads(raw), "source": "bundled"}
    return _bundled_cache[key]


def fingerprint(registry: dict) -> str:
    """A version for a registry built here: its request types, and nothing
    that changes when the same content is built on a different day."""
    body = json.dumps(registry["services"], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()[:16]


def active(conn) -> dict | None:
    row = conn.execute(
        "SELECT version, registry, decided_at, decided_by, note FROM registry_versions "
        "WHERE status = 'active'").fetchone()
    if row is None:
        return None
    return {"version": row["version"], "registry": row["registry"], "source": "database",
            "adopted_at": row["decided_at"], "adopted_by": row["decided_by"], "note": row["note"]}


def current(conn=None) -> dict:
    """The registry in use: {version, registry, source}. Never raises for a
    database problem — the bundled file is the answer then."""
    try:
        if conn is not None:
            return active(conn) or bundled()
        if os.environ.get("DATABASE_URL"):
            from . import db
            with db.connect() as c:
                return active(c) or bundled()
    except Exception as e:
        log.warning("registry: database unavailable (%s); using the bundled file", type(e).__name__)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    return bundled()


# ------------------------------------------------------------------ the diff

def _norm(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"<[^>]+>", " ", t or "").lower()).strip()


def _qkey(q: dict) -> str:
    return q.get("code") or f"q{q['order']}"


def diff(old: dict, new: dict) -> list[str]:
    """What adopting `new` over `old` changes for a person filling the form,
    one readable line each. The first word is the kind of change."""
    was = {s["service_code"]: s for s in old["services"]}
    now = {s["service_code"]: s for s in new["services"]}
    out: list[str] = []
    for code in sorted(now.keys() - was.keys()):
        out.append(f"service_added {code}: {now[code]['service_name']} "
                   f"({len(now[code]['questions'])} questions)")
    for code in sorted(was.keys() - now.keys()):
        out.append(f"service_removed {code}: {was[code]['service_name']}")
    for code in sorted(was.keys() & now.keys()):
        a, b = was[code], now[code]
        name = b["service_name"]
        if _norm(a["service_name"]) != _norm(name):
            out.append(f"service_renamed {code}: {a['service_name']!r} is now {name!r}")
        for field, label in (("description", "description"), ("groups", "groups"),
                             ("keywords", "keywords"), ("banners", "banners")):
            if a.get(field) != b.get(field):
                out.append(f"service_{label}_changed {code}: {name}")
        ca, cb = a.get("coverage") or {}, b.get("coverage") or {}
        if (ca.get("walked"), ca.get("continue_reached")) != (cb.get("walked"), cb.get("continue_reached")):
            out.append(f"walk_changed {code}: {name} — walked {ca.get('walked')}→{cb.get('walked')}, "
                       f"reached Continue {ca.get('continue_reached')}→{cb.get('continue_reached')}")
        qa = {_qkey(q): q for q in a["questions"]}
        qb = {_qkey(q): q for q in b["questions"]}
        for k in sorted(qb.keys() - qa.keys()):
            out.append(f"question_added {code}: {name} — Q{qb[k]['order']} {qb[k]['text']!r}")
        for k in sorted(qa.keys() - qb.keys()):
            out.append(f"question_removed {code}: {name} — Q{qa[k]['order']} {qa[k]['text']!r}")
        for k in sorted(qa.keys() & qb.keys()):
            x, y = qa[k], qb[k]
            where = f"{code}: {name} — Q{y['order']}"
            if _norm(x["text"]) != _norm(y["text"]):
                out.append(f"question_reworded {where} {x['text']!r} is now {y['text']!r}")
            if bool(x.get("required")) != bool(y.get("required")) and y["source"] != "data-only":
                out.append(f"required_changed {where} {y['text']!r} is now "
                           f"{'required' if y.get('required') else 'optional'}")
            if x.get("kind") != y.get("kind"):
                out.append(f"widget_changed {where} {x.get('kind')}→{y.get('kind')}")
            if x.get("source") != y.get("source"):
                out.append(f"question_source_changed {where} {y['text']!r} {x.get('source')}→{y.get('source')}")
            oa = {o["value"]: o for o in x["options"] if o.get("rendered")}
            ob = {o["value"]: o for o in y["options"] if o.get("rendered")}
            for v in sorted(ob.keys() - oa.keys()):
                out.append(f"option_added {where} {v!r}")
            for v in sorted(oa.keys() - ob.keys()):
                out.append(f"option_removed {where} {v!r}")
            for v in sorted(oa.keys() & ob.keys()):
                ra, rb = oa[v].get("rule") or {}, ob[v].get("rule") or {}
                if ra.get("type") != rb.get("type"):
                    out.append(f"rule_changed {where} {v!r}: {ra.get('type') or 'no rule'}"
                               f"→{rb.get('type') or 'no rule'}")
                elif _norm(ra.get("message")) != _norm(rb.get("message")):
                    out.append(f"rule_message_changed {where} {v!r}")
    return out


# ------------------------------------------------------- offering and deciding

def _event(conn, version: str, event: str, by: str | None = None, **detail) -> None:
    """One line in the history. Appended, never changed."""
    conn.execute("INSERT INTO registry_events (version, event, by, detail) VALUES (%s, %s, %s, %s)",
                 (version, event, by, Jsonb(detail)))


def _keep_baseline(conn) -> None:
    """Before the first stored version is offered or goes into use, store the
    registry that was in use until now — the file shipped in the image — so the history
    starts at the beginning rather than at the first change."""
    if conn.execute("SELECT 1 FROM registry_events WHERE event IN ('baseline', 'adopted') LIMIT 1").fetchone():
        return
    b = bundled()
    conn.execute(
        """INSERT INTO registry_versions (version, status, registry, note, decided_at)
           VALUES (%s, 'retired', %s, %s, now()) ON CONFLICT (version) DO NOTHING""",
        (b["version"], Jsonb(b["registry"]),
         "The registry shipped with the site, in use before anything was adopted."))
    _event(conn, b["version"], "baseline", generated=b["registry"].get("generated"))


def _make_active(conn, version: str, by: str) -> None:
    _keep_baseline(conn)
    was = conn.execute("SELECT version FROM registry_versions WHERE status = 'active'").fetchone()
    if was and was["version"] == version:
        return
    if was:
        _event(conn, was["version"], "replaced", by, replaced_by=version)
    conn.execute("UPDATE registry_versions SET status = 'retired', decided_at = now() "
                 "WHERE status = 'active' AND version <> %s", (version,))
    conn.execute("UPDATE registry_versions SET status = 'active', decided_at = now(), decided_by = %s "
                 "WHERE version = %s", (by, version))
    _event(conn, version, "adopted", by, replaced=was["version"] if was else bundled()["version"])


def _store(conn, registry: dict, status: str, changes: list[str], note: str) -> str:
    version = fingerprint(registry)
    conn.execute(
        """INSERT INTO registry_versions (version, status, registry, changes, note)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (version) DO UPDATE
             SET changes = EXCLUDED.changes, note = EXCLUDED.note""",
        (version, status, Jsonb(registry), Jsonb(changes), note))
    return version


def retire_services(conn, codes: set[str], *, by: str = "walk") -> str | None:
    """Take request types the City no longer offers out of the registry in
    use, now. Returns the new version, or None when none of them were in it."""
    cur = current(conn)
    keep = [s for s in cur["registry"]["services"] if s["service_code"] not in codes]
    gone = [s for s in cur["registry"]["services"] if s["service_code"] in codes]
    if not gone:
        return None
    reg = dict(cur["registry"], services=keep)
    changes = [f"service_removed {s['service_code']}: {s['service_name']}" for s in gone]
    version = _store(conn, reg, "retired", changes,
                     "The City retired " + ", ".join(s["service_name"] for s in gone)
                     + "; removed without waiting.")
    _make_active(conn, version, by)
    conn.commit()
    log.warning("registry %s adopted automatically: %s", version, "; ".join(changes))
    return version


def propose(conn, registry: dict, *, note: str = "") -> dict:
    """Offer a registry. Returns {version, changes, status}; status is
    'current' when it changes nothing, 'dismissed' when an administrator
    already said no to exactly this, else 'proposed'."""
    cur = current(conn)
    changes = diff(cur["registry"], registry)
    if not changes:
        conn.execute("UPDATE registry_versions SET status = 'retired', decided_at = now() "
                     "WHERE status = 'proposed'")           # what was waiting is no longer true
        conn.commit()
        return {"version": cur["version"], "changes": [], "status": "current"}
    version = fingerprint(registry)
    seen = conn.execute("SELECT status FROM registry_versions WHERE version = %s", (version,)).fetchone()
    if seen and seen["status"] in ("dismissed", "active"):
        conn.rollback()
        return {"version": version, "changes": changes, "status": seen["status"]}
    _keep_baseline(conn)                 # the history starts no later than the first offer
    # one proposal at a time: a newer walk replaces what was waiting
    conn.execute("UPDATE registry_versions SET status = 'retired', decided_at = now() "
                 "WHERE status = 'proposed' AND version <> %s", (version,))
    _store(conn, registry, "proposed", changes, note)
    conn.execute("UPDATE registry_versions SET status = 'proposed', decided_at = NULL, decided_by = NULL "
                 "WHERE version = %s", (version,))
    if not seen or seen["status"] != "proposed":
        _event(conn, version, "proposed", "walk", against=cur["version"], changes=changes)
    conn.commit()
    return {"version": version, "changes": changes, "status": "proposed", "new": seen is None}


def adopt(conn, version: str, by: str) -> bool:
    """Put a stored version into use — a proposal, or an earlier one (which
    is how a mistake is undone)."""
    row = conn.execute("SELECT status FROM registry_versions WHERE version = %s FOR UPDATE",
                       (version,)).fetchone()
    if row is None:
        conn.rollback()
        return False
    _make_active(conn, version, by)
    conn.commit()
    log.warning("registry %s adopted by %s", version, by)
    return True


def dismiss(conn, version: str, by: str) -> bool:
    n = conn.execute(
        "UPDATE registry_versions SET status = 'dismissed', decided_at = now(), decided_by = %s "
        "WHERE version = %s AND status = 'proposed'", (by, version)).rowcount
    if n:
        _event(conn, version, "dismissed", by)
    conn.commit()
    return n > 0


def use_bundled(conn, by: str) -> None:
    """Stop using any stored version; the file in the image is in use again."""
    for row in conn.execute("SELECT version FROM registry_versions WHERE status = 'active'").fetchall():
        _event(conn, row["version"], "withdrawn", by, replaced_by=bundled()["version"])
    conn.execute("UPDATE registry_versions SET status = 'retired', decided_at = now(), decided_by = %s "
                 "WHERE status = 'active'", (by,))
    conn.commit()


# ---------------------------------------------------------------- the history

def get(conn, version: str) -> dict | None:
    """A stored version, whole: the registry as it was, with how it differed
    from the one before it. Versions are kept for good."""
    return conn.execute(
        "SELECT version, status, registry, changes, note, created_at, decided_at, decided_by "
        "FROM registry_versions WHERE version = %s", (version,)).fetchone()


def events(conn, limit: int = 500) -> list[dict]:
    return conn.execute("SELECT at, version, event, by, detail FROM registry_events "
                        "ORDER BY event_id DESC LIMIT %s", (limit,)).fetchall()


def in_use_at(conn, when) -> str | None:
    """The version the report form was drawn from at a moment in the past —
    the last one put into use at or before it. None before the history starts."""
    row = conn.execute(
        "SELECT version, event, detail FROM registry_events "
        "WHERE at <= %s AND event IN ('baseline', 'adopted', 'withdrawn') ORDER BY event_id DESC LIMIT 1",
        (when,)).fetchone()
    if row is None:
        return None
    return row["detail"].get("replaced_by") if row["event"] == "withdrawn" else row["version"]


def state(conn) -> dict:
    """Everything the admin page shows. Light: the page asks every few
    seconds, so the registry itself is never read here."""
    row = conn.execute(
        "SELECT version, registry->>'generated' AS generated, note, decided_at, decided_by, "
        "       jsonb_array_length(registry->'services') AS services "
        "FROM registry_versions WHERE status = 'active'").fetchone()
    if row:
        in_use = {"version": row["version"], "source": "database", "generated": row["generated"],
                  "services": row["services"], "adopted_at": row["decided_at"],
                  "adopted_by": row["decided_by"], "note": row["note"]}
    else:
        b = bundled()
        in_use = {"version": b["version"], "source": "bundled", "generated": b["registry"].get("generated"),
                  "services": len(b["registry"]["services"]), "adopted_at": None, "adopted_by": None,
                  "note": None}
    proposed = conn.execute(
        "SELECT version, changes, note, created_at, jsonb_array_length(registry->'services') AS services "
        "FROM registry_versions WHERE status = 'proposed' ORDER BY created_at DESC LIMIT 1").fetchone()
    history = conn.execute(
        "SELECT version, status, note, created_at, decided_at, decided_by, "
        "       jsonb_array_length(changes) AS n_changes "
        "FROM registry_versions ORDER BY COALESCE(decided_at, created_at) DESC LIMIT 100").fetchall()
    walks = conn.execute(
        """SELECT count(*) AS walked, min(walked_at) AS oldest, max(walked_at) AS newest,
                  count(*) FILTER (WHERE NOT ok) AS failing,
                  count(*) FILTER (WHERE suspect) AS suspect
             FROM (SELECT DISTINCT ON (service_code) * FROM wizard_walks
                    ORDER BY service_code, walked_at DESC) latest""").fetchone()
    attention = conn.execute(
        """SELECT service_code, service_name, walked_at, ok, suspect, result->>'error' AS error
             FROM (SELECT DISTINCT ON (service_code) * FROM wizard_walks
                    ORDER BY service_code, walked_at DESC) latest
            WHERE suspect OR NOT ok ORDER BY walked_at DESC LIMIT 20""").fetchall()
    last_run = conn.execute(
        "SELECT started_at, finished_at, ok, records_seen, error FROM ingest_runs "
        "WHERE kind = 'walk' ORDER BY started_at DESC LIMIT 1").fetchone()
    return {"in_use": in_use, "bundled_version": bundled()["version"],
            "proposed": proposed, "history": history,
            "walks": dict(walks, total=in_use["services"]), "attention": attention,
            "last_run": last_run}
