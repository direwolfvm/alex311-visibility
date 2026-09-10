"""Registry-driven validation, exercised against the real registry file."""
import pytest

from dashboard import registry as R

REG = R.load_registry()


def svc(code):
    s = R.get_service(REG, code)
    assert s, code
    return s


def test_index_covers_catalog():
    idx = R.service_index(REG)
    assert len(idx) == 111
    assert any(s["service_code"] == "TESMISCO" for s in idx)


def test_required_question_missing_is_an_error():
    s = svc("TESCONTN")
    r = R.validate(s, {})
    assert not r["ok"]
    assert any(e["order"] == 1 for e in r["errors"])


def test_hard_stop_option_blocks():
    s = svc("RPCATREINSP")           # "Is the tree blocking the road?" -> Yes is a hard stop
    r = R.validate(s, {"01PL-RPCTREERD": "Yes"})
    assert r["blocking"] and "Police" in r["blocking"][0]["message"]
    assert not r["ok"]


def test_advisory_does_not_block():
    s = svc("TESCONTN")              # "New Resident" is advisory and reveals Q2, Q3
    r = R.validate(s, {"01PL-CONTAINTYP": "New Resident"})
    assert r["advisories"] and not r["blocking"]


def test_reveal_makes_revealed_question_required():
    s = svc("TESCONTN")
    vis = {q["order"] for q in R.visible_questions(s, {"01PL-CONTAINTYP": "New Resident"}) if q["source"] != "data-only"}
    assert {2, 3} <= vis
    vis2 = {q["order"] for q in R.visible_questions(s, {"01PL-CONTAINTYP": "Missing Container"}) if q["source"] != "data-only"}
    assert 3 not in vis2             # "Date of move in?" only for some choices


def test_unknown_option_rejected():
    s = svc("TESMISCO")
    r = R.validate(s, {"01PL-MISSEDTYP": "Bananas"})
    assert any("not an option" in e["message"] for e in r["errors"])
