"""Who may open the gated prototype at all.

Not to be confused with `alex311.identity`, which answers "which *resident* is
submitting this request". This answers "is this person allowed in the building",
and it exists because the browser's HTTP Basic popup is a poor front door: it
cannot be styled, cannot say what the site is, cannot be logged out of, and
offers one shared password for everybody.

The bar here is deliberately modest — keeping out passers-by, not defeating a
determined attacker. What it does not do is store passwords in the clear, which
would be a real liability for no saving: people reuse passwords, and these are
real email addresses.

    scrypt, per-user salt, stdlib only, no new dependency.

HTTP Basic still works alongside it, with the shared `SUBMIT_PASSWORD`. Scripts,
`curl` in the runbook and the CLI all use that path; people use the login page.

    python -m alex311.portal_auth seed --email you@example.com   # first admin
    python -m alex311.portal_auth add --email them@example.com --role user
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
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

#: scrypt parameters. Comfortably slow for a login form, nowhere near the cost
#: of a password-cracking-resistant setting, which is the right trade for a
#: gate whose job is keeping out passers-by.
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Returns (hash, salt). Never store or log the password itself."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(),
                            n=_N, r=_R, p=_P, dklen=32)
    return digest.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def new_password(words: int = 4) -> str:
    """A password a person can read out loud once and then paste."""
    # no l/1 and no o/0: these get read aloud and typed by hand
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5))
                    for _ in range(words))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


@dataclass
class PortalUser:
    user_id: str
    email: str
    role: str
    disabled: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ------------------------------------------------------------------- users

def create_user(conn, email: str, role: str = "user", *, password: str | None = None,
                created_by: str | None = None) -> tuple[PortalUser, str]:
    """Add a user. Returns the user and their password, shown once."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("that does not look like an email address")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    password = password or new_password()
    digest, salt = hash_password(password)
    user_id = f"pu_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """INSERT INTO portal_users (user_id, email, password_hash, salt, role, created_by)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (user_id, email, digest, salt, role, created_by))
    conn.commit()
    return PortalUser(user_id, email, role), password


def set_password(conn, user_id: str, password: str | None = None) -> str:
    password = password or new_password()
    digest, salt = hash_password(password)
    conn.execute("UPDATE portal_users SET password_hash = %s, salt = %s WHERE user_id = %s",
                 (digest, salt, user_id))
    # a new password ends every session that used the old one
    conn.execute("UPDATE portal_sessions SET revoked_at = now() "
                 "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
    conn.commit()
    return password


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
        """SELECT user_id, email, role, created_at, created_by, last_login_at, disabled_at
             FROM portal_users ORDER BY email""").fetchall()


def count_admins(conn, *, excluding: str | None = None) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM portal_users "
        "WHERE role = 'admin' AND disabled_at IS NULL AND user_id <> COALESCE(%s, '')",
        (excluding,)).fetchone()["n"]


# ---------------------------------------------------------------- sessions

def login(conn, email: str, password: str, *, user_agent: str | None = None) -> str | None:
    """Check a password and open a session. Returns the token, or None."""
    row = conn.execute(
        "SELECT user_id, password_hash, salt, disabled_at FROM portal_users WHERE email = %s",
        (normalize_email(email),)).fetchone()
    if row is None:
        # spend the time anyway, so a missing account is not faster than a wrong password
        hash_password(password)
        return None
    if row["disabled_at"] is not None:
        return None
    if not verify_password(password, row["password_hash"], row["salt"]):
        return None

    token = secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO portal_sessions (token_hash, user_id, expires_at, user_agent)
           VALUES (%s, %s, %s, %s)""",
        (_token_hash(token), row["user_id"],
         datetime.now(timezone.utc) + SESSION_TTL, (user_agent or "")[:200]))
    conn.execute("UPDATE portal_users SET last_login_at = now() WHERE user_id = %s",
                 (row["user_id"],))
    conn.commit()
    return token


def session_user(conn, token: str | None) -> PortalUser | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.user_id, u.email, u.role, u.disabled_at
             FROM portal_sessions s JOIN portal_users u USING (user_id)
            WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
        (_token_hash(token),)).fetchone()
    if row is None or row["disabled_at"] is not None:
        return None
    return PortalUser(row["user_id"], row["email"], row["role"])


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

    seed = sub.add_parser("seed", help="create the first admin if there is none")
    seed.add_argument("--email", required=True)

    add = sub.add_parser("add", help="add a user")
    add.add_argument("--email", required=True)
    add.add_argument("--role", default="user", choices=ROLES)

    reset = sub.add_parser("reset", help="give a user a new password")
    reset.add_argument("--email", required=True)

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
        user, password = create_user(conn, a.email, "admin", created_by="seed")
        print(f"created admin {user.email}")
        print(f"password: {password}")
        print("This is shown once. Store it somewhere safe.")
        return 0

    if a.cmd == "add":
        user, password = create_user(conn, a.email, a.role, created_by="cli")
        print(f"created {a.role} {user.email}")
        print(f"password: {password}")
        return 0

    if a.cmd == "reset":
        row = conn.execute("SELECT user_id FROM portal_users WHERE email = %s",
                           (normalize_email(a.email),)).fetchone()
        if not row:
            print("no such user")
            return 1
        print(f"password: {set_password(conn, row['user_id'])}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
