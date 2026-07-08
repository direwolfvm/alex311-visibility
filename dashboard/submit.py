"""Prototype submission intake (Option A: prefill & hand off to the city).

Gated behind HTTP Basic auth for testing — this is write-adjacent and must
NOT be publicly reachable until the authorization question with the city is
settled. Nothing here submits to the city portal; it collects a clean
request, warns about likely duplicates using our own data, and produces a
handoff (portal link + copyable summary) the resident completes officially.

Scoped to the "missed yard waste collection" scenario for this first cut.
"""
from __future__ import annotations

import math
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()

# The three real categories a "missed yard waste" report maps to, with the
# plain-language prompt we show the resident. Verified present in our data.
YARD_WASTE_SCENARIOS = [
    {"key": "missed", "category": "Missed Collection",
     "label": "My yard waste wasn't picked up on my collection day"},
    {"key": "bulk", "category": "Bulk Yard Waste Pickup",
     "label": "I need a pickup for a large pile of branches/yard debris"},
    {"key": "leaf", "category": "Missed Leaf Collection",
     "label": "My leaf collection was missed"},
]
_VALID_CATEGORIES = {s["category"] for s in YARD_WASTE_SCENARIOS}


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


def register_submit_routes(app, pool_getter) -> None:
    """Attach the gated /submit routes. pool_getter() returns the live pool."""
    router = APIRouter(prefix="/submit", dependencies=[Depends(_auth)])
    page = Path(__file__).parent / "submit.html"

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def submit_page():
        return page.read_text()

    @router.get("/api/scenarios")
    def scenarios():
        return {"scenarios": YARD_WASTE_SCENARIOS}

    @router.get("/api/nearby")
    def nearby(lat: float, long: float, category: str, days: int = 45):
        """Recent same-category requests within ~250m — duplicate warning.

        This is the feature the official portal structurally cannot offer:
        we hold the full history, so we can tell a resident their issue is
        already reported before they file a second ticket."""
        if category not in _VALID_CATEGORIES:
            raise HTTPException(400, "unsupported category for this prototype")
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
