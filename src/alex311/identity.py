"""Verified submitter identity for the gated submission layer.

The anti-abuse policy has per-submitter rules that mean nothing without a
submitter, and the official portal offers no identity lever at all — its
contact step is optional, so everything in §3 of the research doc was possible
anonymously. This module is the missing half: prove control of an email
address, then carry an opaque submitter id.

Deliberate choices:

* **Opaque ids.** `submitter_id` is a random token, not the email. It is what
  the abuse tables, the audit trail and the logs carry, so none of them
  accumulate mailbox addresses.
* **Canonical emails.** `a+1@gmail.com` and `a+2@gmail.com` are one mailbox.
  Without folding those together, per-submitter limits are defeated by typing a
  different tag — which would make the whole identity exercise decorative.
* **Nothing secret at rest.** Verification codes and session tokens are stored
  as salted hashes; the plaintext exists only in the email and the cookie.
* **No new dependency.** Codes go out through a pluggable sender: a console
  sender for development and demos, SMTP for anything real. The provider is a
  config decision, not a code one.

More friction than the City's anonymous flow, on purpose. A resident who will
not identify can still be handed off to the official portal.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol

log = logging.getLogger("alex311.identity")

CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5
#: how many codes one mailbox may request in an hour
MAX_CODES_PER_HOUR = 5
SESSION_TTL = timedelta(days=30)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

#: Providers that ignore dots in the local part. Folding them matters for the
#: same reason plus-tags do: one mailbox should be one submitter.
_DOT_BLIND = {"gmail.com", "googlemail.com"}


class InvalidEmail(ValueError):
    pass


class VerificationFailed(Exception):
    """Wrong code, expired challenge, or too many attempts.

    One exception for all three on purpose: telling a caller which one it was
    tells an attacker which one it was.
    """


def normalize_email(raw: str) -> str:
    """Canonical form of a mailbox, for identity and for rate limiting.

    Lowercases, strips a `+tag`, and removes dots in the local part for
    providers that ignore them. `A.User+311@Gmail.com` and `auser@gmail.com`
    are the same person, and per-submitter limits have to agree.
    """
    email = (raw or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise InvalidEmail("that does not look like an email address")
    local, _, domain = email.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _DOT_BLIND:
        local = local.replace(".", "")
    if not local:
        raise InvalidEmail("that does not look like an email address")
    return f"{local}@{domain}"


def _pepper() -> bytes:
    """Server-side secret mixed into every stored hash.

    Without it, a leaked table of six-digit code hashes is trivially reversed.
    In production this must be set; in development a per-process value keeps
    things working while making it obvious that codes do not survive a restart.
    """
    value = os.environ.get("SUBMIT_SECRET")
    if value:
        return value.encode()
    global _EPHEMERAL
    try:
        return _EPHEMERAL
    except NameError:
        _EPHEMERAL = secrets.token_bytes(32)
        log.warning("SUBMIT_SECRET is not set; using an ephemeral one. "
                    "Codes and sessions will not survive a restart.")
        return _EPHEMERAL


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    h.update(_pepper())
    for p in parts:
        h.update(b"\x00")
        h.update(p.encode())
    return h.hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_submitter_id() -> str:
    return f"sub_{uuid.uuid4().hex}"


# ------------------------------------------------------------------ senders

class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


@dataclass
class ConsoleSender:
    """Writes the message to the log instead of sending it.

    The default, and what the demo runs on: it needs no provider account and
    keeps a walkthrough entirely self-contained. Obviously unsuitable for real
    residents, which is why `sender_from_env` refuses it when SMTP is
    configured and warns when it is not.
    """
    sink: list = None

    def send(self, to: str, subject: str, body: str) -> None:
        if self.sink is not None:
            self.sink.append({"to": to, "subject": subject, "body": body})
        log.warning("[console email] to=%s subject=%s\n%s", to, subject, body)


@dataclass
class SmtpSender:
    """Any provider that speaks SMTP: SES, SendGrid, Postmark, a plain mailbox.

    Chosen over a vendor SDK so switching providers is an environment change
    rather than a code change.
    """
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    sender: str = "no-reply@example.invalid"
    use_tls: bool = True

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            if self.use_tls:
                s.starttls()
            if self.username:
                s.login(self.username, self.password or "")
            s.send_message(msg)


def sender_from_env() -> EmailSender:
    host = os.environ.get("SUBMIT_SMTP_HOST")
    if not host:
        log.warning("no SUBMIT_SMTP_HOST; verification codes will be logged, not emailed")
        return ConsoleSender()
    return SmtpSender(
        host=host, port=int(os.environ.get("SUBMIT_SMTP_PORT", "587")),
        username=os.environ.get("SUBMIT_SMTP_USER"),
        password=os.environ.get("SUBMIT_SMTP_PASSWORD"),
        sender=os.environ.get("SUBMIT_MAIL_FROM", "no-reply@example.invalid"),
    )


# ------------------------------------------------------------------- flows

@dataclass
class Session:
    token: str            # only ever returned once, to the client
    submitter_id: str
    expires_at: datetime


def start_verification(conn, raw_email: str, sender: EmailSender, *,
                       now: datetime | None = None) -> None:
    """Issue a one-time code to an email address.

    Returns nothing and raises nothing for an unknown address: the caller must
    respond identically either way, or this becomes a way to test whether an
    address has an account.
    """
    now = now or datetime.now(timezone.utc)
    email = normalize_email(raw_email)

    recent = conn.execute(
        """SELECT count(*) AS n FROM submitter_verifications
            WHERE email = %s AND created_at > %s""",
        (email, now - timedelta(hours=1)),
    ).fetchone()["n"]
    if recent >= MAX_CODES_PER_HOUR:
        log.warning("verification rate limit hit for a mailbox")
        return                      # silently; the caller's reply is unchanged

    code = new_code()
    conn.execute(
        """INSERT INTO submitter_verifications (email, code_hash, created_at, expires_at)
           VALUES (%s, %s, %s, %s)""",
        (email, _hash(email, code), now, now + CODE_TTL),
    )
    conn.commit()
    sender.send(
        raw_email.strip(),
        "Your Alex311 verification code",
        f"Your code is {code}\n\n"
        f"It expires in {int(CODE_TTL.total_seconds() // 60)} minutes.\n\n"
        "This is an unofficial Alex311 tool, not a City of Alexandria service.\n"
        "If you did not ask for this code, ignore this message.",
    )


def confirm_verification(conn, raw_email: str, code: str, *,
                         now: datetime | None = None,
                         user_agent: str | None = None) -> Session:
    """Check a code and return a session. Raises VerificationFailed otherwise."""
    now = now or datetime.now(timezone.utc)
    email = normalize_email(raw_email)

    row = conn.execute(
        """SELECT verification_id, code_hash, attempts FROM submitter_verifications
            WHERE email = %s AND consumed_at IS NULL AND expires_at > %s
            ORDER BY created_at DESC LIMIT 1""",
        (email, now),
    ).fetchone()
    if row is None:
        raise VerificationFailed("that code is not valid")
    if row["attempts"] >= MAX_CODE_ATTEMPTS:
        raise VerificationFailed("that code is not valid")

    if not hmac.compare_digest(row["code_hash"], _hash(email, (code or "").strip())):
        conn.execute(
            "UPDATE submitter_verifications SET attempts = attempts + 1 WHERE verification_id = %s",
            (row["verification_id"],))
        conn.commit()
        raise VerificationFailed("that code is not valid")

    conn.execute("UPDATE submitter_verifications SET consumed_at = %s WHERE verification_id = %s",
                 (now, row["verification_id"]))

    submitter_id = _upsert_submitter(conn, email, now)
    token = new_token()
    expires = now + SESSION_TTL
    conn.execute(
        """INSERT INTO submitter_sessions (token_hash, submitter_id, created_at, expires_at, user_agent)
           VALUES (%s, %s, %s, %s, %s)""",
        (_hash(token), submitter_id, now, expires, (user_agent or "")[:200]))
    conn.commit()
    return Session(token=token, submitter_id=submitter_id, expires_at=expires)


def _upsert_submitter(conn, email: str, now: datetime) -> str:
    row = conn.execute("SELECT submitter_id, blocked_at FROM submitters WHERE email = %s",
                       (email,)).fetchone()
    if row:
        conn.execute("UPDATE submitters SET verified_at = %s WHERE submitter_id = %s",
                     (now, row["submitter_id"]))
        return row["submitter_id"]
    submitter_id = new_submitter_id()
    conn.execute(
        """INSERT INTO submitters (submitter_id, email, verified_at, created_at)
           VALUES (%s, %s, %s, %s)""",
        (submitter_id, email, now, now))
    return submitter_id


def session_submitter(conn, token: str | None, *, now: datetime | None = None) -> str | None:
    """The submitter behind a session token, or None.

    Also returns None for a blocked submitter, so a block takes effect on the
    next request rather than at the next sign-in.
    """
    if not token:
        return None
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        """SELECT s.submitter_id, m.blocked_at
             FROM submitter_sessions s
             JOIN submitters m USING (submitter_id)
            WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > %s""",
        (_hash(token), now),
    ).fetchone()
    if row is None or row["blocked_at"] is not None:
        return None
    return row["submitter_id"]


def revoke_session(conn, token: str, *, now: datetime | None = None) -> None:
    conn.execute(
        "UPDATE submitter_sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
        (now or datetime.now(timezone.utc), _hash(token)))
    conn.commit()


def block_submitter(conn, submitter_id: str, reason: str, *,
                    now: datetime | None = None) -> None:
    """Block a submitter and drop their sessions."""
    now = now or datetime.now(timezone.utc)
    conn.execute("UPDATE submitters SET blocked_at = %s, blocked_reason = %s WHERE submitter_id = %s",
                 (now, reason, submitter_id))
    conn.execute("UPDATE submitter_sessions SET revoked_at = %s "
                 "WHERE submitter_id = %s AND revoked_at IS NULL", (now, submitter_id))
    conn.commit()


__all__ = ["normalize_email", "InvalidEmail", "VerificationFailed", "Session",
           "EmailSender", "ConsoleSender", "SmtpSender", "sender_from_env",
           "start_verification", "confirm_verification", "session_submitter",
           "revoke_session", "block_submitter",
           "CODE_TTL", "SESSION_TTL", "MAX_CODE_ATTEMPTS", "MAX_CODES_PER_HOUR"]
