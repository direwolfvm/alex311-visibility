"""The fast refresh: every fifteen minutes, the cases people are waiting on.

The ingest reads everything four times a day. This asks the City only about
linked cases, at a rate that matches how likely each is to have changed —
politely: one at a time, capped, and stopping at the first error.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import refresh as R
from alex311.client import Alex311Error

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "deploy/README.md").read_text()


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = answers, []
    def get_detail(self, srid):
        self.calls.append(srid)
        a = self.answers.get(srid, Alex311Error(f"Invalid Service Request number {srid}."))
        if isinstance(a, Exception):
            raise a
        return a


def detail(srid, status="Open"):
    return {"service_request_id": srid, "status": status, "service_name": "Noise Issues",
            "requested_datetime": datetime.now(timezone.utc).isoformat(), "address": "1 W OAK ST",
            "description": "x", "media_url": []}


def test_the_scheduled_pass_stops_at_the_first_real_error():
    class Conn:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): return self
        def commit(self): pass
    client = FakeClient({"A": detail("A"), "B": Alex311Error("HTTP 503"), "C": detail("C")})
    out = R.refresh_cases(Conn(), client, ["A", "B", "C"], pause=0, limit=40, stop_on_error=True)
    assert client.calls == ["A", "B"] and len(out) == 2            # C is not asked about
    # "the City does not know it yet" is an answer, not an error: the pass goes on
    client = FakeClient({"A": Alex311Error("Invalid Service Request number A."), "C": detail("C")})
    out = R.refresh_cases(Conn(), client, ["A", "C"], pause=0, limit=40, stop_on_error=True)
    assert client.calls == ["A", "C"]
    # the button on My requests keeps its old behavior: every case tried
    client = FakeClient({"A": detail("A"), "B": Alex311Error("HTTP 503"), "C": detail("C")})
    R.refresh_cases(Conn(), client, ["A", "B", "C"], pause=0)
    assert client.calls == ["A", "B", "C"]


def test_the_runbook_and_the_limits_agree():
    assert R.MAX_PER_RUN == 40 and R.FRESH_HOURS == 48 and R.RECENT_DAYS == 14
    assert '--schedule="*/15 * * * *"' in README and '--args="-m,alex311.refresh"' in README
    block = README.split("## 5d. Fast refresh job")[1].split("## 6.")[0]
    assert "--max-retries=0" in block and "stops at\nthe first error" in block


DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@pytest.fixture()
def world():
    from alex311 import db, portal_auth as pa
    conn = db.connect()
    conn.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'FR-%'")
    conn.execute("DELETE FROM portal_users WHERE email LIKE 'fr-%@test.io'"); conn.commit()
    u = pa.create_user(conn, "fr-a@test.io", "user", created_by="t")
    now = datetime.now(timezone.utc)
    def case(srid, status, age_days, checked_min_ago, closed=False):
        conn.execute(
            """INSERT INTO service_requests (service_request_id, service_name, status, requested_datetime,
                                             last_ingested_at, closed_datetime)
               VALUES (%s, 'Noise Issues', %s, %s, %s, %s)""",
            (srid, status, now - timedelta(days=age_days), now - timedelta(minutes=checked_min_ago),
             now if closed else None))
    case("FR-FRESH", "Open", 0.5, 20)          # mine, filed today, last asked 20 min ago  -> due (every run)
    case("FR-FRESH-JUST", "Open", 0.5, 5)      # mine, filed today, asked 5 min ago        -> not yet
    case("FR-RECENT", "Open", 5, 70)           # followed, 5 days old, asked 70 min ago    -> due (hourly)
    case("FR-RECENT-JUST", "Open", 5, 30)      # followed, asked 30 min ago                -> not yet
    case("FR-OLD", "Open", 40, 600)            # open but 40 days old                      -> the ingest's job
    case("FR-CLOSED", "Closed", 1, 600, closed=True)                                       # never
    conn.commit()
    for srid, rel in (("FR-FRESH", "mine"), ("FR-FRESH-JUST", "mine"), ("FR-RECENT", "following"),
                      ("FR-RECENT-JUST", "following"), ("FR-OLD", "mine"), ("FR-CLOSED", "mine"),
                      ("FR-UNLISTED", "mine")):       # sent, and the City has not listed it yet
        db.link_request(conn, user_id=u.user_id, service_request_id=srid, relation=rel)
    conn.execute("SELECT set_config('app.role', 'admin', false)")
    conn.execute("UPDATE request_links SET created_at = now() - interval '40 days' WHERE service_request_id = 'FR-OLD'")
    conn.commit()
    yield conn, u
    conn.rollback()
    conn.execute("SELECT set_config('app.role', '', false)")
    conn.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'FR-%'")
    conn.execute("DELETE FROM portal_users WHERE email LIKE 'fr-%@test.io'"); conn.commit(); conn.close()


@needs_db
def test_each_case_is_due_at_its_own_rate(world):
    conn, u = world
    conn.execute("SELECT set_config('app.role', 'admin', false)")
    due = {d["service_request_id"]: d["tier"] for d in R.due_cases(conn)
           if d["service_request_id"].startswith("FR-")}
    assert due == {"FR-FRESH": "fresh", "FR-UNLISTED": "fresh", "FR-RECENT": "recent"}
    order = [d["service_request_id"] for d in R.due_cases(conn) if d["service_request_id"].startswith("FR-")]
    assert order.index("FR-RECENT") == 2                 # the fresh ones come first


@needs_db
def test_a_pass_asks_marks_and_then_tells(world):
    conn, u = world
    client = FakeClient({"FR-FRESH": detail("FR-FRESH", "Closed"), "FR-RECENT": detail("FR-RECENT")})
    told = []
    counts = R.run(conn, client, notify=lambda c, site_origin: told.append(site_origin) or {"sent": 0})
    assert sorted(x for x in client.calls if x.startswith("FR-")) == ["FR-FRESH", "FR-RECENT", "FR-UNLISTED"]
    assert counts["found"] >= 2 and counts["not_yet"] >= 1 and counts["errors"] == 0
    assert told == ["https://alex311-reborn.com"]      # the push pass runs after every refresh
    conn.execute("SELECT set_config('app.role', '', false)")
    assert conn.execute("SELECT status FROM service_requests WHERE service_request_id = 'FR-FRESH'").fetchone()["status"] == "Closed"
    # asked a moment ago, so nothing is due again — including the one the City does not list yet
    conn.execute("SELECT set_config('app.role', 'admin', false)")
    assert not [d for d in R.due_cases(conn) if d["service_request_id"].startswith("FR-")]
    again = FakeClient({})
    R.run(conn, again, notify=lambda c, site_origin: {})
    assert not [x for x in again.calls if x.startswith("FR-")]
