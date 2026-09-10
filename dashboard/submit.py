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

import math
import os
import secrets
from urllib.parse import parse_qs
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from alex311 import abuse, db as adb, identity as ident, portal_auth as pa

from . import registry as R

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


class NewUser(BaseModel):
    email: str
    role: str = "user"


class ValidateBody(BaseModel):
    service_code: str
    answers: dict = {}


SESSION_COOKIE = "alex311_submitter"


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


class PrecheckBody(BaseModel):
    """A proposed submission, as the form would send it."""
    service_code: str
    service_name: str | None = None
    address: str | None = None
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
            return user.email, user
    if _basic_ok(creds):
        return os.environ.get("SUBMIT_USER", "alex311user"), None
    raise Unauthenticated()


def register_submit_routes(app, pool_getter, sender=None) -> None:
    """Attach the gated /submit routes. pool_getter() returns the live pool.

    Two layers of access, doing different jobs. HTTP Basic decides who may see
    the prototype at all while the authorization question with the City is
    open. The session below decides *which resident* is submitting, which is
    what the per-submitter abuse rules need.
    """
    sender = sender or ident.sender_from_env()

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

    @public.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if request.cookies.get(pa.SESSION_COOKIE):
            with pool_getter().connection() as conn:
                if pa.session_user(conn, request.cookies[pa.SESSION_COOKIE]):
                    return RedirectResponse("/submit", status_code=303)
        return HTMLResponse(login_page.read_text())

    @public.post("/login")
    async def do_login(request: Request):
        # Parsed by hand rather than with fastapi's Form(), which would pull in
        # python-multipart. A urlencoded body needs no such thing, and the
        # dependency would ship in every image for one endpoint.
        fields = parse_qs((await request.body()).decode("utf-8", "replace"))
        email = (fields.get("email") or [""])[0]
        password = (fields.get("password") or [""])[0]
        with pool_getter().connection() as conn:
            token = pa.login(conn, email, password,
                             user_agent=request.headers.get("user-agent"))
        if not token:
            return RedirectResponse("/submit/login?error=1", status_code=303)
        resp = RedirectResponse("/submit", status_code=303)
        resp.set_cookie(pa.SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/submit",
                        max_age=int(pa.SESSION_TTL.total_seconds()))
        return resp

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
        return {"user": ({"user_id": user.user_id, "email": user.email, "role": user.role}
                         if user else None)}

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
                user, password = pa.create_user(conn, body.email, body.role, created_by=actor)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except Exception:
                raise HTTPException(409, "that email already has an account")
        return {"user_id": user.user_id, "email": user.email, "role": user.role,
                "password": password}

    @router.post("/api/users/{user_id}/reset")
    def users_reset(user_id: str, actor: str = Depends(admin_only)):
        with pool_getter().connection() as conn:
            row = conn.execute("SELECT email FROM portal_users WHERE user_id = %s",
                               (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no such user")
            password = pa.set_password(conn, user_id)
        return {"user_id": user_id, "email": row["email"], "password": password}

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

    @router.get("/api/registry")
    def registry_index():
        reg = R.load_registry()
        return {"generated": reg["generated"], "sources": reg.get("sources"),
                "services": R.service_index(reg)}

    @router.get("/api/service/{code}")
    def service(code: str):
        s = R.get_service(R.load_registry(), code)
        if not s:
            raise HTTPException(404, "unknown service code")
        return s

    @router.post("/api/validate")
    def validate(body: ValidateBody):
        """Server-authoritative check of a category's answers against the registry."""
        s = R.get_service(R.load_registry(), body.service_code)
        if not s:
            raise HTTPException(404, "unknown service code")
        return R.validate(s, body.answers)

    def _submitter(request: Request) -> str | None:
        """Who is signed in, according to the session cookie alone."""
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        with pool_getter().connection() as conn:
            return ident.session_submitter(conn, token)

    @router.get("/api/auth/me")
    def whoami(request: Request):
        sid = _submitter(request)
        return {"signed_in": sid is not None, "submitter_id": sid}

    @router.post("/api/auth/start")
    def auth_start(body: AuthStart):
        """Send a one-time code.

        The reply is identical whether or not the address is known, and whether
        or not it was rate limited. Anything else turns this into a way to test
        which addresses have accounts.
        """
        try:
            with pool_getter().connection() as conn:
                ident.start_verification(conn, body.email, sender)
        except ident.InvalidEmail:
            raise HTTPException(400, "that does not look like an email address")
        return {"sent": True,
                "message": "If that address can receive mail, a code is on its way."}

    @router.post("/api/auth/confirm")
    def auth_confirm(body: AuthConfirm, request: Request, response: Response):
        try:
            with pool_getter().connection() as conn:
                session = ident.confirm_verification(
                    conn, body.email, body.code,
                    user_agent=request.headers.get("user-agent"))
        except ident.InvalidEmail:
            raise HTTPException(400, "that does not look like an email address")
        except ident.VerificationFailed:
            raise HTTPException(400, "that code is not valid")
        response.set_cookie(
            SESSION_COOKIE, session.token, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", path="/submit",
            expires=session.expires_at)
        return {"submitter_id": session.submitter_id,
                "expires_at": session.expires_at}

    @router.post("/api/auth/signout")
    def auth_signout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            with pool_getter().connection() as conn:
                ident.revoke_session(conn, token)
        response.delete_cookie(SESSION_COOKIE, path="/submit")
        return {"signed_out": True}

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
                cooldown_until=decision.cooldown_until)

        return {"attempt_id": attempt_id, "outcome": decision.outcome,
                "may_proceed": decision.allowed, "findings": findings,
                "summary": abuse.summarize(decision),
                "cooldown_until": decision.cooldown_until,
                "history_considered": len(history),
                "identity_enforced": submitter_id is not None}

    @router.post("/api/queue")
    def queue(body: QueueBody, request: Request):
        """Hand a prepared request to the submission queue.

        Queuing is not filing and not approval. A person still has to approve
        the row, and only the live job can act on it — a browser cannot run in
        this service.
        """
        contact = {"first_name": body.first_name.strip(), "last_name": body.last_name.strip(),
                   "email": body.email.strip(), "phone": body.phone.strip()}
        with pool_getter().connection() as conn:
            row = conn.execute(
                "SELECT attempt_id, submitter_id, submit_state FROM submission_attempts "
                "WHERE attempt_id = %s", (body.attempt_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such attempt")
            mine = _submitter(request)
            if row["submitter_id"] and row["submitter_id"] != mine:
                # an attempt belongs to whoever prepared it
                raise HTTPException(403, "that request was prepared by someone else")
            if row["submit_state"] not in ("prepared", "queued"):
                raise HTTPException(409, f"that request is already {row['submit_state']}")
            adb.queue_attempt(conn, body.attempt_id, contact)
        return {"attempt_id": body.attempt_id, "submit_state": "queued",
                "message": "Queued. An administrator has to release it before it is filed."}

    @router.get("/api/submission-queue")
    def submission_queue(limit: int = 50, actor: str = Depends(admin_only)):
        """Everything waiting to be filed, and what became of what already was.

        A moderation view: it carries other residents' addresses and what they
        reported, so it is not for every signed-in tester.
        """
        with pool_getter().connection() as conn:
            return {"queue": adb.submission_queue(conn, limit=limit)}

    @router.post("/api/approve/{attempt_id}")
    def approve(attempt_id: int, actor: str = Depends(admin_only)):
        """Release a queued request for filing. Administrators only.

        This is the per-request half of the live gate, and the reason it is not
        self-service: with `gate` any signed-in tester could release their own
        request, which collapses the two keys into one and means a real City
        record can be created with nobody else involved.
        """
        with pool_getter().connection() as conn:
            ok = adb.approve_attempt(conn, attempt_id, actor)
            if not ok:
                raise HTTPException(409, "that request is not queued")
            adb.record_moderation(conn, attempt_id=attempt_id, actor=actor,
                                  action="approve", reason="approved for filing")
        return {"attempt_id": attempt_id, "submit_state": "approved",
                "message": "Released. This is the live action: the submission job will "
                           "file it with the City on its next run, and it cannot be "
                           "recalled afterwards."}

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
        """Record a human decision. Append-only: a reversal is a new row."""
        if action not in ("approve", "reject", "block_submitter", "note"):
            raise HTTPException(400, "unknown action")
        with pool_getter().connection() as conn:
            action_id = adb.record_moderation(conn, attempt_id=attempt_id,
                                              actor=actor, action=action, reason=reason)
            if action == "block_submitter":
                row = conn.execute(
                    "SELECT submitter_id FROM submission_attempts WHERE attempt_id = %s",
                    (attempt_id,)).fetchone()
                if not row or not row["submitter_id"]:
                    raise HTTPException(400, "that attempt has no submitter to block")
                ident.block_submitter(conn, row["submitter_id"],
                                      reason or "blocked from the review queue")
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
        prefix = abuse.sql_prefix(q)
        if not key or len(prefix) < 2:
            return {"query": q, "candidates": [], "source": "city-records"}

        with pool_getter().connection() as conn:
            rows = conn.execute(
                f"""SELECT address,
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY lat)  AS lat,
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY long) AS long,
                           count(*) AS seen
                      FROM service_requests
                     WHERE lat IS NOT NULL AND long IS NOT NULL
                       AND {abuse.SQL_ADDRESS_EXPR} LIKE %s
                     GROUP BY address
                     ORDER BY count(*) DESC
                     LIMIT 60""",
                (prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
            ).fetchall()

        return {"query": q, "normalized": key, "source": "city-records",
                "candidates": merge_candidates(rows, key)[:limit]}

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
