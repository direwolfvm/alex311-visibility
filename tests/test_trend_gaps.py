"""A week with nothing in it is still a week.

The weekly charts on Explore used to list only the periods that had rows, so
a quiet week vanished and the bars on either side of it sat together as if
nothing had happened. Both weekly series now carry every period between
their first and last.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")
IDS = ["98-00000001", "98-00000002", "98-00000003"]
T0 = datetime(2031, 1, 6, 12, tzinfo=timezone.utc)      # a Monday, far from any real data


@pytest.fixture()
def rows():
    from alex311 import db
    c = db.connect(DB)
    c.execute("DELETE FROM service_requests WHERE service_request_id = ANY(%s)", (IDS,))
    # week 1, nothing in week 2, week 3 (closed in week 5)
    for id_, when, closed in ((IDS[0], T0, None), (IDS[1], T0 + timedelta(days=14), T0 + timedelta(days=30)),
                              (IDS[2], T0 + timedelta(days=15), None)):
        c.execute("INSERT INTO service_requests (service_request_id, service_name, requested_datetime, closed_datetime, status)"
                  " VALUES (%s, 'Pothole', %s, %s, 'Open')", (id_, when, closed))
    c.commit()
    yield
    c.execute("DELETE FROM service_requests WHERE service_request_id = ANY(%s)", (IDS,)); c.commit(); c.close()


@pytest.fixture()
def client():
    from dashboard.app import app
    with TestClient(app) as c:
        yield c


@needs_db
def test_the_stacked_weekly_bars_keep_their_gaps(client, rows):
    start, end = T0 - timedelta(days=1), T0 + timedelta(days=21)
    d = client.get("/api/trend", params={"interval": "week", "start": start.isoformat(), "end": end.isoformat()}).json()
    periods = d["periods"]
    assert len(periods) == 3                                          # weeks 1, 2 and 3 — the empty one included
    with_rows = {r["period"] for r in d["rows"]}
    assert periods[1] not in with_rows and periods[0] in with_rows and periods[2] in with_rows
    assert all((datetime.fromisoformat(b) - datetime.fromisoformat(a)).days == 7 for a, b in zip(periods, periods[1:]))


@needs_db
def test_opened_versus_closed_keeps_its_gaps(client, rows):
    start, end = T0 - timedelta(days=1), T0 + timedelta(days=21)
    d = client.get("/api/analytics", params={"start": start.isoformat(), "end": end.isoformat()}).json()
    weeks = d["weekly"]
    assert len(weeks) == 5                                            # through the week of the closure
    assert [(+w["opened"], +w["closed"]) for w in weeks] == [(1, 0), (0, 0), (2, 0), (0, 0), (0, 1)]
