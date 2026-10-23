"""Support: a signed-in person's view of somebody else's request.

Pinned: one stance per person per request, changeable and withdrawable;
never on your own request; everyone sees counts and nobody sees who; no
text (a public note would need moderation — decided 2026-09-15); it goes
with the account.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
APP = (ROOT / "dashboard/app.py").read_text()
PAGE = (ROOT / "dashboard/static/request.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


def test_the_shape_of_it():
    table = SCHEMA.split("CREATE TABLE IF NOT EXISTS request_stances")[1].split(";")[0]
    assert "CHECK (stance IN ('support', 'oppose'))" in table
    columns = {line.split()[0] for line in table.splitlines() if line.strip() and not line.strip().startswith(("(", ")", "PRIMARY", "CHECK"))}
    assert columns == {"user_id", "service_request_id", "stance", "created_at", "updated_at"}   # counts, not comments
    assert "ON DELETE CASCADE" in table                                  # goes with the account
    for route in ('@router.get("/api/stance/{case}")', '@router.put("/api/stance/{case}")'):
        assert "Depends(gate)" in ROUTES.split(route)[1].split("\n    @router")[0]
    assert "except adb.OwnRequest" in ROUTES and "raise HTTPException(409" in ROUTES.split("def stance_set(")[1].split("@router")[0]
    assert 'row["support"] = support' in APP                             # the public record carries counts
    assert "support = adb.stance_tally(conn, service_request_id=service_request_id)" in APP   # counts, no account


def test_the_page_offers_it_to_the_right_people():
    assert 'id="support-card"' in PAGE and "I support this" in PAGE and "I do not" in PAGE
    assert "support is for other people's requests" in PAGE                  # the sender is told why not
    assert 'href="/submit/login">Sign in</a> to add yours' in PAGE
    assert "Counts are shown to everyone; who said so is not." in PAGE
    assert "drawStance(user)" in PAGE


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")
CASE = "97-00000001"


@pytest.fixture()
def conn():
    from alex311 import db
    c = db.connect(DB)
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM portal_users WHERE email LIKE 'support-%%@test.io'")
    c.execute("DELETE FROM service_requests WHERE service_request_id = %s", (CASE,))
    c.execute("INSERT INTO service_requests (service_request_id, service_name, status) VALUES (%s, 'Pothole', 'Open')", (CASE,))
    c.commit()
    yield c
    c.rollback()
    c.execute("SELECT set_config('app.role', 'admin', false)")
    c.execute("DELETE FROM portal_users WHERE email LIKE 'support-%%@test.io'")
    c.execute("DELETE FROM service_requests WHERE service_request_id = %s", (CASE,))
    c.commit(); c.close()


def users(conn, n):
    from alex311 import portal_auth as pa
    out = []
    for i in range(n):
        out.append(pa.create_user(conn, f"support-{i}@test.io").user_id)
        conn.execute("SELECT set_config('app.role', 'admin', false)")
    return out


@needs_db
def test_one_each_changeable_withdrawable_and_counted(conn):
    from alex311 import db
    a, b, c = users(conn, 3)
    assert db.set_stance(conn, user_id=a, service_request_id=CASE, stance="support") == {"stance": "support", "support": 1, "oppose": 0}
    assert db.set_stance(conn, user_id=b, service_request_id=CASE, stance="support")["support"] == 2
    assert db.set_stance(conn, user_id=c, service_request_id=CASE, stance="oppose") == {"stance": "oppose", "support": 2, "oppose": 1}
    assert db.set_stance(conn, user_id=a, service_request_id=CASE, stance="support")["support"] == 2   # same again: still one
    assert db.set_stance(conn, user_id=a, service_request_id=CASE, stance="oppose") == {"stance": "oppose", "support": 1, "oppose": 2}
    assert db.my_stance(conn, user_id=a, service_request_id=CASE) == "oppose"
    assert db.set_stance(conn, user_id=a, service_request_id=CASE, stance=None) == {"stance": None, "support": 1, "oppose": 1}
    assert db.my_stance(conn, user_id=a, service_request_id=CASE) is None
    assert db.stance_tallies(conn, [CASE, "nope"]) == {CASE: {"support": 1, "oppose": 1}}
    with pytest.raises(ValueError):
        db.set_stance(conn, user_id=a, service_request_id=CASE, stance="meh")


@needs_db
def test_not_on_your_own_request(conn):
    from alex311 import db
    (me,) = users(conn, 1)
    db.link_request(conn, user_id=me, service_request_id=CASE, relation="mine")
    conn.execute("SELECT set_config('app.role', 'admin', false)")
    with pytest.raises(db.OwnRequest):
        db.set_stance(conn, user_id=me, service_request_id=CASE, stance="support")
    assert db.stance_tally(conn, service_request_id=CASE) == {"support": 0, "oppose": 0}


@needs_db
def test_it_goes_with_the_account_and_the_app_role_can_do_all_of_it(conn):
    from alex311 import db
    a, b = users(conn, 2)
    app = db.connect(os.environ.get("APP_DATABASE_URL") or DB)
    try:
        assert db.set_stance(app, user_id=a, service_request_id=CASE, stance="support")["support"] == 1
        assert db.set_stance(app, user_id=b, service_request_id=CASE, stance="oppose")["oppose"] == 1
        assert db.my_stance(app, user_id=a, service_request_id=CASE) == "support"
    finally:
        app.close()
    assert db.delete_account(conn, user_id=a)
    conn.execute("SELECT set_config('app.role', 'admin', false)")
    assert db.stance_tally(conn, service_request_id=CASE) == {"support": 0, "oppose": 1}
