"""Build docs/data/form-registry.json — the single schema a generic form consumes.

Merges three sources per service:
  catalog   docs/data/service-catalog.json          names, codes, descriptions, departments, groups
  mined     docs/data/question-schema.mined.json    question codes, datatypes, option vocab, presence %
  wizard    docs/data/wizard-rules.json             rendered questions, widget kind, required, per-option
                                                    rules (hard stop / advisory / info) and reveals

Questions are matched wizard<->mined by attribute code when the wizard exposes
one (radios do), otherwise by normalised question text. Mined-only questions
(conditional ones the safe path never revealed, or services not yet walked)
are kept and flagged, so nothing known is dropped.

Usage: uv run python spike/build_registry.py
"""
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/data/form-registry.json"
COUNTER = re.compile(r"mandatory question", re.I)


def strip_html(t):
    """The vendor embeds markup (<br/>, <img>) in some question texts."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def norm(t):
    return re.sub(r"[^a-z0-9]+", " ", strip_html(t).lower()).strip()


def clean_alerts(p):
    out = []
    for a in p.get("alert", []):
        if COUNTER.search(a) or a.strip() == "Validation Alert":
            continue
        out.append(a.replace("Validation Alert ", "", 1))
    return list(dict.fromkeys(out))


def rule_for(p):
    msgs = clean_alerts(p)
    if p.get("hard_stop"):
        return {"type": "hard_stop", "message": " / ".join(msgs)}
    if p.get("advisory"):
        return {"type": "advisory", "message": " / ".join(msgs)}
    if msgs:
        return {"type": "info", "message": " / ".join(msgs)}
    return None


catalog = json.load(open(ROOT / "docs/data/service-catalog.json"))
mined = json.load(open(ROOT / "docs/data/question-schema.mined.json"))
wizard = json.load(open(ROOT / "docs/data/wizard-rules.json"))

mined_by_name = {}
for r in mined:
    mined_by_name.setdefault(r["service_name"], []).append(r)
wiz_by_code = {c["service_code"]: c for c in wizard["categories"]}

services = []
for t in catalog:
    code, name = t["service_code"], t["service_name"]
    defs = t.get("definitions") or {}
    mrows = sorted(mined_by_name.get(name, []), key=lambda r: r["ord"])
    wiz = wiz_by_code.get(code)
    questions = []
    used_mined = set()

    if wiz and not wiz.get("error"):
        for wq in wiz["questions"]:
            # match to a mined question: by code, else by normalised text
            m = None
            if wq.get("code"):
                m = next((r for r in mrows if r["code"] == wq["code"]), None)
            if m is None:
                nt = norm(wq["question"])
                m = next((r for r in mrows if r["code"] not in used_mined and
                          (norm(r["question"]) == nt or nt.startswith(norm(r["question"])) or norm(r["question"]).startswith(nt))), None)
            if m:
                used_mined.add(m["code"])
            vocab = set((m or {}).get("answer_vocab") or [])
            rendered = wq.get("options") or []
            opts = []
            for o in rendered:
                p = wq["probes"].get(o, {})
                opts.append({"value": o, "rendered": True, "seen_in_data": o in vocab,
                             "rule": rule_for(p) if "error" not in p else None,
                             "reveals": p.get("reveals", []),
                             "probe_error": p.get("error")})
            for o in sorted(vocab - set(rendered)):
                opts.append({"value": o, "rendered": False, "seen_in_data": True, "rule": None, "reveals": [],
                             "note": "in data but not rendered on the web wizard (phone/agent channel?)"})
            questions.append({
                "order": wq["order"], "code": (m or {}).get("code") or wq.get("code") or "",
                "text": strip_html(wq["question"]), "kind": wq["input"],
                "datatype": (m or {}).get("datatype"), "required": bool(wq.get("required")),
                "presence_pct": (m or {}).get("pct"), "options": opts,
                "parts": wq.get("parts"), "source": "wizard+data" if m else "wizard",
            })
        banners = [b for b in wiz.get("banners", []) if not COUNTER.search(b)]
    else:
        banners = []

    for r in mrows:                       # mined-only questions (not revealed on the safe path / not walked)
        if r["code"] in used_mined:
            continue
        questions.append({
            "order": r["ord"], "code": r["code"], "text": strip_html(r["question"]), "kind": None,
            "datatype": r["datatype"], "required": (r["pct"] or 0) >= 95, "presence_pct": r["pct"],
            "options": [{"value": o, "rendered": False, "seen_in_data": True, "rule": None, "reveals": []}
                        for o in (r["answer_vocab"] or [])],
            "source": "data-only",
            "note": "conditional (never revealed on the walked path)" if wiz and not wiz.get("error") else "service not yet walked",
        })
    questions.sort(key=lambda q: (q["order"], q["code"]))

    # Conditionality. Per-option "reveals" from the probe are noisy for plain
    # sequential questions (whichever option happened to be selected when the
    # next question rendered gets the credit), so a walked question counts as
    # conditional only if some option reveals it AND real submissions show it
    # on fewer than 95% of records. Data-only questions are conditional by
    # construction (the safe path never revealed them).
    walked_qs = [q for q in questions if q["source"] != "data-only"]
    for q in questions:
        rb = [{"question": (pq.get("code") or f"q{pq['order']}"), "option": o["value"]}
              for pq in walked_qs for o in pq["options"] if q["order"] in (o.get("reveals") or []) and pq is not q]
        q["revealed_by"] = rb
        if q["source"] == "data-only":
            q["conditional"] = True
        else:
            pct = q.get("presence_pct")
            q["conditional"] = bool(rb) and (pct is None or pct < 95)

    hard = sum(1 for q in questions for o in q["options"] if (o.get("rule") or {}).get("type") == "hard_stop")
    adv = sum(1 for q in questions for o in q["options"] if (o.get("rule") or {}).get("type") == "advisory")
    info = sum(1 for q in questions for o in q["options"] if (o.get("rule") or {}).get("type") == "info")
    branching = any(len({tuple(o.get("reveals", [])) for o in q["options"] if o.get("rendered")}) > 1 for q in questions)
    services.append({
        "service_code": code, "service_name": name, "description": t.get("description"),
        "keywords": t.get("keywords"), "type": t.get("type"),
        "departments": defs.get("service_departments", []),
        "groups": [c["name"] for c in defs.get("service_categories", [])],
        "coverage": {"mined_records": (mrows[0]["total"] if mrows else 0),
                     "walked": bool(wiz and not wiz.get("error")),
                     "walk_error": (wiz or {}).get("error"), "walk_note": (wiz or {}).get("note"),
                     "continue_reached": (wiz or {}).get("continue_enabled_at_end")},
        "banners": banners,
        "questions": questions,
        "rules_summary": {"hard_stops": hard, "advisories": adv, "info": info, "branching": branching},
    })

out = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
       "sources": {"catalog": len(catalog), "mined_categories": len(mined_by_name), "walked": len(wiz_by_code)},
       "services": services}
OUT.write_text(json.dumps(out, indent=1))
walked = sum(1 for s in services if s["coverage"]["walked"])
data_only = sum(1 for s in services if not s["coverage"]["walked"] and s["coverage"]["mined_records"])
none = sum(1 for s in services if not s["coverage"]["walked"] and not s["coverage"]["mined_records"])
nq = sum(len(s["questions"]) for s in services)
print(f"wrote {OUT}: {len(services)} services — walked {walked}, data-only {data_only}, no coverage {none}; "
      f"{nq} questions; {sum(s['rules_summary']['hard_stops'] for s in services)} hard stops, "
      f"{sum(s['rules_summary']['advisories'] for s in services)} advisories, "
      f"{sum(1 for s in services if s['rules_summary']['branching'])} branching services")
