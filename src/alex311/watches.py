"""Watches: follow a request type, an address (or several), or an area.

A request link (`db.link_request`) follows one case. A watch follows a
*kind* of thing, and the feed on My requests shows every request the mirror
has seen since the person last looked that matches any of their watches —
new ones, and ones whose City record changed.

Three kinds, one table (`watches`), forced row-level security like the rest
of the account tables. The `spec` is JSON, shaped per kind:

    category  {"service_name": "Noise Issues"}
    address   {"addresses": [{"text": "500 N PITT ST", "prefixes": ["500 N PITT"],
                              "lat": 38.8, "long": -77.0}, ...], "nearby": false}
    area      {"polygon": [[lng, lat], ...]}

Matching is done in SQL against `service_requests`, the same way the pages
already do it: a category by name; an address by the City's own spelling
(the prefix up to the street-type word, the way the report form's suggestion
finds it), optionally widened to roughly a block around the address's
coordinates; an area by the built-in `point <@ polygon` test Explore uses
for draw-an-area. No PostGIS, nothing the shared Cloud SQL instance lacks.

A watch is personal data — an address is often a home, a polygon a block —
so it is never shown to anyone else and goes with the account when the
account is deleted.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from . import abuse, db

KINDS = ("category", "address", "area")
MAX_WATCHES = 20
MAX_ADDRESSES = 10
MAX_POLYGON_POINTS = 100
NEARBY_METERS = 150.0
FEED_LIMIT = 100

# Alexandria, with margin: a polygon or an address outside it is a mistake.
LAT_RANGE = (38.70, 38.95)
LONG_RANGE = (-77.25, -76.95)


class BadWatch(ValueError):
    pass


# --------------------------------------------------------------- specs

def category_spec(conn: psycopg.Connection, payload: dict) -> tuple[str, dict]:
    name = " ".join(str(payload.get("service_name") or "").split())
    if not name:
        raise BadWatch("pick a request type")
    row = conn.execute("SELECT 1 FROM service_requests WHERE service_name = %s LIMIT 1",
                       (name,)).fetchone()
    if not row:
        raise BadWatch("that is not a request type the City has used")
    return name, {"service_name": name}


def address_spec(conn: psycopg.Connection, payload: dict) -> tuple[str, dict]:
    raw = payload.get("addresses") or []
    if isinstance(raw, str):
        raw = [raw]
    texts = [" ".join(str(a).split()) for a in raw if str(a).strip()]
    if not texts:
        raise BadWatch("give at least one address")
    if len(texts) > MAX_ADDRESSES:
        raise BadWatch(f"at most {MAX_ADDRESSES} addresses in one watch")
    out, seen = [], set()
    for text in texts:
        prefixes = abuse.sql_prefixes(text)
        if not prefixes or not any(ch.isdigit() for ch in prefixes[0].split(" ")[0]):
            raise BadWatch(f"'{text}' does not look like a street address (number and street)")
        key = abuse.normalize_address(text)
        if key in seen:
            continue
        seen.add(key)
        # The City's own spelling and coordinates, where it has used this
        # address: that is what "nearby" measures from, and what the person
        # sees on the watch instead of what they typed.
        best = conn.execute(
            f"""SELECT address, avg(lat) AS lat, avg(long) AS long, count(*) AS n
                  FROM service_requests
                 WHERE {abuse.SQL_ADDRESS_EXPR} LIKE ANY(%s)
                   AND lat IS NOT NULL AND long IS NOT NULL
                 GROUP BY address ORDER BY n DESC LIMIT 20""",
            ([p + " %" for p in prefixes],)).fetchall()
        exact = [b for b in best if abuse.normalize_address(b["address"]) == key]
        pick = exact[0] if exact else None
        out.append({"text": pick["address"] if pick else text.upper(),
                    "prefixes": prefixes,
                    "lat": float(pick["lat"]) if pick else None,
                    "long": float(pick["long"]) if pick else None,
                    "known": bool(pick)})
    nearby = bool(payload.get("nearby"))
    label = out[0]["text"] + (f" and {len(out) - 1} more" if len(out) > 1 else "")
    if nearby:
        label += " (and nearby)"
    return label, {"addresses": out, "nearby": nearby}


def area_spec(conn: psycopg.Connection, payload: dict) -> tuple[str, dict]:
    pts = payload.get("polygon") or []
    if isinstance(pts, str):                       # Explore's 'lng,lat;lng,lat' form
        try:
            pts = [[float(a) for a in pair.split(",")] for pair in pts.split(";") if pair]
        except ValueError:
            raise BadWatch("that polygon could not be read")
    clean = []
    for p in pts:
        try:
            lng, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise BadWatch("that polygon could not be read")
        if not (LONG_RANGE[0] <= lng <= LONG_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
            raise BadWatch("the area has to be in Alexandria")
        clean.append([round(lng, 6), round(lat, 6)])
    if len(clean) < 3:
        raise BadWatch("an area needs at least three corners")
    if len(clean) > MAX_POLYGON_POINTS:
        raise BadWatch(f"at most {MAX_POLYGON_POINTS} corners")
    n = conn.execute(
        "SELECT count(*) AS n FROM service_requests WHERE lat IS NOT NULL "
        "AND point(long, lat) <@ %s::polygon", (polygon_literal(clean),)).fetchone()["n"]
    label = f"Drawn area ({n} request{'' if n == 1 else 's'} on record)"
    return label, {"polygon": clean}


def polygon_literal(points: list[list[float]]) -> str:
    return "(" + ",".join(f"({lng},{lat})" for lng, lat in points) + ")"


SPEC_BUILDERS = {"category": category_spec, "address": address_spec, "area": area_spec}


# --------------------------------------------------------------- storage

def add_watch(conn: psycopg.Connection, *, user_id: str, kind: str, payload: dict,
              label: str | None = None) -> dict:
    """Validate, normalize and store one watch. Raises BadWatch with a
    sentence a person can act on."""
    if kind not in KINDS:
        raise BadWatch("kind must be category, address or area")
    auto_label, spec = SPEC_BUILDERS[kind](conn, payload or {})
    label = " ".join((label or "").split())[:80] or auto_label
    db.as_user(conn, user_id)
    n = conn.execute("SELECT count(*) AS n FROM watches WHERE user_id = %s",
                     (user_id,)).fetchone()["n"]
    if n >= MAX_WATCHES:
        raise BadWatch(f"that is {MAX_WATCHES} watches already; remove one first")
    dup = conn.execute(
        "SELECT watch_id FROM watches WHERE user_id = %s AND kind = %s AND spec = %s",
        (user_id, kind, Jsonb(spec))).fetchone()
    if dup:
        conn.rollback()
        return {"watch_id": dup["watch_id"], "kind": kind, "label": label, "spec": spec,
                "existing": True}
    row = conn.execute(
        """INSERT INTO watches (user_id, kind, label, spec)
           VALUES (%s, %s, %s, %s) RETURNING watch_id, created_at""",
        (user_id, kind, label, Jsonb(spec))).fetchone()
    conn.commit()
    return {"watch_id": row["watch_id"], "kind": kind, "label": label, "spec": spec,
            "created_at": row["created_at"], "existing": False}


def remove_watch(conn: psycopg.Connection, *, user_id: str, watch_id: int) -> bool:
    db.as_user(conn, user_id)
    n = conn.execute("DELETE FROM watches WHERE user_id = %s AND watch_id = %s",
                     (user_id, watch_id)).rowcount
    conn.commit()
    return n > 0


def list_watches(conn: psycopg.Connection, *, user_id: str) -> list[dict]:
    db.as_user(conn, user_id)
    return conn.execute(
        """SELECT watch_id, kind, label, spec, created_at
             FROM watches WHERE user_id = %s ORDER BY created_at DESC""",
        (user_id,)).fetchall()


# ---------------------------------------------------------------- matching

def _clause(w: dict) -> tuple[str, list]:
    """One watch as a SQL condition over service_requests r, with params."""
    spec = w["spec"] if isinstance(w["spec"], dict) else json.loads(w["spec"])
    if w["kind"] == "category":
        return "r.service_name = %s", [spec["service_name"]]
    if w["kind"] == "area":
        return "(r.lat IS NOT NULL AND point(r.long, r.lat) <@ %s::polygon)", \
               [polygon_literal(spec["polygon"])]
    parts, params = [], []
    for a in spec["addresses"]:
        parts.append(f"({abuse.SQL_ADDRESS_EXPR} LIKE ANY(%s))")
        params.append([p + " %" for p in a["prefixes"]] + list(a["prefixes"]))
        if spec.get("nearby") and a.get("lat") is not None:
            dlat = NEARBY_METERS / 111_320.0
            dlng = dlat / max(math.cos(math.radians(a["lat"])), 0.2)
            parts.append("(r.lat BETWEEN %s AND %s AND r.long BETWEEN %s AND %s)")
            params += [a["lat"] - dlat, a["lat"] + dlat, a["long"] - dlng, a["long"] + dlng]
    return "(" + " OR ".join(parts) + ")", params


def feed(conn: psycopg.Connection, *, user_id: str, since: datetime | None,
         limit: int = FEED_LIMIT) -> dict:
    """Requests seen or changed since `since` that match any of the account's
    watches, newest first, each saying which watches it matched."""
    watches = list_watches(conn, user_id=user_id)
    conn.rollback()                                   # drop the RLS setting before the big read
    if not watches:
        return {"since": since, "watches": [], "items": [], "total": 0}
    since = since or datetime(2000, 1, 1, tzinfo=timezone.utc)
    conds, tags = [], []
    for w in watches:
        c, _ = _clause(w)
        conds.append(c)
        tags.append(f"CASE WHEN {c} THEN %s END")
    sql = f"""
        SELECT r.service_request_id, r.service_name, r.status, r.address, r.lat, r.long,
               r.requested_datetime, r.last_updated_datetime, r.first_seen_at,
               (r.first_seen_at > %s) AS is_new,
               ARRAY_REMOVE(ARRAY[{", ".join(tags)}], NULL) AS watch_ids
          FROM service_requests r
         WHERE (r.first_seen_at > %s OR r.last_updated_datetime > %s)
           AND ({" OR ".join(conds)})
         ORDER BY GREATEST(r.first_seen_at, COALESCE(r.last_updated_datetime, r.first_seen_at)) DESC
         LIMIT %s"""
    # parameter order follows the text: conds first (in the WHERE), then the tags (in SELECT)
    where_params = []
    tag_params = []
    for w in watches:
        c, p = _clause(w)
        where_params += p
        tag_params += p + [w["watch_id"]]
    rows = conn.execute(sql, [since] + tag_params + [since, since] + where_params + [limit]).fetchall()
    conn.rollback()
    return {"since": since, "watches": watches, "items": rows, "total": len(rows)}


def mark_seen(conn: psycopg.Connection, *, user_id: str, at: datetime | None = None) -> datetime:
    at = at or datetime.now(timezone.utc)
    conn.execute("UPDATE portal_users SET feed_seen_at = %s WHERE user_id = %s", (at, user_id))
    conn.commit()
    return at


def seen_at(conn: psycopg.Connection, *, user_id: str) -> datetime | None:
    row = conn.execute("SELECT feed_seen_at FROM portal_users WHERE user_id = %s",
                       (user_id,)).fetchone()
    return row["feed_seen_at"] if row else None
