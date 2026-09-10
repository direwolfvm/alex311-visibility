"""Drift detection: does the registry still describe the City's live form?

Synthetic fixtures, not the real registry — these assert the *rules* of the
check, so they stay meaningful after the next crawl changes the real numbers.
"""
from datetime import datetime, timezone

import pytest

from alex311.registry_drift import catalog_drift, data_drift, is_web, observed


def reg(*services, generated=None):
    return {"generated": generated or CRAWLED, "services": list(services)}


def service(code="TESNOISE", name="Noise Issues", questions=()):
    return {"service_code": code, "service_name": name, "questions": list(questions)}


def question(code="01PL-NOISESOUR", text="What is the source of the noise?",
             datatype="singlevaluelist", options=(), **kw):
    q = {"code": code, "order": 1, "text": text, "datatype": datatype,
         "options": [o if isinstance(o, dict) else {"value": o, "rule": None} for o in options]}
    q.update(kw)
    return q


RECENT = datetime(2026, 9, 20, tzinfo=timezone.utc)     # after CRAWLED
BEFORE = datetime(2026, 8, 14, tzinfo=timezone.utc)     # before CRAWLED
CRAWLED = "2026-09-01 00:00 UTC"


def row(code="TESNOISE", attrs=None, source="iOS", origin="API", at=RECENT):
    return {"service_code": code, "source": source, "origin": origin,
            "requested_datetime": at, "attrs": attrs if attrs is not None else []}


def attr(code="01PL-NOISESOUR", description="What is the source of the noise?",
         datatype="singlevaluelist", answers=("Other",)):
    return {"code": code, "description": description, "datatype": datatype,
            "values": [{"answer": a, "answer_value": a} for a in answers]}


def rows_for(*, n=5, **kw):
    return [row(**kw) for _ in range(n)]


# ------------------------------------------------------------------- catalog

def test_catalog_reports_added_and_renamed_services():
    registry = reg(service("A", "Alpha"), service("B", "Bravo"))
    live = [{"service_code": "A", "service_name": "Alpha"},
            {"service_code": "B", "service_name": "Bravo Renamed"},
            {"service_code": "C", "service_name": "Charlie"}]
    found = {(f.kind, f.service_code) for f in catalog_drift(live, registry)}
    assert found == {("service_added", "C"), ("service_renamed", "B")}


def test_catalog_ignores_cosmetic_renames():
    registry = reg(service("A", "Tree Inspection Request"))
    live = [{"service_code": "A", "service_name": "Tree  Inspection <b>Request</b>"}]
    assert catalog_drift(live, registry) == []


def test_catalog_reports_a_service_that_disappeared():
    registry = reg(service("A", "Alpha"), service("B", "Bravo"))
    live = [{"service_code": "A", "service_name": "Alpha"}]
    found = catalog_drift(live, registry)
    assert [(f.kind, f.service_code) for f in found] == [("service_removed", "B")]


# ---------------------------------------------------------------- channel

@pytest.mark.parametrize("source,origin,expected", [
    ("Web", "API", True),
    ("iOS", "API", True),
    ("Android Browser", "API", True),
    ("ios browser", "API", True),   # case-insensitive
    ("Agent", "Phone", False),      # staff typing a phone call
    ("Web", "Phone", False),
    (None, "Email", False),         # emailed report, transcribed by staff
    (None, "Facebook", False),
    ("snap311", "API", False),      # third-party app with its own form
    (None, None, False),            # unknown channel: excluded, never assumed
])
def test_is_web_allows_only_wizard_channels(source, origin, expected):
    assert is_web({"source": source, "origin": origin}) is expected


def test_observed_counts_only_web_rows():
    rows = [row(source="iOS"), row(source="Agent", origin="Phone"),
            row(source=None, origin="Email")]
    seen = observed(rows)
    assert seen["TESNOISE"]["rows"] == 1


# ---------------------------------------------------------------- data side

def test_new_question_code_is_drift():
    registry = reg(service(questions=[question()]))
    rows = rows_for(attrs=[attr(), attr(code="01PL-BRANDNEW", description="New one?")])
    found = data_drift(rows, registry)
    assert [f.kind for f in found] == ["question_added"]
    assert found[0].detail["question"] == "01PL-BRANDNEW"


def test_reworded_question_is_drift():
    registry = reg(service(questions=[question(text="What is the source of the noise?")]))
    rows = rows_for(attrs=[attr(description="Where is the noise coming from?")])
    assert [f.kind for f in data_drift(rows, registry)] == ["question_text_changed"]


def test_markup_and_spacing_are_not_drift():
    registry = reg(service(questions=[question(text="What is the source of the noise?")]))
    rows = rows_for(attrs=[attr(description="What is the source of  the <br/>noise?")])
    assert data_drift(rows, registry) == []


def test_datatype_change_is_drift():
    registry = reg(service(questions=[question(datatype="singlevaluelist")]))
    rows = rows_for(attrs=[attr(datatype="multivaluelist")])
    assert [f.kind for f in data_drift(rows, registry)] == ["datatype_changed"]


def test_unknown_answer_value_is_drift():
    registry = reg(service(questions=[question(options=["Other", "Music"])]))
    rows = rows_for(attrs=[attr(answers=("Construction",))])
    found = data_drift(rows, registry)
    assert [f.kind for f in found] == ["option_added"]
    assert found[0].detail["option"] == "Construction"


def test_known_answer_is_not_drift():
    registry = reg(service(questions=[question(options=["Other", "Music"])]))
    rows = rows_for(attrs=[attr(answers=("Music",))])
    assert data_drift(rows, registry) == []


def test_free_text_answers_are_never_option_drift():
    registry = reg(service(questions=[question(datatype="string", options=[])]))
    rows = rows_for(attrs=[attr(datatype="string", answers=("anything a resident typed",))])
    assert data_drift(rows, registry) == []


def test_hard_stopped_answer_submitted_after_the_crawl_is_drift():
    opt = {"value": "Private", "rule": {"type": "hard_stop", "message": "not City property"}}
    registry = reg(service(questions=[question(options=["Public", opt])]))
    rows = rows_for(attrs=[attr(answers=("Private",))], at=RECENT)
    found = data_drift(rows, registry)
    assert [f.kind for f in found] == ["rule_contradicted"]
    assert "2026-09-20" in found[0].summary


def test_hard_stopped_answer_from_before_the_crawl_is_not_drift():
    """Only a submission newer than the walk can mean the rule was relaxed;
    older ones are the phone-channel skew the registry already records."""
    opt = {"value": "Private", "rule": {"type": "hard_stop", "message": "not City property"}}
    registry = reg(service(questions=[question(options=["Public", opt])]))
    rows = rows_for(attrs=[attr(answers=("Private",))], at=BEFORE)
    assert data_drift(rows, registry) == []


def test_hard_stopped_answer_from_an_agent_is_not_drift():
    """Phone agents bypass the wizard's rules, so their records prove nothing."""
    opt = {"value": "Private", "rule": {"type": "hard_stop", "message": "not City property"}}
    registry = reg(service(questions=[question(options=["Public", opt])]))
    rows = rows_for(attrs=[attr(answers=("Private",))], source="Agent", origin="Phone")
    assert data_drift(rows, registry) == []


def test_padded_answers_are_not_drift():
    """The portal stores some answers with surrounding spaces."""
    registry = reg(service(questions=[question(options=["Construction", "Other"])]))
    rows = rows_for(attrs=[attr(answers=(" Construction ",))])
    assert data_drift(rows, registry) == []


def test_data_only_questions_are_never_option_drift():
    """We never saw the question rendered, so its vocabulary is not a claim
    about what the form offers — comparing answers to it is pure noise."""
    q = question(options=["N/A", "Yes", "No"], source="data-only")
    registry = reg(service(questions=[q]))
    rows = rows_for(attrs=[attr(answers=("NA",))])
    assert data_drift(rows, registry) == []


def test_thin_data_does_not_raise_an_alarm():
    registry = reg(service(questions=[question(options=["Other"])]))
    rows = rows_for(n=2, attrs=[attr(answers=("Construction",))])
    assert data_drift(rows, registry, min_rows=5) == []
    assert data_drift(rows, registry, min_rows=2) != []


def test_services_missing_from_the_registry_are_left_to_the_catalog_check():
    registry = reg(service("OTHER"))
    rows = rows_for(code="TESNOISE", attrs=[attr()])
    assert data_drift(rows, registry) == []


# ------------------------------------------------------- locating the registry

def test_registry_path_prefers_the_env_override(tmp_path, monkeypatch):
    from alex311.registry_drift import load_registry, registry_path

    f = tmp_path / "custom.json"
    f.write_text('{"services": [], "generated": "x"}')
    monkeypatch.setenv("FORM_REGISTRY", str(f))
    assert registry_path() == f
    assert load_registry()["generated"] == "x"


def test_registry_path_falls_back_to_the_working_directory(tmp_path, monkeypatch):
    """The container pip-installs alex311 but copies docs/data next to WORKDIR."""
    from alex311 import registry_drift

    f = tmp_path / "docs/data/form-registry.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"services": [], "generated": "cwd"}')
    monkeypatch.delenv("FORM_REGISTRY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry_drift, "REGISTRY_NAME", "docs/data/form-registry.json")
    monkeypatch.setattr(registry_drift.Path, "cwd", staticmethod(lambda: tmp_path))
    assert registry_drift.registry_path() == f
