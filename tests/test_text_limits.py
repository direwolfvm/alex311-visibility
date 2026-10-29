"""The City's form keeps 1,000 characters of any multi-line text box.

It does not refuse more: it silently keeps the first thousand, and on
2026-10-27 a 2,529-character report reached the City as 995. Pinned: the
check refuses what the City would cut (validate reports it, precheck answers
422) instead of anyone cutting it quietly; the form counts as you type; the
apps are told the limit; and the private rating note is refused, not cut.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import registry as R

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "dashboard/submit.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()


def a_service_with_free_text():
    for s in R.load_registry()["services"]:
        for q in s["questions"]:
            if q["kind"] == "text" and q["source"] != "data-only" and not q.get("conditional"):
                return s, q
    raise AssertionError("no free-text question in the registry")


def test_the_limit_is_the_citys():
    assert R.TEXT_LIMIT == 1000
    assert R.too_long("x" * 1000) is None and R.too_long("x" * 1001) == 1001
    assert R.too_long("  " + "x" * 1000 + "\n\n") is None          # what is sent is what is trimmed
    assert R.too_long(["x" * 5000]) is None and R.too_long(None) is None
    assert "shorten it by 1,529" in R.length_message(2529)


def test_validate_reports_a_long_description_and_a_long_answer():
    s, q = a_service_with_free_text()
    key = q.get("code") or f"q{q['order']}"
    v = R.validate(s, {key: "y" * 1200}, "x" * 2529)
    by_q = {e["question"]: e["message"] for e in v["errors"]}
    assert "this is 2,529" in by_q["description"] and "this is 1,200" in by_q[key]
    assert not v["ok"]
    ok = R.validate(s, {key: "y" * 1000}, "x" * 1000)
    assert "description" not in {e["question"] for e in ok["errors"]}
    assert key not in {e["question"] for e in ok["errors"]}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SUBMIT_USER", "alex311user")
    monkeypatch.setenv("SUBMIT_PASSWORD", "localtest")
    from dashboard.app import app
    return TestClient(app)                 # no lifespan: these never reach the database


def test_precheck_refuses_before_anything_is_recorded(client):
    s, _ = a_service_with_free_text()
    r = client.post("/submit/api/precheck", auth=("alex311user", "localtest"),
                    json={"service_code": s["service_code"], "address": "1 King St",
                          "description": "x" * 2529, "answers": {}})
    assert r.status_code == 422 and "this is 2,529" in r.json()["detail"]


def test_the_endpoint_takes_the_description(client):
    s, _ = a_service_with_free_text()
    r = client.post("/submit/api/validate", auth=("alex311user", "localtest"),
                    json={"service_code": s["service_code"], "answers": {}, "description": "x" * 1001})
    assert r.status_code == 200 and any(e["question"] == "description" for e in r.json()["errors"])


def test_the_apps_are_told():
    assert json.loads(R.full_payload())["limits"] == {"text": 1000}


def test_the_form_counts_and_never_cuts():
    assert 'id="desc"' in FORM and 'data-limit="1000"' in FORM and 'id="desc-count"' in FORM
    assert 'data-limit="${TEXT_LIMIT}"' in FORM                   # free-text answers too
    assert "const TEXT_LIMIT = 1000;" in FORM and "function countChars(el)" in FORM
    assert 'maxlength="1000"' not in FORM                          # a paste would be cut just as quietly
    assert "description: $('desc').value.trim() })" in FORM.replace("\n                           ", " ")


def test_nothing_is_cut_quietly_any_more():
    assert "[:2000]" not in ROUTES and "NOTE_LIMIT = 2000" in ROUTES
    fn = ROUTES.split('@router.put("/api/feedback/{case}")')[1].split("@router")[0]
    assert "raise HTTPException(422" in fn
