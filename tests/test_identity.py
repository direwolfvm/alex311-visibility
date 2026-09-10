"""Verified submitter identity.

The email-canonicalisation tests are the ones that matter most for abuse: if
`a+1@gmail.com` and `a+2@gmail.com` are two submitters, every per-submitter
limit is one keystroke away from being defeated, and the whole point of adding
identity friction is lost.

These run against a real Postgres when DATABASE_URL is set, and skip otherwise,
because the flows are mostly about what the database enforces.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from alex311.identity import (ConsoleSender, InvalidEmail, VerificationFailed,
                              block_submitter, confirm_verification, new_code,
                              normalize_email, revoke_session, session_submitter,
                              start_verification)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------- pure, always run

@pytest.mark.parametrize("a,b", [
    ("A.User+311@Gmail.com", "auser@gmail.com"),
    ("a.user@gmail.com", "auser@gmail.com"),
    ("AUser+anything@googlemail.com", "auser@googlemail.com"),
    ("Bob+spam@outlook.com", "bob@outlook.com"),
    ("  Bob@Outlook.com  ", "bob@outlook.com"),
])
def test_one_mailbox_is_one_submitter(a, b):
    """Plus-tags and (where the provider ignores them) dots must fold."""
    assert normalize_email(a) == normalize_email(b)


def test_dots_are_kept_where_the_provider_honours_them():
    assert normalize_email("b.ob@outlook.com") != normalize_email("bob@outlook.com")


def test_different_people_stay_different():
    assert normalize_email("alice@example.com") != normalize_email("bob@example.com")


@pytest.mark.parametrize("bad", ["", "   ", "not-an-email", "@example.com",
                                 "a@b", "a b@example.com", "+tag@gmail.com"])
def test_rubbish_is_rejected(bad):
    with pytest.raises(InvalidEmail):
        normalize_email(bad)


def test_codes_are_the_right_shape():
    codes = {new_code() for _ in range(200)}
    assert all(c.isdigit() and len(c) == 6 for c in codes)
    assert len(codes) > 150, "codes should not repeat this often"


# --------------------------------------------------- flows, need a database

pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                   reason="needs DATABASE_URL")


@pytest.fixture()
def conn():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs DATABASE_URL")
    from alex311 import db
    c = db.connect()
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def mailbox():
    return ConsoleSender(sink=[])


def _code(mailbox):
    return mailbox.sink[-1]["body"].split("Your code is ")[1].split("\n")[0].strip()


def _fresh(prefix="t"):
    import uuid
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


@pytestmark_db
def test_a_code_verifies_once_and_yields_a_session(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox)
    session = confirm_verification(conn, email, _code(mailbox))
    assert session.submitter_id.startswith("sub_")
    assert session_submitter(conn, session.token) == session.submitter_id
    # the same code cannot be spent twice
    with pytest.raises(VerificationFailed):
        confirm_verification(conn, email, _code(mailbox))


@pytestmark_db
def test_the_same_mailbox_written_differently_is_one_submitter(conn, mailbox):
    """The abuse rules count per submitter, so this must not create two."""
    base = f"user{os.urandom(4).hex()}"
    start_verification(conn, f"{base}@gmail.com", mailbox)
    first = confirm_verification(conn, f"{base}@gmail.com", _code(mailbox))
    start_verification(conn, f"{base}+311@gmail.com", mailbox)
    second = confirm_verification(conn, f"{base}+311@gmail.com", _code(mailbox))
    assert first.submitter_id == second.submitter_id


@pytestmark_db
def test_a_wrong_code_fails_and_burns_an_attempt(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox)
    with pytest.raises(VerificationFailed):
        confirm_verification(conn, email, "000000")
    row = conn.execute("SELECT attempts FROM submitter_verifications WHERE email = %s",
                       (normalize_email(email),)).fetchone()
    assert row["attempts"] == 1


@pytestmark_db
def test_guessing_is_capped(conn, mailbox):
    from alex311.identity import MAX_CODE_ATTEMPTS
    email = _fresh()
    start_verification(conn, email, mailbox)
    good = _code(mailbox)
    for _ in range(MAX_CODE_ATTEMPTS):
        with pytest.raises(VerificationFailed):
            confirm_verification(conn, email, "000000")
    # even the correct code is refused once the challenge is exhausted
    with pytest.raises(VerificationFailed):
        confirm_verification(conn, email, good)


@pytestmark_db
def test_an_expired_code_is_refused(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox, now=NOW - timedelta(hours=2))
    with pytest.raises(VerificationFailed):
        confirm_verification(conn, email, _code(mailbox), now=NOW)


@pytestmark_db
def test_code_requests_are_rate_limited_without_saying_so(conn, mailbox):
    from alex311.identity import MAX_CODES_PER_HOUR
    email = _fresh()
    for _ in range(MAX_CODES_PER_HOUR + 3):
        start_verification(conn, email, mailbox)      # never raises
    sent = sum(1 for m in mailbox.sink if m["to"].strip().lower() == email.lower())
    assert sent == MAX_CODES_PER_HOUR


@pytestmark_db
def test_the_code_is_never_stored_in_the_clear(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox)
    code = _code(mailbox)
    row = conn.execute("SELECT code_hash FROM submitter_verifications WHERE email = %s",
                       (normalize_email(email),)).fetchone()
    assert code not in row["code_hash"] and len(row["code_hash"]) == 64


@pytestmark_db
def test_the_session_token_is_never_stored_in_the_clear(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox)
    s = confirm_verification(conn, email, _code(mailbox))
    row = conn.execute("SELECT token_hash FROM submitter_sessions WHERE submitter_id = %s",
                       (s.submitter_id,)).fetchone()
    assert s.token not in row["token_hash"]


@pytestmark_db
def test_signing_out_ends_the_session(conn, mailbox):
    email = _fresh()
    start_verification(conn, email, mailbox)
    s = confirm_verification(conn, email, _code(mailbox))
    revoke_session(conn, s.token)
    assert session_submitter(conn, s.token) is None


@pytestmark_db
def test_blocking_takes_effect_on_the_next_request(conn, mailbox):
    """A block should not wait for the session to expire."""
    email = _fresh()
    start_verification(conn, email, mailbox)
    s = confirm_verification(conn, email, _code(mailbox))
    assert session_submitter(conn, s.token) == s.submitter_id
    block_submitter(conn, s.submitter_id, "targeting one address")
    assert session_submitter(conn, s.token) is None


@pytestmark_db
def test_an_unknown_token_is_nobody(conn):
    assert session_submitter(conn, "not-a-real-token") is None
    assert session_submitter(conn, None) is None
