"""The registry as data: what is in use, what is offered, who decides.

What is pinned: the bundled file is the registry until something is adopted
and whenever the database cannot be read; a retired request type leaves at
once and nothing else changes without a person; one proposal at a time; a
dismissed proposal is not offered again; an earlier version can be put back.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from alex311 import registry_store as S
from dashboard import registry as R

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = json.loads((ROOT / "docs/data/form-registry.json").read_text())
ADMIN = (ROOT / "dashboard/admin.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()


def changed(fn):
    reg = copy.deepcopy(BUNDLED)
    fn(reg)
    return reg


def first_with_options(reg):
    for s in reg["services"]:
        for q in s["questions"]:
            if q["source"] != "data-only" and len([o for o in q["options"] if o["rendered"]]) >= 2:
                return s, q
    raise AssertionError("no walked list question in the registry")


def kinds(lines):
    return sorted(line.split()[0] for line in lines)


# ------------------------------------------------------------- always run

def test_the_same_registry_differs_from_itself_in_nothing():
    assert S.diff(BUNDLED, copy.deepcopy(BUNDLED)) == []


def test_the_diff_says_what_a_resident_would_notice():
    def edit(reg):
        s, q = first_with_options(reg)
        q["text"] = "Something else entirely?"
        q["required"] = not q["required"]
        rendered = [o for o in q["options"] if o["rendered"]]
        q["options"].remove(rendered[0])
        rendered[1]["rule"] = {"type": "hard_stop", "message": "Call 911."}
        q["options"].append({"value": "A new answer", "rendered": True, "seen_in_data": False,
                             "rule": None, "reveals": []})
        s["questions"].append({"order": 99, "code": "NEWQ", "text": "A new question?", "kind": "radio",
                               "required": True, "options": [], "source": "wizard"})
        reg["services"].pop()
    lines = S.diff(BUNDLED, changed(edit))
    assert kinds(lines) == ["option_added", "option_removed", "question_added", "question_reworded",
                            "required_changed", "rule_changed", "service_removed"]
    assert all(": " in line for line in lines)                   # every line names its request type


def test_the_version_ignores_the_day_it_was_built():
    again = dict(copy.deepcopy(BUNDLED), generated="another day")
    assert S.fingerprint(again) == S.fingerprint(BUNDLED) and len(S.fingerprint(BUNDLED)) == 16
    assert S.fingerprint(changed(lambda r: r["services"].pop())) != S.fingerprint(BUNDLED)


def test_without_a_database_the_bundled_file_is_the_registry(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cur = S.current()
    assert cur["source"] == "bundled" and cur["registry"] == BUNDLED

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("the database is away")
        def rollback(self):
            pass
    assert S.current(Broken())["source"] == "bundled"            # a problem is not an outage


def test_the_site_follows_an_adopted_version_and_survives_losing_it(monkeypatch):
    bundled_version = R.registry_version()
    adopted = {"version": "abc123abc123abcd", "registry": changed(lambda r: r["services"].pop())}
    calls = []

    def provider(have):
        calls.append(have)
        if adopted is None:
            raise RuntimeError("the database is away")
        return {"version": adopted["version"], "registry": None} if have == adopted["version"] else adopted
    try:
        R.set_provider(provider)
        assert R.registry_version() == "abc123abc123abcd" and R.registry_source() == "database"
        assert len(R.load_registry()["services"]) == len(BUNDLED["services"]) - 1
        assert json.loads(R.full_payload())["version"] == "abc123abc123abcd"
        assert calls == [None]                                   # asked once, then served from memory
        monkeypatch.setattr(R, "TTL_SECONDS", 0)
        assert R.registry_version() == "abc123abc123abcd" and calls[-1] == "abc123abc123abcd"
        adopted = None                                           # now the database goes away
        assert R.registry_version() == "abc123abc123abcd"        # and it keeps what it had
        R.set_provider(lambda have: None)                        # nothing adopted
        assert R.registry_version() == bundled_version and R.registry_source() == "bundled"
    finally:
        R.set_provider(None)
    assert R.registry_version() == bundled_version


def test_deciding_is_for_administrators_and_is_on_the_admin_page():
    for route in ('@router.get("/api/admin/registry")', '@router.post("/api/admin/registry/adopt")',
                  '@router.post("/api/admin/registry/dismiss")'):
        fn = ROUTES.split(route)[1].split("\n    @router")[0]
        assert "Depends(admin_only)" in fn
    assert "R.refresh()" in ROUTES.split('"/api/admin/registry/adopt"')[1].split("@router")[0]
    assert 'id="st-registry"' in ADMIN and 'id="reg-adopt"' in ADMIN and 'id="reg-dismiss"' in ADMIN
    assert "api('/admin/registry')" in ADMIN and "confirm('Adopt this registry?" in ADMIN
    # every kind of change the diff can produce has words on the page
    import re
    produced = set(re.findall(r'f"(\w+) \{(?:code|where)\}', (ROOT / "src/alex311/registry_store.py").read_text()))
    produced |= {"service_description_changed", "service_groups_changed", "service_keywords_changed",
                 "service_banners_changed"}
    produced.discard("service_")
    for kind in produced:
        assert f"{kind}:" in ADMIN, kind


def test_the_worker_and_the_drift_check_read_the_registry_in_use():
    assert 'registry_store.current()["registry"]' in (ROOT / "src/alex311/submit_browser.py").read_text()
    assert "registry_store.current()" in (ROOT / "src/alex311/registry_drift.py").read_text()


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@pytest.fixture()
def conn():
    from alex311 import db
    c = db.connect(DB)
    c.execute("DELETE FROM registry_versions"); c.execute("DELETE FROM wizard_walks"); c.commit()
    yield c
    c.rollback()
    c.execute("DELETE FROM registry_versions"); c.execute("DELETE FROM wizard_walks"); c.commit()
    c.close()


@needs_db
def test_a_proposal_waits_and_adopting_it_puts_it_in_use(conn):
    assert S.current(conn)["source"] == "bundled"
    new = changed(lambda r: first_with_options(r)[1].update(text="Reworded by the City?"))
    got = S.propose(conn, new, note="test")
    assert got["status"] == "proposed" and got["new"] and kinds(got["changes"]) == ["question_reworded"]
    assert S.current(conn)["source"] == "bundled"                # offered, not used
    st = S.state(conn)
    assert st["proposed"]["version"] == got["version"] and st["in_use"]["source"] == "bundled"

    assert S.adopt(conn, got["version"], "admin@test.io") is True
    cur = S.current(conn)
    assert cur["source"] == "database" and cur["version"] == got["version"]
    assert cur["adopted_by"] == "admin@test.io" and cur["registry"]["services"] == new["services"]
    assert S.state(conn)["proposed"] is None
    assert S.propose(conn, new)["status"] == "current"           # the same thing again changes nothing
    assert S.adopt(conn, "nosuchversion000", "admin@test.io") is False


@needs_db
def test_the_app_role_can_read_and_decide_but_the_walks_are_read_only(conn):
    from alex311 import db
    got = S.propose(conn, changed(lambda r: r["services"][0].update(description="New words.")))
    app = db.connect(os.environ.get("APP_DATABASE_URL") or DB)
    try:
        assert S.state(app)["proposed"]["version"] == got["version"]
        assert S.adopt(app, got["version"], "admin@test.io") is True
        assert S.current(app)["version"] == got["version"]
    finally:
        app.close()


@needs_db
def test_one_proposal_at_a_time_and_a_dismissed_one_stays_dismissed(conn):
    a = S.propose(conn, changed(lambda r: r["services"][0].update(description="First.")))
    b = S.propose(conn, changed(lambda r: r["services"][0].update(description="Second.")))
    rows = {r["version"]: r["status"] for r in conn.execute("SELECT version, status FROM registry_versions")}
    assert rows == {a["version"]: "retired", b["version"]: "proposed"}
    assert S.dismiss(conn, b["version"], "admin@test.io") is True
    again = S.propose(conn, changed(lambda r: r["services"][0].update(description="Second.")))
    assert again["status"] == "dismissed" and S.state(conn)["proposed"] is None
    assert S.dismiss(conn, b["version"], "admin@test.io") is False


@needs_db
def test_a_retired_request_type_leaves_at_once_and_can_be_put_back(conn):
    gone = BUNDLED["services"][-1]
    version = S.retire_services(conn, {gone["service_code"]})
    cur = S.current(conn)
    assert cur["version"] == version and cur["adopted_by"] == "walk"
    assert gone["service_code"] not in {s["service_code"] for s in cur["registry"]["services"]}
    assert S.retire_services(conn, {gone["service_code"]}) is None            # already gone
    # an administrator can undo an adoption by adopting an earlier version
    first = S.propose(conn, BUNDLED)
    assert kinds(first["changes"]) == ["service_added"]
    S.adopt(conn, first["version"], "admin@test.io")
    assert len(S.current(conn)["registry"]["services"]) == len(BUNDLED["services"])
    S.adopt(conn, version, "admin@test.io")                                   # and back again
    assert S.current(conn)["version"] == version
    assert conn.execute("SELECT count(*) AS n FROM registry_versions WHERE status = 'active'").fetchone()["n"] == 1
