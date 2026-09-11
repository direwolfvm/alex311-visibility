"""The submission worker: claiming, gating, and what happens when filing fails.

The queue exists because a browser cannot live in the public web image and this
project's Cloud Run rejects per-execution argument overrides, so the job cannot
be told what to file on the command line. That makes the *state machine* the
safety surface, and these tests pin it.
"""
import os
from datetime import datetime, timezone

import pytest

from alex311 import submit_worker as w
from alex311.submit_browser import ENV_GATE, SubmitResult

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                reason="needs DATABASE_URL")


@pytest.fixture()
def conn():
    from alex311 import db
    c = db.connect()
    c.execute("DELETE FROM moderation_actions")
    c.execute("DELETE FROM submission_attempts")
    c.commit()
    yield c
    c.execute("DELETE FROM moderation_actions")
    c.execute("DELETE FROM submission_attempts")
    c.commit()
    c.close()


def make(conn, *, state="approved", tries=0, address="100 King St"):
    from psycopg.types.json import Jsonb
    row = conn.execute(
        """INSERT INTO submission_attempts
             (service_code, service_name, address, address_key, description,
              answers, outcome, findings, submit_state, approved_at, approved_by, tries, contact)
           VALUES ('TESMISCO','Missed Collection',%s,'100 KING STREET','x',
                   %s,'allow','[]'::jsonb,%s, now(), 'tester', %s, %s)
           RETURNING attempt_id""",
        (address, Jsonb({}), state, tries, Jsonb({"first_name": "A"}))).fetchone()
    conn.commit()
    return row["attempt_id"]


def state_of(conn, attempt_id):
    return conn.execute("SELECT submit_state, tries, submit_error, city_case_number "
                        "FROM submission_attempts WHERE attempt_id = %s",
                        (attempt_id,)).fetchone()


# ------------------------------------------------------------- claiming

def test_only_approved_rows_are_claimed(conn):
    """Queued is not approved. A resident asking is not a person agreeing."""
    for s in ("prepared", "queued", "filing", "filed", "failed", "cancelled"):
        make(conn, state=s)
    assert w.claim_next(conn) is None


def test_claiming_marks_the_row_so_a_second_worker_takes_a_different_one(conn):
    a = make(conn)
    claimed = w.claim_next(conn)
    assert claimed["attempt_id"] == a
    assert state_of(conn, a)["submit_state"] == "filing"
    assert w.claim_next(conn) is None, "the same row must not be claimed twice"


def test_the_oldest_approval_goes_first(conn):
    first = make(conn)
    conn.execute("UPDATE submission_attempts SET approved_at = now() - interval '1 hour' "
                 "WHERE attempt_id = %s", (first,))
    conn.commit()
    make(conn)
    assert w.claim_next(conn)["attempt_id"] == first


def test_a_row_that_keeps_failing_is_left_alone(conn):
    """The City's form changing is the likely cause; hammering it helps nobody."""
    a = make(conn, tries=w.MAX_TRIES)
    assert w.claim_next(conn) is None


# --------------------------------------------------------------- finishing

def test_filing_records_the_case_number_and_the_time(conn):
    a = make(conn)
    w.finish(conn, a, state="filed", case_number="26-00099999")
    row = state_of(conn, a)
    assert row["submit_state"] == "filed" and row["city_case_number"] == "26-00099999"
    assert conn.execute("SELECT relayed_at FROM submission_attempts WHERE attempt_id = %s",
                        (a,)).fetchone()["relayed_at"] is not None


def test_a_failure_goes_back_for_another_try(conn):
    a = make(conn)
    w.claim_next(conn)
    w.release(conn, a, "the wizard moved")
    row = state_of(conn, a)
    assert row["submit_state"] == "approved" and "wizard moved" in row["submit_error"]


def test_a_failure_on_the_last_try_parks_the_row(conn):
    a = make(conn, tries=w.MAX_TRIES - 1)
    w.claim_next(conn)                      # takes tries to MAX_TRIES
    w.release(conn, a, "still broken")
    assert state_of(conn, a)["submit_state"] == "failed"


# ------------------------------------------------------------------ gates

def test_the_worker_refuses_live_without_the_environment_gate(monkeypatch, capsys):
    monkeypatch.delenv(ENV_GATE, raising=False)
    assert w.main(["--live"]) == 2


def test_a_rehearsal_leaves_the_row_approved(conn, monkeypatch):
    """A rehearsal proves the path works; it must not consume the request."""
    a = make(conn)
    monkeypatch.setattr(w, "prepare_submission", lambda **kw: SubmitResult(
        True, False, "ready_not_submitted", "TESMISCO", "Missed Collection"))
    monkeypatch.delenv(ENV_GATE, raising=False)
    assert w.main([]) == 0
    row = state_of(conn, a)
    assert row["submit_state"] == "approved", "a rehearsal must not consume the row"
    assert row["tries"] == 0, "and must not burn a try"


def test_a_live_run_files_and_marks_the_row(conn, monkeypatch):
    a = make(conn)
    monkeypatch.setenv(ENV_GATE, "1")
    monkeypatch.setattr(w, "prepare_submission", lambda **kw: SubmitResult(
        True, True, "submitted", "TESMISCO", "Missed Collection",
        case_number="26-00012345"))
    assert w.main(["--live"]) == 0
    row = state_of(conn, a)
    assert row["submit_state"] == "filed" and row["city_case_number"] == "26-00012345"


def test_a_crash_never_strands_a_row_in_filing(conn, monkeypatch):
    a = make(conn)
    monkeypatch.setenv(ENV_GATE, "1")

    def boom(**kw):
        raise RuntimeError("chromium died")
    monkeypatch.setattr(w, "prepare_submission", boom)
    assert w.main(["--live"]) == 0
    assert state_of(conn, a)["submit_state"] != "filing"


def test_one_request_per_run_by_default(conn, monkeypatch):
    """Every filing dispatches city staff, so there is no batch mode."""
    for _ in range(3):
        make(conn)
    monkeypatch.setenv(ENV_GATE, "1")
    calls = []
    monkeypatch.setattr(w, "prepare_submission", lambda **kw: (
        calls.append(1), SubmitResult(True, True, "submitted", "X", "X", case_number="26-1"))[1])
    w.main(["--live"])
    assert len(calls) == 1


def test_submit_without_a_case_number_is_parked_not_filed_and_not_retried():
    """Either the City refused it or it took it and we could not read the
    number. Retrying the second case files a duplicate in a real person's
    name, so the row is parked at the tries limit with what the City showed,
    for a person to look at."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/alex311/submit_worker.py").read_text()
    branch = src.split('elif result.stage == "submitted":')[1].split("\n        else:")[0]
    assert 'state="failed"' in branch
    assert "SET tries = %s" in branch and "MAX_TRIES, attempt_id" in branch
    assert "may file a duplicate" in branch
    assert 'if result.stage == "submitted" and result.case_number:' in src


def test_the_worker_files_against_the_row_it_claimed_rather_than_inserting_one():
    """Each Submit click used to add a second "relayed" row. The first real
    filing left three of them, two with wrong case numbers, and the policy then
    counted the address as having made three requests that day."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    worker = (root / "src/alex311/submit_worker.py").read_text()
    browser = (root / "src/alex311/submit_browser.py").read_text()
    assert 'attempt_id=row["attempt_id"]' in worker.split("def file_one(")[1].split("\ndef ")[0]
    record = browser.split("def _record(")[1].split("\ndef ")[0]
    assert "if attempt_id is not None:" in record
    assert record.index("if attempt_id is not None:") < record.index("adb.record_attempt(")
