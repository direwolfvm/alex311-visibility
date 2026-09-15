"""Phase 1 of the account system: sign in with Firebase, alongside the passwords.

Two doors, one gate. The password form stays because testers are already on
it; Firebase is how everyone else arrives. What is pinned here is the shape of
that arrangement — what we verify, what we keep, what we refuse, and the two
things phase 1 stops storing: the emailed one-time codes, and the contact
details on a request the City already has.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "dashboard/submit.py").read_text()
LOGIN = (ROOT / "dashboard/login.html").read_text()
ADMIN = (ROOT / "dashboard/admin.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()
WORKER = (ROOT / "src/alex311/submit_worker.py").read_text()
PA = (ROOT / "src/alex311/portal_auth.py").read_text()


# ------------------------------------------------------------ verification

def test_a_token_cannot_be_verified_without_knowing_the_project(monkeypatch):
    """The audience check is what stops a token minted for some other Firebase
    project signing into this one. No project, no verification."""
    from alex311 import firebase_auth as fb
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    with pytest.raises(fb.NotConfigured):
        fb.verify_id_token("anything")
    assert fb.configured() is False
    assert fb.client_config() is None


def test_a_forged_or_expired_token_is_refused_not_trusted(monkeypatch):
    from alex311 import firebase_auth as fb
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "some-project")
    with pytest.raises(fb.BadToken):
        fb.verify_id_token("not.a.jwt")


def test_the_client_config_is_only_ever_public_values(monkeypatch):
    """A browser key belongs in the page; nothing secret does."""
    from alex311 import firebase_auth as fb
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "p")
    monkeypatch.setenv("FIREBASE_API_KEY", "AIza-public")
    cfg = fb.client_config()
    assert set(cfg) == {"apiKey", "projectId", "authDomain", "appId"}
    assert cfg["authDomain"] == "p.firebaseapp.com"


# ---------------------------------------------------------- the two doors

def test_the_password_door_is_still_open():
    """Testers were given passwords and are mid-test. Retiring that path was
    explicitly not wanted."""
    assert '<form id="form" method="post" action="/submit/login">' in LOGIN
    assert 'name="password"' in LOGIN
    assert "def login(conn, email: str, password: str" in PA


def test_the_firebase_door_mints_the_same_cookie():
    """One gate: the session a Firebase sign-in gets is the one a password
    login gets, so every downstream check is unchanged."""
    fn = API.split('@public.post("/api/session")')[1].split("@public.get")[0]
    assert "pa.sign_in_with_firebase(" in fn
    assert "resp.set_cookie(pa.SESSION_COOKIE" in fn
    assert "fb.verify_id_token(body.id_token)" in fn


def test_a_bad_token_gets_401_and_a_disabled_account_403():
    fn = API.split('@public.post("/api/session")')[1].split("@public.get")[0]
    assert 'HTTPException(401' in fn
    assert 'HTTPException(403' in fn


def test_an_unverified_email_cannot_claim_an_existing_account():
    """Linking by email is the one place the token's email is used, and only
    when Firebase vouches for it. Otherwise anyone could type a tester's
    address into a provider that does not verify and inherit their role."""
    fn = PA.split("def sign_in_with_firebase(")[1].split("\ndef ")[0]
    assert "if row is None and email and email_verified:" in fn
    assert "WHERE firebase_uid IS NULL AND email = %s" in fn


def test_a_new_firebase_account_holds_the_uid_and_nothing_else():
    fn = PA.split("def sign_in_with_firebase(")[1].split("\ndef ")[0]
    insert = fn.split("INSERT INTO portal_users")[1].split("VALUES")[0]
    assert "email" not in insert
    assert "firebase_uid" in insert and "policy_version" in insert


def test_the_login_page_shows_the_policy_beside_the_sign_in():
    assert "used in aggregate" in LOGIN
    assert "your email stays with the sign-in provider" in LOGIN
    assert "POLICY_VERSION" in API


def test_firebase_is_optional_at_runtime():
    """An image with no FIREBASE_* set is the password-only site it was."""
    assert "if (!cfg.config) return;" in LOGIN
    assert '<div id="firebase-signin" hidden>' in LOGIN


# ----------------------------------------------------------- what went away

def test_the_emailed_code_identity_system_is_gone():
    assert not (ROOT / "src/alex311/identity.py").exists()
    for route in ("/api/auth/start", "/api/auth/confirm", "/api/auth/signout", "/api/auth/me"):
        assert route not in API
    assert "alex311_submitter" not in API
    for table in ("submitter_verifications", "submitter_sessions"):
        assert f"DROP TABLE IF EXISTS {table}" in SCHEMA
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in SCHEMA


def test_the_submitter_is_the_account_so_per_submitter_limits_are_real():
    fn = API.split("def _submitter(request: Request)")[1].split("\n    @router")[0]
    assert "pa.session_user(conn, token)" in fn
    assert "user.user_id if user else None" in fn


def test_blocking_a_submitter_disables_their_account():
    fn = API.split('if action == "block_submitter":')[1].split("return {")[0]
    assert "pa.set_disabled(conn, row[\"submitter_id\"], True)" in fn
    assert "last administrator" in fn


def test_contact_details_are_purged_the_moment_a_request_is_filed():
    """They were typed into the City's form and are needed for nothing else.
    The City holds them under a case number we also hold."""
    fn = WORKER.split("def finish(")[1].split("\ndef ")[0]
    assert "contact = CASE WHEN %s = 'filed' THEN NULL ELSE contact END" in fn


# --------------------------------------------------------- the users panel

def test_the_users_panel_can_show_an_account_that_has_no_email():
    assert "u.has_password" in ADMIN and "u.firebase_linked" in ADMIN
    assert "firebase_uid IS NOT NULL AS firebase_linked" in PA


def test_the_actor_is_the_account_not_its_email():
    """Some accounts now have no email, and approved_by / moderation actor
    must still name somebody."""
    fn = API.split("def _identify(")[1].split("\ndef ")[0]
    assert "return user.user_id, user" in fn


# ------------------------------------------------- against a real database

@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
def test_linking_rules_against_postgres():
    from alex311 import db, portal_auth as pa
    with db.connect() as conn:
        conn.execute("DELETE FROM portal_sessions WHERE user_id IN "
                     "(SELECT user_id FROM portal_users WHERE email LIKE 'p1-%@test' OR firebase_uid LIKE 'p1-%')")
        conn.execute("DELETE FROM portal_users WHERE email LIKE 'p1-%@test' OR firebase_uid LIKE 'p1-%'")
        conn.commit()
        # a tester with a password, no firebase yet
        tester, _pw = pa.create_user(conn, "p1-tester@test", "user", created_by="test")
        # 1. unverified email must NOT claim the tester's account
        tok = pa.sign_in_with_firebase(conn, uid="p1-uid-a", email="p1-tester@test",
                                       email_verified=False, policy_version="t")
        who = pa.session_user(conn, tok)
        assert who.user_id != tester.user_id and who.email is None
        # 2. verified email links to the existing account instead of twinning it
        tok = pa.sign_in_with_firebase(conn, uid="p1-uid-b", email="p1-tester@test",
                                       email_verified=True, policy_version="t")
        who = pa.session_user(conn, tok)
        assert who.user_id == tester.user_id and who.firebase_uid == "p1-uid-b"
        # 3. the same uid signs straight in next time, email or not
        tok = pa.sign_in_with_firebase(conn, uid="p1-uid-b", email=None,
                                       email_verified=False, policy_version="t")
        assert pa.session_user(conn, tok).user_id == tester.user_id
        # 4. a disabled account gets no session
        pa.set_disabled(conn, tester.user_id, True)
        assert pa.sign_in_with_firebase(conn, uid="p1-uid-b", email=None,
                                        email_verified=False, policy_version="t") is None
        conn.execute("DELETE FROM portal_sessions WHERE user_id IN "
                     "(SELECT user_id FROM portal_users WHERE email LIKE 'p1-%@test' OR firebase_uid LIKE 'p1-%')")
        conn.execute("DELETE FROM portal_users WHERE email LIKE 'p1-%@test' OR firebase_uid LIKE 'p1-%'")
        conn.commit()
