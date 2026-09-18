"""Who may open the gated pages, and the session that says so.

One door: Firebase sign-in (Google, or an emailed link) in this site's own
tenant. The browser brings back an ID token, `firebase_auth` checks it, and
`sign_in_with_firebase` here turns it into an account row and a session
cookie. An administrator can invite someone ahead of time — the row exists
with a role, and the person's first sign-in with that address links to it.

The password door that served the first testers is retired (2026-09-18):
there is nothing to hand over, nothing to reset, and no hash to protect.
HTTP Basic still works alongside it, with the shared `SUBMIT_PASSWORD`, for
scripts and for a locked-out administrator to get back in.

Sessions are random tokens; only a hash of the token is stored. The last
administrator cannot be disabled or deleted.
"""
from __future__ import annotations

import argparse
import hmac
import hashlib
import logging
import os
import re
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("alex311.portal_auth")

SESSION_TTL = timedelta(days=14)
SESSION_COOKIE = "alex311_portal"
ROLES = ("admin", "user")



def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


@dataclass
class PortalUser:
    user_id: str
    email: str | None          # None for an account that only ever signed in through Firebase
    role: str
    disabled: bool = False
    firebase_uid: str | None = None
    policy_version: str | None = None
    display_name: str | None = None

    @property
    def label(self) -> str:
        """Something a person can recognize: the name they chose, else the
        email where we hold one, else a short form of the uid. We do not fetch
        the email from Firebase to show it — the uid is what we chose to keep."""
        return self.display_name or self.email or f"account {self.user_id[-6:]}"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ------------------------------------------------------------------- users

def create_user(conn, email: str, role: str = "user", *,
                created_by: str | None = None) -> PortalUser:
    """Invite someone: the account exists with a role before they arrive.

    There is no password to hand over. The person signs in with Google or an
    emailed link using this address, and `sign_in_with_firebase` links that
    sign-in to this row (a verified email is the match) instead of making a
    second account. Until then the row is an invitation, nothing more."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("that does not look like an email address")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    user_id = f"pu_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """INSERT INTO portal_users (user_id, email, role, created_by)
           VALUES (%s, %s, %s, %s)""",
        (user_id, email, role, created_by))
    conn.commit()
    return PortalUser(user_id, email, role)



def set_disabled(conn, user_id: str, disabled: bool) -> None:
    conn.execute("UPDATE portal_users SET disabled_at = %s WHERE user_id = %s",
                 (datetime.now(timezone.utc) if disabled else None, user_id))
    if disabled:
        conn.execute("UPDATE portal_sessions SET revoked_at = now() "
                     "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    conn.commit()


def set_role(conn, user_id: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    conn.execute("UPDATE portal_users SET role = %s WHERE user_id = %s", (role, user_id))
    conn.commit()


def list_users(conn) -> list[dict]:
    return conn.execute(
        """SELECT user_id, email, role, created_at, created_by, last_login_at, disabled_at,
                  firebase_uid IS NOT NULL AS firebase_linked, policy_version, display_name
             FROM portal_users ORDER BY created_at""").fetchall()


def count_admins(conn, *, excluding: str | None = None) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM portal_users "
        "WHERE role = 'admin' AND disabled_at IS NULL AND user_id <> COALESCE(%s, '')",
        (excluding,)).fetchone()["n"]


# ---------------------------------------------------------------- sessions


def sign_in_with_firebase(conn, *, uid: str, email: str | None, email_verified: bool,
                          policy_version: str, user_agent: str | None = None) -> str | None:
    """Turn a verified Firebase identity into a session. Returns the token,
    or None if the account is disabled.

    Three cases, in order:
      1. an account already linked to this uid — sign it in;
      2. a tester account with a matching, verified email — link the uid to it
         so they keep their role and history rather than gaining a twin;
      3. nobody — create an account holding the uid and nothing else.

    Case 2 is the only place the email from the token is used, and only when
    Firebase says it is verified: an unverified address must not be able to
    claim an existing account.
    """
    row = conn.execute("SELECT user_id, disabled_at FROM portal_users WHERE firebase_uid = %s",
                       (uid,)).fetchone()
    if row is None and email and email_verified:
        row = conn.execute(
            "SELECT user_id, disabled_at FROM portal_users "
            "WHERE firebase_uid IS NULL AND email = %s", (normalize_email(email),)).fetchone()
        if row is not None:
            conn.execute("UPDATE portal_users SET firebase_uid = %s WHERE user_id = %s",
                         (uid, row["user_id"]))
    if row is None:
        user_id = "pu_" + secrets.token_hex(8)
        conn.execute(
            """INSERT INTO portal_users (user_id, firebase_uid, role, created_by, policy_version)
               VALUES (%s, %s, 'user', 'firebase', %s)""",
            (user_id, uid, policy_version))
        row = {"user_id": user_id, "disabled_at": None}
    if row["disabled_at"] is not None:
        conn.commit()
        return None
    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO portal_sessions (token_hash, user_id, expires_at, user_agent)
           VALUES (%s, %s, now() + %s, %s)""",
        (_token_hash(token), row["user_id"], SESSION_TTL, (user_agent or "")[:200]))
    conn.execute(
        "UPDATE portal_users SET last_login_at = now(), policy_version = %s WHERE user_id = %s",
        (policy_version, row["user_id"]))
    conn.commit()
    return token


def session_user(conn, token: str | None) -> PortalUser | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.user_id, u.email, u.role, u.disabled_at, u.firebase_uid,
                  u.policy_version, u.display_name
             FROM portal_sessions s JOIN portal_users u USING (user_id)
            WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
        (_token_hash(token),)).fetchone()
    if row is None or row["disabled_at"] is not None:
        return None
    return PortalUser(row["user_id"], row["email"], row["role"],
                      firebase_uid=row.get("firebase_uid"),
                      policy_version=row.get("policy_version"),
                      display_name=row.get("display_name"))


NAME_MAX = 40
_NAME_OK = re.compile(r"^[\w .'’\-]+$", re.UNICODE)


class BadName(ValueError):
    pass


def clean_display_name(raw: str | None) -> str | None:
    """A name as a person would write it for themselves: trimmed, single
    spaces, at most NAME_MAX characters of letters, digits, spaces and
    period, apostrophe, hyphen. Empty means "no name". Anything else is
    refused rather than quietly reshaped — it is their name."""
    name = " ".join((raw or "").split())
    if not name:
        return None
    if len(name) > NAME_MAX:
        raise BadName(f"keep it to {NAME_MAX} characters")
    if not _NAME_OK.match(name):
        raise BadName("letters, digits, spaces, periods, apostrophes and hyphens only")
    return name


def set_display_name(conn, user_id: str, name: str | None) -> None:
    conn.execute("UPDATE portal_users SET display_name = %s WHERE user_id = %s", (name, user_id))
    conn.commit()


def logout_all(conn, user_id: str) -> int:
    """Revoke every live session of one account — "sign out everywhere"."""
    n = conn.execute("UPDATE portal_sessions SET revoked_at = now() "
                     "WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()",
                     (user_id,)).rowcount
    conn.commit()
    return n


def logout(conn, token: str) -> None:
    conn.execute("UPDATE portal_sessions SET revoked_at = now() "
                 "WHERE token_hash = %s AND revoked_at IS NULL", (_token_hash(token),))
    conn.commit()


# --------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(prog="alex311.portal_auth",
                                description="Manage who can open the gated prototype.")
    sub = p.add_subparsers(dest="cmd", required=True)

    seed = sub.add_parser("seed", help="invite the first admin if there is none")
    seed.add_argument("--email", required=True)

    add = sub.add_parser("add", help="invite a user by email")
    add.add_argument("--email", required=True)
    add.add_argument("--role", default="user", choices=ROLES)

    sub.add_parser("list", help="list users")
    a = p.parse_args(argv)

    conn = db.connect()
    if a.cmd == "list":
        for u in list_users(conn):
            state = "disabled" if u["disabled_at"] else "active"
            seen = u["last_login_at"].strftime("%Y-%m-%d") if u["last_login_at"] else "never"
            print(f"  {u['email']:34} {u['role']:6} {state:9} last login {seen}")
        return 0

    if a.cmd == "seed":
        if count_admins(conn):
            print("an admin already exists; nothing to do")
            return 0
        user = create_user(conn, a.email, "admin", created_by="seed")
        print(f"invited admin {user.email}: they sign in with Google or an emailed link "
              "using that address, and the account links itself")
        return 0

    if a.cmd == "add":
        user = create_user(conn, a.email, a.role, created_by="cli")
        print(f"invited {a.role} {user.email}: they sign in with Google or an emailed link "
              "using that address")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
