"""Dashboard API + static frontend for the Alex311 visibility portal.

Run locally:
  DATABASE_URL=postgresql://... MEDIA_DIR=./media_store uv run uvicorn dashboard.app:app --reload

Reads only from Postgres and the media store — never touches the
Salesforce portal, so it is safe to scale and cache.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from alex311.client import Alex311Client
from alex311.media_store import GcsMediaStore, LocalMediaStore, store_from_env

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = ConnectionPool(
        os.environ["DATABASE_URL"],
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        kwargs={"row_factory": dict_row},
    )
    yield
    pool.close()


app = FastAPI(title="Alex311 Visibility", lifespan=lifespan)


def _parse_polygon(spec: str) -> str:
    """'lng,lat;lng,lat;...' -> a Postgres polygon literal.

    Uses the built-in geometric polygon type (point <@ polygon), so no
    PostGIS extension is needed on the shared Cloud SQL instance.
    """
    try:
        pts = []
        for pair in spec.split(";"):
            lng_s, lat_s = pair.split(",")
            lng, lat = float(lng_s), float(lat_s)
            if not (-180.0 <= lng <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError(pair)
            pts.append((lng, lat))
    except ValueError:
        raise HTTPException(400, "polygon must be 'lng,lat;lng,lat;...'")
    if not 3 <= len(pts) <= 200:
        raise HTTPException(400, "polygon needs 3 to 200 vertices")
    return "(" + ",".join(f"({lng},{lat})" for lng, lat in pts) + ")"


def _filters(
    start: datetime | None,
    end: datetime | None,
    category: list[str] | None,
    status: str | None,
    q: str | None,
    polygon: str | None = None,
):
    where, params = ["TRUE"], []
    if start:
        where.append("requested_datetime >= %s")
        params.append(start)
    if end:
        where.append("requested_datetime < %s")
        params.append(end)
    if category:
        where.append("service_name = ANY(%s)")
        params.append(category)
    if status and status.lower() != "all":
        where.append("lower(status) = lower(%s)")
        params.append(status)
    if q:
        where.append("(address ILIKE %s OR description ILIKE %s OR service_request_id = %s)")
        like = f"%{q}%"
        params.extend([like, like, q])
    if polygon:
        where.append("lat IS NOT NULL AND long IS NOT NULL "
                     "AND point(long, lat) <@ %s::polygon")
        params.append(_parse_polygon(polygon))
    return " AND ".join(where), params


@app.get("/api/meta")
def meta():
    with pool.connection() as conn:
        cats = conn.execute(
            """SELECT service_name, count(*) AS n FROM service_requests
               WHERE service_name IS NOT NULL
               GROUP BY service_name ORDER BY n DESC"""
        ).fetchall()
        rng = conn.execute(
            """SELECT min(requested_datetime) AS min_date,
                      max(requested_datetime) AS max_date,
                      count(*) AS total,
                      count(*) FILTER (WHERE lower(status) = 'open') AS open
               FROM service_requests"""
        ).fetchone()
        last_run = conn.execute(
            """SELECT finished_at, ok FROM ingest_runs
               WHERE kind IN ('incremental','backfill') AND finished_at IS NOT NULL
               ORDER BY run_id DESC LIMIT 1"""
        ).fetchone()
    return {
        "categories": cats,
        "min_date": rng["min_date"],
        "max_date": rng["max_date"],
        "total": rng["total"],
        "open": rng["open"],
        "last_ingest": last_run,
    }


# whitelist: sort key from the UI -> real column (never interpolate user input)
SORT_COLUMNS = {
    "case": "service_request_id",
    "requested": "requested_datetime",
    "updated": "last_updated_datetime",
    "category": "service_name",
    "status": "status",
    "address": "address",
    "photos": "media_count",
}


@app.get("/api/requests")
def requests_list(
    start: datetime | None = None,
    end: datetime | None = None,
    category: list[str] | None = Query(None),
    status: str | None = None,
    q: str | None = None,
    polygon: str | None = None,
    sort: str = "requested",
    dir: str = "desc",
    limit: int = Query(500, le=2000),
    offset: int = 0,
):
    if sort not in SORT_COLUMNS:
        raise HTTPException(400, f"sort must be one of {sorted(SORT_COLUMNS)}")
    if dir not in ("asc", "desc"):
        raise HTTPException(400, "dir must be asc|desc")
    order = f"{SORT_COLUMNS[sort]} {dir.upper()} NULLS LAST, service_request_id DESC"
    where, params = _filters(start, end, category, status, q, polygon)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT sr.service_request_id, sr.status, sr.service_name,
                       sr.lat, sr.long, sr.address, sr.requested_datetime,
                       sr.last_updated_datetime, sr.closed_datetime,
                       sr.description, sr.media_count,
                       COALESCE(m.media_ids, '{{}}') AS media_ids
                FROM service_requests sr
                LEFT JOIN (
                    SELECT service_request_id,
                           array_agg(media_id ORDER BY created_datetime) AS media_ids
                    FROM media WHERE downloaded_at IS NOT NULL
                    GROUP BY service_request_id
                ) m USING (service_request_id)
                WHERE {where}
                ORDER BY {order}
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        ).fetchall()
        total = conn.execute(
            f"SELECT count(*) AS n FROM service_requests WHERE {where}", params
        ).fetchone()["n"]
    for r in rows:
        r["report_url"] = Alex311Client.deep_link(r["service_request_id"])
    return {"total": total, "rows": rows}


@app.get("/api/points")
def points(
    start: datetime | None = None,
    end: datetime | None = None,
    category: list[str] | None = Query(None),
    status: str | None = None,
    q: str | None = None,
    polygon: str | None = None,
):
    """Every geocoded record matching the filters, as compact arrays.

    Feeds the map independently of table pagination, so the dots always
    reflect the full filtered set. [case_number, lat, long, status]
    """
    where, params = _filters(start, end, category, status, q, polygon)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT service_request_id, lat, long, lower(status) AS s,
                       service_name,
                       COALESCE(GREATEST(0, EXTRACT(epoch FROM now() - requested_datetime)
                                            ::bigint / 86400), -1)::int AS age_days
                FROM service_requests
                WHERE {where} AND lat IS NOT NULL AND long IS NOT NULL""",
            params,
        ).fetchall()
    # category names are sent once and referenced by index to keep the payload small
    cat_idx: dict[str, int] = {}
    pts = []
    for r in rows:
        cat = r["service_name"] or "Unknown"
        idx = cat_idx.setdefault(cat, len(cat_idx))
        pts.append([r["service_request_id"], r["lat"], r["long"], r["s"], idx, r["age_days"]])
    return {"total": len(pts), "categories": list(cat_idx), "points": pts}


@app.get("/api/categories")
def category_counts(
    start: datetime | None = None,
    end: datetime | None = None,
    status: str | None = None,
    q: str | None = None,
    polygon: str | None = None,
):
    """Per-category counts under the current filters (except the category
    selection itself), so the sidebar numbers reflect the active window."""
    where, params = _filters(start, end, None, status, q, polygon)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT service_name, count(*) AS n FROM service_requests
                WHERE {where} AND service_name IS NOT NULL
                GROUP BY service_name ORDER BY n DESC""",
            params,
        ).fetchall()
    return {"categories": rows}


@app.get("/api/trend")
def trend(
    start: datetime | None = None,
    end: datetime | None = None,
    category: list[str] | None = Query(None),
    status: str | None = None,
    q: str | None = None,
    polygon: str | None = None,
    interval: str = "week",
    top: int = Query(8, le=15),
):
    if interval not in ("day", "week", "month"):
        raise HTTPException(400, "interval must be day|week|month")
    where, params = _filters(start, end, category, status, q, polygon)
    with pool.connection() as conn:
        rows = conn.execute(
            f"""WITH filtered AS (
                    SELECT date_trunc(%s, requested_datetime) AS period, service_name
                    FROM service_requests
                    WHERE {where} AND requested_datetime IS NOT NULL
                ),
                top_cats AS (
                    SELECT service_name FROM filtered
                    GROUP BY service_name ORDER BY count(*) DESC LIMIT %s
                )
                SELECT period,
                       CASE WHEN service_name IN (SELECT service_name FROM top_cats)
                            THEN service_name ELSE 'Other' END AS category,
                       count(*) AS n
                FROM filtered
                GROUP BY period, category
                ORDER BY period""",
            [interval] + params + [top],
        ).fetchall()
    return {"interval": interval, "rows": rows}


@app.get("/api/analytics")
def analytics(
    start: datetime | None = None,
    end: datetime | None = None,
    category: list[str] | None = Query(None),
    status: str | None = None,
    q: str | None = None,
    polygon: str | None = None,
):
    """All analytics-tab aggregates in one round trip, under the same
    filter semantics as the rest of the dashboard.

    Time-to-close uses closed - requested, floored at 0 (a handful of
    records carry closed timestamps before the request timestamp).
    "Open age" is now - requested for not-yet-closed/cancelled records.
    """
    where, params = _filters(start, end, category, status, q, polygon)
    ttc = "GREATEST(0, EXTRACT(epoch FROM closed_datetime - requested_datetime) / 86400.0)"
    open_age = ("GREATEST(0, EXTRACT(epoch FROM now() - requested_datetime) / 86400.0)")
    is_open = "closed_datetime IS NULL AND canceled_datetime IS NULL"

    with pool.connection() as conn:
        summary = conn.execute(
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE {is_open}) AS open,
                       count(closed_datetime) AS closed,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY {ttc})
                           FILTER (WHERE closed_datetime IS NOT NULL) AS median_ttc,
                       avg({ttc}) FILTER (WHERE closed_datetime IS NOT NULL) AS mean_ttc,
                       count(*) FILTER (WHERE closed_datetime IS NOT NULL AND {ttc} <= 30)
                           AS closed_within_30d,
                       avg({open_age}) FILTER (WHERE {is_open}) AS avg_open_age,
                       max({open_age}) FILTER (WHERE {is_open}) AS oldest_open_age
                FROM service_requests WHERE {where}""",
            params,
        ).fetchone()

        ttc_hist = conn.execute(
            f"""SELECT width_bucket({ttc}, ARRAY[1, 3, 7, 14, 30, 90]) AS bucket,
                       count(*) AS n
                FROM service_requests
                WHERE {where} AND closed_datetime IS NOT NULL
                GROUP BY bucket ORDER BY bucket""",
            params,
        ).fetchall()

        by_category = conn.execute(
            f"""SELECT service_name, count(*) AS n,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY {ttc})
                           FILTER (WHERE closed_datetime IS NOT NULL) AS median_ttc,
                       count(*) FILTER (WHERE {is_open}) AS open_n,
                       avg({open_age}) FILTER (WHERE {is_open}) AS avg_open_age
                FROM service_requests
                WHERE {where} AND service_name IS NOT NULL
                GROUP BY service_name ORDER BY n DESC""",
            params,
        ).fetchall()

        weekly = conn.execute(
            f"""SELECT week, sum(opened) AS opened, sum(closed) AS closed FROM (
                    SELECT date_trunc('week', requested_datetime) AS week,
                           1 AS opened, 0 AS closed
                    FROM service_requests
                    WHERE {where} AND requested_datetime IS NOT NULL
                    UNION ALL
                    SELECT date_trunc('week', closed_datetime), 0, 1
                    FROM service_requests
                    WHERE {where} AND closed_datetime IS NOT NULL
                ) t GROUP BY week ORDER BY week""",
            params + params,
        ).fetchall()

        weekday = conn.execute(
            f"""SELECT EXTRACT(isodow FROM requested_datetime)::int AS dow, count(*) AS n
                FROM service_requests
                WHERE {where} AND requested_datetime IS NOT NULL
                GROUP BY dow ORDER BY dow""",
            params,
        ).fetchall()

    return {
        "summary": summary,
        "ttc_histogram": ttc_hist,
        "by_category": by_category,
        "weekly": weekly,
        "weekday": weekday,
    }


@app.get("/api/media/{media_id}")
def media(media_id: str):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT stored_path, mime_type, stored_mime FROM media WHERE media_id = %s AND downloaded_at IS NOT NULL",
            (media_id,),
        ).fetchone()
    if not row or not row["stored_path"]:
        raise HTTPException(404, "media not stored")
    mime = row["stored_mime"] or row["mime_type"] or "application/octet-stream"
    cache = {"Cache-Control": "public, max-age=86400"}
    store = store_from_env()
    if isinstance(store, LocalMediaStore):
        path = store.open_path(row["stored_path"])
        if not path.exists():
            raise HTTPException(404, "file missing from store")
        return FileResponse(path, media_type=mime, headers=cache)
    assert isinstance(store, GcsMediaStore)
    return Response(store.get(row["stored_path"]), media_type=mime, headers=cache)


# note: bare /healthz is a reserved path on run.app domains (GFE intercepts it)
@app.get("/api/healthz")
def healthz():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


app.mount(
    "/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static"
)
