"""Gated submission prototype: a registry-driven, single-page 311 intake.

Behind HTTP Basic auth for testing — write-adjacent, must not be publicly
reachable while the authorization question with the City is open. Nothing
here submits to the city portal: the page renders every question of a
category at once from docs/data/form-registry.json, validates answers
server-side against the same registry (so nothing the wizard would reject
gets prepared), warns about likely duplicates using our own data, and hands
off to the official portal with a copyable summary.
"""
from __future__ import annotations

import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, Depends, HTTPException, Request,
                     Response)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from alex311.client import Alex311Client
from alex311 import (abuse, db as adb, digest, firebase_auth as fb, job_runner, mail, photos, push, refresh,
                     signin_code, watches,
                     portal_auth as pa)

from . import registry as R
from alex311 import registry_store

log = logging.getLogger("alex311.submit")

#: Basic auth is kept alongside the login page on purpose. People get a real
#: page they can read, style and sign out of; scripts, the runbook's curl
#: examples and the CLI keep the one shared credential they already use.
_security = HTTPBasic(auto_error=False)


def _basic_ok(creds: HTTPBasicCredentials | None) -> bool:
    if creds is None:
        return False
    user = os.environ.get("SUBMIT_USER", "alex311user")
    pw = os.environ.get("SUBMIT_PASSWORD", "")
    return bool(pw) and secrets.compare_digest(creds.username, user) \
        and secrets.compare_digest(creds.password, pw)


def _wants_html(request: Request) -> bool:
    """A browser gets sent to the login page; anything else gets a 401.

    Redirecting an API call would hand the caller a login form with a 200 on it,
    which is a confusing thing for a script to receive.
    """
    return "text/html" in request.headers.get("accept", "")


class Unauthenticated(HTTPException):
    """Raised when nobody is signed in. Turned into a redirect for browsers."""

    def __init__(self):
        super().__init__(status_code=401, detail="sign in at /submit/login")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def merge_candidates(rows: list[dict], key: str) -> list[dict]:
    """Fold address spellings into one candidate each, best match first.

    The City writes one address several ways — "1437 JANNEY'S LN" and
    "1437 JANNEY'S LA" are 51 and 48 records of the same place. Offering both as
    separate choices would be confusing and would split the confidence signal,
    so they merge and the most-used spelling represents them.
    """
    merged: dict[str, dict] = {}
    for r in rows:
        k = abuse.normalize_address(r["address"])
        if k not in merged:
            merged[k] = {"address": r["address"], "key": k, "lat": r["lat"],
                         "long": r["long"], "seen": 0, "exact": k == key}
        elif r["seen"] > merged[k]["seen"]:
            # a later row is the more common spelling; let it show its wording
            merged[k]["address"] = r["address"]
        merged[k]["seen"] += r["seen"]
    return sorted(merged.values(), key=lambda c: (not c["exact"], -c["seen"]))


class EmailLinkBody(BaseModel):
    email: str


class EmailCodeBody(BaseModel):
    email: str
    code: str


class ProfileBody(BaseModel):
    display_name: str | None = None


class FirebaseSession(BaseModel):
    id_token: str


class FeedbackBody(BaseModel):
    """A resident's verdict. `score` 1–5, or null for "not sure"."""
    score: int | None = None
    note: str = ""
    #: show the score (never the note) on the request's public page
    share_score: bool = False


class NewUser(BaseModel):
    email: str
    role: str = "user"


class WatchBody(BaseModel):
    kind: str
    spec: dict = {}
    label: str | None = None


class DigestBody(BaseModel):
    email: str


class DeviceBody(BaseModel):
    token: str
    environment: str = "production"


class ValidateBody(BaseModel):
    service_code: str
    answers: dict = {}




class AuthStart(BaseModel):
    email: str


class AuthConfirm(BaseModel):
    email: str
    code: str


class QueueBody(BaseModel):
    """Ask for an already-prechecked request to be filed on your behalf."""
    attempt_id: int
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    # set when the person has read a policy warning and still means to send it
    acknowledged: bool = False


class PrecheckBody(BaseModel):
    """A proposed submission, as the form would send it."""
    service_code: str
    service_name: str | None = None
    address: str | None = None
    #: the City's own spelling of that address, when the lookup found one. Kept
    #: apart from `address` on purpose: `address` is the resident's wording and
    #: is what we show back to them, this is what the City's gazetteer accepts.
    city_address: str | None = None
    lat: float | None = None
    long: float | None = None
    description: str = ""
    answers: dict = {}
    #: a field no real user can see; anything in it is a bot
    website: str = ""
    # Note there is no submitter_id here. It comes from the session cookie and
    # nowhere else: a client that could name its own submitter could pick a
    # fresh one per request and every per-submitter limit would be decorative.


def _identify(request: Request, pool_getter,
              creds: HTTPBasicCredentials | None) -> tuple[str, pa.PortalUser | None]:
    """Who is at the door: a signed-in user, a script with the shared password,
    or nobody."""
    token = request.cookies.get(pa.SESSION_COOKIE)
    if token:
        with pool_getter().connection() as conn:
            user = pa.session_user(conn, token)
        if user is not None:
            return user.user_id, user
    if _basic_ok(creds):
        return os.environ.get("SUBMIT_USER", "alex311user"), None
    raise Unauthenticated()


class RegistryDecision(BaseModel):
    version: str = Field(min_length=8, max_length=64)


def register_submit_routes(app, pool_getter, sender=None) -> None:  # sender: kept for callers
    """Attach the gated /submit routes. pool_getter() returns the live pool.

    Two layers of access, doing different jobs. HTTP Basic decides who may see
    the prototype at all while the authorization question with the City is
    open. The session below decides *which resident* is submitting, which is
    what the per-submitter abuse rules need.
    """

    def gate(request: Request,
             creds: HTTPBasicCredentials | None = Depends(_security)) -> str:
        actor, _user = _identify(request, pool_getter, creds)
        return actor

    def admin_only(request: Request,
                   creds: HTTPBasicCredentials | None = Depends(_security)) -> str:
        """User management is for admins. The shared script credential counts as
        one: it is how the first admin is seeded and how a locked-out admin
        recovers."""
        actor, user = _identify(request, pool_getter, creds)
        if user is not None and not user.is_admin:
            raise HTTPException(403, "that page is for administrators")
        return actor

    # The login page and its POST are the only unguarded routes: everything
    # else hangs off `gate`.
    public = APIRouter(prefix="/submit")
    login_page = Path(__file__).parent / "login.html"
    admin_page = Path(__file__).parent / "admin.html"
    my_page = Path(__file__).parent / "my.html"
    account_page = Path(__file__).parent / "account.html"

    @public.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if request.cookies.get(pa.SESSION_COOKIE):
            with pool_getter().connection() as conn:
                if pa.session_user(conn, request.cookies[pa.SESSION_COOKIE]):
                    return RedirectResponse("/submit", status_code=303)
        return HTMLResponse(login_page.read_text())


    @public.post("/logout")
    @public.get("/logout")
    def do_logout(request: Request):
        token = request.cookies.get(pa.SESSION_COOKIE)
        if token:
            with pool_getter().connection() as conn:
                pa.logout(conn, token)
        resp = RedirectResponse("/submit/login", status_code=303)
        resp.delete_cookie(pa.SESSION_COOKIE, path="/submit")
        return resp

    @public.get("/api/firebase-config")
    def firebase_config():
        """What the sign-in page hands the Firebase SDK. Public by nature: a
        browser key is meant to be in the page, and it is scoped to Firebase
        APIs and does nothing without a signed-in user behind it."""
        return {"config": fb.client_config(), "policy_version": fb.POLICY_VERSION}

    # A sign-in link is one email an hour per address, and a handful per
    # caller, so a stranger cannot use this site to flood an inbox.
    link_sends: dict[str, list[float]] = {}

    def _allow(key: str, limit: int, window: float = 3600.0) -> bool:
        now = time.monotonic()
        recent = [t for t in link_sends.get(key, []) if now - t < window]
        if len(recent) >= limit:
            link_sends[key] = recent
            return False
        recent.append(now)
        link_sends[key] = recent
        return True

    # Sign-in links: enough for real life (a link lands in Junk, a mail scanner
    # or a browser uses it up, someone asks again before the first arrives),
    # still far short of flooding an inbox. The numbers are a contract with
    # the iOS app (Session.sendsPerHour / Session.resendDelay): change both.
    LINKS_PER_ADDRESS_PER_HOUR = 5
    LINK_SPACING_SECONDS = 60
    LINKS_PER_CALLER_PER_HOUR = 20          # many residents can share one address: a library, an office

    def _wait(key: str, limit: int, window: float = 3600.0, spacing: float = 0.0) -> int:
        """Seconds until `key` may send again; 0 if it may now. Records nothing."""
        now = time.monotonic()
        recent = [t for t in link_sends.get(key, []) if now - t < window]
        link_sends[key] = recent
        if recent and spacing and now - recent[-1] < spacing:
            return max(1, math.ceil(spacing - (now - recent[-1])))
        if len(recent) >= limit:
            return max(1, math.ceil(window - (now - recent[0])))   # when the oldest leaves the window
        return 0

    @public.post("/api/email-link")
    def email_sign_in_link(body: EmailLinkBody, request: Request):
        """Send a sign-in link in this site's own email.

        Firebase mints the link; we put it on our domain and send it from our
        sender with our words, because the project's own email — its name, its
        address, its template — is shared with another application and is not
        ours to change. 503 when no mail transport is configured, which the
        sign-in page reads as "let Firebase send its own".
        """
        if not (fb.configured() and mail.configured()):
            raise HTTPException(503, "this site does not send its own sign-in email")
        email = body.email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(400, "that does not look like an email address")
        caller = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
            or (request.client.host if request.client else "?")
        # both checked before either is counted, so one refusal does not spend the other's allowance
        wait = max(_wait(f"email:{email}", LINKS_PER_ADDRESS_PER_HOUR, spacing=LINK_SPACING_SECONDS),
                   _wait(f"ip:{caller}", LINKS_PER_CALLER_PER_HOUR))
        if wait:
            when = f"{wait} seconds" if wait < 90 else f"about {math.ceil(wait / 60)} minutes"
            raise HTTPException(429, f"a link was sent recently; check that inbox, or ask again in {when}",
                                headers={"Retry-After": str(wait)})
        for key in (f"email:{email}", f"ip:{caller}"):
            link_sends.setdefault(key, []).append(time.monotonic())
        origin = os.environ.get("SITE_ORIGIN") or str(request.base_url).rstrip("/")
        try:
            link = fb.mint_sign_in_link(email, origin)
            # the same email carries a short code, for when it is read on another device
            code = None
            if signin_code.available():
                try:
                    oob = parse_qs(urlsplit(link).query).get("oobCode", [""])[0]
                    with pool_getter().connection() as conn:
                        code = signin_code.pretty(signin_code.issue(conn, email=email, oob_code=oob))
                except Exception as e:           # a code is a convenience; the link still goes
                    log.warning("sign-in code not issued: %s", type(e).__name__)
            subject, text, html = fb.sign_in_email(link, origin, code)
            mail.send(email, subject, text, html)
        except Exception as e:                   # never echo the link or the address
            log.warning("sign-in email failed: %s", type(e).__name__)
            raise HTTPException(502, "could not send the email just now; try again shortly")
        return {"sent": True, "code": code is not None}

    CODE_TRIES_PER_CALLER_PER_HOUR = 30

    @public.post("/api/email-code")
    def email_sign_in_code(body: EmailCodeBody, request: Request):
        """Trade the six-digit code from the sign-in email for the link's
        one-time oobCode, which the caller redeems with Firebase exactly as
        it would the link.

        400 a wrong code — or an address that never asked; the two are
        indistinguishable on purpose. 410 expired or already used. 429 burned
        by wrong tries, or too many tries from one caller.
        """
        if not signin_code.available():
            raise HTTPException(503, "sign-in codes are not available here")
        caller = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
            or (request.client.host if request.client else "?")
        wait = _wait(f"code-ip:{caller}", CODE_TRIES_PER_CALLER_PER_HOUR)
        if wait:
            raise HTTPException(429, "too many tries; ask for a new link later",
                                headers={"Retry-After": str(wait)})
        link_sends.setdefault(f"code-ip:{caller}", []).append(time.monotonic())
        with pool_getter().connection() as conn:
            try:
                oob = signin_code.redeem(conn, email=body.email, code=body.code)
            except signin_code.WrongCode as e:
                raise HTTPException(400, str(e))
            except signin_code.Gone as e:
                raise HTTPException(410, str(e))
            except signin_code.TooMany as e:
                raise HTTPException(429, str(e))
        return {"oobCode": oob}

    @public.post("/api/session")
    def session_from_firebase(body: FirebaseSession, request: Request):
        """Trade a Firebase ID token for our session cookie.

        The browser has just signed in with Firebase and holds a token that
        says so. We check it is genuine and for this project, and from there
        the account and the cookie follow — one gate, one door.
        """
        try:
            who = fb.verify_id_token(body.id_token)
        except fb.NotConfigured:
            raise HTTPException(503, "sign-in with Firebase is not set up here")
        except fb.BadToken:
            raise HTTPException(401, "that sign-in could not be verified")
        with pool_getter().connection() as conn:
            token = pa.sign_in_with_firebase(
                conn, uid=who.uid, email=who.email, email_verified=who.email_verified,
                policy_version=fb.POLICY_VERSION,
                user_agent=request.headers.get("user-agent"))
        if not token:
            raise HTTPException(403, "that account cannot sign in")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(pa.SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/submit",
                        max_age=int(pa.SESSION_TTL.total_seconds()))
        return resp

    # The two links in a digest email work without a sign-in: the person may
    # be on a phone, in a mail app, and should not have to prove who they are
    # to stop an email. The token proves the link came from us for that inbox.
    def _plain_page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Alex311 Reborn</title>
<style>body{{margin:0;background:#f6f7f9;color:#1c2733;font:15px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif}}
main{{max-width:520px;margin:48px auto;padding:24px 28px;background:#fff;border:1px solid #e2e8f0;border-radius:12px}}
h1{{font-size:20px;margin:0 0 10px}}a{{color:#1d4ed8}}</style></head>
<body><main><h1>{title}</h1><p>{body}</p><p><a href="/">Alex311 Reborn</a> · <a href="/submit/account">Your account</a></p></main></body></html>""")

    @public.get("/digest/confirm", response_class=HTMLResponse)
    def digest_confirm(t: str = ""):
        try:
            with pool_getter().connection() as conn:
                email = digest.confirm(conn, t)
        except (digest.BadToken, digest.NotConfigured) as e:
            return _plain_page("That link did not work", str(e))
        return _plain_page("Digest on", f"Each morning there is something new for what you watch, "
                           f"one email goes to <b>{email}</b>. Every one has a link to stop it.")

    @public.get("/digest/unsubscribe", response_class=HTMLResponse)
    def digest_unsubscribe(t: str = ""):
        try:
            with pool_getter().connection() as conn:
                email = digest.unsubscribe(conn, t)
        except (digest.BadToken, digest.NotConfigured) as e:
            return _plain_page("That link did not work", str(e))
        return _plain_page("Digest off", f"No more digests will go to <b>{email}</b>. Your watches "
                           f"are still on My requests; turn the email back on from your account page any time.")

    @public.get("/api/whoami")
    def whoami_portal(request: Request,
                      creds: HTTPBasicCredentials | None = Depends(_security)):
        """Who is at the door, including nobody.

        On the public router on purpose: the dashboard is a page anybody may
        open, and it asks this to decide whether to show the Admin tab. Behind
        the gate the question could only ever be answered by someone who was
        already through it.
        """
        try:
            _actor, user = _identify(request, pool_getter, creds)
        except Unauthenticated:
            return {"user": None}
        return {"user": ({"user_id": user.user_id, "email": user.email, "role": user.role,
                          "label": user.label, "firebase_uid": user.firebase_uid,
                          "firebase_linked": user.firebase_uid is not None,
                          "policy_version": user.policy_version,
                          "display_name": user.display_name}
                         if user else None),
                "firebase": fb.configured()}

    @app.exception_handler(Unauthenticated)
    def _needs_login(request: Request, exc: Unauthenticated):
        """A person gets the login page; a script gets a 401 it can act on."""
        if _wants_html(request):
            return RedirectResponse("/submit/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Alex311 (beta)"'})

    app.include_router(public)

    router = APIRouter(prefix="/submit", dependencies=[Depends(gate)])
    page = Path(__file__).parent / "submit.html"

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def submit_page():
        return page.read_text()

    @router.get("/admin", response_class=HTMLResponse)
    def admin_ui(actor: str = Depends(admin_only)):
        """Moderation and user management. The gate is here, not in the page:
        a hidden link is a courtesy, not a permission."""
        return admin_page.read_text()

    @router.get("/account", response_class=HTMLResponse)
    def account_ui(actor: str = Depends(gate)):
        """Who you are here, what we hold, and the ways out."""
        return account_page.read_text()

    @router.put("/api/profile")
    def set_profile(body: ProfileBody, request: Request, actor: str = Depends(gate)):
        """The one thing a person can set about themselves: a display name,
        shown to them in the chip and on the account page and nowhere public."""
        user_id = _account(request)
        try:
            name = pa.clean_display_name(body.display_name)
        except pa.BadName as e:
            raise HTTPException(400, str(e))
        with pool_getter().connection() as conn:
            pa.set_display_name(conn, user_id, name)
        return {"display_name": name}

    @router.post("/api/logout-all")
    def logout_everywhere(request: Request, actor: str = Depends(gate)):
        """Revoke every session of the signed-in account, this one included."""
        user_id = _account(request)
        with pool_getter().connection() as conn:
            pa.logout_all(conn, user_id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(pa.SESSION_COOKIE, path="/submit")
        return resp

    @router.get("/my", response_class=HTMLResponse)
    def my_ui(actor: str = Depends(gate)):
        """An account's own requests: the ones it sent, and the ones it follows."""
        return my_page.read_text()

    # ------------------------------------------------------------- links
    # A link is a pointer: the City's case number, and whether this account
    # sent the request or is only watching it. The City holds the request;
    # the mirror holds the public record; we hold neither twice.

    CASE_ID = re.compile(r"^\d{2}-\d{8}$")

    def _account(request: Request) -> str:
        user_id = _submitter(request)
        if not user_id:
            # the shared script credential has no account to hang links on
            raise HTTPException(403, "sign in with an account to keep a list")
        return user_id

    @router.get("/api/my")
    def my_requests(request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            links = adb.my_links(conn, user_id=user_id)
        # The City's record exists the moment a case number does — before this
        # mirror has read it — so the link goes on every row, not just the mirrored.
        for link in links:
            link["report_url"] = Alex311Client.deep_link(link["service_request_id"])
        return {"links": links}

    # One refresh per account every two minutes: the City's endpoint is shared
    # with the ingest job and every other resident, and a stale list is not an
    # emergency.
    REFRESH_COOLDOWN = 120.0

    @router.post("/api/my/refresh")
    def refresh_my_requests(request: Request, actor: str = Depends(gate)):
        """Ask the City about the cases on this account's list, right now.

        The mirror reads the City four times a day; this reads the few cases
        on one person's list on demand, newest first, capped and paced (see
        alex311.refresh), and folds the answers into the mirror for everyone.
        """
        user_id = _account(request)
        if not _allow(f"refresh:{user_id}", 1, REFRESH_COOLDOWN):
            raise HTTPException(429, "refreshed a moment ago; give the City a couple of minutes")
        with pool_getter().connection() as conn:
            links = adb.my_links(conn, user_id=user_id)
            conn.rollback()                      # drop the RLS setting before writing the mirror
            cases = [l["service_request_id"] for l in links][:refresh.MAX_PER_PRESS]
            if not cases:
                return {"results": [], "skipped": 0}
            with Alex311Client() as client:
                results = refresh.refresh_cases(conn, client, cases)
        return {"results": results, "skipped": max(0, len(links) - len(cases))}

    @router.get("/api/link/{case}")
    def link_state(case: str, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            return {"case": case, "relation": adb.link_state(conn, user_id=user_id,
                                                              service_request_id=case)}

    @router.post("/api/follow/{case}")
    def follow(case: str, request: Request, actor: str = Depends(gate)):
        """Follow needs no proof; "mine" is never set here, only by the worker."""
        if not CASE_ID.match(case):
            raise HTTPException(400, "that is not a case number")
        user_id = _account(request)
        with pool_getter().connection() as conn:
            relation = adb.link_request(conn, user_id=user_id, service_request_id=case,
                                        relation="following")
        return {"case": case, "relation": relation}

    @router.delete("/api/follow/{case}")
    def unfollow(case: str, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            removed = adb.unlink_request(conn, user_id=user_id, service_request_id=case)
        return {"case": case, "relation": None, "removed": removed}

    # ----------------------------------------------------------- watches
    # Follow a request type, an address or several, or a drawn area; the feed
    # is what the mirror has seen since the person last looked that matches.

    @router.get("/api/watches")
    def watches_list(request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            return {"watches": watches.list_watches(conn, user_id=user_id)}

    @router.post("/api/watches")
    def watches_add(body: WatchBody, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            try:
                return watches.add_watch(conn, user_id=user_id, kind=body.kind,
                                         payload=body.spec, label=body.label)
            except watches.BadWatch as e:
                raise HTTPException(400, str(e))

    @router.delete("/api/watches/{watch_id}")
    def watches_remove(watch_id: int, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            removed = watches.remove_watch(conn, user_id=user_id, watch_id=watch_id)
        return {"watch_id": watch_id, "removed": removed}

    # ------------------------------------------------------- push devices
    # The iOS app registers its APNs token here once the person allows
    # notifications, and again whenever it launches (tokens change).

    @router.post("/api/devices")
    def device_register(body: DeviceBody, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            try:
                out = push.register(conn, user_id=user_id, token=body.token,
                                    environment=body.environment)
            except push.BadDevice as e:
                raise HTTPException(400, str(e))
        out["push_available"] = push.configured()
        return out

    @router.delete("/api/devices/{token}")
    def device_unregister(token: str, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            return {"removed": push.unregister(conn, user_id=user_id, token=token)}

    @router.get("/api/digest")
    def digest_state(request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            st = digest.state(conn, user_id=user_id)
        st["available"] = mail.configured()
        return st

    @router.put("/api/digest")
    def digest_set(body: DigestBody, request: Request, actor: str = Depends(gate)):
        """Turn the daily digest on for an address — at once if it is the
        verified address the account signs in with, otherwise after the
        person opens a confirmation link we send there."""
        user_id = _account(request)
        origin = os.environ.get("SITE_ORIGIN") or str(request.base_url).rstrip("/")
        with pool_getter().connection() as conn:
            try:
                return digest.request(conn, user_id=user_id, email=body.email, site_origin=origin)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except digest.NotConfigured:
                raise HTTPException(503, "this site cannot send email just now")
            except Exception as e:
                log.warning("digest confirmation email failed: %s", type(e).__name__)
                raise HTTPException(502, "could not send the confirmation just now; try again shortly")

    @router.delete("/api/digest")
    def digest_off(request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            digest.turn_off(conn, user_id=user_id)
        return {"status": "off"}

    @router.get("/api/feed")
    def feed_get(request: Request, actor: str = Depends(gate)):
        """What matched your watches since you last looked. Reading does not
        mark it seen; the person does that, so a glance on a phone does not
        lose the list."""
        user_id = _account(request)
        with pool_getter().connection() as conn:
            since = watches.seen_at(conn, user_id=user_id)
            return watches.feed(conn, user_id=user_id, since=since)

    @router.post("/api/feed/seen")
    def feed_seen(request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            at = watches.mark_seen(conn, user_id=user_id)
        return {"seen_at": at}

    # ---------------------------------------------------------- feedback
    # "Was this issue actually addressed?" — the resident's own verdict, one
    # per account per request, private. It is the one signal the City's data
    # cannot carry: the record says closed; the person says whether it was.

    @router.get("/api/feedback/{case}")
    def feedback_get(case: str, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            return {"case": case, "feedback": adb.get_feedback(conn, user_id=user_id,
                                                                service_request_id=case)}

    @router.put("/api/feedback/{case}")
    def feedback_put(case: str, body: FeedbackBody, request: Request,
                     actor: str = Depends(gate)):
        if not CASE_ID.match(case):
            raise HTTPException(400, "that is not a case number")
        if body.score is not None and not 1 <= body.score <= 5:
            raise HTTPException(400, "score is 1 to 5, or nothing for not sure")
        user_id = _account(request)
        with pool_getter().connection() as conn:
            saved = adb.save_feedback(conn, user_id=user_id, service_request_id=case,
                                      score=body.score, note=body.note.strip()[:2000],
                                      share_score=body.share_score)
        return {"case": case, "feedback": saved}

    @router.delete("/api/feedback/{case}")
    def feedback_delete(case: str, request: Request, actor: str = Depends(gate)):
        user_id = _account(request)
        with pool_getter().connection() as conn:
            removed = adb.delete_feedback(conn, user_id=user_id, service_request_id=case)
        return {"case": case, "removed": removed}

    @router.delete("/api/account")
    def delete_my_account(request: Request, actor: str = Depends(gate)):
        """Remove the account and everything hanging off it.

        The last administrator cannot delete themselves: there would be
        nobody left who could let anyone in.

        The sign-in record goes too — from this site's own tenant only (see
        firebase_auth.delete_tenant_user). If Firebase cannot be reached the
        account here is still gone, and the answer says the sign-in is not.
        """
        user_id = _account(request)
        with pool_getter().connection() as conn:
            if not pa.count_admins(conn, excluding=user_id):
                raise HTTPException(409, "that is the last administrator")
            row = conn.execute("SELECT firebase_uid FROM portal_users WHERE user_id = %s",
                               (user_id,)).fetchone()
            uid = row["firebase_uid"] if row else None
            removed = adb.delete_account(conn, user_id=user_id)
        sign_in_removed = None                      # None: there was no sign-in record to remove
        if removed and uid:
            try:
                sign_in_removed = fb.delete_tenant_user(uid)
            except Exception as e:
                log.warning("sign-in record not removed: %s", type(e).__name__)
                sign_in_removed = False
        resp = JSONResponse({"removed": removed, "sign_in_removed": sign_in_removed})
        resp.delete_cookie(pa.SESSION_COOKIE, path="/submit")
        return resp

    @router.get("/users")
    def users_ui(actor: str = Depends(admin_only)):
        # where user management used to live
        return RedirectResponse("/submit/admin#users", status_code=308)

    @router.get("/api/users")
    def users_list(actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            return {"users": pa.list_users(conn)}

    @router.post("/api/users")
    def users_create(body: NewUser, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            try:
                user = pa.create_user(conn, body.email, body.role, created_by=actor)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception:
                raise HTTPException(409, "that email already has an account")
        # an invitation: the person signs in with this address and is linked to it
        return {"user_id": user.user_id, "email": user.email, "role": user.role}


    @router.post("/api/users/{user_id}/disable")
    def users_disable(user_id: str, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            if not pa.count_admins(conn, excluding=user_id):
                # with nobody left holding the keys there is no way back in
                raise HTTPException(409, "that is the last administrator")
            pa.set_disabled(conn, user_id, True)
        return {"user_id": user_id, "disabled": True}

    @router.post("/api/users/{user_id}/enable")
    def users_enable(user_id: str, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            pa.set_disabled(conn, user_id, False)
        return {"user_id": user_id, "disabled": False}

    # The registry describes a form the City controls, and it changes without
    # notice (two request types were retired in October 2026). Every registry
    # response carries a version and an ETag, so the app — or the page — keeps
    # its copy only while it is current, and nobody has to ship a build to
    # follow the City.
    def _fresh(request: Request) -> Response | None:
        """304 if the caller already holds this version."""
        etag = f'"{R.registry_version()}"'
        held = request.headers.get("if-none-match", "")
        if etag in [h.strip().removeprefix("W/") for h in held.split(",")]:
            return Response(status_code=304, headers=_registry_headers())
        return None

    def _registry_headers() -> dict:
        return {"ETag": f'"{R.registry_version()}"', "Cache-Control": "private, max-age=300",
                "X-Registry-Version": R.registry_version(), "X-Registry-Schema": str(R.SCHEMA)}

    @public.get("/api/registry/version")
    def registry_version(request: Request):
        """Is my copy current? Public and tiny, so the app can ask at launch
        or in a background refresh without a session."""
        if (r := _fresh(request)) is not None:
            return r
        reg = R.load_registry()
        return JSONResponse({"version": R.registry_version(), "schema": R.SCHEMA,
                             "generated": reg["generated"], "services": len(reg["services"])},
                            headers={"Cache-Control": "public, max-age=300",
                                     "ETag": f'"{R.registry_version()}"'})

    @router.get("/api/registry")
    def registry_index(request: Request):
        """The picker's list: one light entry per request type."""
        if (r := _fresh(request)) is not None:
            return r
        reg = R.load_registry()
        return JSONResponse({"version": R.registry_version(), "schema": R.SCHEMA,
                             "generated": reg["generated"], "sources": reg.get("sources"),
                             "services": R.service_index(reg)}, headers=_registry_headers())

    @router.get("/api/registry/full")
    def registry_full(request: Request):
        """Every request type with its questions and rules, in one response —
        what a client stores to work from, revalidating with If-None-Match."""
        if (r := _fresh(request)) is not None:
            return r
        gz = "gzip" in request.headers.get("accept-encoding", "")
        headers = _registry_headers() | {"Vary": "Accept-Encoding"}
        if gz:
            headers["Content-Encoding"] = "gzip"
        return Response(R.full_payload(gz), media_type="application/json", headers=headers)

    @router.get("/api/service/{code}")
    def service(code: str, request: Request):
        if (r := _fresh(request)) is not None:
            return r
        s = R.get_service(R.load_registry(), code)
        if not s:
            raise HTTPException(404, "unknown service code")
        return JSONResponse(s, headers=_registry_headers())

    # The registry as data: the nightly walk offers a new registry when the
    # City's form has moved, and an administrator decides. Adopting changes
    # what every resident is asked, so it is for administrators and it is
    # recorded with who did it.
    @router.get("/api/admin/registry")
    def registry_state(actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            data = registry_store.state(conn)
        data["serving"] = {"version": R.registry_version(), "source": R.registry_source()}
        return data

    @router.post("/api/admin/registry/adopt")
    def registry_adopt(body: RegistryDecision, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            if not registry_store.adopt(conn, body.version, actor):
                raise HTTPException(404, "no such registry version")
        R.refresh()                      # this instance now; the others within a minute
        return {"adopted": body.version, "serving": R.registry_version()}

    @router.post("/api/admin/registry/dismiss")
    def registry_dismiss(body: RegistryDecision, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            if not registry_store.dismiss(conn, body.version, actor):
                raise HTTPException(404, "that version is not waiting")
        return {"dismissed": body.version}

    @router.post("/api/validate")
    def validate(body: ValidateBody):
        """Server-authoritative check of a category's answers against the registry."""
        s = R.get_service(R.load_registry(), body.service_code)
        if not s:
            raise HTTPException(404, "unknown service code")
        return R.validate(s, body.answers)

    def _submitter(request: Request) -> str | None:
        """Which account is making this request, from the portal session.

        This used to read a separate resident-identity cookie that almost
        nobody had, so per-submitter limits were decorative. The portal session
        is the one thing every request already carries.
        """
        token = request.cookies.get(pa.SESSION_COOKIE)
        if not token:
            return None
        with pool_getter().connection() as conn:
            user = pa.session_user(conn, token)
        return user.user_id if user else None

    @router.post("/api/precheck")
    def precheck(body: PrecheckBody, request: Request):
        """Run the anti-abuse policy over a proposed submission.

        Records every evaluation, whatever the outcome: the rate limits count
        real attempts, and an audit trail holding only the refusals explains
        nothing. Nothing here reaches the City — the relay stays gated.
        """
        submitter_id = _submitter(request)
        key = abuse.normalize_address(body.address)
        proposed = abuse.Event(
            at=datetime.now(timezone.utc), address=body.address or "",
            category=body.service_name or body.service_code,
            submitter_id=submitter_id, description=body.description,
            source="ours")

        history: list[abuse.Event] = []
        if key:
            with pool_getter().connection() as conn:
                rows = adb.abuse_history(conn, address_key=key,
                                         sql_prefix=abuse.sql_prefix(body.address),
                                         submitter_id=submitter_id)
            for r in rows:
                # SQL narrowed by prefix; this is the exact match
                if r["source"] == "city" and abuse.normalize_address(r["address"]) != key:
                    continue
                history.append(abuse.Event(
                    at=r["at"], address=r["address"] or "", category=r["category"] or "",
                    submitter_id=r["submitter_id"], description=r["description"] or "",
                    closed_at=r["closed_at"], source=r["source"]))

        decision = abuse.evaluate(proposed, history,
                                  honeypot_filled=bool(body.website.strip()))

        findings = [{"rule": f.rule, "outcome": f.outcome, "message": f.message,
                     "detail": f.detail} for f in decision.findings]
        with pool_getter().connection() as conn:
            attempt_id = adb.record_attempt(
                conn, submitter_id=submitter_id, service_code=body.service_code,
                service_name=body.service_name, address=body.address, address_key=key,
                lat=body.lat, long=body.long, description=body.description,
                answers=body.answers, outcome=decision.outcome, findings=findings,
                cooldown_until=decision.cooldown_until,
                city_address=(body.city_address or "").strip() or None)

        return {"attempt_id": attempt_id, "outcome": decision.outcome,
                "may_proceed": decision.allowed, "findings": findings,
                "summary": abuse.summarize(decision),
                "cooldown_until": decision.cooldown_until,
                "history_considered": len(history),
                "identity_enforced": submitter_id is not None}

    # ------------------------------------------------------------- photos
    # Up to three photos on a request being prepared. The body is the file
    # itself (no multipart), so a phone can PUT what the camera made and a
    # browser can send a File as-is. Re-encoded on the way in — see
    # alex311.photos — and attached by the worker on the City's own form.

    def _own_open_attempt(conn, attempt_id: int, request: Request) -> dict:
        row = conn.execute(
            "SELECT attempt_id, submitter_id, submit_state FROM submission_attempts "
            "WHERE attempt_id = %s", (attempt_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such request")
        if row["submitter_id"] and row["submitter_id"] != _submitter(request):
            raise HTTPException(403, "that request was prepared by someone else")
        return row

    @router.get("/api/attempt/{attempt_id}/photos")
    def photos_list(attempt_id: int, request: Request, actor: str = Depends(gate)):
        with pool_getter().connection() as conn:
            _own_open_attempt(conn, attempt_id, request)
            return {"photos": photos.listing(conn, attempt_id=attempt_id),
                    "max": photos.MAX_PHOTOS}

    @router.post("/api/attempt/{attempt_id}/photos")
    async def photos_add(attempt_id: int, request: Request, name: str = "",
                         actor: str = Depends(gate)):
        """Add one photo. The request body is the image bytes."""
        declared = int(request.headers.get("content-length") or 0)
        if declared > photos.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "that photo is too large; 15 MB is the limit")
        data = await request.body()

        def store():
            with pool_getter().connection() as conn:
                row = _own_open_attempt(conn, attempt_id, request)
                if row["submit_state"] not in photos.OPEN_STATES:
                    raise HTTPException(409, "that request has already been sent")
                try:
                    return photos.add(conn, attempt_id=attempt_id, data=data, name=name)
                except photos.BadPhoto as e:
                    raise HTTPException(400, str(e))
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(store)        # decoding a photo is not event-loop work

    @router.delete("/api/attempt/{attempt_id}/photos/{photo_id}")
    def photos_remove(attempt_id: int, photo_id: int, request: Request,
                      actor: str = Depends(gate)):
        with pool_getter().connection() as conn:
            row = _own_open_attempt(conn, attempt_id, request)
            if row["submit_state"] not in photos.OPEN_STATES:
                raise HTTPException(409, "that request has already been sent")
            return {"removed": photos.remove(conn, attempt_id=attempt_id, photo_id=photo_id)}

    @router.post("/api/queue")
    def queue(body: QueueBody, request: Request, background: BackgroundTasks,
              actor: str = Depends(gate)):
        """Send a prepared request to the City. This is the live action.

        There used to be an administrator between this and the City. Testers
        found waiting for one worse than no feature at all, and it was removed
        deliberately: the press that reaches this endpoint is the last human
        step, and the environment gate on the job is the only thing left. The
        answer says so rather than implying somebody is still checking.

        The policy still speaks. `block` refuses outright; `review` is a warning
        the person has to acknowledge, which is feedback rather than moderation,
        and the acknowledgement is recorded against the request.
        """
        contact = {"first_name": body.first_name.strip(), "last_name": body.last_name.strip(),
                   "email": body.email.strip(), "phone": body.phone.strip()}
        with pool_getter().connection() as conn:
            row = conn.execute(
                "SELECT attempt_id, submitter_id, submit_state, outcome "
                "FROM submission_attempts WHERE attempt_id = %s",
                (body.attempt_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such attempt")
            mine = _submitter(request)
            if row["submitter_id"] and row["submitter_id"] != mine:
                # an attempt belongs to whoever prepared it
                raise HTTPException(403, "that request was prepared by someone else")
            if row["submit_state"] not in ("prepared", "queued"):
                raise HTTPException(409, f"that request is already {row['submit_state']}")
            if row["outcome"] == abuse.BLOCK:
                raise HTTPException(409, "the anti-abuse policy refused this one. "
                                         "Nothing was sent.")
            if row["outcome"] == abuse.REVIEW and not body.acknowledged:
                raise HTTPException(409, "this one looks like a repeat. Read the warning "
                                         "and send it again if you still mean to.")

            adb.queue_attempt(conn, body.attempt_id, contact)
            released = adb.approve_attempt(conn, body.attempt_id, actor)
            if not released:                       # somebody else got there first
                raise HTTPException(409, "that request is no longer waiting to be sent")
            adb.record_moderation(
                conn, attempt_id=body.attempt_id, actor=actor, action="release",
                reason=("acknowledged the policy warning and sent it"
                        if row["outcome"] == abuse.REVIEW else "sent by the person who made it"))

        # Fire and forget: a released request that the job did not start for is
        # still released, and the next release or the schedule collects it.
        background.add_task(job_runner.kick)
        return {"attempt_id": body.attempt_id, "submit_state": "approved",
                "filing": job_runner.configured(),
                "message": ("Sent. It is being filed with the City now — watch below for "
                            "their case number." if job_runner.configured() else
                            "Sent. It will be filed on the next run of the submission job.")}

    @router.get("/api/status/{attempt_id}")
    def attempt_status(attempt_id: int, request: Request,
                       creds: HTTPBasicCredentials | None = Depends(_security)):
        """Where one request has got to. Watched by whoever sent it.

        This is the real-time half: with nobody in the way, the feedback a
        tester gets has to come from the request itself.

        Ownership is decided by who pressed send, not by the resident-identity
        cookie: that cookie is optional in this deployment, so a check that
        skipped when it was absent would hand every signed-in tester the address
        on anybody's request. An administrator can read any of them, which is
        the same thing the status board already shows them.
        """
        actor, user = _identify(request, pool_getter, creds)
        with pool_getter().connection() as conn:
            row = adb.attempt_status(conn, attempt_id)
        if row is None:
            raise HTTPException(404, "no such request")
        an_admin = user is None or user.is_admin      # the shared credential counts
        mine = _submitter(request)
        owner = ((row["submitter_id"] is not None and row["submitter_id"] == mine)
                 or (row["approved_by"] is not None and row["approved_by"] == actor))
        if not (owner or an_admin):
            raise HTTPException(403, "that request was sent by someone else")
        return {"attempt_id": row["attempt_id"], "submit_state": row["submit_state"],
                "city_case_number": row["city_case_number"],
                "error": row["submit_error"], "tries": row["tries"],
                "sent_at": row["approved_at"] or row["queued_at"],
                # how many photos rode along, and what the City's form did with them
                "photos": row.get("photos") or 0, "photo_note": row.get("photo_note"),
                "filed_at": row["relayed_at"],
                "service_name": row["service_name"], "address": row["address"]}

    @router.get("/api/submission-queue")
    def submission_queue(limit: int = 50, actor: str = Depends(admin_only)):
        """Everything waiting to be filed, and what became of what already was.

        A moderation view: it carries other residents' addresses and what they
        reported, so it is not for every signed-in tester.
        """
        with pool_getter().connection() as conn:
            return {"queue": adb.submission_queue(conn, limit=limit)}

    @router.get("/api/instrument")
    def instrument(days: int = 30, actor: str = Depends(admin_only)):
        """Numbers for the status board: what the system did, and how fast."""
        with pool_getter().connection() as conn:
            data = adb.instrumentation(conn, days=days)
        data["filing_starts_immediately"] = job_runner.configured()
        return data

    @router.post("/api/retry/{attempt_id}")
    def retry(attempt_id: int, background: BackgroundTasks,
              actor: str = Depends(admin_only)):
        """Put a failed request back in the queue.

        Not a moderation step returning by another name: the person who made
        the request already sent it, and this only applies to rows the worker
        could not file — usually because the City's form moved.
        """
        with pool_getter().connection() as conn:
            ok = adb.retry_attempt(conn, attempt_id, actor)
        if not ok:
            raise HTTPException(409, "that request is not one that failed")
        background.add_task(job_runner.kick)
        return {"attempt_id": attempt_id, "submit_state": "approved",
                "message": "Back in the queue. It will be filed with the City."}

    @router.get("/api/review-queue")
    def queue(limit: int = 50, actor: str = Depends(admin_only)):
        """Attempts a human still needs to look at.

        Same reasoning as the submission queue: other people's reports.
        """
        with pool_getter().connection() as conn:
            return {"queue": adb.review_queue(conn, limit)}

    @router.post("/api/review/{attempt_id}")
    def moderate(attempt_id: int, action: str, reason: str | None = None,
                 actor: str = Depends(admin_only)):
        """Record a human decision. Append-only: a reversal is a new row.

        `approve` and `reject` used to live here and were dropped with the
        review desk: there is nothing left to approve, and a recorded rejection
        of a request the City already has would only mislead whoever read it.
        """
        if action not in ("block_submitter", "note"):
            raise HTTPException(400, "unknown action")
        with pool_getter().connection() as conn:
            action_id = adb.record_moderation(conn, attempt_id=attempt_id,
                                              actor=actor, action=action, reason=reason)
            if action == "block_submitter":
                row = conn.execute(
                    "SELECT submitter_id FROM submission_attempts WHERE attempt_id = %s",
                    (attempt_id,)).fetchone()
                if not row or not row["submitter_id"]:
                    raise HTTPException(400, "that attempt has no account behind it to block")
                if not pa.count_admins(conn, excluding=row["submitter_id"]):
                    raise HTTPException(409, "that is the last administrator")
                pa.set_disabled(conn, row["submitter_id"], True)
        return {"action_id": action_id, "attempt_id": attempt_id, "action": action}

    @router.get("/api/geocode")
    def geocode(q: str, limit: int = 6):
        """Turn a typed address into coordinates, using the City's own records.

        We hold 16,000-odd distinct Alexandria addresses that the City itself
        geocoded when it logged a request there. Matching against those beats an
        external geocoder on three counts: the resident's address never leaves
        our infrastructure, there is no rate limit or third-party dependency,
        and a match returns *the coordinates the City already uses for that
        address*, so the request lands where they expect it.

        The cost is coverage: an address that has never had a 311 request is not
        in here. Those residents drop a pin or use their location instead, which
        is why neither path is required.
        """
        key = abuse.normalize_address(q)
        # Both spellings of the directional: the City abbreviates most of its
        # addresses and spells out a few, and a resident may type either.
        prefixes = [p.replace("%", r"\%").replace("_", r"\_") + "%"
                    for p in abuse.sql_prefixes(q) if len(p) >= 2]
        if not key or not prefixes:
            return {"query": q, "candidates": [], "source": "city-records"}

        with pool_getter().connection() as conn:
            rows = conn.execute(
                f"""SELECT address,
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY lat)  AS lat,
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY long) AS long,
                           count(*) AS seen
                      FROM service_requests
                     WHERE lat IS NOT NULL AND long IS NOT NULL
                       AND {abuse.SQL_ADDRESS_EXPR} LIKE ANY(%s)
                     GROUP BY address
                     ORDER BY count(*) DESC
                     LIMIT 60""",
                (prefixes,),
            ).fetchall()

        cands = merge_candidates(rows, key)[:limit]

        # The one we would file under, and whether it reads differently from
        # what they typed. A tester found this step hard, and the reason was
        # that nothing ever told them the City spells their street its own way
        # — they typed a correct address, got a match, and the request still
        # went to the City in wording its gazetteer does not recognize.
        # Only an exact match earns a suggestion. A prefix match is not the
        # same address: "500 north st" finds "500 NORTH VIEW TER", and telling
        # someone "the City files this address as 500 NORTH VIEW TER" asserts
        # an identity that is false. Near misses still appear in the candidate
        # list, where the wording asks rather than tells.
        best = cands[0] if cands and cands[0]["exact"] else None
        suggestion = None
        if best:
            suggestion = {
                "address": best["address"],
                "lat": best["lat"], "long": best["long"],
                "seen": best["seen"], "exact": True,
                # differs in what a person would notice, not in whitespace
                "differs": abuse.normalize_address(q) != abuse.normalize_address(best["address"])
                           or q.strip().upper() != best["address"].strip().upper(),
            }
        return {"query": q, "normalized": key, "source": "city-records",
                "suggestion": suggestion, "candidates": cands}

    @router.get("/api/reverse")
    def reverse(lat: float, long: float, within_m: int = 250):
        """The nearest address the City has used near a point.

        Used after "use my location" so the address box fills itself. Same
        gazetteer, same reason: it returns the City's own wording for the place.
        """
        dlat, dlng = within_m / 111_320, within_m / 87_000
        with pool_getter().connection() as conn:
            rows = conn.execute(
                """SELECT address, lat, long FROM service_requests
                    WHERE lat BETWEEN %s AND %s AND long BETWEEN %s AND %s
                      AND address IS NOT NULL
                    LIMIT 400""",
                (lat - dlat, lat + dlat, long - dlng, long + dlng),
            ).fetchall()
        best = None
        for r in rows:
            d = _haversine_m(lat, long, r["lat"], r["long"])
            if d <= within_m and (best is None or d < best["meters"]):
                best = {"address": r["address"], "lat": r["lat"], "long": r["long"],
                        "meters": round(d)}
        return {"nearest": best, "source": "city-records"}

    @router.get("/api/nearby")
    def nearby(lat: float, long: float, category: str, days: int = 45):
        """Recent same-category requests within ~250m — duplicate warning.

        The feature the official portal structurally cannot offer: we hold
        the full history, so a resident can see their issue is already
        reported before filing a second ticket."""
        dlat, dlng = 0.00225, 0.00290  # ~250m box at Alexandria's latitude
        with pool_getter().connection() as conn:
            rows = conn.execute(
                """SELECT service_request_id, service_name, address, status,
                          requested_datetime, closed_datetime, lat, long
                   FROM service_requests
                   WHERE service_name = %s
                     AND lat BETWEEN %s AND %s AND long BETWEEN %s AND %s
                     AND requested_datetime > now() - make_interval(days => %s)
                   ORDER BY requested_datetime DESC LIMIT 25""",
                (category, lat - dlat, lat + dlat, long - dlng, long + dlng, days),
            ).fetchall()
        out = []
        for r in rows:
            r["meters"] = round(_haversine_m(lat, long, r["lat"], r["long"]))
            r["is_open"] = r["closed_datetime"] is None
            out.append(r)
        out = [r for r in out if r["meters"] <= 250]
        out.sort(key=lambda x: (not x["is_open"], x["meters"]))
        return {"nearby": out, "open_count": sum(r["is_open"] for r in out)}

    app.include_router(router)
