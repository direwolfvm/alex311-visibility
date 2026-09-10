"""Diff a fresh wizard crawl against the committed rules.

The other half of drift detection. `alex311.registry_drift` watches the
catalog and the answers people actually submit — both browser-free, both
runnable in the deployed image. This one covers what only a walk can see:
the questions the wizard renders and the rules it enforces per option.

Workflow (weekly):

    cp docs/data/wizard-rules.json /tmp/rules-baseline.json
    uv run python spike/wizard_rules.py all            # read-only, never submits
    uv run python spike/rules_diff.py /tmp/rules-baseline.json docs/data/wizard-rules.json

Exits 1 when anything material changed, so a scheduled run can alert. Nothing
here touches the portal; it reads two JSON files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "docs/data/wizard-rules.json"


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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="rules_diff",
        description="Diff two wizard-rules.json crawls; exit 1 on any material change.")
    p.add_argument("baseline", nargs="?", default=str(COMMITTED),
                   help="the rules we believe (default: the committed file)")
    p.add_argument("fresh", help="a newly crawled wizard-rules.json")
    p.add_argument("--ignore-message-text", action="store_true",
                   help="report only rule-kind flips, not reworded City messages")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    old = json.loads(Path(a.baseline).read_text())
    new = json.loads(Path(a.fresh).read_text())
    changes = diff(old, new, ignore_message_text=a.ignore_message_text)

    if a.json:
        print(json.dumps({"baseline": a.baseline, "fresh": a.fresh, "changes": changes}, indent=1))
    else:
        print(f"baseline {old.get('generated')} ({len(old['categories'])} services) vs "
              f"fresh {new.get('generated')} ({len(new['categories'])} services)")
        for c in changes:
            print(f"  {c}")
        print(f"{len(changes)} change(s)" if changes else "no changes")
    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
