"""Sending an email, for the one email this site sends: a sign-in link.

Firebase would send that email itself, from the GCP project's own address
(`noreply@<project>.firebaseapp.com`), with the project's name in the subject
and a link into `<project>.firebaseapp.com`. None of that is this site, and the
project's email settings are shared with another application, so they are
not ours to change. Instead the server asks Firebase for the link and sends
it in an email of its own — our sender, our words, our domain in the link.

Two transports, chosen by what is configured:

    ALEX311_MAIL_FROM      "Alex311 Reborn <no-reply@alex311visibility.me>" (required)
    MAILGUN_API_KEY +      Mailgun's HTTP API; the domain must be verified there
    ALEX311_MAIL_DOMAIN    (e.g. alex311visibility.me)
    SMTP_HOST, SMTP_PORT,  any SMTP relay with STARTTLS (587) instead
    SMTP_USER, SMTP_PASSWORD

With neither, `configured()` is False and the sign-in page falls back to
letting Firebase send its own email, as it did before.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("alex311.mail")


def configured() -> bool:
    if not os.environ.get("ALEX311_MAIL_FROM"):
        return False
    return bool((os.environ.get("MAILGUN_API_KEY") and os.environ.get("ALEX311_MAIL_DOMAIN"))
                or os.environ.get("SMTP_HOST"))


def send(to: str, subject: str, text: str, html: str | None = None) -> None:
    """Deliver one message, or raise. Never logs the body: it holds the link."""
    sender = os.environ["ALEX311_MAIL_FROM"]
    if os.environ.get("MAILGUN_API_KEY") and os.environ.get("ALEX311_MAIL_DOMAIN"):
        _mailgun(sender, to, subject, text, html)
    elif os.environ.get("SMTP_HOST"):
        _smtp(sender, to, subject, text, html)
    else:
        raise RuntimeError("no mail transport is configured")
    log.info("sent %r to a resident", subject)


def _mailgun(sender: str, to: str, subject: str, text: str, html: str | None) -> None:
    import requests
    domain = os.environ["ALEX311_MAIL_DOMAIN"]
    base = os.environ.get("MAILGUN_API_BASE", "https://api.mailgun.net/v3")
    data = {"from": sender, "to": to, "subject": subject, "text": text}
    if html:
        data["html"] = html
    r = requests.post(f"{base}/{domain}/messages", auth=("api", os.environ["MAILGUN_API_KEY"]),
                      data=data, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"mailgun refused the message ({r.status_code})")


def _smtp(sender: str, to: str, subject: str, text: str, html: str | None) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
