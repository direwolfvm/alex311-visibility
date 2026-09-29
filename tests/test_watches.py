"""Watches: follow a request type, an address or several, or a drawn area.

The pure parts always run. The matching runs against the local database:
seeded requests at known addresses, types and coordinates, and watches that
should and should not catch them — including the row-level security that
keeps one person's watches (an address is often a home) from another.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import watches as W

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
MY = (ROOT / "dashboard/my.html").read_text()
EXPLORE = (ROOT / "dashboard/static/explore.html").read_text()
RECORD = (ROOT / "dashboard/static/request.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


# ------------------------------------------------------------- always run

def test_the_table_is_personal_and_goes_with_the_account():
    assert "CREATE TABLE IF NOT EXISTS watches" in SCHEMA
    assert "REFERENCES portal_users (user_id) ON DELETE CASCADE" in SCHEMA.split("CREATE TABLE IF NOT EXISTS watches")[1].split(";")[0]
    assert "ALTER TABLE watches FORCE ROW LEVEL SECURITY" in SCHEMA
    assert "CREATE POLICY own   ON watches USING (user_id = current_setting('app.user_id', true))" in SCHEMA
    assert "feed_seen_at TIMESTAMPTZ" in SCHEMA


def test_the_routes_are_gated_and_keyed_on_the_account():
    for route in ('@router.get("/api/watches")', '@router.post("/api/watches")',
                  '@router.delete("/api/watches/{watch_id}")', '@router.get("/api/feed")',
                  '@router.post("/api/feed/seen")'):
        assert route in ROUTES
    for fn in ("watches_list", "watches_add", "watches_remove", "feed_get", "feed_seen"):
        body = ROUTES.split(f"def {fn}(")[1].split("\n    @router")[0]
        assert "user_id = _account(request)" in body, fn
    assert "raise HTTPException(400, str(e))" in ROUTES.split("def watches_add(")[1].split("\n    @router")[0]


def test_the_pages_offer_the_three_kinds():
    assert 'id="feed-card"' in MY and 'id="watch-card"' in MY
    for kind in ("category", "address", "area"):
        assert f'data-kind="{kind}"' in MY
    assert "api('/watches', {method: 'POST'" in MY and "api('/feed')" in MY
    assert 'id="watch-area"' in EXPLORE and "kind: 'area', spec: {polygon: state.polygon}" in EXPLORE
    assert 'data-watch="address"' in RECORD and 'data-watch="category"' in RECORD
    # the feed marks seen only when the person says so
    assert "api('/feed/seen', {method: 'POST'})" in MY


def test_polygon_literals_and_clauses():
    assert W.polygon_literal([[-77.05, 38.8], [-77.04, 38.8], [-77.04, 38.81]]) == \
        "((-77.05,38.8),(-77.04,38.8),(-77.04,38.81))"
    c, p = W._clause({"kind": "category", "spec": {"service_name": "Noise Issues"}})
    assert c == "r.service_name = %s" and p == ["Noise Issues"]
    c, p = W._clause({"kind": "address", "spec": {"addresses": [
        {"text": "500 N PITT ST", "prefixes": ["500 N PITT", "500 NORTH PITT"], "lat": 38.81, "long": -77.04}],
        "nearby": True}})
    assert "LIKE ANY(%s)" in c and "BETWEEN" in c
    assert p[0] == ["500 N PITT %", "500 NORTH PITT %", "500 N PITT", "500 NORTH PITT"]
    c, p = W._clause({"kind": "area", "spec": {"polygon": [[-77.05, 38.8], [-77.04, 38.8], [-77.04, 38.81]]}})
    assert "<@ %s::polygon" in c


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@pytest.fixture()
def seeded():
    from alex311 import db, portal_auth as pa
    owner = db.connect()
    owner.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'W-%'")
    owner.execute("DELETE FROM portal_users WHERE email LIKE 'w-%@test'")
    owner.commit()
    a = pa.create_user(owner, "w-a@test", "user", created_by="t")
    b = pa.create_user(owner, "w-b@test", "user", created_by="t")
    old = datetime.now(timezone.utc) - timedelta(days=3)
    rows = [
        # id, type, address, lat, long
        ("W-1", "Noise Issues", "500 N PITT ST", 38.8100, -77.0430),
        ("W-2", "Noise Issues", "502 N PITT ST", 38.8101, -77.0431),        # next door
        ("W-3", "Pothole", "500 N PITTMAN ST", 38.8300, -77.0700),         # a longer street name, elsewhere
        ("W-4", "Pothole", "9 KING ST", 38.8045, -77.0410),                # inside the area below
        ("W-5", "Tree Inspection Request", "1 FAR AWAY RD", 38.9000, -77.2000),
    ]
    for srid, svc, addr, lat, lng in rows:
        owner.execute(
            """INSERT INTO service_requests (service_request_id, service_name, address, lat, long,
                                             status, requested_datetime, first_seen_at, last_updated_datetime)
               VALUES (%s, %s, %s, %s, %s, 'open', now(), now(), now())""",
            (srid, svc, addr, lat, lng))
    # one seen long ago and never changed, so a 'since' filter can exclude it
    owner.execute("UPDATE service_requests SET first_seen_at = %s, last_updated_datetime = %s "
                  "WHERE service_request_id = 'W-5'", (old, old))
    owner.commit()
    app = db.connect(os.environ.get("APP_DATABASE_URL") or DB)
    yield owner, app, a, b
    app.close()
    owner.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'W-%'")
    owner.execute("DELETE FROM portal_users WHERE email LIKE 'w-%@test'")
    owner.commit(); owner.close()


@needs_db
def test_each_kind_matches_what_it_should(seeded):
    owner, app, a, b = seeded
    ids = lambda f: sorted(r["service_request_id"] for r in f["items"] if r["service_request_id"].startswith("W-"))

    W.add_watch(app, user_id=a.user_id, kind="category", payload={"service_name": "Noise Issues"})
    assert ids(W.feed(app, user_id=a.user_id, since=None)) == ["W-1", "W-2"]
    for w in W.list_watches(app, user_id=a.user_id):
        W.remove_watch(app, user_id=a.user_id, watch_id=w["watch_id"])

    # an address, typed the way a person would: matched to the City's spelling
    w = W.add_watch(app, user_id=a.user_id, kind="address", payload={"addresses": ["500 north pitt street"]})
    assert w["spec"]["addresses"][0]["text"] == "500 N PITT ST" and w["spec"]["addresses"][0]["known"]
    assert ids(W.feed(app, user_id=a.user_id, since=None)) == ["W-1"]          # not 502, not PITTMAN
    W.remove_watch(app, user_id=a.user_id, watch_id=w["watch_id"])

    # ... and nearby widens it to next door, still not to PITTMAN across town
    w = W.add_watch(app, user_id=a.user_id, kind="address", payload={"addresses": ["500 N Pitt St"], "nearby": True})
    assert w["label"].endswith("(and nearby)")
    assert ids(W.feed(app, user_id=a.user_id, since=None)) == ["W-1", "W-2"]
    W.remove_watch(app, user_id=a.user_id, watch_id=w["watch_id"])

    # an area around King St catches W-4 only
    w = W.add_watch(app, user_id=a.user_id, kind="area", payload={"polygon": [
        [-77.0420, 38.8040], [-77.0400, 38.8040], [-77.0400, 38.8050], [-77.0420, 38.8050]]})
    import re
    assert re.fullmatch(r"Drawn area \(\d+ requests? on record\)", w["label"])   # real rows may sit inside too
    assert ids(W.feed(app, user_id=a.user_id, since=None)) == ["W-4"]

    # several watches: every item says which ones it matched; the feed is one list
    W.add_watch(app, user_id=a.user_id, kind="category", payload={"service_name": "Pothole"})
    f = W.feed(app, user_id=a.user_id, since=None)
    assert ids(f) == ["W-3", "W-4"]
    four = next(r for r in f["items"] if r["service_request_id"] == "W-4")
    assert len(four["watch_ids"]) == 2 and four["is_new"]


@needs_db
def test_since_hides_the_old_and_unchanged_and_seen_moves_it(seeded):
    owner, app, a, b = seeded
    W.add_watch(app, user_id=a.user_id, kind="category", payload={"service_name": "Tree Inspection Request"})
    assert [r["service_request_id"] for r in W.feed(app, user_id=a.user_id, since=None)["items"]
            if r["service_request_id"] == "W-5"] == ["W-5"]
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    assert not [r for r in W.feed(app, user_id=a.user_id, since=yesterday)["items"]
                if r["service_request_id"] == "W-5"]
    assert W.seen_at(app, user_id=a.user_id) is None
    at = W.mark_seen(app, user_id=a.user_id)
    assert W.seen_at(app, user_id=a.user_id) == at


@needs_db
def test_watches_are_private_and_limited(seeded):
    owner, app, a, b = seeded
    W.add_watch(app, user_id=a.user_id, kind="address", payload={"addresses": ["500 N Pitt St"]})
    assert W.list_watches(app, user_id=b.user_id) == []
    # b cannot remove a's watch, even knowing its id
    wid = W.list_watches(app, user_id=a.user_id)[0]["watch_id"]
    assert W.remove_watch(app, user_id=b.user_id, watch_id=wid) is False
    assert len(W.list_watches(app, user_id=a.user_id)) == 1
    # the same watch twice is one watch
    again = W.add_watch(app, user_id=a.user_id, kind="address", payload={"addresses": ["500 north pitt st"]})
    assert again["existing"] and again["watch_id"] == wid
    # and what it refuses says why
    with pytest.raises(W.BadWatch):
        W.add_watch(app, user_id=a.user_id, kind="category", payload={"service_name": "Not A Type"})
    with pytest.raises(W.BadWatch):
        W.add_watch(app, user_id=a.user_id, kind="address", payload={"addresses": ["Pitt Street"]})
    with pytest.raises(W.BadWatch):
        W.add_watch(app, user_id=a.user_id, kind="area", payload={"polygon": [[0, 0], [1, 1], [1, 0]]})
    with pytest.raises(W.BadWatch):
        W.add_watch(app, user_id=a.user_id, kind="area", payload={"polygon": [[-77.04, 38.8], [-77.04, 38.81]]})
    # deleting the account takes the watches with it
    from alex311 import db
    db.delete_account(owner, user_id=a.user_id)
    owner.execute("SELECT set_config('app.role', 'admin', false)")
    assert owner.execute("SELECT count(*) AS n FROM watches WHERE user_id = %s",
                         (a.user_id,)).fetchone()["n"] == 0
    owner.rollback()
