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
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

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


class ValidateBody(BaseModel):
    service_code: str
    answers: dict = {}


def register_submit_routes(app, pool_getter) -> None:
    """Attach the gated /submit routes. pool_getter() returns the live pool."""
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
