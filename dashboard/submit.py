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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from alex311 import abuse, db as adb, identity as ident

from . import registry as R

_security = HTTPBasic()


def _auth(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    user = os.environ.get("SUBMIT_USER", "alex311user")
    pw = os.environ.get("SUBMIT_PASSWORD", "")
    ok = bool(pw) and secrets.compare_digest(creds.username, user) \
        and secrets.compare_digest(creds.password, pw)
    if not ok:
        raise HTTPException(
            status_code=401, detail="invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Alex311 submit (beta)"'},
        )
    return creds.username


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


class ValidateBody(BaseModel):
    service_code: str
    answers: dict = {}


SESSION_COOKIE = "alex311_submitter"


class AuthStart(BaseModel):
    email: str


class AuthConfirm(BaseModel):
    email: str
    code: str


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


def register_submit_routes(app, pool_getter, sender=None) -> None:
    """Attach the gated /submit routes. pool_getter() returns the live pool.

    Two layers of access, doing different jobs. HTTP Basic decides who may see
    the prototype at all while the authorization question with the City is
    open. The session below decides *which resident* is submitting, which is
    what the per-submitter abuse rules need.
    """
    sender = sender or ident.sender_from_env()
    router = APIRouter(prefix="/submit", dependencies=[Depends(_auth)])
    page = Path(__file__).parent / "submit.html"

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def submit_page():
        return page.read_text()

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

    @router.get("/api/review-queue")
    def queue(limit: int = 50):
        """Attempts a human still needs to look at."""
        with pool_getter().connection() as conn:
            return {"queue": adb.review_queue(conn, limit)}

    @router.post("/api/review/{attempt_id}")
    def moderate(attempt_id: int, action: str, reason: str | None = None,
                 actor: str = Depends(_auth)):
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
