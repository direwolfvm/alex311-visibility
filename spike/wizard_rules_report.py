"""Render docs/data/wizard-rules.json (from spike/wizard_rules.py) as docs/wizard-rules.md."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data = json.load(open(ROOT / "docs/data/wizard-rules.json"))
mined = json.load(open(ROOT / "docs/data/question-schema.mined.json"))

COUNTER = re.compile(r"mandatory question", re.I)


def clean_alerts(p):
    """Drop the progress counter and the bare 'Validation Alert' title; keep the guidance text."""
    out = []
    for a in p.get("alert", []):
        if COUNTER.search(a) or a.strip() == "Validation Alert":
            continue
        out.append(a.replace("Validation Alert ", "", 1))
    return list(dict.fromkeys(out))


# Mined vocab by (category, code) so we can flag rendered options the data never shows.
VOCAB = {}
for r in mined:
    if r["datatype"] and "list" in r["datatype"]:
        VOCAB[(r["service_name"], r["code"])] = set(r["answer_vocab"] or [])


def stats(c):
    qs = c.get("questions", [])
    hard = sum(1 for e in qs for p in e["probes"].values() if p.get("hard_stop"))
    adv = sum(1 for e in qs for p in e["probes"].values() if p.get("advisory"))
    info = sum(1 for e in qs for p in e["probes"].values() if not p.get("hard_stop") and not p.get("advisory") and clean_alerts(p))
    branch = any(len({tuple(p["reveals"]) for p in e["probes"].values() if "reveals" in p}) > 1 for e in qs)
    return len(qs), hard, adv, info, branch


L = ["# Wizard rules — discovered per category", "",
     f"*Generated {data['generated']} by `spike/wizard_rules.py`, read-only (it never presses Submit). "
     f"Each option of each list question was selected in place and its effect recorded. "
     f"Skipped because not in the current catalog: {', '.join(data['skipped_not_in_catalog']) or '—'}.*", "",
     "**Hard stop** — a Validation Alert appeared and the answer was rejected after OK. "
     "**Advisory** — an alert appeared but the answer stood. **Info** — an inline message without a modal. "
     "**Reveals** — which question numbers became visible on choosing that option; options that reveal "
     "different sets are skip-logic the form must replicate.", "",
     "| Category | Code | Questions | Hard stops | Advisory | Info msgs | Branching | Continue at end |",
     "|---|---|---|---|---|---|---|---|"]
for c in data["categories"]:
    if c.get("error"):
        L.append(f"| {c['service_name']} | `{c['service_code']}` | — | — | — | — | — | ERROR: {c['error']} |")
        continue
    n, hard, adv, info, branch = stats(c)
    L.append(f"| {c['service_name']} | `{c['service_code']}` | {n} | {hard} | {adv} | {info} | {'yes' if branch else 'no'} | {c.get('continue_enabled_at_end')} |")
L.append("")

for c in data["categories"]:
    L += [f"## {c['service_name']} (`{c['service_code']}`)", ""]
    if c.get("error"):
        L += [f"**Error:** {c['error']}", ""]
        continue
    banners = [b for b in c.get("banners", []) if not COUNTER.search(b)]
    if banners:
        L += ["Banners: " + " · ".join(f"“{b}”" for b in banners), ""]
    for e in c["questions"]:
        req = " *(required)*" if e.get("required") else ""
        code = f" — `{e['code']}`" if e.get("code") else ""
        if e["input"] in ("text", "composite"):
            parts = ", ".join(p["placeholder"] or p["type"] or p["tag"] for p in e.get("parts", [])) or "free text"
            L.append(f"{e['order']}. **{e['question']}**{req}{code} — {e['input']}: {parts}")
            continue
        opts = ", ".join(e["options"]) if e["options"] else "—"
        L.append(f"{e['order']}. **{e['question']}**{req}{code} — {e['input']}; options: {opts}")
        v = VOCAB.get((c["service_name"], e["code"])) if e.get("code") else None
        if v is not None:
            unseen = [o for o in e["options"] if o not in v]
            if unseen:
                L.append(f"   - ⚠ rendered but never present in the mined data: {', '.join(unseen)}")
        for opt, p in e["probes"].items():
            if p.get("error"):
                L.append(f"   - {opt}: probe error — {p['error']}")
                continue
            tags = []
            if p.get("hard_stop"):
                tags.append("**HARD STOP**")
            elif p.get("advisory"):
                tags.append("advisory")
            al = clean_alerts(p)
            if al:
                tags.append("“" + " / ".join(al) + "”")
            if p.get("reveals"):
                tags.append("reveals → Q" + ", Q".join(str(r) for r in p["reveals"]))
            if tags:
                L.append(f"   - {opt}: " + "; ".join(tags))
    L.append("")

out = ROOT / "docs/wizard-rules.md"
out.write_text("\n".join(L))
tot = [stats(c) for c in data["categories"] if not c.get("error")]
print(f"wrote {out} — {len(data['categories'])} categories, {sum(t[0] for t in tot)} questions, "
      f"{sum(t[1] for t in tot)} hard stops, {sum(t[2] for t in tot)} advisories, {sum(t[3] for t in tot)} info msgs, "
      f"{sum(1 for t in tot if t[4])} with branching, {sum(1 for c in data['categories'] if c.get('error'))} errors")
