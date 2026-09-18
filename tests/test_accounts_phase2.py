"""Phase 2: a pointer from an account to a request, and the database deciding
whose pointers come back.

"Mine" is written once, by the worker, when the City hands back a case
number. "Following" is anyone saying they care. Both are a case number and
nothing the City already holds. Row-level security on the table is forced,
so a handler that forgets to name the account gets nothing — and the last
test here proves that against a real database rather than asserting it.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "dashboard/submit.py").read_text()
DB = (ROOT / "src/alex311/db.py").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()
WORKER = (ROOT / "src/alex311/submit_worker.py").read_text()
RECORD = (ROOT / "dashboard/static/request.html").read_text()
MY = (ROOT / "dashboard/my.html").read_text()
APP = (ROOT / "dashboard/app.py").read_text()


# ------------------------------------------------------------ a pointer

def test_a_link_is_a_case_number_and_nothing_the_city_holds():
    table = SCHEMA.split("CREATE TABLE IF NOT EXISTS request_links (")[1].split(");")[0]
    for col in ("user_id", "service_request_id", "relation", "attempt_id", "created_at"):
        assert col in table
    import re
    cols = set(re.findall(r"^\s+(\w+)\s", table, re.M))
    for copied in ("address", "description", "status", "lat", "long"):
        assert copied not in cols, f"the link table copies {copied}"


def test_mine_is_only_ever_written_by_the_worker():
    """The City's feed carries no reporter identity, so a claim to have filed
    something elsewhere cannot be checked. The one moment we can vouch for
    "mine" is when the worker gets the case number back."""
    follow = API.split('@router.post("/api/follow/{case}")')[1].split("@router")[0]
    assert 'relation="following"' in follow and 'relation="mine"' not in follow
    filed = WORKER.split('if result.stage == "submitted" and result.case_number:')[1].split("elif")[0]
    assert 'relation="mine"' in filed


def test_a_link_failure_cannot_unsay_a_filing():
    filed = WORKER.split('if result.stage == "submitted" and result.case_number:')[1].split("elif")[0]
    assert filed.index("finish(conn, attempt_id, state=\"filed\"") < filed.index("db.link_request(")
    assert "except Exception" in filed and "conn.rollback()" in filed


def test_mine_outranks_following_and_nothing_downgrades_it():
    fn = DB.split("def link_request(")[1].split("\ndef ")[0]
    assert "WHEN EXCLUDED.relation = 'mine' THEN 'mine'" in fn
    assert "ELSE request_links.relation" in fn


def test_the_shared_script_credential_cannot_keep_a_list():
    fn = API.split("def _account(request: Request)")[1].split("\n    @router")[0]
    assert "HTTPException(403" in fn


def test_a_case_number_has_to_look_like_one():
    assert 'CASE_ID = re.compile(r"^\\d{2}-\\d{8}$")' in API
    follow = API.split('@router.post("/api/follow/{case}")')[1].split("@router")[0]
    assert "CASE_ID.match(case)" in follow


# ---------------------------------------------------------- the pages

def test_the_record_page_offers_follow_and_says_when_it_is_yours():
    """A small pill beside the case number: following is a state of the case,
    not a card of its own in the sidebar."""
    assert 'id="follow-btn"' in RECORD and ">Follow</button>" in RECORD
    assert "You sent this one" in RECORD
    assert "/submit/api/link/" in RECORD


def test_my_requests_admits_how_fresh_it_is():
    """A request sent minutes ago is not in the mirror until the next ingest."""
    assert "four times a day" in MY
    assert "not in the mirror yet" in MY
    assert "LEFT JOIN service_requests" in DB.split("def my_links(")[1]


# ---------------------------------------------------------- the database

def test_rls_is_forced_and_the_service_can_connect_as_a_non_owner():
    assert "ALTER TABLE request_links ENABLE ROW LEVEL SECURITY;" in SCHEMA
    assert "ALTER TABLE request_links FORCE ROW LEVEL SECURITY;" in SCHEMA
    assert "current_setting('app.user_id', true)" in SCHEMA
    assert 'os.environ.get("APP_DATABASE_URL") or os.environ["DATABASE_URL"]' in APP


def test_every_account_read_or_write_names_the_account_first():
    for fn in ("link_request", "unlink_request", "link_state", "my_links"):
        body = DB.split(f"def {fn}(")[1].split("\ndef ")[0]
        assert "as_user(conn, user_id)" in body, fn


def test_the_setting_is_transaction_local_so_it_cannot_leak_between_requests():
    fn = DB.split("def as_user(")[1].split("\ndef ")[0]
    assert "set_config('app.user_id', %s, true)" in fn


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
def test_rls_bites_against_a_real_database():
    """The one that matters. A connection that does not name an account gets
    no rows; naming one gets that account's rows and no other's. Run as the
    owner too (FORCE), and as the app role when APP_DATABASE_URL is set."""
    from alex311 import db, portal_auth as pa
    dsns = [os.environ["DATABASE_URL"]]
    if os.environ.get("APP_DATABASE_URL"):
        dsns.append(os.environ["APP_DATABASE_URL"])
    checked = 0
    with db.connect() as owner:
        owner.execute("DELETE FROM portal_users WHERE email LIKE 'p2-%@test'"); owner.commit()
        a = pa.create_user(owner, "p2-a@test", "user", created_by="t")
        b = pa.create_user(owner, "p2-b@test", "user", created_by="t")
        db.link_request(owner, user_id=a.user_id, service_request_id="26-00000001", relation="following")
        db.link_request(owner, user_id=b.user_id, service_request_id="26-00000002", relation="following")
    try:
        for dsn in dsns:
            with db.connect(dsn) as conn:
                who = conn.execute("SELECT current_user AS u, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su").fetchone()
                if who["su"]:
                    continue          # a superuser bypasses RLS by design; the app role is the proof
                checked += 1
                # no account named: nothing, not everything
                n = conn.execute("SELECT count(*) AS n FROM request_links WHERE service_request_id LIKE '26-0000000%'").fetchone()["n"]
                assert n == 0, f"{who['u']}: an unfiltered read returned {n} rows"
                # account a sees only a's row
                db.as_user(conn, a.user_id)
                rows = conn.execute("SELECT service_request_id FROM request_links WHERE service_request_id LIKE '26-0000000%'").fetchall()
                assert [r["service_request_id"] for r in rows] == ["26-00000001"]
                conn.rollback()
                # and a cannot write a row in b's name
                db.as_user(conn, a.user_id)
                with pytest.raises(Exception):
                    conn.execute("INSERT INTO request_links (user_id, service_request_id, relation) VALUES (%s, '26-00000003', 'following')", (b.user_id,))
                conn.rollback()
                # an admin sees both
                db.as_user(conn, None, "admin")
                assert conn.execute("SELECT count(*) AS n FROM request_links WHERE service_request_id LIKE '26-0000000%'").fetchone()["n"] == 2
                conn.rollback()
        assert checked, "no non-superuser connection to prove RLS against; set APP_DATABASE_URL"
    finally:
        with db.connect() as owner:
            owner.execute("DELETE FROM portal_users WHERE email LIKE 'p2-%@test'"); owner.commit()


def test_every_row_on_my_requests_links_to_the_citys_record():
    """A case number is enough for the City's own record page, mirrored here
    or not — a request filed minutes ago is not in the mirror until the next
    ingest, and the link is the one thing the person can already use."""
    routes = (ROOT / "dashboard/submit.py").read_text()
    fn = routes.split("def my_requests(")[1].split("\n    @router")[0]
    assert 'link["report_url"] = Alex311Client.deep_link(link["service_request_id"])' in fn
    assert 'href="${esc(l.report_url)}" target="_blank" rel="noopener"' in MY
    assert "City record" in MY
