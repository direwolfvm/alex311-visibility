"""What changed between two walks of the City's wizard.

A walk records, per request type, the questions the wizard renders and what
each answer does. This compares two such records and says what moved, in
lines a person can read: a question added or reworded, an option gone, a
rule that flipped from a warning to a hard stop.

Pure: it reads two structures and touches nothing. `spike/rules_diff.py`
runs it over two files; the nightly walk (`alex311.walk`) runs it per
request type against the registry in use.
"""
from __future__ import annotations

import re


def norm(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"<[^>]+>", " ", t or "").lower()).strip()


def rule_of(probe: dict) -> tuple[str, str]:
    """(kind, message) for one probed option, in the shape build_registry uses."""
    msgs = [a.replace("Validation Alert ", "", 1) for a in probe.get("alert", [])
            if not re.search(r"mandatory question", a, re.I) and a.strip() != "Validation Alert"]
    text = " / ".join(dict.fromkeys(msgs))
    if probe.get("hard_stop"):
        return "hard_stop", text
    if probe.get("advisory"):
        return "advisory", text
    if probe.get("suggests"):
        return "suggestion", probe["suggests"][:120]
    return ("info", text) if text else ("none", "")


def questions_of(cat: dict) -> dict[int, dict]:
    return {q["order"]: q for q in cat.get("questions", [])}


def diff(old: dict, new: dict, *, ignore_message_text: bool = False) -> list[str]:
    o = {c["service_code"]: c for c in old["categories"]}
    n = {c["service_code"]: c for c in new["categories"]}
    out: list[str] = []

    for code in sorted(n.keys() - o.keys()):
        out.append(f"service_added        {code} ({n[code]['service_name']}) newly walked")
    for code in sorted(o.keys() - n.keys()):
        out.append(f"service_removed      {code} ({o[code]['service_name']}) no longer walked")

    for code in sorted(o.keys() & n.keys()):
        oc, nc = o[code], n[code]
        if bool(oc.get("continue_enabled_at_end")) != bool(nc.get("continue_enabled_at_end")):
            out.append(f"walk_status_changed  {code}: reached the end "
                       f"{oc.get('continue_enabled_at_end')} -> {nc.get('continue_enabled_at_end')}")
        oq, nq = questions_of(oc), questions_of(nc)

        for k in sorted(nq.keys() - oq.keys()):
            out.append(f"question_added       {code} Q{k}: {nq[k]['question'][:70]!r}")
        for k in sorted(oq.keys() - nq.keys()):
            out.append(f"question_removed     {code} Q{k}: {oq[k]['question'][:70]!r}")

        for k in sorted(oq.keys() & nq.keys()):
            a, b = oq[k], nq[k]
            if norm(a["question"]) != norm(b["question"]):
                out.append(f"question_text        {code} Q{k}: {a['question'][:50]!r} -> {b['question'][:50]!r}")
            if bool(a.get("required")) != bool(b.get("required")):
                out.append(f"required_changed     {code} Q{k}: {a.get('required')} -> {b.get('required')}")
            if a.get("input") != b.get("input"):
                out.append(f"widget_changed       {code} Q{k}: {a.get('input')} -> {b.get('input')}")

            ao, bo = set(a.get("options") or []), set(b.get("options") or [])
            for v in sorted(bo - ao):
                out.append(f"option_added         {code} Q{k}: {v!r}")
            for v in sorted(ao - bo):
                out.append(f"option_removed       {code} Q{k}: {v!r}")

            for v in sorted(ao & bo):
                ak, am = rule_of(a["probes"].get(v, {}))
                bk, bm = rule_of(b["probes"].get(v, {}))
                if ak != bk:
                    out.append(f"rule_changed         {code} Q{k} {v!r}: {ak} -> {bk}"
                               + (f" ({bm[:80]!r})" if bm else ""))
                elif am != bm and not ignore_message_text:
                    out.append(f"rule_message_changed {code} Q{k} {v!r}: {am[:60]!r} -> {bm[:60]!r}")
    return out


def diff_one(old_cat: dict | None, new_cat: dict) -> list[str]:
    """Changes for a single request type; a type with no earlier walk is new."""
    if old_cat is None:
        return [f"service_added        {new_cat['service_code']} ({new_cat['service_name']}) newly walked"]
    return diff({"categories": [old_cat]}, {"categories": [new_cat]})
