"""The daily digest: what's new for your watches, by email, if you asked.

Opt-in, per account, to an address the person confirmed. Once a day a job
runs the same feed the My requests page shows — everything the mirror has
seen or seen change that matches the account's watches — over the window
since the last digest, and sends one email if there is anything in it.
Nothing to say, nothing sent. The email's mark (`digest_sent_at`) is kept
apart from the page's (`feed_seen_at`), so reading one never silences the
other.

Addresses: we hold an email only for accounts that arrived by an emailed
link or an invitation, and never fetch one from Firebase. So the digest has
its own address, typed by the person and confirmed by a link — except when
it is the very address they already sign in with, which Firebase verified.
Every digest carries an unsubscribe link that needs no sign-in. Three
failed sends in a row switch the digest off rather than retrying forever.

Links are signed, not stored: an HMAC over (purpose, user, email, expiry)
with the site's secret, so a confirmation cannot be forged for someone
else's inbox and an unsubscribe link works from any mail client.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg

from . import db, mail, watches

log = logging.getLogger("alex311.digest")

CONFIRM_TTL = timedelta(hours=48)
MAX_FAILURES = 3
MAX_ITEMS_IN_MAIL = 40


class NotConfigured(RuntimeError):
    pass


class BadToken(ValueError):
    pass


# ----------------------------------------------------------------- tokens

def _secret() -> bytes:
    s = os.environ.get("DIGEST_SECRET") or os.environ.get("SUBMIT_SECRET")
    if not s:
        raise NotConfigured("DIGEST_SECRET (or SUBMIT_SECRET) is not set")
    return s.encode()


def make_token(purpose: str, user_id: str, email: str, expires: datetime | None) -> str:
    exp = str(int(expires.timestamp())) if expires else "0"
    body = f"{purpose}|{user_id}|{email.lower()}|{exp}".encode()
    sig = hmac.new(_secret(), body, hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(body + b"|" + sig.encode()).decode().rstrip("=")


def read_token(token: str, purpose: str) -> tuple[str, str]:
    """(user_id, email) from a token, or BadToken. Constant-time on the MAC."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body, sig = raw.rsplit(b"|", 1)              # the hex signature holds no '|'
        p, user_id, email, exp = body.decode().split("|")
    except Exception:
        raise BadToken("that link is not one of ours")
    want = hmac.new(_secret(), body, hashlib.sha256).hexdigest()[:40].encode()
    if not hmac.compare_digest(sig, want) or p != purpose:
        raise BadToken("that link is not one of ours")
    if exp != "0" and int(exp) < time.time():
        raise BadToken("that link has expired; ask for a new one")
    return user_id, email


# ----------------------------------------------------------------- state

def state(conn: psycopg.Connection, *, user_id: str) -> dict:
    row = conn.execute(
        """SELECT digest_email, digest_confirmed_at, digest_sent_at, digest_failures,
                  email, firebase_uid IS NOT NULL AS linked
             FROM portal_users WHERE user_id = %s""", (user_id,)).fetchone()
    if not row:
        return {"email": None, "status": "off"}
    if row["digest_email"] and row["digest_confirmed_at"]:
        st = "on"
    elif row["digest_email"]:
        st = "pending"
    else:
        st = "off"
    return {"email": row["digest_email"], "status": st,
            "confirmed_at": row["digest_confirmed_at"], "last_sent_at": row["digest_sent_at"],
            "account_email": row["email"] if row["linked"] else None}


def request(conn: psycopg.Connection, *, user_id: str, email: str, site_origin: str) -> dict:
    """Turn the digest on for `email`: immediately if it is the verified
    address the account signs in with, otherwise after a confirmation link."""
    email = " ".join(email.split()).lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("that does not look like an email address")
    if not mail.configured():
        raise NotConfigured("this site cannot send email")
    st = state(conn, user_id=user_id)
    if st.get("account_email") and st["account_email"].lower() == email:
        conn.execute(
            """UPDATE portal_users SET digest_email = %s, digest_confirmed_at = now(),
                      digest_failures = 0 WHERE user_id = %s""", (email, user_id))
        conn.commit()
        return {"status": "on", "email": email}
    conn.execute(
        """UPDATE portal_users SET digest_email = %s, digest_confirmed_at = NULL,
                  digest_failures = 0 WHERE user_id = %s""", (email, user_id))
    conn.commit()
    token = make_token("confirm", user_id, email, datetime.now(timezone.utc) + CONFIRM_TTL)
    link = f"{site_origin}/submit/digest/confirm?t={token}"
    subject, text, html = confirm_email(link, site_origin)
    mail.send(email, subject, text, html)
    return {"status": "pending", "email": email}


def confirm(conn: psycopg.Connection, token: str) -> str:
    user_id, email = read_token(token, "confirm")
    n = conn.execute(
        """UPDATE portal_users SET digest_confirmed_at = now(), digest_failures = 0
            WHERE user_id = %s AND lower(digest_email) = %s""", (user_id, email)).rowcount
    conn.commit()
    if not n:
        raise BadToken("that address is no longer the one on the account")
    return email


def unsubscribe(conn: psycopg.Connection, token: str) -> str:
    user_id, email = read_token(token, "unsubscribe")
    conn.execute(
        """UPDATE portal_users SET digest_email = NULL, digest_confirmed_at = NULL
            WHERE user_id = %s AND lower(digest_email) = %s""", (user_id, email))
    conn.commit()
    return email


def turn_off(conn: psycopg.Connection, *, user_id: str) -> None:
    conn.execute("UPDATE portal_users SET digest_email = NULL, digest_confirmed_at = NULL "
                 "WHERE user_id = %s", (user_id,))
    conn.commit()


# ----------------------------------------------------------------- the email

def _style() -> str:
    return "font:15px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2733;max-width:600px"


def confirm_email(link: str, site_origin: str) -> tuple[str, str, str]:
    host = site_origin.split("//", 1)[-1]
    subject = "Confirm your daily digest from Alex311 Reborn"
    text = f"""Hello,

Someone - most likely you - asked Alex311 Reborn ({host}) to send a daily
email of what's new for the request types, addresses and areas they watch,
to this address. To confirm, open:

{link}

The link works for two days. If you did not ask for this, ignore this email
and nothing will be sent.

Alex311 Reborn is an unofficial mirror of the City of Alexandria's 311
service requests, not the City.

{site_origin}
"""
    html = f"""<div style="{_style()}">
<p>Hello,</p>
<p>Someone &mdash; most likely you &mdash; asked <b>Alex311 Reborn</b> ({host}) to send a daily
email of what's new for the request types, addresses and areas they watch, to this address.</p>
<p style="margin:20px 0"><a href="{link}" style="background:#1d4ed8;color:#fff;text-decoration:none;
padding:10px 18px;border-radius:8px;font-weight:600;display:inline-block">Yes, send me the digest</a></p>
<p style="font-size:13px;color:#5c6675">Or copy this address into your browser:<br>
<a href="{link}" style="color:#1d4ed8;word-break:break-all">{link}</a></p>
<p>The link works for two days. If you did not ask for this, ignore this email and nothing will be sent.</p>
<p style="font-size:13px;color:#5c6675;border-top:1px solid #e2e8f0;padding-top:12px;margin-top:20px">
Alex311 Reborn is an unofficial mirror of the City of Alexandria's 311 service requests, not the City.
<a href="{site_origin}" style="color:#1d4ed8">{host}</a></p></div>"""
    return subject, text, html


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def digest_email(feed: dict, site_origin: str, unsubscribe_link: str,
                 since: datetime) -> tuple[str, str, str]:
    """One email for one account's feed. Grouped by watch: a one-line count,
    then the cases, each linking to its record page here."""
    host = site_origin.split("//", 1)[-1]
    by_watch: dict[int, list[dict]] = {w["watch_id"]: [] for w in feed["watches"]}
    for item in feed["items"]:
        for wid in item["watch_ids"] or []:
            by_watch.setdefault(wid, []).append(item)
    labels = {w["watch_id"]: w["label"] for w in feed["watches"]}
    n_new = sum(1 for i in feed["items"] if i["is_new"])
    n_chg = len(feed["items"]) - n_new
    subject = f"Alex311 Reborn: {n_new} new, {n_chg} changed for what you watch"
    when = since.astimezone(timezone.utc).strftime("%b %d")

    text_parts = [f"What's new since {when} for the things you watch on Alex311 Reborn ({host}).\n"]
    html_parts = [f"<p>What's new since {when} for the things you watch on <b>Alex311 Reborn</b> ({host}).</p>"]
    for wid, items in by_watch.items():
        if not items:
            continue
        new = sum(1 for i in items if i["is_new"])
        head = f"{labels.get(wid, 'a watch')} — {new} new, {len(items) - new} changed"
        text_parts.append(head)
        html_parts.append(f'<h3 style="font-size:15px;margin:18px 0 6px">{_esc(head)}</h3><ul style="padding-left:18px;margin:0">')
        for i in items[:MAX_ITEMS_IN_MAIL]:
            url = f"{site_origin}/r/{i['service_request_id']}"
            tag = "new" if i["is_new"] else "changed"
            line = (f"{i['service_request_id']} · {i['service_name'] or 'Request'} · "
                    f"{i['address'] or 'no address'} · {(i['status'] or 'unknown').lower()} · {tag}")
            text_parts.append(f"  - {line}\n    {url}")
            html_parts.append(
                f'<li style="margin:3px 0"><a href="{url}" style="color:#1d4ed8;font-weight:600">'
                f'{_esc(i["service_request_id"])}</a> &middot; {_esc(i["service_name"] or "Request")} &middot; '
                f'{_esc(i["address"] or "no address")} &middot; {_esc((i["status"] or "unknown").lower())} '
                f'&middot; <span style="color:#5c6675">{tag}</span></li>')
        if len(items) > MAX_ITEMS_IN_MAIL:
            more = f"and {len(items) - MAX_ITEMS_IN_MAIL} more on the site"
            text_parts.append(f"  {more}")
            html_parts.append(f'<li style="color:#5c6675">{more}</li>')
        html_parts.append("</ul>")
        text_parts.append("")
    foot_text = (f"See it all under My requests: {site_origin}/submit/my\n\n"
                 f"You asked for this daily email. Stop it any time: {unsubscribe_link}\n"
                 f"Alex311 Reborn is an unofficial mirror of the City of Alexandria's 311 requests, not the City.")
    foot_html = (f'<p style="margin-top:18px"><a href="{site_origin}/submit/my" style="color:#1d4ed8">See it all under My requests</a></p>'
                 f'<p style="font-size:13px;color:#5c6675;border-top:1px solid #e2e8f0;padding-top:12px;margin-top:20px">'
                 f'You asked for this daily email. <a href="{unsubscribe_link}" style="color:#1d4ed8">Stop it any time</a>. '
                 f'Alex311 Reborn is an unofficial mirror of the City of Alexandria\'s 311 requests, not the City.</p>')
    text = "\n".join(text_parts) + "\n" + foot_text + "\n"
    html = f'<div style="{_style()}">' + "".join(html_parts) + foot_html + "</div>"
    return subject, text, html


# ----------------------------------------------------------------- the run

def run(conn: psycopg.Connection, *, site_origin: str, now: datetime | None = None,
        send=None) -> dict:
    """One pass over every confirmed account. Returns counts. `send` is
    mail.send unless a test says otherwise."""
    now = now or datetime.now(timezone.utc)
    send = send or mail.send
    conn.execute("SELECT set_config('app.role', 'admin', false)")   # the job reads every watch
    people = conn.execute(
        """SELECT user_id, digest_email, digest_confirmed_at, digest_sent_at, digest_failures
             FROM portal_users
            WHERE digest_email IS NOT NULL AND digest_confirmed_at IS NOT NULL
              AND disabled_at IS NULL ORDER BY user_id""").fetchall()
    counts = {"accounts": len(people), "sent": 0, "empty": 0, "failed": 0, "disabled": 0}
    for p in people:
        since = p["digest_sent_at"] or p["digest_confirmed_at"]
        feed = watches.feed(conn, user_id=p["user_id"], since=since)
        conn.execute("SELECT set_config('app.role', 'admin', false)")   # feed() rolled it back
        if not feed["items"]:
            counts["empty"] += 1
            conn.execute("UPDATE portal_users SET digest_sent_at = %s WHERE user_id = %s",
                         (now, p["user_id"]))
            conn.commit()
            continue
        unsub = f"{site_origin}/submit/digest/unsubscribe?t=" + \
            make_token("unsubscribe", p["user_id"], p["digest_email"], None)
        subject, text, html = digest_email(feed, site_origin, unsub, since)
        try:
            send(p["digest_email"], subject, text, html)
        except Exception as e:
            fails = (p["digest_failures"] or 0) + 1
            log.warning("digest to account %s failed (%d): %s", p["user_id"], fails, type(e).__name__)
            counts["failed"] += 1
            if fails >= MAX_FAILURES:
                counts["disabled"] += 1
                conn.execute("UPDATE portal_users SET digest_confirmed_at = NULL, digest_failures = %s "
                             "WHERE user_id = %s", (fails, p["user_id"]))
            else:
                conn.execute("UPDATE portal_users SET digest_failures = %s WHERE user_id = %s",
                             (fails, p["user_id"]))
            conn.commit()
            continue
        conn.execute("UPDATE portal_users SET digest_sent_at = %s, digest_failures = 0 WHERE user_id = %s",
                     (now, p["user_id"]))
        conn.commit()
        counts["sent"] += 1
    log.info("digest run: %s", counts)
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    p = argparse.ArgumentParser(prog="alex311.digest", description="Send the daily digests.")
    p.add_argument("--site-origin", default=os.environ.get("SITE_ORIGIN", "https://alex311-reborn.com"))
    a = p.parse_args(argv)
    if not mail.configured():
        print("no mail transport configured; nothing sent", file=sys.stderr)
        return 2
    with db.connect() as conn:
        counts = run(conn, site_origin=a.site_origin)
    print(counts)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
