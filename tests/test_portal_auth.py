"""The front door: who may open the gated pages.

One door now — Firebase sign-in, linked to an account row here — and the
session that follows. The password door was retired on 2026-09-18; what stays
non-negotiable is tested here: session tokens are never stored in the clear,
a disabled account loses its sessions at once, an invitation links to the
sign-in that claims it, and the last administrator cannot be locked out.
"""
import os

import pytest

from alex311 import portal_auth as pa

pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                   reason="needs DATABASE_URL")


# --------------------------------------------------------- pure, always run

def test_the_password_machinery_is_gone():
    for name in ("hash_password", "verify_password", "new_password", "login", "set_password"):
        assert not hasattr(pa, name), name


@pytest.mark.parametrize("raw, clean", [
    ("A@Example.com", "a@example.com"),
    ("  b@example.com  ", "b@example.com"),
])
def test_emails_are_normalized(raw, clean):
    assert pa.normalize_email(raw) == clean


def test_roles_are_limited():
    assert set(pa.ROLES) == {"admin", "user"}


def test_the_label_prefers_what_the_person_chose():
    assert pa.PortalUser("pu_abcdef123456", "a@b.co", "user", display_name="Jo").label == "Jo"
    assert pa.PortalUser("pu_abcdef123456", "a@b.co", "user").label == "a@b.co"
    assert pa.PortalUser("pu_abcdef123456", None, "user").label == "account 123456"


# ------------------------------------------------------- flows, need a database

@pytest.fixture()
def conn():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("needs DATABASE_URL")
    if "localhost" not in url and "127.0.0.1" not in url:
        pytest.skip("this fixture wipes every account; it only runs against a local database")
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


def arrive(conn, uid, email, verified=True):
    return pa.sign_in_with_firebase(conn, uid=uid, email=email, email_verified=verified,
                                    policy_version="t")


@pytestmark_db
def test_a_first_sign_in_makes_an_account_and_a_session(conn):
    token = arrive(conn, "uid-a", "a@example.com")
    assert token
    seen = pa.session_user(conn, token)
    assert seen.firebase_uid == "uid-a" and seen.is_admin is False


@pytestmark_db
def test_an_invitation_is_claimed_by_the_matching_verified_sign_in(conn):
    invited = pa.create_user(conn, "a@example.com", "admin", created_by="test")
    token = arrive(conn, "uid-a", "A@Example.com")
    seen = pa.session_user(conn, token)
    assert seen.user_id == invited.user_id and seen.is_admin
    assert conn.execute("SELECT count(*) AS n FROM portal_users").fetchone()["n"] == 1


@pytestmark_db
def test_an_unverified_address_cannot_claim_an_invitation(conn):
    invited = pa.create_user(conn, "a@example.com", "admin", created_by="test")
    token = arrive(conn, "uid-x", "a@example.com", verified=False)
    seen = pa.session_user(conn, token)
    assert seen.user_id != invited.user_id and not seen.is_admin


@pytestmark_db
def test_a_disabled_user_loses_their_session_at_once(conn):
    token = arrive(conn, "uid-a", "a@example.com")
    user = pa.session_user(conn, token)
    pa.set_disabled(conn, user.user_id, True)
    assert pa.session_user(conn, token) is None, "an open session must end at once"
    assert arrive(conn, "uid-a", "a@example.com") is None


@pytestmark_db
def test_signing_out_ends_that_session_only(conn):
    one = arrive(conn, "uid-a", "a@example.com")
    two = arrive(conn, "uid-a", "a@example.com")
    pa.logout(conn, one)
    assert pa.session_user(conn, one) is None
    assert pa.session_user(conn, two) is not None


@pytestmark_db
def test_signing_out_everywhere_ends_every_session(conn):
    one = arrive(conn, "uid-a", "a@example.com")
    two = arrive(conn, "uid-a", "a@example.com")
    user = pa.session_user(conn, one)
    assert pa.logout_all(conn, user.user_id) == 2
    assert pa.session_user(conn, one) is None and pa.session_user(conn, two) is None


@pytestmark_db
def test_an_unknown_token_is_nobody(conn):
    assert pa.session_user(conn, "nope") is None
    assert pa.session_user(conn, None) is None


@pytestmark_db
def test_the_session_token_is_not_stored_in_the_clear(conn):
    token = arrive(conn, "uid-a", "a@example.com")
    rows = conn.execute("SELECT token_hash FROM portal_sessions").fetchall()
    assert rows and all(token not in r["token_hash"] for r in rows)


@pytestmark_db
def test_the_last_admin_is_countable_so_the_ui_can_refuse(conn):
    a = pa.create_user(conn, "a@example.com", "admin")
    assert pa.count_admins(conn) == 1
    assert pa.count_admins(conn, excluding=a.user_id) == 0
    pa.create_user(conn, "b@example.com", "admin")
    assert pa.count_admins(conn, excluding=a.user_id) == 1


@pytestmark_db
def test_two_people_cannot_share_an_address(conn):
    pa.create_user(conn, "a@example.com")
    with pytest.raises(Exception):
        pa.create_user(conn, "A@example.com")
    conn.rollback()                                  # the refused insert aborted the transaction


@pytestmark_db
@pytest.mark.parametrize("bad", ["", "nope", "   "])
def test_rubbish_addresses_are_refused(conn, bad):
    with pytest.raises(ValueError):
        pa.create_user(conn, bad)


@pytestmark_db
def test_an_unknown_role_is_refused(conn):
    with pytest.raises(ValueError):
        pa.create_user(conn, "a@example.com", "owner")
