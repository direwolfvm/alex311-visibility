"""Phase 4: a score a resident chose to show, and the door out.

Sharing is per verdict, off by default, and shows the score only — never the
note, never a name. Deleting the account removes everything we hold and
never touches Firebase, because the sign-in pool is shared with another
application and the person's Google identity is not ours to delete.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "dashboard/submit.py").read_text()
APP = (ROOT / "dashboard/app.py").read_text()
DB = (ROOT / "src/alex311/db.py").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()
RECORD = (ROOT / "dashboard/static/request.html").read_text()
MY = (ROOT / "dashboard/my.html").read_text()


def test_sharing_is_off_by_default_and_per_verdict():
    assert "share_score BOOLEAN NOT NULL DEFAULT false" in SCHEMA.replace("  ", " ").replace("  ", " ") or \
           "share_score         BOOLEAN NOT NULL DEFAULT false" in SCHEMA
    assert "share_score: bool = False" in API
    assert '<input type="checkbox" id="verdict-share">' in RECORD


def test_the_public_read_never_selects_the_note_or_the_account():
    """The `shared` policy exposes the row; this query is the guard."""
    fn = DB.split("def public_scores(")[1].split("\ndef ")[0]
    query = fn.split('"""SELECT')[1].split('"""')[0]
    assert "note" not in query and "user_id" not in query
    assert "AND share_score" in query
    out = APP.split('row["resident_scores"] = ')[1].split("\n")[0]
    assert "note" not in out


def test_the_warning_sits_beside_the_checkbox():
    """A score without a name still identifies its author to neighbours when
    only one person could have sent the request. Say so where the choice is."""
    import re
    share = re.sub(r"\s+", " ", RECORD.split('id="verdict-share"')[1].split("</label>")[0])
    assert "never the note, and never your name" in share
    assert "neighbours may still work out" in share


def test_what_is_shown_publicly_is_a_score_and_a_count():
    block = RECORD.split("Did residents say it was addressed?</h2>")[1].split("</section>")[0]
    assert "s.score" in block and "resident_scores.length" in block
    assert ".note" not in block and "s.note" not in block   # the CSS class map-note is not a note


def test_deleting_the_account_takes_everything_and_touches_nothing_else():
    fn = DB.split("def delete_account(")[1].split("\ndef ")[0]
    assert "DELETE FROM portal_users WHERE user_id = %s" in fn
    assert "Never touches Firebase" in fn
    for table in ("request_links", "feedback", "portal_sessions"):
        assert "ON DELETE CASCADE" in SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (")[1].split(");")[0]


def test_the_last_administrator_cannot_delete_themselves():
    fn = API.split('@router.delete("/api/account")')[1].split("@router")[0]
    assert "last administrator" in fn
    assert "resp.delete_cookie" in fn


def test_deletion_takes_two_presses_and_says_what_it_does():
    assert "Confirm: delete my account and my list" in MY
    assert "is not touched" in MY


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
def test_sharing_and_deletion_against_a_real_database():
    from alex311 import db, portal_auth as pa
    dsn_app = os.environ.get("APP_DATABASE_URL")
    with db.connect() as owner:
        owner.execute("DELETE FROM portal_users WHERE email LIKE 'p4-%@test'"); owner.commit()
        a, _ = pa.create_user(owner, "p4-a@test", "user", created_by="t")
        b, _ = pa.create_user(owner, "p4-b@test", "user", created_by="t")
        db.save_feedback(owner, user_id=a.user_id, service_request_id="26-00000044", score=2, note="a private", share_score=True)
        db.save_feedback(owner, user_id=b.user_id, service_request_id="26-00000044", score=4, note="b private", share_score=False)
    try:
        conn = db.connect(dsn_app) if dsn_app else db.connect()
        with conn:
            # nobody named: only the shared score, and only the score
            shown = db.public_scores(conn, service_request_id="26-00000044")
            conn.rollback()
            assert [s["score"] for s in shown] == [2]
            assert "note" not in shown[0] and "user_id" not in shown[0]
            su = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()["rolsuper"]
            if not su:
                raw = conn.execute("SELECT count(*) AS n FROM feedback WHERE service_request_id='26-00000044'").fetchone()["n"]
                assert raw == 1, "with no account named, only the shared row is admitted"
        # deleting a takes a's verdict with it; b's stays
        with db.connect() as owner:
            assert db.delete_account(owner, user_id=a.user_id)
            db.as_user(owner, None, "admin")
            left = owner.execute("SELECT count(*) AS n FROM feedback WHERE service_request_id='26-00000044'").fetchone()["n"]
            owner.rollback()
            assert left == 1
    finally:
        with db.connect() as owner:
            owner.execute("DELETE FROM portal_users WHERE email LIKE 'p4-%@test'"); owner.commit()
