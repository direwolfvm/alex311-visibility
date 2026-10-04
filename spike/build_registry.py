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
from pathlib import Path

from alex311.registry_build import build

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/data/form-registry.json"

catalog = json.load(open(ROOT / "docs/data/service-catalog.json"))
mined = json.load(open(ROOT / "docs/data/question-schema.mined.json"))
wizard = json.load(open(ROOT / "docs/data/wizard-rules.json"))
out = build(catalog, mined, wizard)
services = out["services"]
OUT.write_text(json.dumps(out, indent=1))
walked = sum(1 for s in services if s["coverage"]["walked"])
data_only = sum(1 for s in services if not s["coverage"]["walked"] and s["coverage"]["mined_records"])
none = sum(1 for s in services if not s["coverage"]["walked"] and not s["coverage"]["mined_records"])
nq = sum(len(s["questions"]) for s in services)
print(f"wrote {OUT}: {len(services)} services — walked {walked}, data-only {data_only}, no coverage {none}; "
      f"{nq} questions; {sum(s['rules_summary']['hard_stops'] for s in services)} hard stops, "
      f"{sum(s['rules_summary']['advisories'] for s in services)} advisories, "
      f"{sum(1 for s in services if s['rules_summary']['branching'])} branching services")
