"""A six-digit code in the sign-in email, for when the link is on the wrong device.

The link signs you in where you tap it. Read the email on a laptop while
signing in on a phone and there is nothing to tap — so the same email
carries a short code, and the app (or the sign-in page) trades the code for
the link's one-time `oobCode` and redeems that with Firebase exactly as it
would the link.

What is kept, and how, is the point of this module:

  * No email address. The row is keyed by an HMAC of it.
  * No code. Only an HMAC of (address, code) to compare against.
  * No readable `oobCode`. It is encrypted under a key derived from the
    server's secret *and the code itself*, so the stored value is useless
    without both — a copy of the table alone opens nothing, and neither
    does the secret alone.
  * Fifteen minutes. Five wrong tries burn it. One use. A new link for the
    same address replaces it.

Errors follow the contract with the app: a wrong code (or an address that
never asked — indistinguishable on purpose) is WrongCode; expired or
already used is Gone; burned by wrong tries is TooMany.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

TTL = timedelta(minutes=15)
MAX_WRONG = 5


class NotConfigured(RuntimeError):
    pass


class WrongCode(ValueError):
    """Not the code — or no code was ever issued for that address."""


class Gone(ValueError):
    """Expired, or already used."""


class TooMany(ValueError):
    """Burned by wrong tries."""


def _secret() -> bytes:
    s = os.environ.get("DIGEST_SECRET") or os.environ.get("SUBMIT_SECRET")
    if not s:
        raise NotConfigured("no signing secret is set")
    return s.encode()


def available() -> bool:
    return bool(os.environ.get("DIGEST_SECRET") or os.environ.get("SUBMIT_SECRET"))


def _norm(email: str) -> str:
    return " ".join(email.split()).lower()


def _mac(label: str, *parts: str) -> bytes:
    return hmac.new(_secret(), "|".join((label,) + parts).encode(), hashlib.sha256).digest()


def email_key(email: str) -> str:
    return _mac("signin-email", _norm(email)).hex()


def code_hash(email: str, code: str) -> str:
    return _mac("signin-code", _norm(email), code).hex()


def _fernet(email: str, code: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(_mac("signin-oob-key", _norm(email), code)))


def digits(code: str) -> str:
    """What a person typed, down to its digits: '482 913', '482-913', '482913'."""
    return "".join(ch for ch in (code or "") if ch.isdigit())


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def pretty(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def issue(conn: psycopg.Connection, *, email: str, oob_code: str,
          now: datetime | None = None) -> str:
    """Store a fresh code for this address, replacing any before it, and
    return it — the only moment it exists in the clear, on its way into the
    email."""
    now = now or datetime.now(timezone.utc)
    code = new_code()
    sealed = _fernet(email, code).encrypt(oob_code.encode()).decode()
    conn.execute(
        """INSERT INTO signin_codes (email_key, code_hash, oob_sealed, expires_at, wrong, used_at)
           VALUES (%s, %s, %s, %s, 0, NULL)
           ON CONFLICT (email_key) DO UPDATE
             SET code_hash = EXCLUDED.code_hash, oob_sealed = EXCLUDED.oob_sealed,
                 expires_at = EXCLUDED.expires_at, wrong = 0, used_at = NULL, created_at = now()""",
        (email_key(email), code_hash(email, code), sealed, now + TTL))
    # yesterday's rows are of no use to anyone
    conn.execute("DELETE FROM signin_codes WHERE expires_at < %s", (now - timedelta(days=1),))
    conn.commit()
    return code


def redeem(conn: psycopg.Connection, *, email: str, code: str,
           now: datetime | None = None) -> str:
    """The link's oobCode for the right code, once. Raises WrongCode, Gone or TooMany."""
    now = now or datetime.now(timezone.utc)
    code = digits(code)
    key = email_key(email)
    row = conn.execute(
        "SELECT code_hash, oob_sealed, expires_at, wrong, used_at FROM signin_codes "
        "WHERE email_key = %s FOR UPDATE", (key,)).fetchone()
    if row is None:
        conn.rollback()
        # spend the same work a real comparison would, and say the same thing
        hmac.compare_digest(code_hash(email, code), code_hash(email, "000000"))
        raise WrongCode("that code is not right")
    if row["wrong"] >= MAX_WRONG:
        conn.rollback()
        raise TooMany("too many wrong tries; ask for a new link")
    if row["used_at"] is not None or row["expires_at"] < now:
        conn.rollback()
        raise Gone("that code has expired or was already used; ask for a new link")
    if len(code) != 6 or not hmac.compare_digest(row["code_hash"], code_hash(email, code)):
        conn.execute("UPDATE signin_codes SET wrong = wrong + 1 WHERE email_key = %s", (key,))
        conn.commit()
        if row["wrong"] + 1 >= MAX_WRONG:
            raise TooMany("too many wrong tries; ask for a new link")
        raise WrongCode("that code is not right")
    oob = _fernet(email, code).decrypt(row["oob_sealed"].encode()).decode()
    # single use: the sealed value goes too, so nothing redeemable is left behind
    conn.execute("UPDATE signin_codes SET used_at = %s, oob_sealed = '' WHERE email_key = %s",
                 (now, key))
    conn.commit()
    return oob
