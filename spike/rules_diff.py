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


from alex311.rules_diff import diff, norm, questions_of, rule_of  # noqa: E402,F401  (moved into the package)


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
