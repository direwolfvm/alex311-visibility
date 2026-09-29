"""The daily digest: what's new for your watches, by email, if you asked.

Pure parts always run: the signed links, the email's wording, the run's
bookkeeping with a fake sender. The database parts run locally: opting in
with a verified sign-in address versus a typed one, confirm, unsubscribe,
and a run that sends to one account and not to another.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import digest as D

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
ACCOUNT = (ROOT / "dashboard/account.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("DIGEST_SECRET", "test-secret")


# ------------------------------------------------------------- always run

def test_links_are_signed_scoped_and_expire():
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    t = D.make_token("confirm", "pu_1", "A@Example.com", exp)
    assert D.read_token(t, "confirm") == ("pu_1", "a@example.com")
    with pytest.raises(D.BadToken):
        D.read_token(t, "unsubscribe")                 # a confirm link cannot unsubscribe
    with pytest.raises(D.BadToken):
        D.read_token(t[:-3] + "xyz", "confirm")        # tampered
    old = D.make_token("confirm", "pu_1", "a@example.com", datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(D.BadToken):
        D.read_token(old, "confirm")
    forever = D.make_token("unsubscribe", "pu_1", "a@example.com", None)
    assert D.read_token(forever, "unsubscribe") == ("pu_1", "a@example.com")


def test_without_a_secret_nothing_can_be_signed(monkeypatch):
    monkeypatch.delenv("DIGEST_SECRET"); monkeypatch.delenv("SUBMIT_SECRET", raising=False)
    with pytest.raises(D.NotConfigured):
        D.make_token("confirm", "pu_1", "a@b.co", None)


def feed_fixture():
    return {"watches": [{"watch_id": 1, "label": "Noise Issues"}, {"watch_id": 2, "label": "500 N PITT ST"}],
            "items": [
                {"service_request_id": "26-1", "service_name": "Noise Issues", "address": "9 KING ST",
                 "status": "Open", "is_new": True, "watch_ids": [1]},
                {"service_request_id": "26-2", "service_name": "Noise Issues", "address": "500 N PITT ST",
                 "status": "Closed", "is_new": False, "watch_ids": [1, 2]},
            ]}


def test_the_email_groups_by_watch_links_here_and_can_be_stopped():
    since = datetime(2026, 9, 28, tzinfo=timezone.utc)
    subject, text, html = D.digest_email(feed_fixture(), "https://alex311visibility.me",
                                         "https://alex311visibility.me/submit/digest/unsubscribe?t=x", since)
    assert subject == "Alex311 Reborn: 1 new, 1 changed for what you watch"
    assert "Noise Issues — 1 new, 1 changed" in text and "500 N PITT ST — 0 new, 1 changed" in text
    assert "https://alex311visibility.me/r/26-1" in text and "https://alex311visibility.me/r/26-2" in html
    assert "digest/unsubscribe?t=x" in text and "digest/unsubscribe?t=x" in html
    assert "not the City" in text and "alexandriava.gov" not in text     # we link here, not to the City
    assert "<script" not in html


def test_a_run_sends_once_per_account_and_switches_a_dead_address_off(monkeypatch):
    sent, calls = [], {"n": 0}
    class Conn:
        def __init__(self):
            self.sql = []
            self.people = [{"user_id": "pu_a", "digest_email": "a@x.co", "digest_confirmed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
                            "digest_sent_at": None, "digest_failures": 2}]
        def execute(self, q, params=None):
            self.sql.append((q, params)); return self
        def fetchall(self): return self.people
        def commit(self): pass
    feed = feed_fixture()
    monkeypatch.setattr(D.watches, "feed", lambda conn, user_id, since: feed)
    def boom(*a): raise RuntimeError("mailgun down")
    conn = Conn()
    counts = D.run(conn, site_origin="https://s", now=datetime(2026, 9, 29, tzinfo=timezone.utc), send=boom)
    assert counts == {"accounts": 1, "sent": 0, "empty": 0, "failed": 1, "disabled": 1}
    assert any("digest_confirmed_at = NULL" in q for q, _ in conn.sql)     # third failure: off
    # a good send records the mark and resets failures
    conn = Conn(); conn.people[0]["digest_failures"] = 0
    counts = D.run(conn, site_origin="https://s", now=datetime(2026, 9, 29, tzinfo=timezone.utc),
                   send=lambda to, s, t, h: sent.append((to, s)))
    assert counts["sent"] == 1 and sent[0][0] == "a@x.co"
    assert any("digest_sent_at = %s, digest_failures = 0" in q for q, _ in conn.sql)
    # nothing new: no email, but the window still moves
    monkeypatch.setattr(D.watches, "feed", lambda conn, user_id, since: {"watches": feed["watches"], "items": []})
    conn = Conn(); sent.clear()
    counts = D.run(conn, site_origin="https://s", send=lambda *a: sent.append(a))
    assert counts["empty"] == 1 and not sent
    assert any("digest_sent_at = %s WHERE" in q for q, _ in conn.sql)


def test_the_routes_and_the_page():
    for r in ('@router.get("/api/digest")', '@router.put("/api/digest")', '@router.delete("/api/digest")',
              '@public.get("/digest/confirm"', '@public.get("/digest/unsubscribe"'):
        assert r in ROUTES
    body = ROUTES.split("def digest_set(")[1].split("\n    @router")[0]
    assert "user_id = _account(request)" in body and "digest.request(" in body
    assert 'id="digest-card"' in ACCOUNT and "api('/digest', {method: 'PUT'" in ACCOUNT
    assert "Every email has a link to stop it" in ACCOUNT
    for col in ("digest_email", "digest_confirmed_at", "digest_sent_at", "digest_failures"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in SCHEMA
    assert "--max-retries=0" in (ROOT / "deploy/README.md").read_text()


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@needs_db
def test_opting_in_confirming_and_unsubscribing(monkeypatch):
    from alex311 import db, mail, portal_auth as pa
    monkeypatch.setenv("ALEX311_MAIL_FROM", "t <t@x>"); monkeypatch.setenv("SMTP_HOST", "smtp.test")
    outbox = []
    monkeypatch.setattr(mail, "send", lambda to, s, t, h=None: outbox.append((to, s, t)))
    conn = db.connect()
    conn.execute("DELETE FROM portal_users WHERE email LIKE 'dg-%@test.io' OR firebase_uid LIKE 'dg-%'"); conn.commit()
    # an invited address, claimed by a verified sign-in: we hold it and Firebase verified it,
    # so the digest can go there without a confirmation link. (A plain Firebase sign-in leaves
    # us no email at all, on purpose, and gets the link.)
    pa.create_user(conn, "dg-a@test.io", "user", created_by="t")
    tok = pa.sign_in_with_firebase(conn, uid="dg-uid", email="dg-a@test.io", email_verified=True, policy_version="t")
    a = pa.session_user(conn, tok)
    assert D.request(conn, user_id=a.user_id, email="DG-A@test.io", site_origin="https://s") == {"status": "on", "email": "dg-a@test.io"}
    assert D.state(conn, user_id=a.user_id)["status"] == "on" and not outbox
    # a different address: pending until the link is opened
    r = D.request(conn, user_id=a.user_id, email="dg-other@test.io", site_origin="https://s")
    assert r["status"] == "pending" and outbox[-1][0] == "dg-other@test.io"
    link = [w for w in outbox[-1][2].split() if "digest/confirm?t=" in w][0]
    token = link.split("t=")[1]
    assert D.state(conn, user_id=a.user_id)["status"] == "pending"
    assert D.confirm(conn, token) == "dg-other@test.io"
    assert D.state(conn, user_id=a.user_id)["status"] == "on"
    # unsubscribe from the email, no sign-in
    off = D.make_token("unsubscribe", a.user_id, "dg-other@test.io", None)
    assert D.unsubscribe(conn, off) == "dg-other@test.io"
    assert D.state(conn, user_id=a.user_id)["status"] == "off"
    with pytest.raises(ValueError):
        D.request(conn, user_id=a.user_id, email="nope", site_origin="https://s")
    conn.execute("DELETE FROM portal_users WHERE email LIKE 'dg-%@test.io' OR firebase_uid LIKE 'dg-%'"); conn.commit(); conn.close()
