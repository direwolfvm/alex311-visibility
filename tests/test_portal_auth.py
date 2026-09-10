"""The front door: who may open the gated prototype.

Not `alex311.identity`, which answers which *resident* filed a request. This is
the gate, and its job is keeping out passers-by. The bar is modest on purpose,
but two things are not negotiable and are tested here: passwords are never
stored in the clear, and the last administrator cannot be locked out.
"""
import os

import pytest

from alex311 import portal_auth as pa

pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                   reason="needs DATABASE_URL")


# --------------------------------------------------------- pure, always run

def test_a_password_never_matches_its_own_hash():
    digest, salt = pa.hash_password("hunter2")
    assert "hunter2" not in digest and len(digest) == 64


def test_the_same_password_hashes_differently_for_two_people():
    """Per-user salt, so one cracked hash does not unlock the other account."""
    a, _ = pa.hash_password("same password")
    b, _ = pa.hash_password("same password")
    assert a != b


def test_verify_accepts_the_right_password_and_nothing_else():
    digest, salt = pa.hash_password("correct horse")
    assert pa.verify_password("correct horse", digest, salt) is True
    assert pa.verify_password("correct hors", digest, salt) is False
    assert pa.verify_password("", digest, salt) is False


def test_generated_passwords_are_unguessable_enough_and_readable():
    passwords = {pa.new_password() for _ in range(200)}
    assert len(passwords) == 200, "generated passwords must not repeat"
    one = passwords.pop()
    assert one.count("-") == 3 and len(one) == 23
    assert not set("l1o0") & set(one), "characters that look alike are excluded"


@pytest.mark.parametrize("raw,clean", [
    ("  Person@Example.COM ", "person@example.com"),
    ("A@B.co", "a@b.co"),
])
def test_emails_are_normalised(raw, clean):
    assert pa.normalize_email(raw) == clean


def test_roles_are_limited():
    assert pa.ROLES == ("admin", "user")


# ------------------------------------------------------- flows, need a database

@pytest.fixture()
def conn():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs DATABASE_URL")
    from alex311 import db
    c = db.connect()
    c.execute("DELETE FROM portal_sessions")
    c.execute("DELETE FROM portal_users")
    c.commit()
    yield c
    c.execute("DELETE FROM portal_sessions")
    c.execute("DELETE FROM portal_users")
    c.commit()
    c.close()


@pytestmark_db
def test_a_new_user_can_sign_in_and_is_recognised(conn):
    user, password = pa.create_user(conn, "a@example.com", "user")
    token = pa.login(conn, "a@example.com", password)
    assert token
    seen = pa.session_user(conn, token)
    assert seen.email == "a@example.com" and seen.is_admin is False


@pytestmark_db
def test_the_password_is_not_in_the_table(conn):
    _user, password = pa.create_user(conn, "a@example.com")
    row = conn.execute("SELECT password_hash, salt FROM portal_users").fetchone()
    assert password not in row["password_hash"] and password not in row["salt"]


@pytestmark_db
def test_the_wrong_password_opens_nothing(conn):
    pa.create_user(conn, "a@example.com", password="right")
    assert pa.login(conn, "a@example.com", "wrong") is None


@pytestmark_db
def test_an_unknown_address_is_refused_without_saying_so(conn):
    """Same answer as a wrong password, so this cannot be used to find accounts."""
    assert pa.login(conn, "nobody@example.com", "anything") is None


@pytestmark_db
def test_signing_in_is_case_and_space_insensitive(conn):
    _user, password = pa.create_user(conn, "Person@Example.com")
    assert pa.login(conn, "  person@EXAMPLE.com ", password)


@pytestmark_db
def test_a_disabled_user_cannot_sign_in_and_loses_their_session(conn):
    user, password = pa.create_user(conn, "a@example.com")
    token = pa.login(conn, "a@example.com", password)
    pa.set_disabled(conn, user.user_id, True)
    assert pa.session_user(conn, token) is None, "an open session must end at once"
    assert pa.login(conn, "a@example.com", password) is None


@pytestmark_db
def test_changing_a_password_ends_the_old_sessions(conn):
    user, password = pa.create_user(conn, "a@example.com")
    token = pa.login(conn, "a@example.com", password)
    fresh = pa.set_password(conn, user.user_id)
    assert pa.session_user(conn, token) is None
    assert pa.login(conn, "a@example.com", fresh)


@pytestmark_db
def test_signing_out_ends_that_session_only(conn):
    _user, password = pa.create_user(conn, "a@example.com")
    one = pa.login(conn, "a@example.com", password)
    two = pa.login(conn, "a@example.com", password)
    pa.logout(conn, one)
    assert pa.session_user(conn, one) is None
    assert pa.session_user(conn, two) is not None


@pytestmark_db
def test_an_unknown_token_is_nobody(conn):
    assert pa.session_user(conn, "not-a-token") is None
    assert pa.session_user(conn, None) is None


@pytestmark_db
def test_the_session_token_is_not_stored_in_the_clear(conn):
    _user, password = pa.create_user(conn, "a@example.com")
    token = pa.login(conn, "a@example.com", password)
    row = conn.execute("SELECT token_hash FROM portal_sessions").fetchone()
    assert token not in row["token_hash"]


@pytestmark_db
def test_the_last_admin_is_countable_so_the_ui_can_refuse(conn):
    """Disabling the only admin would leave nobody who could let anyone back in."""
    admin, _ = pa.create_user(conn, "admin@example.com", "admin")
    pa.create_user(conn, "user@example.com", "user")
    assert pa.count_admins(conn) == 1
    assert pa.count_admins(conn, excluding=admin.user_id) == 0

    second, _ = pa.create_user(conn, "admin2@example.com", "admin")
    assert pa.count_admins(conn, excluding=admin.user_id) == 1
    pa.set_disabled(conn, second.user_id, True)
    assert pa.count_admins(conn, excluding=admin.user_id) == 0


@pytestmark_db
def test_two_people_cannot_share_an_address(conn):
    pa.create_user(conn, "a@example.com")
    with pytest.raises(Exception):
        pa.create_user(conn, "A@Example.com")
    conn.rollback()          # the failed insert leaves the transaction unusable


@pytestmark_db
@pytest.mark.parametrize("bad", ["", "   ", "not-an-email"])
def test_rubbish_addresses_are_refused(conn, bad):
    with pytest.raises(ValueError):
        pa.create_user(conn, bad)


@pytestmark_db
def test_an_unknown_role_is_refused(conn):
    with pytest.raises(ValueError):
        pa.create_user(conn, "a@example.com", "superuser")
