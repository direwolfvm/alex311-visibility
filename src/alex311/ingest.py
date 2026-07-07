"""Ingest job: pull list windows, upsert, enrich changed records, download media.

Usage:
  python -m alex311.ingest incremental [--days 14] [--max-details N] [--max-media N] [--no-media]
  python -m alex311.ingest backfill --start 2025-01-01 [--end 2026-07-01] ...
  python -m alex311.ingest init-db

Idempotent: upserts by service_request_id; overlapping windows never
double-count. Enrichment targets records whose last_updated_datetime moved
past enriched_at, so re-runs converge instead of re-fetching everything.
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

from . import db, models
from .client import Alex311Client
from .media_store import object_name, store_from_env

log = logging.getLogger("alex311.ingest")

HEIC_MIMES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heif", b"mif1", b"msf1"}


def is_heic(mime: str | None, file_name: str | None, data: bytes) -> bool:
    if mime and mime.split(";")[0].strip().lower() in HEIC_MIMES:
        return True
    if file_name and re.search(r"\.hei[cf]$", file_name, re.IGNORECASE):
        return True
    return len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS


def heic_to_jpeg(data: bytes) -> bytes:
    """Convert HEIC/HEIF bytes to JPEG (most browsers can't render HEIC).

    Baking the orientation and re-saving also drops EXIF — including any
    GPS tags a reporter's phone embedded, which we'd rather not republish.
    """
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener()
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def _jpeg_name(name: str) -> str:
    return re.sub(r"(\.[^./]+)?$", ".jpg", name, count=1)

# Refetch this much history on every incremental run, on top of --days.
# Covers records whose activity timestamps drift during pagination.
INCREMENTAL_OVERLAP = timedelta(days=1)


def run_ingest(
    conn,
    client: Alex311Client,
    *,
    kind: str,
    start: datetime,
    end: datetime,
    max_details: int,
    max_media: int,
    download_media: bool,
) -> None:
    run_id = db.start_run(conn, kind, start, end)
    seen = upserted = details = downloaded = 0
    try:
        records = client.fetch_range(start, end)
        seen = len(records)

        drift = models.validate_list_record(next(iter(records.values()))) if records else []
        if drift:
            raise RuntimeError(f"list schema drift, missing keys: {drift}")

        upserted = db.upsert_list_records(
            conn,
            ((models.list_record_columns(rec), rec) for rec in records.values()),
        )
        log.info("upserted %d/%d records", upserted, seen)

        details = enrich(conn, client, max_details)
        if download_media:
            downloaded = fetch_pending_media(conn, client, max_media)
            # self-heal: convert any HEIC stored by older code paths
            convert_stored_heic(conn, max_media)

        db.finish_run(conn, run_id, ok=True, records_seen=seen,
                      records_upserted=upserted, details_fetched=details,
                      media_downloaded=downloaded)
    except Exception as e:
        db.finish_run(conn, run_id, ok=False, records_seen=seen,
                      records_upserted=upserted, details_fetched=details,
                      media_downloaded=downloaded, error=f"{type(e).__name__}: {e}")
        raise


def enrich(conn, client: Alex311Client, limit: int) -> int:
    """Fetch details for new/changed records; stage their media rows."""
    if limit <= 0:
        return 0
    fetched = 0
    for srid in db.select_needing_enrichment(conn, limit):
        try:
            detail = client.get_detail(srid)
        except Exception as e:
            log.warning("detail failed for %s: %s", srid, e)
            continue
        drift = models.validate_detail(detail)
        if drift:
            raise RuntimeError(f"detail schema drift for {srid}, missing keys: {drift}")
        db.apply_detail(conn, srid, models.detail_columns(detail), detail)
        db.upsert_media_rows(conn, models.media_rows(srid, detail))
        fetched += 1
    log.info("enriched %d records", fetched)
    return fetched


def fetch_pending_media(conn, client: Alex311Client, limit: int) -> int:
    """Download staged media into the configured store."""
    if limit <= 0:
        return 0
    store = store_from_env()
    done = 0
    for m in db.select_media_pending_download(conn, limit):
        name = object_name(m["service_request_id"], m["media_id"], m["file_name"])
        try:
            data, ctype = client.fetch_media(m["source_url"])
            mime = (ctype or m["mime_type"] or "").split(";")[0].strip()
            if is_heic(mime, m["file_name"], data):
                data = heic_to_jpeg(data)
                name, mime = _jpeg_name(name), "image/jpeg"
            store.put(name, data, mime)
            db.mark_media_downloaded(conn, m["media_id"], name, len(data), mime)
            done += 1
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            # private media are expected to be unreadable by the guest session
            db.mark_media_failed(conn, m["media_id"], f"HTTP {code}"
                                 + (" (private)" if m["private"] else ""))
            log.log(logging.INFO if m["private"] else logging.WARNING,
                    "media %s failed: HTTP %s (private=%s)",
                    m["media_id"], code, m["private"])
        except Exception as e:
            db.mark_media_failed(conn, m["media_id"], f"{type(e).__name__}: {e}")
            log.warning("media %s failed: %s", m["media_id"], e)
    log.info("downloaded %d media files", done)
    return done


def convert_stored_heic(conn, limit: int) -> int:
    """One-off/maintenance pass: convert already-stored HEIC files to JPEG."""
    store = store_from_env()
    done = 0
    for m in db.select_stored_heic(conn, limit):
        old_path = m["stored_path"]
        try:
            data = store.get(old_path)
            if not is_heic(m["stored_mime"] or m["mime_type"], m["file_name"], data):
                # mislabelled (e.g. an iPhone upload that was already JPEG) — just record reality
                db.update_media_stored(conn, m["media_id"], old_path, len(data),
                                       m["mime_type"] or "application/octet-stream")
                continue
            jpeg = heic_to_jpeg(data)
            new_path = _jpeg_name(old_path)
            store.put(new_path, jpeg, "image/jpeg")
            db.update_media_stored(conn, m["media_id"], new_path, len(jpeg), "image/jpeg")
            if new_path != old_path:
                store.delete(old_path)
            done += 1
        except Exception as e:
            log.warning("heic conversion failed for %s (%s): %s",
                        m["media_id"], old_path, e)
    log.info("converted %d stored HEIC files", done)
    return done


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(prog="alex311.ingest")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="apply schema.sql (idempotent)")

    inc = sub.add_parser("incremental", help="pull recent windows")
    inc.add_argument("--days", type=int, default=14)

    back = sub.add_parser("backfill", help="pull an explicit historical range")
    back.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    back.add_argument("--end", help="YYYY-MM-DD (UTC), default now")

    conv = sub.add_parser("convert-heic",
                          help="convert already-stored HEIC media to JPEG")
    conv.add_argument("--limit", type=int, default=100000)

    for cmd in (inc, back):
        cmd.add_argument("--max-details", type=int, default=500)
        cmd.add_argument("--max-media", type=int, default=500)
        cmd.add_argument("--no-media", action="store_true")

    args = p.parse_args(argv)
    conn = db.connect()

    if args.cmd == "init-db":
        db.apply_schema(conn)
        print("schema applied")
        return 0

    if args.cmd == "convert-heic":
        convert_stored_heic(conn, args.limit)
        return 0

    now = datetime.now(timezone.utc)
    if args.cmd == "incremental":
        start = now - timedelta(days=args.days) - INCREMENTAL_OVERLAP
        end = now + timedelta(hours=1)
    else:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
               if args.end else now)

    with Alex311Client() as client:
        run_ingest(
            conn, client, kind=args.cmd, start=start, end=end,
            max_details=args.max_details, max_media=args.max_media,
            download_media=not args.no_media,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
