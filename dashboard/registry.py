"""Form registry: load docs/data/form-registry.json and validate answers against it.

The registry (built by spike/build_registry.py) is the single schema the
generic form consumes — catalog + mined questions + wizard rules. Validation
here is server-authoritative: the client mirrors it for inline feedback, but
nothing reaches the submission queue that the wizard itself would reject.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "docs/data/form-registry.json"


@lru_cache(maxsize=1)
def load_registry(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("FORM_REGISTRY", DEFAULT_PATH))
    return json.loads(p.read_text())


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
