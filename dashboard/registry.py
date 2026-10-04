"""Form registry: the schema the report form is drawn from, and validation against it.

The registry — catalog + mined questions + wizard rules — is the single
schema the generic form consumes. Validation here is server-authoritative:
the client mirrors it for inline feedback, but nothing reaches the
submission queue that the wizard itself would reject.

Which registry: the one in use (`alex311.registry_store`) — the version an
administrator adopted, kept in the database so following the City does not
take a deploy — or, until something has been adopted and whenever the
database cannot be read, the file shipped in the image
(`docs/data/form-registry.json`). The service asks the database at most once
a minute; everything else is served from memory.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("alex311.registry")

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "docs/data/form-registry.json"

# What a client must understand to use the registry: the shape of a question,
# an option's `rule`, `reveals`/`revealed_by`, and `source`. Raised only when
# that shape changes in a way an older client would get wrong — not when the
# City adds or retires a request type. A client that sees a number higher
# than it knows should fall back to the website for reporting.
SCHEMA = 1

TTL_SECONDS = 60

_provider = None                 # () -> {"version", "registry"} | None, set by the web service
_lock = threading.Lock()
_held: dict | None = None        # {"at", "version", "registry", "source"}
_payloads: dict[tuple[str, bool], bytes] = {}


def set_provider(fn) -> None:
    """Tell the registry where the adopted version lives. `fn(have)` returns
    {"version", "registry"} for it — `registry` may be None when the version
    is the one already held (`have`) — or None when nothing has been adopted.
    Without a provider (tests, scripts) the bundled file is the registry."""
    global _provider
    _provider = fn
    refresh()


def refresh() -> None:
    """Forget what is held, so the next request asks again — called when an
    administrator adopts a version, so this instance follows at once."""
    global _held
    with _lock:
        _held = None
        _payloads.clear()


@lru_cache(maxsize=4)
def _bundled(path: str | None = None) -> tuple[str, dict]:
    p = Path(path or os.environ.get("FORM_REGISTRY", DEFAULT_PATH))
    raw = p.read_bytes()
    return hashlib.sha256(raw).hexdigest()[:16], json.loads(raw)


def _current() -> dict:
    global _held
    if _provider is None:
        version, reg = _bundled()
        return {"version": version, "registry": reg, "source": "bundled"}
    now = time.monotonic()
    held = _held
    if held is not None and now - held["at"] < TTL_SECONDS:
        return held
    with _lock:
        if _held is not None and now - _held["at"] < TTL_SECONDS:
            return _held
        try:
            got = _provider(_held["version"] if _held else None)
            if got is not None and got.get("registry") is None:      # still the version we hold
                got = {"version": _held["version"], "registry": _held["registry"]}
        except Exception as e:
            # keep serving what we have; failing that, the file in the image
            log.warning("registry: could not read the adopted version (%s)", type(e).__name__)
            got = _held and {"version": _held["version"], "registry": _held["registry"]}
            source = (_held or {}).get("source", "bundled")
        else:
            source = "database"
        if got is None:
            version, reg = _bundled()
            got, source = {"version": version, "registry": reg}, "bundled"
        if _held is None or _held["version"] != got["version"]:
            _payloads.clear()
        _held = {"at": now, "version": got["version"], "registry": got["registry"], "source": source}
        return _held


def load_registry(path: str | None = None) -> dict:
    """The registry in use (or, given a path, that file)."""
    return _bundled(path)[1] if path else _current()["registry"]


def registry_version(path: str | None = None) -> str:
    """A short fingerprint of the registry's content. Changes exactly when the
    registry does — an adoption after the City changes its form, say — so a
    client can ask "is mine still current?" for the price of a header."""
    return _bundled(path)[0] if path else _current()["version"]


def registry_source() -> str:
    return _current()["source"]


def full_payload(gzipped: bool = False) -> bytes:
    """The whole registry in one response, serialized once per version: every
    service with its questions, plus the version and schema a client caches it under."""
    cur = _current()
    key = (cur["version"], gzipped)
    body = _payloads.get(key)
    if body is None:
        reg = cur["registry"]
        body = json.dumps({"version": cur["version"], "schema": SCHEMA,
                           "generated": reg["generated"], "sources": reg.get("sources"),
                           "services": reg["services"]}, separators=(",", ":")).encode()
        if gzipped:
            body = gzip.compress(body, 6, mtime=0)
        _payloads[key] = body
    return body


def service_index(reg: dict) -> list[dict]:
    """Lightweight list for the picker."""
    out = []
    for s in reg["services"]:
        out.append({
            "service_code": s["service_code"], "service_name": s["service_name"],
            "description": s.get("description") or "", "keywords": s.get("keywords") or "",
            "groups": s.get("groups") or [],
            "departments": [d["name"] for d in s.get("departments") or []],
            "coverage": s["coverage"], "rules_summary": s["rules_summary"],
            "n_questions": len(s["questions"]),
        })
    return out


def get_service(reg: dict, code: str) -> dict | None:
    return next((s for s in reg["services"] if s["service_code"] == code), None)


def _qkey(q: dict) -> str:
    return q.get("code") or f"q{q['order']}"


def visible_questions(service: dict, answers: dict) -> list[dict]:
    """Questions to show for the current answers.

    Walked questions are always shown unless the registry marks them
    `conditional`, in which case they appear only when one of their
    `revealed_by` (question, option) pairs is currently selected. Data-only
    questions (never revealed on the walked path) are shown but never required.
    """
    def selected(key, value):
        v = answers.get(key)
        return v == value or (isinstance(v, list) and value in v)

    out = []
    for q in service["questions"]:
        if q["source"] == "data-only":
            out.append(q)
        elif not q.get("conditional") or any(selected(r["question"], r["option"]) for r in q.get("revealed_by", [])):
            out.append(q)
    return out


def validate(service: dict, answers: dict) -> dict:
    """Return {ok, errors, blocking, advisories, info} for a set of answers.

    errors    — required-but-empty or not-an-allowed-option (field-level)
    blocking  — chosen options the wizard hard-stops on (with the redirect text)
    advisories/info — guidance to show, non-blocking
    """
    errors, blocking, advisories, info = [], [], [], []
    for q in visible_questions(service, answers):
        key = _qkey(q)
        v = answers.get(key)
        empty = v in (None, "", []) or (isinstance(v, dict) and not any(v.values()))
        required = bool(q.get("required")) and q["source"] != "data-only"
        if empty:
            if required:
                errors.append({"question": key, "order": q["order"], "message": "This question is required."})
            continue
        opts = {o["value"]: o for o in q["options"]}
        if opts:
            chosen = v if isinstance(v, list) else [v]
            for c in chosen:
                o = opts.get(c)
                if o is None:
                    errors.append({"question": key, "order": q["order"], "message": f"'{c}' is not an option here."})
                    continue
                if not o.get("rendered") and o.get("seen_in_data"):
                    errors.append({"question": key, "order": q["order"],
                                   "message": f"'{c}' is not accepted on the web form (it only appears via phone/agent submissions)."})
                    continue
                rule = o.get("rule") or {}
                item = {"question": key, "order": q["order"], "option": c, "message": rule.get("message", "")}
                if rule.get("type") == "hard_stop":
                    blocking.append(item)
                elif rule.get("type") == "advisory":
                    advisories.append(item)
                elif rule.get("type") == "info":
                    info.append(item)
    return {"ok": not errors and not blocking, "errors": errors, "blocking": blocking,
            "advisories": advisories, "info": info}
