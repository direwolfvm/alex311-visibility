"""The six-digit code in the sign-in email.

For when the email is read on one device and the sign-in is on another. The
code is traded for the link's one-time oobCode. What is pinned: nothing
readable is stored (no address, no code, no oobCode); fifteen minutes; five
wrong tries; one use; a new link replaces it; and an address that never
asked is indistinguishable from a wrong code.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alex311 import firebase_auth as fb, signin_code as C

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
LOGIN = (ROOT / "dashboard/login.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("DIGEST_SECRET", "test-secret")


# ------------------------------------------------------------- always run

def test_codes_are_six_digits_and_read_however_they_are_typed():
    for _ in range(50):
        c = C.new_code()
        assert len(c) == 6 and c.isdigit()
    assert C.pretty("482913") == "482 913"
    assert C.digits("482 913") == C.digits("482-913") == C.digits(" 482913 ") == "482913"


def test_hashes_are_keyed_and_tied_to_the_address(monkeypatch):
    a = C.code_hash("A@Example.com", "482913")
    assert a == C.code_hash(" a@example.com ", "482913")            # the address is normalized
    assert a != C.code_hash("b@example.com", "482913")              # the same code elsewhere is another hash
    assert "482913" not in a and "example" not in C.email_key("a@example.com")
    monkeypatch.setenv("DIGEST_SECRET", "another-secret")
    assert a != C.code_hash("a@example.com", "482913")              # and useless without the secret


def test_the_sealed_oobcode_opens_only_with_the_right_code():
    from cryptography.fernet import InvalidToken
    sealed = C._fernet("a@example.com", "482913").encrypt(b"OOB-SECRET")
    assert b"OOB-SECRET" not in sealed
    assert C._fernet("a@example.com", "482913").decrypt(sealed) == b"OOB-SECRET"
    with pytest.raises(InvalidToken):
        C._fernet("a@example.com", "482914").decrypt(sealed)
    with pytest.raises(InvalidToken):
        C._fernet("b@example.com", "482913").decrypt(sealed)


def test_without_a_secret_there_are_no_codes(monkeypatch):
    monkeypatch.delenv("DIGEST_SECRET"); monkeypatch.delenv("SUBMIT_SECRET", raising=False)
    assert C.available() is False
    with pytest.raises(C.NotConfigured):
        C.code_hash("a@b.co", "000000")


def test_the_email_carries_the_code_only_when_there_is_one():
    link = "https://alex311visibility.me/submit/login?mode=signIn&oobCode=abc&apiKey=k"
    _, text, html = fb.sign_in_email(link, "https://alex311visibility.me", "482 913")
    for body in (text, html):
        assert "482 913" in body and "15 minutes" in body and "different device" in body
    _, text, html = fb.sign_in_email(link, "https://alex311visibility.me")
    assert "different device" not in text and "different device" not in html and "{code" not in text + html


def test_the_endpoint_follows_the_contract():
    assert '@public.post("/api/email-code")' in ROUTES
    fn = ROUTES.split("def email_sign_in_code(")[1].split("\n    @public")[0]
    assert 'return {"oobCode": oob}' in fn
    assert "raise HTTPException(400, str(e))" in fn and "raise HTTPException(410, str(e))" in fn
    assert fn.count("raise HTTPException(429") == 2                # burned code, and per-caller tries
    assert 'headers={"Retry-After": str(wait)}' in fn
    send = ROUTES.split("def email_sign_in_link(")[1].split("\n    CODE_TRIES")[0]
    assert "signin_code.issue(conn, email=email, oob_code=oob)" in send
    assert "sign-in code not issued" in send                       # a code problem never stops the link
    table = SCHEMA.split("CREATE TABLE IF NOT EXISTS signin_codes")[1].split(";")[0]
    assert "email_key" in table and "code_hash" in table and "oob_sealed" in table
    assert " email " not in table and " code " not in table        # nothing readable is a column


def test_the_sign_in_page_takes_a_code_after_a_link_is_sent():
    assert 'id="code-entry" hidden' in LOGIN and 'autocomplete="one-time-code"' in LOGIN
    assert "$('code-entry').hidden = false;" in LOGIN
    assert "fetch('/submit/api/email-code'" in LOGIN and "auth.signInWithEmailLink(email, link)" in LOGIN


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@pytest.fixture()
def conn():
    from alex311 import db
    c = db.connect(os.environ.get("APP_DATABASE_URL") or DB)        # as the app role, like the service
    yield c
    c.rollback()
    c.execute("DELETE FROM signin_codes WHERE email_key IN (%s, %s)",
              (C.email_key("sc-a@test.io"), C.email_key("sc-b@test.io")))
    c.commit(); c.close()


@needs_db
def test_the_right_code_returns_the_oobcode_once(conn):
    code = C.issue(conn, email="SC-A@test.io", oob_code="OOB-1")
    row = conn.execute("SELECT * FROM signin_codes WHERE email_key = %s", (C.email_key("sc-a@test.io"),)).fetchone()
    stored = " ".join(str(v) for v in row.values())
    assert code not in stored and "OOB-1" not in stored and "sc-a" not in stored.lower()
    assert C.redeem(conn, email="sc-a@test.io", code=C.pretty(code)) == "OOB-1"
    with pytest.raises(C.Gone):
        C.redeem(conn, email="sc-a@test.io", code=code)             # single use
    assert conn.execute("SELECT oob_sealed FROM signin_codes WHERE email_key = %s",
                        (C.email_key("sc-a@test.io"),)).fetchone()["oob_sealed"] == ""


@needs_db
def test_wrong_tries_burn_it_and_an_unknown_address_looks_like_a_wrong_code(conn):
    code = C.issue(conn, email="sc-a@test.io", oob_code="OOB-2")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(C.MAX_WRONG - 1):
        with pytest.raises(C.WrongCode):
            C.redeem(conn, email="sc-a@test.io", code=wrong)
    with pytest.raises(C.TooMany):
        C.redeem(conn, email="sc-a@test.io", code=wrong)            # the fifth burns it
    with pytest.raises(C.TooMany):
        C.redeem(conn, email="sc-a@test.io", code=code)             # even the right one, now
    with pytest.raises(C.WrongCode) as e:
        C.redeem(conn, email="sc-b@test.io", code="123456")         # never asked
    assert str(e.value) == "that code is not right"


@needs_db
def test_it_expires_and_a_new_link_replaces_it(conn):
    now = datetime.now(timezone.utc)
    old = C.issue(conn, email="sc-a@test.io", oob_code="OOB-OLD", now=now - timedelta(minutes=16))
    with pytest.raises(C.Gone):
        C.redeem(conn, email="sc-a@test.io", code=old)
    first = C.issue(conn, email="sc-a@test.io", oob_code="OOB-A")
    second = C.issue(conn, email="sc-a@test.io", oob_code="OOB-B")
    if first != second:
        with pytest.raises(C.WrongCode):
            C.redeem(conn, email="sc-a@test.io", code=first)        # the earlier code is dead
    assert C.redeem(conn, email="sc-a@test.io", code=second) == "OOB-B"
