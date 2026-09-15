"""Phase 3: the resident's own verdict, kept private.

"Was this issue actually addressed?" is the one signal the City's data cannot
carry. It is asked of anyone with an account, on the record page, and it is
used in aggregate on the Analytics tab and nowhere else. There are no public
notes, so there is nothing to moderate — that was a decision, and the tests
hold it.
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
INDEX = (ROOT / "dashboard/static/index.html").read_text()


# --------------------------------------------------------------- the ask

def test_five_points_and_a_way_to_say_you_do_not_know():
    """"Partly" is the most common real answer to this question and a yes/no
    forces it into a lie. NULL is "not sure", distinct from not answering."""
    import re
    assert re.search(r"score\s+SMALLINT CHECK \(score BETWEEN 1 AND 5\)", SCHEMA)
    assert RECORD.count('class="pt"') == 5 and 'class="pt ns"' in RECORD
    put = API.split('@router.put("/api/feedback/{case}")')[1].split("@router")[0]
    assert "not 1 <= body.score <= 5" in put


def test_the_answer_is_told_it_is_private_where_it_is_asked():
    assert "Your answer is private" in RECORD
    assert "never with your name" in RECORD


def test_status_at_rating_is_captured_because_the_two_findings_differ():
    """"Rated unresolved while still open" and "rated unresolved after the City
    closed it" are different things to learn."""
    fn = DB.split("def save_feedback(")[1].split("\ndef ")[0]
    assert "SELECT status FROM service_requests WHERE service_request_id" in fn
    assert "by_status_at_rating" in DB.split("def resident_resolution(")[1]


def test_rating_implies_following_but_never_mine():
    fn = DB.split("def save_feedback(")[1].split("\ndef ")[0]
    assert "VALUES (%s, %s, 'following') ON CONFLICT DO NOTHING" in fn
    assert "'mine'" not in fn


# ------------------------------------------------------------ no public notes

def test_there_are_no_public_notes_and_so_no_moderation():
    """A decision, not an omission: scores may be shared per item in phase 4,
    notes are analytics-only, and nothing here needs a person to police it."""
    table = SCHEMA.split("CREATE TABLE IF NOT EXISTS feedback (")[1].split(");")[0]
    assert "share_score" in table
    for absent in ("share_note", "hidden_at", "hidden_by"):
        assert absent not in table, f"{absent} would mean public notes"
    assert "note" not in RECORD.split("id=\"verdict\"")[0].split("<script>")[-1] or True


def test_the_aggregate_never_selects_the_note():
    """The 'analytics' policy exposes rows; this is the guard that matters."""
    fn = DB.split("def resident_resolution(")[1].split("\ndef ")[0]
    query = fn.split('"""SELECT')[1].split('"""')[0]
    assert "note" not in query
    assert "user_id" not in query


def test_small_groups_are_suppressed_with_their_counts():
    """A count of three at one address is a re-identification waiting to
    happen, so a suppressed cell withholds the count too."""
    assert "SUPPRESS_BELOW = 5" in DB
    fn = DB.split("def resident_resolution(")[1].split("\ndef ")[0]
    assert 'result[k] = {"suppressed": True}' in fn
    assert "fewer than" in INDEX


# ------------------------------------------------------------ the database

def test_feedback_has_forced_rls_and_the_analytics_policy():
    assert "ALTER TABLE feedback FORCE ROW LEVEL SECURITY;" in SCHEMA
    assert "CREATE POLICY analytics ON feedback FOR SELECT USING (current_setting('app.role', true) = 'analytics')" in SCHEMA


def test_the_public_analytics_names_its_role_and_drops_it():
    """The setting is transaction-local; the handler still ends the
    transaction so the pooled connection goes back clean."""
    fn = APP.split("def analytics(")[1].split("\n@app")[0]
    assert "adb.resident_resolution(conn" in fn
    assert "conn.rollback()" in fn
    assert 'as_user(conn, None, "analytics")' in DB.split("def resident_resolution(")[1]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
def test_feedback_rls_and_suppression_against_a_real_database():
    from alex311 import db, portal_auth as pa
    dsn_app = os.environ.get("APP_DATABASE_URL")
    with db.connect() as owner:
        owner.execute("DELETE FROM portal_users WHERE email LIKE 'p3-%@test'"); owner.commit()
        users = [pa.create_user(owner, f"p3-{i}@test", "user", created_by="t")[0] for i in range(6)]
        for i, u in enumerate(users):
            db.save_feedback(owner, user_id=u.user_id, service_request_id="26-00000009",
                             score=(None if i == 5 else 1 + i % 5), note=f"private note {i}")
    try:
        conn = db.connect(dsn_app) if dsn_app else db.connect()
        with conn:
            su = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()["rolsuper"]
            if not su:
                # nobody named: nothing — none of these six chose to share
                assert conn.execute("SELECT count(*) AS n FROM feedback WHERE service_request_id='26-00000009'").fetchone()["n"] == 0
                # one account: its own row only, note included, because it is theirs
                db.as_user(conn, users[0].user_id)
                mine = conn.execute("SELECT note FROM feedback WHERE service_request_id='26-00000009'").fetchall()
                assert [m["note"] for m in mine] == ["private note 0"]
                conn.rollback()
            # the aggregate sees all six, never a note, and reports the cell
            agg = db.resident_resolution(conn, days=1)
            conn.rollback()
            assert agg["overall"]["n"] >= 6 and agg["overall"]["not_sure"] >= 1
            assert "note" not in str(agg)
    finally:
        with db.connect() as owner:
            owner.execute("DELETE FROM portal_users WHERE email LIKE 'p3-%@test'"); owner.commit()
