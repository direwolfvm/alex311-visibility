"""The sign-in email, and what every account page says an account is not.

Firebase would send the sign-in link from the GCP project's own address with
the project's name on it. That project is shared with another application,
so its email settings are not ours to touch; the server mints the link and
sends its own email instead. And every place a resident meets the account
says the one thing testers wondered about: it is not their Alex311 account
with the City, and does not connect to one.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOME = (ROOT / "dashboard/static/home.html").read_text()
MY = (ROOT / "dashboard/my.html").read_text()
LOGIN = (ROOT / "dashboard/login.html").read_text()
SUBMIT = (ROOT / "dashboard/submit.py").read_text()

from alex311 import firebase_auth as fb, mail  # noqa: E402


# ------------------------------------------------------------ the disclosure
@pytest.mark.parametrize("page, text", [
    ("home", "it is not your Alex311 account with the\n          City, and does not connect to one"),
    ("home", "Signing in here is not signing in to Alex311"),
    ("my", "This account is separate from the City's"),
    ("my", "not signing\n    in to Alex311"),
    ("login", "This sign-in is for this site only"),
    ("login", "not your Alex311 account with the City, and it does not connect to one"),
])
def test_every_account_surface_says_it_is_not_the_citys_account(page, text):
    html = {"home": HOME, "my": MY, "login": LOGIN}[page]
    assert text in html, (page, text)


def test_the_email_itself_says_so_and_carries_the_link_on_our_domain():
    link = "https://alex311visibility.me/submit/login?mode=signIn&oobCode=abc&apiKey=k"
    subject, text, html = fb.sign_in_email(link, "https://alex311visibility.me")
    assert subject == "Your sign-in link for Alex311 Reborn"
    for body in (text, html):
        assert link in body
        assert "separate from any\nAlex311 account" in body or "separate from any Alex311 account" in body
        assert "does not connect to\nit" in body or "does not connect to it" in body
        assert "nothing happens without the link" in body
        assert "not run by the City" in body
        # nothing of the GCP project's leaks into what a resident reads
        assert "permitting-ai-helper" not in body and "firebaseapp" not in body


# ------------------------------------------------------- minting the link
def test_the_link_is_rewritten_onto_our_domain(monkeypatch):
    """Firebase hands back a link into <project>.firebaseapp.com. The JS SDK
    needs only the mode, code and key, so the link a resident clicks is ours."""
    class Creds:
        token = "t"
        def refresh(self, _): pass
    import types
    monkeypatch.setattr(fb, "os", fb.os)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "permitting-ai-helper")
    monkeypatch.setenv("FIREBASE_TENANT_ID", "alex311-qfnem")
    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (Creds(), "p"))
    seen = {}
    class Resp:
        status_code = 200
        def json(self):
            return {"email": "a@b.co", "oobLink":
                    "https://permitting-ai-helper.firebaseapp.com/__/auth/action?mode=signIn"
                    "&oobCode=CODE123&apiKey=KEY&continueUrl=https%3A%2F%2Falex311visibility.me%2Fsubmit%2Flogin"
                    "&lang=en&tenantId=alex311-qfnem"}
    import requests
    def fake_post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, body=json, headers=headers); return Resp()
    monkeypatch.setattr(requests, "post", fake_post)
    link = fb.mint_sign_in_link("A@B.co", "https://alex311visibility.me")
    assert link.startswith("https://alex311visibility.me/submit/login?")
    assert "oobCode=CODE123" in link and "apiKey=KEY" in link and "mode=signIn" in link
    assert "tenantId=alex311-qfnem" in link
    assert "firebaseapp.com" not in link
    # asked in our tenant, with the link returned rather than emailed by Firebase
    assert seen["body"]["returnOobLink"] is True
    assert seen["body"]["tenantId"] == "alex311-qfnem"
    assert seen["body"]["requestType"] == "EMAIL_SIGNIN"
    assert seen["headers"]["x-goog-user-project"] == "permitting-ai-helper"


def test_a_link_without_a_code_is_refused(monkeypatch):
    class Creds:
        token = "t"
        def refresh(self, _): pass
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "p")
    import google.auth, requests
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (Creds(), "p"))
    class Resp:
        status_code = 200
        def json(self): return {"oobLink": "https://x/__/auth/action?mode=resetPassword&apiKey=k"}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError):
        fb.mint_sign_in_link("a@b.co", "https://s")


# --------------------------------------------------------------- transport
def test_nothing_configured_means_firebase_sends_its_own(monkeypatch):
    for k in ("ALEX311_MAIL_FROM", "MAILGUN_API_KEY", "ALEX311_MAIL_DOMAIN", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    assert mail.configured() is False
    monkeypatch.setenv("ALEX311_MAIL_FROM", "x <x@y>")
    assert mail.configured() is False          # a sender alone is not a transport
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    assert mail.configured() is True
    # and the endpoint turns that into a 503 the page reads as "fall back"
    assert 'if not (fb.configured() and mail.configured()):' in SUBMIT
    assert 'raise HTTPException(503' in SUBMIT
    assert "if (r.status === 503)" in LOGIN and "auth.sendSignInLinkToEmail" in LOGIN


def test_mailgun_gets_sender_recipient_subject_and_both_bodies(monkeypatch):
    monkeypatch.setenv("ALEX311_MAIL_FROM", "Alex311 Reborn <no-reply@alex311visibility.me>")
    monkeypatch.setenv("MAILGUN_API_KEY", "key-1")
    monkeypatch.setenv("ALEX311_MAIL_DOMAIN", "alex311visibility.me")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import requests
    seen = {}
    class Resp: status_code = 200
    def fake_post(url, auth=None, data=None, timeout=None):
        seen.update(url=url, auth=auth, data=data); return Resp()
    monkeypatch.setattr(requests, "post", fake_post)
    mail.send("r@example.org", "Subj", "plain", "<b>html</b>")
    assert seen["url"] == "https://api.mailgun.net/v3/alex311visibility.me/messages"
    assert seen["auth"] == ("api", "key-1")
    assert seen["data"] == {"from": "Alex311 Reborn <no-reply@alex311visibility.me>",
                            "to": "r@example.org", "subject": "Subj", "text": "plain", "html": "<b>html</b>"}


def test_a_refused_message_raises_rather_than_pretending(monkeypatch):
    monkeypatch.setenv("ALEX311_MAIL_FROM", "x <x@y>")
    monkeypatch.setenv("MAILGUN_API_KEY", "k"); monkeypatch.setenv("ALEX311_MAIL_DOMAIN", "d")
    import requests
    class Resp: status_code = 401
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError):
        mail.send("r@example.org", "s", "t")


def test_the_endpoint_never_echoes_the_link_or_the_address():
    """The failure path logs an exception type and nothing else, and the
    success path returns only that it was sent."""
    fn = SUBMIT.split("def email_sign_in_link(")[1].split("\n    @public")[0]
    assert 'log.warning("sign-in email failed: %s", type(e).__name__)' in fn
    assert 'return {"sent": True}' in fn
    assert "_allow(f\"email:{email}\"" in fn and "_allow(f\"ip:{caller}\"" in fn
