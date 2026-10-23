"""Issue threads: which requests are about the same thing.

The model is small and pinned here signal by signal: nearer is more, the same
type is more, similar wording is more, a missing signal is left out rather
than counted against, the City's own duplicate link wins, and votes move a
score by a little each and by at most so much.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import threads as T

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def req(id="26-1", lat=38.805, long=-77.047, name="Pothole", code="TESPOTH", desc=None, when=NOW,
        dept="Transportation & Environmental Services", dup=None, parent=None):
    return {"service_request_id": id, "lat": lat, "long": long, "service_name": name, "service_code": code,
            "description": desc, "requested_datetime": when, "primary_service_department": dept,
            "duplicate_parent_service_request_id": dup, "parent_service_request_id": parent,
            "status": "Open", "address": "1 TEST ST", "closed_datetime": None, "media_count": 0}


def test_nearer_is_more_and_a_block_away_is_little():
    assert T.proximity(0) == 1.0
    assert 0.45 < T.proximity(35) < 0.55                       # half a block away is half
    assert T.proximity(100) < 0.15 and T.proximity(250) < 0.01
    assert T.proximity(None) is None


def test_far_apart_means_not_the_same_thing_unless_the_words_say_so():
    """Same type and the same week is not enough from 150 m away — a
    neighbourhood gets several potholes — but the same type, the same week
    and the same story is."""
    a = req(desc="Deep pothole by the bus stop, cars swerving into the bike lane")
    far = req(id="26-2", lat=38.805 + 150 / 111_000, desc="Loose dog in the park barking at kids")
    text = T.TextModel({"26-1": a["description"], "26-2": far["description"], "26-3": a["description"]})
    s, why = T.score_pair(a, far, text)
    assert s == 0.0 and why["too_far"] and 140 < why["distance_m"] < 160
    alike = req(id="26-3", lat=38.805 + 150 / 111_000, desc=a["description"])
    s, why = T.score_pair(a, alike, text)
    assert s > T.THRESHOLD and "too_far" not in why
    near = req(id="26-2", lat=38.805 + 40 / 111_000, desc=None)
    s, why = T.score_pair(a, near, T.TextModel({"26-1": a["description"], "26-2": None}))
    assert s > T.THRESHOLD and "too_far" not in why           # next door needs no words
    d = T.distance_m(38.805, -77.047, 38.806, -77.047)
    assert 105 < d < 118                                       # a thousandth of a degree of latitude


def test_the_same_type_is_more_than_the_same_department_is_more_than_nothing():
    a = req()
    assert T.category(a, req(id="26-2")) == 1.0
    assert T.category(a, req(id="26-2", name="Sidewalk Repair", code="TESSIDE")) == 0.4
    assert T.category(a, req(id="26-2", name="Rodent", code="CODRODEN", dept="Code")) == 0.0


def test_wording_is_compared_by_what_it_says_not_how_long_it_is():
    m = T.TextModel({"a": "Large pothole in the right lane near the bus stop, cars swerving",
                     "b": "Pothole right lane by the bus stop. Cars are swerving around it.",
                     "c": "Dead tree leaning over the sidewalk, branches falling",
                     "d": None})
    assert m.similarity("a", "b") > 0.6 > m.similarity("a", "c")
    assert m.similarity("a", "d") is None                       # no description: no opinion
    assert "pothole" in m.shared("a", "b") and "swerv" in m.shared("a", "b")
    assert T.tokens("The trees' branches were falling, please fix!") == ["tree", "branch", "fall", "fix"]


def test_a_missing_signal_is_left_out_not_counted_against():
    assert T.combine({"proximity": 1.0, "category": 1.0, "text": None, "time": 1.0}) == 1.0
    assert T.combine({"proximity": None, "category": None, "text": None, "time": None}) == 0.0
    both = T.combine({"proximity": 1.0, "category": 0.0, "text": 0.0, "time": 0.0})
    assert abs(both - T.WEIGHTS["proximity"]) < 1e-9
    assert abs(sum(T.WEIGHTS.values()) - 1.0) < 1e-9


def test_the_citys_own_duplicate_link_wins():
    a, b = req(), req(id="26-2", lat=38.9, long=-77.2, name="Rodent", code="CODRODEN", dept="Code",
                      when=NOW - timedelta(days=200), dup="26-1")
    assert T.city_linked(a, b) and T.city_linked(b, a)
    score, why = T.score_pair(a, b, T.TextModel({"26-1": None, "26-2": None}))
    assert score >= 0.95 and why["city_linked"]
    assert not T.city_linked(a, req(id="26-3"))


def test_votes_move_a_little_each_and_only_so_far():
    assert T.vote_shift(0, 0) == 0
    assert T.vote_shift(1, 0) == T.VOTE_STEP and T.vote_shift(0, 1) == -T.VOTE_STEP
    assert T.vote_shift(10, 0) == T.VOTE_CAP and T.vote_shift(0, 10) == -T.VOTE_CAP
    assert T.vote_shift(3, 3) == 0


def test_the_pair_is_stored_in_one_order():
    assert T.pair("26-2", "26-1") == T.pair("26-1", "26-2") == ("26-1", "26-2")


def test_the_page_and_the_bar():
    page = (ROOT / "dashboard/static/threads.html").read_text()
    assert 'href="/threads" aria-current="page"' in page
    assert "/api/threads/random" in page and "/submit/api/threads/" in page
    assert 'data-vote="true"' in page and 'data-vote="false"' in page           # the feedback
    assert "Sign in</a> to say whether this is related" in page
    app = (ROOT / "dashboard/app.py").read_text()
    assert '@app.get("/threads"' in app and '@app.get("/api/threads/random")' in app
    routes = (ROOT / "dashboard/submit.py").read_text()
    vote = routes.split('@router.put("/api/threads/{case}/{other}")')[1].split("@router")[0]
    assert "Depends(gate)" in vote and "_account(request)" in vote                 # a vote needs an account


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")
IDS = [f"99-0000000{i}" for i in range(1, 7)]


@pytest.fixture()
def conn():
    from alex311 import db
    c = db.connect(DB)
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM thread_feedback WHERE case_a = ANY(%s) OR case_b = ANY(%s)", (IDS, IDS))
    c.execute("DELETE FROM service_requests WHERE service_request_id = ANY(%s)", (IDS,))
    c.execute("DELETE FROM portal_users WHERE email LIKE 'threads-%%@test.io'")
    rows = [  # a pothole, reported three times at one corner, a sidewalk next door, and a rodent across town
        (IDS[0], "Pothole", "TESPOTH", 38.8050, -77.0470, "Big pothole in the right lane by the bus stop", NOW),
        (IDS[1], "Pothole", "TESPOTH", 38.8051, -77.0471, "Pothole right lane near bus stop, cars swerving", NOW - timedelta(days=3)),
        (IDS[2], "Pothole", "TESPOTH", 38.8049, -77.0469, "Deep hole in the road at the corner", NOW - timedelta(days=40)),
        (IDS[3], "Sidewalk Repair", "TESSIDE", 38.8052, -77.0469, "Sidewalk slab lifted by a tree root, trip hazard", NOW - timedelta(days=10)),
        (IDS[4], "Rodent", "CODRODEN", 38.8300, -77.0900, "Rats in the alley behind the restaurant", NOW),
        (IDS[5], "Pothole", "TESPOTH", 38.8050, -77.0470, "Pothole at the bus stop again", NOW - timedelta(days=500)),
    ]
    for r in rows:
        c.execute("""INSERT INTO service_requests (service_request_id, service_name, service_code, lat, long, description,
                     requested_datetime, status, primary_service_department)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, 'Open', 'Transportation & Environmental Services')""", r)
    c.commit()
    yield c
    c.rollback()
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM thread_feedback WHERE case_a = ANY(%s) OR case_b = ANY(%s)", (IDS, IDS))
    c.execute("DELETE FROM service_requests WHERE service_request_id = ANY(%s)", (IDS,))
    c.execute("DELETE FROM portal_users WHERE email LIKE 'threads-%%@test.io'")
    c.commit(); c.close()


@needs_db
def test_a_thread_finds_the_corner_and_leaves_the_rest(conn):
    t = T.thread(conn, IDS[0])
    got = [r["service_request_id"] for r in t["related"]]
    assert got[0] == IDS[1] and IDS[2] in got                 # the two other reports of the hole, best first
    # (real requests from the mirror may sit between them; the corner is a real one)
    assert IDS[4] not in got                                   # across town
    assert IDS[5] not in got                                   # outside the time window
    assert IDS[3] in got                                       # next door, same department: offered, lower
    assert t["related"][0]["why"]["same_type"] and t["related"][0]["why"]["distance_m"] < 20
    assert "pothole" in t["related"][0]["why"]["shared_words"]
    assert any(e["a"] == IDS[1] and e["b"] == IDS[2] or e["a"] == IDS[2] and e["b"] == IDS[1] for e in t["edges"])
    assert T.thread(conn, "no-such-case") is None


@needs_db
def test_votes_change_the_score_and_the_persons_own_is_remembered(conn):
    from alex311 import portal_auth as pa
    users = []
    for n in range(4):
        u = pa.create_user(conn, f"threads-{n}@test.io")
        users.append(u["user_id"] if isinstance(u, dict) else u.user_id)
    before = {r["service_request_id"]: r["score"] for r in T.thread(conn, IDS[0])["related"]}
    for u in users[:3]:
        T.vote(conn, user_id=u, case_a=IDS[0], case_b=IDS[3], related=False)
    T.vote(conn, user_id=users[3], case_a=IDS[3], case_b=IDS[0], related=True)   # the other way round: same pair
    t = T.thread(conn, IDS[0], user_id=users[3])
    after = {r["service_request_id"]: r for r in t["related"]}
    if IDS[3] in after:
        assert after[IDS[3]]["score"] < before[IDS[3]]
        assert after[IDS[3]]["votes"] == {"related": 1, "not_related": 3}
        assert after[IDS[3]]["my_vote"] is True
    else:
        assert before[IDS[3]] - 2 * T.VOTE_STEP < T.THRESHOLD                  # voted below the line
    T.vote(conn, user_id=users[3], case_a=IDS[0], case_b=IDS[3], related=None)   # withdrawn
    assert T.votes(conn, IDS[0], [IDS[3]])[IDS[3]] == {"related": 0, "not_related": 3}
    with pytest.raises(ValueError):
        T.vote(conn, user_id=users[0], case_a=IDS[0], case_b=IDS[0], related=True)


@needs_db
def test_a_random_start_is_a_real_request(conn):
    case = T.random_case(conn, tries=2)
    assert case is None or T.get(conn, case) is not None
