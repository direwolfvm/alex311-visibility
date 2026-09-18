"""Refresh status: a few cases from the City, on demand, politely.

The mirror reads the City four times a day. A person looking at their list
wants to know now, so the button asks the City about their cases, one at a
time with a pause, capped per press and cooled down per account, and folds
the answers into the mirror the same way the ingest job would.
"""
from pathlib import Path

import pytest

from alex311 import refresh
from alex311.client import Alex311Error

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
MY = (ROOT / "dashboard/my.html").read_text()


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get_detail(self, srid):
        self.calls.append(srid)
        a = self.answers[srid]
        if isinstance(a, Exception):
            raise a
        return a


class Conn:
    """Enough of a connection for the db helpers to run against."""
    def __init__(self):
        self.sql = []
        self.committed = 0
    def cursor(self):
        return self
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, q, params=None):
        self.sql.append(q)
        return self
    def commit(self):
        self.committed += 1


def detail(srid, status="Open"):
    return {"service_request_id": srid, "status": status, "service_name": "Noise Issues",
            "requested_datetime": "2026-09-16T10:00:00Z", "address": "1 W OAK ST",
            "description": "loud", "media_url": [{"id": "m1", "fileName": "a.jpg"}]}


def test_each_case_is_asked_for_in_turn_and_folded_into_the_mirror(monkeypatch):
    conn = Conn()
    client = FakeClient({"26-1": detail("26-1", "Closed"),
                         "26-2": Alex311Error("empty detail for 26-2"),
                         "26-3": Alex311Error("HTTP 500"),
                         "26-4": Alex311Error("Invalid Service Request number 26-4.")})
    out = refresh.refresh_cases(conn, client, ["26-1", "26-2", "26-3", "26-4"], pause=0)
    assert client.calls == ["26-1", "26-2", "26-3", "26-4"]
    # what the City actually says for a number it does not know (seen live)
    assert out[3] == {"service_request_id": "26-4", "found": False, "status": None, "error": None}
    assert out[0] == {"service_request_id": "26-1", "found": True, "status": "Closed", "error": None}
    assert out[1] == {"service_request_id": "26-2", "found": False, "status": None, "error": None}
    assert out[2]["found"] is False and out[2]["error"] == "HTTP 500"
    # the found case went through the same three writes the ingest job does
    joined = " ".join(conn.sql)
    assert "INSERT INTO service_requests" in joined
    assert "UPDATE service_requests SET" in joined and "raw_detail" in joined
    assert "media" in joined.lower()


def test_a_press_is_capped_and_paced():
    conn = Conn()
    ids = [f"26-{i}" for i in range(15)]
    client = FakeClient({i: detail(i) for i in ids})
    slept = []
    import alex311.refresh as r
    r.time.sleep = lambda s: slept.append(s)
    out = r.refresh_cases(conn, client, ids, pause=0.5)
    assert len(out) == refresh.MAX_PER_PRESS == 10
    assert slept == [0.5] * 9                       # a pause between calls, none before the first


def test_the_endpoint_is_gated_cooled_down_and_uses_the_account_list():
    assert '@router.post("/api/my/refresh")' in ROUTES
    fn = ROUTES.split("def refresh_my_requests(")[1].split("\n    @router")[0]
    assert "user_id = _account(request)" in fn
    assert '_allow(f"refresh:{user_id}", 1, REFRESH_COOLDOWN)' in fn
    assert "raise HTTPException(429" in fn
    assert "adb.my_links(conn, user_id=user_id)" in fn
    assert "conn.rollback()" in fn                  # the RLS setting must not leak into the writes
    assert "with Alex311Client() as client:" in fn
    assert "refresh.refresh_cases(conn, client, cases)" in fn


def test_the_button_says_what_happened_and_reloads_the_list():
    assert 'id="refresh"' in MY and "Refresh status" in MY
    assert "api('/my/refresh', {method: 'POST'})" in MY
    for phrase in ("updated", "not at the City yet", "could not be checked", "await load()"):
        assert phrase in MY
