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
