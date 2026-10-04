"""The nightly walk: what it walks, what it believes, what it offers.

No browser here: the probe is replaced by one that answers from the walk
shipped with the site. What is pinned: oldest first, and anything doubtful
before that; a walk that sees less than the one before is not believed until
a second walk agrees; a short catalog is a bad answer, not a mass
retirement; it stops when the portal refuses; filings go first.
"""
import asyncio
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import registry_store as S, submit_worker, walk as W

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "docs/data/service-catalog.json").read_text())
RULES = {c["service_code"]: c for c in json.loads((ROOT / "docs/data/wizard-rules.json").read_text())["categories"]}
SOURCE = (ROOT / "src/alex311/walk.py").read_text()
WITH_QUESTIONS = [c for c in RULES.values() if c.get("questions") and not c.get("error")
                  and c.get("continue_enabled_at_end") is True]


def test_the_walk_never_submits_and_shares_the_floor():
    assert "Submit Request" not in SOURCE and 'press(pg, "Submit' not in SOURCE
    assert "from .submit_worker import FLOOR_LOCK" in SOURCE
    assert W.WALK_LOCK != submit_worker.FLOOR_LOCK
    worker = (ROOT / "src/alex311/submit_worker.py").read_text()
    assert "if not take_the_floor(conn) and not wait_out_a_walk(conn):" in worker


def test_what_counts_as_a_walk_that_ran():
    assert W.ok({"questions": [], "continue_enabled_at_end": True, "error": None})
    assert W.ok({"questions": [], "continue_enabled_at_end": False, "error": None})
    assert not W.ok({"questions": [], "error": "TimeoutError: page did not load"})
    assert not W.ok({"questions": [], "continue_enabled_at_end": None, "error": None})
    assert W.ok({"questions": [], "error": "RuntimeError: this request type is view-only"})


def test_seeing_less_than_before_is_doubted():
    before = {"questions": [{}, {}], "continue_enabled_at_end": True}
    assert W.degraded(before, {"questions": [], "continue_enabled_at_end": True})
    assert W.degraded(before, {"questions": [{}, {}], "continue_enabled_at_end": False})
    assert not W.degraded(before, {"questions": [{}, {}, {}], "continue_enabled_at_end": True})
    assert not W.degraded(None, {"questions": [], "continue_enabled_at_end": False})


def test_tonights_slice_is_the_doubtful_then_the_never_walked_then_the_oldest():
    now = datetime.now(timezone.utc)
    cat = CATALOG[:5]
    a, b, c, d, e = (t["service_code"] for t in cat)
    latest = {a: {"suspect": False, "walked_at": now - timedelta(days=1)},
              b: {"suspect": False, "walked_at": now - timedelta(days=6)},
              c: {"suspect": True, "walked_at": now},
              d: {"suspect": False, "walked_at": now - timedelta(days=3)}}
    assert [code for _, code, _ in W.pick(cat, latest, 10)] == [c, e, b, d, a]
    assert [code for _, code, _ in W.pick(cat, latest, 2)] == [c, e]
    assert [code for _, code, _ in W.pick(cat, latest, 10, codes=[d, "NOPE", a])] == [d, a]
    name, code, groups = W.pick(cat, latest, 1)[0]
    assert isinstance(groups, list) and name


def test_the_job_is_bounded_and_polite():
    assert W.PAUSE_SECONDS >= 2 and W.MAX_CONSECUTIVE_ERRORS <= 3
    assert "len(live) < 0.8 * len(known)" in SOURCE
    main = SOURCE.split("def main(")[1]
    assert '"--limit", type=int, default=24' in main and '"--minutes", type=float, default=45' in main


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


def answering(overrides=None, seen=None):
    """A probe that answers from the shipped walk, or from `overrides`."""
    async def probe(browser, name, code, groups):
        if seen is not None:
            seen.append(code)
        rec = (overrides or {}).get(code)
        if isinstance(rec, Exception):
            raise rec
        return copy.deepcopy(rec or RULES.get(code) or
                             {"service_name": name, "service_code": code, "questions": [],
                              "error": None, "continue_enabled_at_end": True})
    return probe


@needs_db
def test_an_unchanged_form_offers_nothing(conn):
    codes = [c["service_code"] for c in WITH_QUESTIONS[:3]]
    got = W.run(conn, limit=3, minutes=5, codes=codes, catalog=CATALOG, probe=answering(), pause=0)
    assert got["walked"] == 3 and got["changed"] == 0 and got["retired"] == [] and got["stopped"] is None
    assert got["proposal"]["status"] == "current"
    assert conn.execute("SELECT count(*) AS n FROM wizard_walks WHERE ok AND NOT suspect").fetchone()["n"] == 3
    assert S.current(conn)["source"] == "bundled"


@needs_db
def test_a_changed_question_is_recorded_and_offered_not_adopted(conn):
    target = WITH_QUESTIONS[0]
    moved = copy.deepcopy(target)
    moved["questions"][0]["question"] = "A question the City rewrote overnight?"
    got = W.run(conn, limit=1, minutes=5, codes=[target["service_code"]], catalog=CATALOG,
                probe=answering({target["service_code"]: moved}), pause=0)
    assert got["changed"] == 1 and any("question_text" in c for c in got["changes"])
    assert got["proposal"]["status"] == "proposed" and got["proposal"]["new"]
    assert any(line.startswith("question_") for line in got["proposal"]["changes"])
    assert all(target["service_code"] in line for line in got["proposal"]["changes"])
    assert S.current(conn)["source"] == "bundled"                # a person decides
    row = conn.execute("SELECT changes FROM wizard_walks").fetchone()
    assert row["changes"] == got["changes"]
    # the next night finds the same form: the proposal is not "new" again, so no second alert
    again = W.run(conn, limit=1, minutes=5, codes=[target["service_code"]], catalog=CATALOG,
                  probe=answering({target["service_code"]: moved}), pause=0)
    assert again["proposal"]["status"] == "proposed" and not again["proposal"]["new"]
    assert again["changed"] == 0                                 # against the walk now believed


@needs_db
def test_a_walk_that_sees_less_is_believed_only_the_second_time(conn):
    target = WITH_QUESTIONS[0]
    code = target["service_code"]
    empty = dict(copy.deepcopy(target), questions=[], continue_enabled_at_end=False)
    first = W.run(conn, limit=1, minutes=5, codes=[code], catalog=CATALOG,
                  probe=answering({code: empty}), pause=0)
    assert first["suspect"] == 1 and first["changed"] == 0 and first["proposal"]["status"] == "current"
    assert W.pick(CATALOG, W.latest_walks(conn), 1)[0][1] == code           # first in line tomorrow
    assert code not in W.believed_walks(conn)
    second = W.run(conn, limit=1, minutes=5, codes=[code], catalog=CATALOG,
                   probe=answering({code: empty}), pause=0)
    assert second["suspect"] == 0 and second["changed"] == 1               # seen twice: believed
    assert second["proposal"]["status"] == "proposed"
    assert W.believed_walks(conn)[code]["questions"] == []


@needs_db
def test_a_slow_page_once_does_not_reach_the_registry(conn):
    target = WITH_QUESTIONS[0]
    code = target["service_code"]
    empty = dict(copy.deepcopy(target), questions=[], continue_enabled_at_end=False)
    W.run(conn, limit=1, minutes=5, codes=[code], catalog=CATALOG, probe=answering({code: empty}), pause=0)
    back = W.run(conn, limit=1, minutes=5, codes=[code], catalog=CATALOG, probe=answering(), pause=0)
    assert back["suspect"] == 0 and back["changed"] == 0 and back["proposal"]["status"] == "current"


@needs_db
def test_a_retired_request_type_is_removed_without_waiting(conn):
    gone = CATALOG[-1]
    got = W.run(conn, limit=0, minutes=5, catalog=CATALOG[:-1], probe=answering(), pause=0)
    assert got["retired"] == [gone["service_code"]] and got["retired_version"]
    cur = S.current(conn)
    assert cur["source"] == "database" and cur["version"] == got["retired_version"]
    assert gone["service_code"] not in {s["service_code"] for s in cur["registry"]["services"]}
    assert got["proposal"]["status"] == "current"                # nothing else to decide


@needs_db
def test_a_short_catalog_is_not_believed(conn):
    with pytest.raises(RuntimeError, match="not believing it"):
        W.run(conn, limit=1, minutes=5, catalog=CATALOG[:20], probe=answering(), pause=0)
    assert S.current(conn)["source"] == "bundled"
    # and the lock is let go, so the next night can run
    assert conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (W.WALK_LOCK,)).fetchone()["ok"]
    conn.execute("SELECT pg_advisory_unlock(%s)", (W.WALK_LOCK,)); conn.commit()


@needs_db
def test_it_stops_when_the_portal_refuses_and_keeps_what_it_had(conn):
    codes = [c["service_code"] for c in WITH_QUESTIONS[:5]]
    seen = []
    down = {c: TimeoutError("the page did not load") for c in codes[1:]}
    got = W.run(conn, limit=5, minutes=5, codes=codes, catalog=CATALOG,
                probe=answering(down, seen), pause=0)
    assert seen == codes[:3]                                     # one good, two refusals, stop
    assert got["failed"] == 2 and "failed" in got["stopped"]
    assert got["proposal"]["status"] == "current"                # a failed walk changes nothing
    assert set(W.believed_walks(conn)) == {codes[0]}


@needs_db
def test_out_of_time_means_the_rest_wait_for_tomorrow(conn):
    codes = [c["service_code"] for c in WITH_QUESTIONS[:3]]
    seen = []
    got = W.run(conn, limit=3, minutes=0, codes=codes, catalog=CATALOG, probe=answering(seen=seen), pause=0)
    assert seen == [] and got["walked"] == 0 and "out of time" in got["stopped"]


@needs_db
def test_a_filing_goes_first(conn, monkeypatch):
    from alex311 import db, job_runner
    monkeypatch.setattr(W, "QUEUE_WAIT_SECONDS", 0)
    monkeypatch.setattr(job_runner, "kick", lambda job=None: "started test")
    other = db.connect(DB)                                       # a filing worker, mid-filing
    try:
        assert submit_worker.take_the_floor(other)
        seen = []
        got = W.run(conn, limit=1, minutes=5, codes=[WITH_QUESTIONS[0]["service_code"]], catalog=CATALOG,
                    probe=answering(seen=seen), pause=0)
        assert seen == [] and "stood aside" in got["stopped"]
        submit_worker.leave_the_floor(other)
        got = W.run(conn, limit=1, minutes=5, codes=[WITH_QUESTIONS[0]["service_code"]], catalog=CATALOG,
                    probe=answering(seen=seen), pause=0)
        assert len(seen) == 1 and got["stopped"] is None
        # and the walk does not keep the floor between request types
        assert submit_worker.take_the_floor(other)
        submit_worker.leave_the_floor(other)
    finally:
        other.close()


@needs_db
def test_the_worker_waits_out_a_walk_but_not_another_worker(conn):
    from alex311 import db
    walker, worker = db.connect(DB), db.connect(DB)
    try:
        # another worker has the floor, no walk is on: leave at once, as before
        assert submit_worker.take_the_floor(walker)
        assert submit_worker.wait_out_a_walk(worker, seconds=30, every=0.05) is False
        # a walk is on (its lock is held) and has the floor for one request type
        walker.execute("SELECT pg_advisory_lock(%s)", (W.WALK_LOCK,)); walker.commit()

        async def scene():
            waiting = asyncio.to_thread(submit_worker.wait_out_a_walk, worker, seconds=10, every=0.05)
            task = asyncio.ensure_future(waiting)
            await asyncio.sleep(0.3)
            assert not task.done()                               # still waiting for the walk
            submit_worker.leave_the_floor(walker)                # the walk finishes that request type
            return await task
        assert asyncio.run(scene()) is True                      # and the worker has the floor
        submit_worker.leave_the_floor(worker)
    finally:
        walker.close(); worker.close()
