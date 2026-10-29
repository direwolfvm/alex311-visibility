"""One request, refreshed from its own page.

The record page says when this mirror last heard from the City about the
request, and a signed-in person can ask the City again. Pinned: the time
shown is the later of the list pass and the detail read; the City is asked
once and not again within two minutes however many people press; a person
gets a handful of presses in ten minutes; a City that does not answer is
reported, not hidden; signed-out visitors see the time and a sign-in link.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.py").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
PAGE = (ROOT / "dashboard/static/request.html").read_text()


def test_the_page_shows_the_time_and_offers_the_button():
    assert "GREATEST(last_ingested_at, enriched_at) AS mirrored_at" in APP
    assert "['In this mirror'," in PAGE and "function ago(v)" in PAGE
    assert ">Refresh from the City</button>" in PAGE
    assert 'href="/submit/login">Sign in</a> to refresh it from the City' in PAGE
    assert "fetch(`/submit/api/refresh/${encodeURIComponent(CASE)}`, {method: 'POST'})" in PAGE
    fn = ROUTES.split('@router.post("/api/refresh/{case}")')[1].split("\n    @router")[0]
    assert "Depends(gate)" in fn and "_account(request)" in fn          # signed in, with an account


DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")
CASE, TOKEN = "96-00000001", "record-refresh-test-session"


@pytest.fixture()
def signed_in(monkeypatch):
    from alex311 import db, portal_auth as pa
    c = db.connect(DB)
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM portal_users WHERE email = 'record-refresh@test.io'")
    c.execute("DELETE FROM service_requests WHERE service_request_id = %s", (CASE,))
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    c.execute("INSERT INTO service_requests (service_request_id, service_name, status, last_ingested_at, enriched_at)"
              " VALUES (%s, 'Pothole', 'Open', %s, %s)", (CASE, old, old - timedelta(hours=1)))
    u = pa.create_user(c, "record-refresh@test.io")
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("INSERT INTO portal_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + interval '1 hour')",
              (pa._token_hash(TOKEN), u.user_id))
    c.commit()
    from dashboard.app import app
    with TestClient(app) as client:
        client.cookies.set(pa.SESSION_COOKIE, TOKEN)
        yield client, c
    c.rollback()
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM portal_users WHERE email = 'record-refresh@test.io'")
    c.execute("DELETE FROM service_requests WHERE service_request_id = %s", (CASE,))
    c.commit(); c.close()


class City:
    """Stands in for the City's portal: counts the questions, answers Closed."""
    asked = 0

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


def fake_refresh(status="Closed", error=None):
    def refresh_cases(conn, client, ids, **kw):
        City.asked += 1
        if error:
            return [{"service_request_id": ids[0], "found": False, "status": None, "error": error}]
        conn.execute("UPDATE service_requests SET status = %s, enriched_at = now() WHERE service_request_id = %s",
                     (status, ids[0]))
        conn.commit()
        return [{"service_request_id": ids[0], "found": True, "status": status, "error": None}]
    return refresh_cases


@needs_db
def test_the_record_says_when_the_mirror_last_heard(signed_in):
    client, _ = signed_in
    d = client.get(f"/api/request/{CASE}").json()
    mirrored = datetime.fromisoformat(d["mirrored_at"])
    assert timedelta(hours=4, minutes=59) < datetime.now(timezone.utc) - mirrored < timedelta(hours=5, minutes=1)


@needs_db
def test_asking_once_then_answering_from_the_mirror(signed_in, monkeypatch):
    client, _ = signed_in
    from dashboard import submit as S
    monkeypatch.setattr(S, "Alex311Client", City)
    monkeypatch.setattr(S.refresh, "refresh_cases", fake_refresh("Closed"))
    City.asked = 0
    r = client.post(f"/submit/api/refresh/{CASE}")
    assert r.status_code == 200 and r.json()["asked_city"] and r.json()["status"] == "Closed"
    assert City.asked == 1
    again = client.post(f"/submit/api/refresh/{CASE}").json()                 # moments later
    assert again["asked_city"] is False and City.asked == 1                   # the City is not asked twice
    d = client.get(f"/api/request/{CASE}").json()
    assert d["status"] == "Closed"
    assert datetime.now(timezone.utc) - datetime.fromisoformat(d["mirrored_at"]) < timedelta(minutes=1)


@needs_db
def test_a_city_that_does_not_answer_is_reported(signed_in, monkeypatch):
    client, _ = signed_in
    from dashboard import submit as S
    monkeypatch.setattr(S, "Alex311Client", City)
    monkeypatch.setattr(S.refresh, "refresh_cases", fake_refresh(error="could not reach the City just now"))
    r = client.post(f"/submit/api/refresh/{CASE}")
    assert r.status_code == 502 and "could not reach the City" in r.json()["detail"]


@needs_db
def test_signed_out_and_bad_numbers_are_turned_away(signed_in):
    client, _ = signed_in
    assert client.post("/submit/api/refresh/not-a-case").status_code == 400
    client.cookies.clear()
    assert client.post(f"/submit/api/refresh/{CASE}", headers={"Accept": "application/json"}).status_code == 401
