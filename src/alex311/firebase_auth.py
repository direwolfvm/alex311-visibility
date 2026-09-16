"""Sign-in through Firebase Authentication.

The browser talks to Firebase and comes back with an ID token. This module
does the one server-side thing that matters: checking that the token is
genuine, current, and issued for this project, and reading the uid out of it.
Everything after that — the account row, the session cookie, the roles — is
`portal_auth`, exactly as it was for password logins.

Firebase is the system of record for who a person is. We keep the uid, an
opaque string, and nothing else: no email, no name. That is the point of using
it rather than the emailed codes it replaces.

Configuration comes from the environment so the same image serves any project:

    FIREBASE_PROJECT_ID   required to verify tokens; the GCP project id
    FIREBASE_API_KEY      the web app's public browser key, for the client SDK
    FIREBASE_AUTH_DOMAIN  usually <project>.firebaseapp.com
    FIREBASE_APP_ID       the web app id, for the client SDK
    FIREBASE_TENANT_ID    the Identity Platform tenant this site signs into

The tenant is the point. The GCP project's Identity Platform is shared with
another application, and without a tenant the two would share one pool of
users, one email template and one set of providers — deleting a user from one
would sign them out of the other. A tenant is a separate pool inside the same
project. The client asks Firebase to sign into it, and `verify_id_token`
refuses a token minted for any other pool, tenant or default.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("alex311.firebase_auth")

POLICY_VERSION = "2026-09-15"


@dataclass(frozen=True)
class Identity:
    uid: str
    email: str | None
    email_verified: bool
    provider: str          # google.com | password | emailLink | ...


class NotConfigured(RuntimeError):
    pass


class BadToken(ValueError):
    pass


def client_config() -> dict | None:
    """What the sign-in page hands the Firebase JS SDK. None when unset, so the
    page can say sign-in by Firebase is not available here rather than fail."""
    key = os.environ.get("FIREBASE_API_KEY")
    project = os.environ.get("FIREBASE_PROJECT_ID")
    if not key or not project:
        return None
    return {"apiKey": key, "projectId": project,
            "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN") or f"{project}.firebaseapp.com",
            "appId": os.environ.get("FIREBASE_APP_ID"),
            "tenantId": os.environ.get("FIREBASE_TENANT_ID")}


def configured() -> bool:
    return bool(os.environ.get("FIREBASE_PROJECT_ID"))


def verify_id_token(token: str) -> Identity:
    """Check a Firebase ID token and return who it is for.

    google-auth fetches Google's public keys, checks the signature, expiry,
    issuer and audience (this project). A token for a different Firebase
    project, an expired one, or a forged one all raise `BadToken`.
    """
    project = os.environ.get("FIREBASE_PROJECT_ID")
    if not project:
        raise NotConfigured("FIREBASE_PROJECT_ID is not set")
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token as g_id_token
        claims = g_id_token.verify_firebase_token(token, g_requests.Request(), audience=project)
    except Exception as e:                       # ValueError for a bad token; transport errors too
        raise BadToken(str(e)[:200]) from e
    if not claims or not claims.get("sub"):
        raise BadToken("token carries no subject")
    firebase = claims.get("firebase") or {}
    # A token from the project's default pool, or from some other tenant, is
    # a real Firebase token for a real person — and not one of ours.
    want = os.environ.get("FIREBASE_TENANT_ID")
    if want and firebase.get("tenant") != want:
        raise BadToken("token is not for this site's tenant")
    return Identity(uid=claims["sub"], email=claims.get("email"),
                    email_verified=bool(claims.get("email_verified")),
                    provider=firebase.get("sign_in_provider", "unknown"))


# ------------------------------------------------------------ sign-in links
# Firebase can mint the sign-in link and hand it back instead of emailing it.
# We put the link on this site's own domain — the JS SDK only needs the code,
# the key and the mode in the query — and send it ourselves (see `mail`).

LINK_PARAMS = ("mode", "oobCode", "apiKey", "continueUrl", "lang", "tenantId")


def mint_sign_in_link(email: str, site_origin: str) -> str:
    """Ask Firebase for a one-time sign-in link for `email`, in this site's
    tenant, and return it rewritten to land on our own sign-in page."""
    from urllib.parse import parse_qsl, urlencode, urlsplit

    project = os.environ.get("FIREBASE_PROJECT_ID")
    if not project:
        raise NotConfigured("FIREBASE_PROJECT_ID is not set")
    import google.auth
    import requests
    from google.auth.transport.requests import Request as GRequest
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(GRequest())
    body = {"requestType": "EMAIL_SIGNIN", "email": email, "returnOobLink": True,
            "continueUrl": f"{site_origin}/submit/login", "canHandleCodeInApp": True}
    tenant = os.environ.get("FIREBASE_TENANT_ID")
    if tenant:
        body["tenantId"] = tenant
    r = requests.post("https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode",
                      json=body, timeout=15,
                      headers={"Authorization": f"Bearer {creds.token}",
                               "x-goog-user-project": project})
    if r.status_code != 200:
        raise RuntimeError(f"Firebase would not mint a link ({r.status_code})")
    link = r.json().get("oobLink") or ""
    q = dict(parse_qsl(urlsplit(link).query))
    if q.get("mode") != "signIn" or not q.get("oobCode") or not q.get("apiKey"):
        raise RuntimeError("Firebase returned a link without a usable code")
    keep = {k: q[k] for k in LINK_PARAMS if k in q}
    return f"{site_origin}/submit/login?{urlencode(keep)}"


def sign_in_email(link: str, site_origin: str) -> tuple[str, str, str]:
    """Subject, plain text and HTML for the email that carries a sign-in link.

    Written for the person opening it, in this site's name, and clear about
    what the account is not: it is separate from any Alex311 account with
    the City, and nothing happens unless the link is used."""
    host = site_origin.split("//", 1)[-1]
    subject = "Your sign-in link for Alex311 Reborn"
    text = f"""Hello,

Someone - most likely you - asked to sign in to Alex311 Reborn ({host})
with this email address. Open this link on the device where you asked for it:

{link}

The link works once and expires soon. If you did not ask for it, you can ignore
this email; nothing happens without the link.

About this account: it is for {host} only. It keeps the list of requests you
sent or follow here and the ratings you give them. It is separate from any
Alex311 account you have with the City of Alexandria and does not connect to
it. This site is an unofficial mirror of the City's 311 service requests and
is not run by the City.

Alex311 Reborn
{site_origin}
"""
    html = f"""<div style="font:15px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2733;max-width:560px">
<p>Hello,</p>
<p>Someone &mdash; most likely you &mdash; asked to sign in to <b>Alex311 Reborn</b> ({host})
with this email address. Open this link on the device where you asked for it:</p>
<p style="margin:20px 0"><a href="{link}" style="background:#1d4ed8;color:#fff;text-decoration:none;
padding:10px 18px;border-radius:8px;font-weight:600;display:inline-block">Sign in to Alex311 Reborn</a></p>
<p style="font-size:13px;color:#5c6675">Or copy this address into your browser:<br>
<a href="{link}" style="color:#1d4ed8;word-break:break-all">{link}</a></p>
<p>The link works once and expires soon. If you did not ask for it, you can ignore this email;
nothing happens without the link.</p>
<p style="font-size:13px;color:#5c6675;border-top:1px solid #e2e8f0;padding-top:12px;margin-top:20px">
<b>About this account.</b> It is for {host} only: it keeps the list of requests you sent or follow
here and the ratings you give them. It is separate from any Alex311 account you have with the City
of Alexandria and does not connect to it. This site is an unofficial mirror of the City's 311
service requests and is not run by the City.</p>
<p style="font-size:13px;color:#5c6675">Alex311 Reborn &middot; <a href="{site_origin}" style="color:#1d4ed8">{host}</a></p>
</div>"""
    return subject, text, html
