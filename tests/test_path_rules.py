"""A refusal the City applies only after certain earlier answers.

The Sewer form refuses "Street/Main Line" after "Backup - Not Weather
Related" / "not inside the house", and takes it after "Missing or Broken
Manhole Cover" (checked on the live form 2026-11-02). The walk only walks
one path, so it saw a refusal and the registry applied it everywhere — and
for ten days residents could not report the most common sewer location. The
City's own web-form records showed it going through, and the drift check
said so. Pinned: such evidence scopes the refusal to its path; validation
blocks only on that path and advises elsewhere; the diff names the change;
the drift check stops calling it contradicted unless a record took that path.
"""
from datetime import datetime, timezone

from alex311 import registry_build as B, registry_drift as D, registry_store as S
from dashboard import registry as R

CATALOG = [{"service_code": "TESSEWER", "service_name": "Sewer Infrastructure Issues", "definitions": {}}]
MINED = [
    {"service_name": "Sewer Infrastructure Issues", "code": "01PL-SEWERTYPE", "ord": 1, "question": "What is the nature of your sewer request?",
     "datatype": "singlevaluelist", "pct": 100, "total": 50, "answer_vocab": ["Backup - Not Weather Related", "Missing or Broken Manhole Cover"]},
    {"service_name": "Sewer Infrastructure Issues", "code": "01PL-FLOODING", "ord": 2, "question": "Is the issue inside your house?",
     "datatype": "singlevaluelist", "pct": 40, "total": 50, "answer_vocab": ["Yes", "No"]},
    {"service_name": "Sewer Infrastructure Issues", "code": "01PL-MANHOLE", "ord": 3, "question": "Where is the location of your issue?",
     "datatype": "singlevaluelist", "pct": 90, "total": 50, "answer_vocab": ["Street/Main Line", "Sidewalk"]},
]
CALL = "Please call 703.746.4311 to speak to staff or to be connected to the afterhours service."


def probes(*hard):
    return lambda opts: {o: ({"hard_stop": True, "alert": [f"Validation Alert {CALL}"]} if o in hard else {}) for o in opts}


def walk():
    q = lambda order, text, opts, chosen, hard=(): {"order": order, "question": text, "required": True, "input": "radio",
                                                    "code": "", "options": opts, "probes": probes(*hard)(opts), "chosen": chosen}
    return {"categories": [{"service_code": "TESSEWER", "service_name": "Sewer Infrastructure Issues", "continue_enabled_at_end": True,
                            "questions": [q(1, "What is the nature of your sewer request?", ["Backup - Not Weather Related", "Missing or Broken Manhole Cover"], "Backup - Not Weather Related"),
                                          q(2, "Is the issue inside your house?", ["Yes", "No"], "No", hard=("Yes",)),
                                          q(3, "Where is the location of your issue?", ["Street/Main Line", "Sidewalk"], "Sidewalk", hard=("Street/Main Line",))]}]}


def rule(reg, qcode, value):
    s = reg["services"][0]
    q = next(q for q in s["questions"] if q["code"] == qcode)
    return next(o for o in q["options"] if o["value"] == value)["rule"]


def test_without_evidence_a_refusal_stays_everywhere():
    reg = B.build(CATALOG, MINED, walk())
    assert rule(reg, "01PL-MANHOLE", "Street/Main Line") == {"type": "hard_stop", "message": CALL}


def test_the_web_forms_own_records_scope_it_to_the_path_it_was_seen_on():
    reg = B.build(CATALOG, MINED, walk(), web_answers={("TESSEWER", "01PL-MANHOLE"): {"Street/Main Line", "Sidewalk"}})
    r = rule(reg, "01PL-MANHOLE", "Street/Main Line")
    assert r["type"] == "hard_stop" and r["when"] == [
        {"question": "01PL-SEWERTYPE", "option": "Backup - Not Weather Related"},
        {"question": "01PL-FLOODING", "option": "No"}]
    assert "when" not in rule(reg, "01PL-FLOODING", "Yes")                      # no evidence: untouched
    first = B.build(CATALOG, MINED, walk(), web_answers={("TESSEWER", "01PL-SEWERTYPE"): {"Backup - Not Weather Related"}})
    assert rule(first, "01PL-SEWERTYPE", "Backup - Not Weather Related") is None  # it never was a refusal


def test_validation_blocks_only_on_that_path():
    reg = B.build(CATALOG, MINED, walk(), web_answers={("TESSEWER", "01PL-MANHOLE"): {"Street/Main Line"}})
    s = reg["services"][0]
    backup = R.validate(s, {"01PL-SEWERTYPE": "Backup - Not Weather Related", "01PL-FLOODING": "No",
                            "01PL-MANHOLE": "Street/Main Line"})
    assert [b["option"] for b in backup["blocking"]] == ["Street/Main Line"] and not backup["ok"]
    manhole = R.validate(s, {"01PL-SEWERTYPE": "Missing or Broken Manhole Cover", "01PL-MANHOLE": "Street/Main Line"})
    assert manhole["blocking"] == [] and [a["message"] for a in manhole["advisories"]] == [CALL]


def test_the_diff_names_the_change():
    before = B.build(CATALOG, MINED, walk())
    after = B.build(CATALOG, MINED, walk(), web_answers={("TESSEWER", "01PL-MANHOLE"): {"Street/Main Line"}})
    lines = S.diff(before, after)
    assert len(lines) == 1 and lines[0].startswith("rule_scope_changed TESSEWER")
    assert "now only after 01PL-SEWERTYPE = Backup - Not Weather Related; 01PL-FLOODING = No" in lines[0]


def record(when, answers, source="Web"):
    return {"service_code": "TESSEWER", "source": source, "origin": "API", "requested_datetime": when,
            "attrs": [{"code": k, "description": k, "datatype": "singlevaluelist", "values": [{"answer_value": v}]}
                      for k, v in answers.items()]}


def test_the_drift_check_honours_the_scope():
    reg = B.build(CATALOG, MINED, walk(), web_answers={("TESSEWER", "01PL-MANHOLE"): {"Street/Main Line"}})
    reg["generated"] = "2026-10-23 06:54 UTC"
    later = datetime(2026, 10, 29, tzinfo=timezone.utc)
    manholes = [record(later, {"01PL-SEWERTYPE": "Missing or Broken Manhole Cover", "01PL-MANHOLE": "Street/Main Line"})] * 6
    assert [f for f in D.data_drift(manholes, reg) if f.kind == "rule_contradicted"] == []
    backup = record(later, {"01PL-SEWERTYPE": "Backup - Not Weather Related", "01PL-FLOODING": "No",
                            "01PL-MANHOLE": "Street/Main Line"})
    found = [f for f in D.data_drift(manholes + [backup], reg) if f.kind == "rule_contradicted"]
    assert len(found) == 1 and "on 2026-10-29" in found[0].summary
