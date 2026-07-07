"""Postgres (Cloud SQL) storage layer.

Connection via DATABASE_URL, e.g.
  postgresql://user:pass@localhost:5432/alex311
On Cloud Run, point at the Cloud SQL unix socket:
  postgresql://user:pass@/alex311?host=/cloudsql/PROJECT:REGION:INSTANCE
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from importlib import resources
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def connect(dsn: str | None = None) -> psycopg.Connection:
    dsn = dsn or os.environ["DATABASE_URL"]
    return psycopg.connect(dsn, row_factory=dict_row)


def apply_schema(conn: psycopg.Connection) -> None:
    sql = resources.files("alex311").joinpath("schema.sql").read_text()
    conn.execute(sql)
    conn.commit()


LIST_COLS = [
    "sf_id", "status", "service_name", "service_code", "lat", "long", "address",
    "requested_datetime", "expected_datetime", "updated_datetime",
    "last_updated_datetime", "closed_datetime", "canceled_datetime", "media_count",
]

DETAIL_COLS = [
    "description", "zipcode", "origin", "source", "priority",
    "primary_service_department", "agency_responsible", "status_notes",
    "closure_details", "parent_service_request_id",
    "duplicate_parent_service_request_id", "owner",
]


def upsert_list_records(
    conn: psycopg.Connection, rows: Iterable[tuple[dict, dict]]
) -> int:
    """Upsert (columns, raw_record) pairs from the list endpoint.

    Detail-only columns are left untouched on conflict; enrichment is
    triggered separately by comparing last_updated_datetime to enriched_at.
    """
    now = datetime.now(timezone.utc)
    count = 0
    with conn.cursor() as cur:
        for cols, raw in rows:
            values = {c: cols.get(c) for c in LIST_COLS}
            values["service_request_id"] = cols["service_request_id"]
            values["raw_list"] = Jsonb(raw)
            values["last_ingested_at"] = now
            col_names = list(values)
            placeholders = ", ".join(f"%({c})s" for c in col_names)
            updates = ", ".join(
                f"{c} = EXCLUDED.{c}"
                for c in col_names
                if c != "service_request_id"
            )
            cur.execute(
                f"""INSERT INTO service_requests ({", ".join(col_names)})
                    VALUES ({placeholders})
                    ON CONFLICT (service_request_id) DO UPDATE SET {updates}""",
                values,
            )
            count += 1
    conn.commit()
    return count


def select_needing_enrichment(conn: psycopg.Connection, limit: int) -> list[str]:
    """Case numbers never enriched, or with activity since last enrichment."""
    rows = conn.execute(
        """SELECT service_request_id FROM service_requests
           WHERE enriched_at IS NULL
              OR (last_updated_datetime IS NOT NULL
                  AND last_updated_datetime > enriched_at)
           ORDER BY enriched_at IS NOT NULL, requested_datetime DESC
           LIMIT %s""",
        (limit,),
    ).fetchall()
    return [r["service_request_id"] for r in rows]


def apply_detail(
    conn: psycopg.Connection, service_request_id: str, cols: dict, raw: dict
) -> None:
    values = {c: cols.get(c) for c in DETAIL_COLS}
    values["raw_detail"] = Jsonb(raw)
    values["enriched_at"] = datetime.now(timezone.utc)
    values["srid"] = service_request_id
    sets = ", ".join(f"{c} = %({c})s" for c in values if c != "srid")
    conn.execute(
        f"UPDATE service_requests SET {sets} WHERE service_request_id = %(srid)s",
        values,
    )
    conn.commit()


def upsert_media_rows(conn: psycopg.Connection, rows: list[dict]) -> None:
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """INSERT INTO media (media_id, service_request_id, file_name,
                       mime_type, private, source_url, created_datetime)
                   VALUES (%(media_id)s, %(service_request_id)s, %(file_name)s,
                       %(mime_type)s, %(private)s, %(source_url)s, %(created_datetime)s)
                   ON CONFLICT (media_id) DO UPDATE SET
                       file_name = EXCLUDED.file_name,
                       mime_type = EXCLUDED.mime_type,
                       private = EXCLUDED.private,
                       source_url = EXCLUDED.source_url""",
                r,
            )
    conn.commit()


def select_media_pending_download(conn: psycopg.Connection, limit: int) -> list[dict]:
    return conn.execute(
        """SELECT media_id, service_request_id, file_name, mime_type, source_url, private
           FROM media
           WHERE downloaded_at IS NULL AND download_error IS NULL
             AND source_url IS NOT NULL
           LIMIT %s""",
        (limit,),
    ).fetchall()


def mark_media_downloaded(
    conn: psycopg.Connection, media_id: str, stored_path: str, size: int,
    stored_mime: str | None = None,
) -> None:
    conn.execute(
        """UPDATE media SET stored_path = %s, stored_bytes = %s, stored_mime = %s,
               downloaded_at = now(), download_error = NULL
           WHERE media_id = %s""",
        (stored_path, size, stored_mime, media_id),
    )
    conn.commit()


def select_stored_heic(conn: psycopg.Connection, limit: int) -> list[dict]:
    """Downloaded media that still look like HEIC (candidates for conversion)."""
    return conn.execute(
        """SELECT media_id, service_request_id, file_name, mime_type,
                  stored_path, stored_mime
           FROM media
           WHERE downloaded_at IS NOT NULL AND stored_path IS NOT NULL
             AND COALESCE(stored_mime, '') NOT IN ('image/jpeg', 'image/png',
                                                   'image/gif', 'image/webp')
             AND (mime_type ILIKE '%%hei%%' OR file_name ~* '\\.hei[cf]$'
                  OR stored_mime ILIKE '%%hei%%')
           LIMIT %s""",
        (limit,),
    ).fetchall()


def update_media_stored(
    conn: psycopg.Connection, media_id: str, stored_path: str, size: int,
    stored_mime: str,
) -> None:
    conn.execute(
        """UPDATE media SET stored_path = %s, stored_bytes = %s, stored_mime = %s
           WHERE media_id = %s""",
        (stored_path, size, stored_mime, media_id),
    )
    conn.commit()


def mark_media_failed(conn: psycopg.Connection, media_id: str, error: str) -> None:
    conn.execute(
        "UPDATE media SET download_error = %s WHERE media_id = %s",
        (error[:500], media_id),
    )
    conn.commit()


def start_run(conn: psycopg.Connection, kind: str,
              range_start: datetime | None, range_end: datetime | None) -> int:
    row = conn.execute(
        """INSERT INTO ingest_runs (kind, range_start, range_end)
           VALUES (%s, %s, %s) RETURNING run_id""",
        (kind, range_start, range_end),
    ).fetchone()
    conn.commit()
    return row["run_id"]


def finish_run(conn: psycopg.Connection, run_id: int, *, ok: bool,
               records_seen: int = 0, records_upserted: int = 0,
               details_fetched: int = 0, media_downloaded: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        """UPDATE ingest_runs SET finished_at = now(), ok = %s,
               records_seen = %s, records_upserted = %s, details_fetched = %s,
               media_downloaded = %s, error = %s
           WHERE run_id = %s""",
        (ok, records_seen, records_upserted, details_fetched,
         media_downloaded, error[:1000] if error else None, run_id),
    )
    conn.commit()
