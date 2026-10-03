"""Photos on a report: held briefly, sent to the City with the request, then gone.

A phone is where the photo is. A resident may add up to three to a request
they are preparing; the filing worker attaches them on the first step of the
City's own form (its File Upload step takes images up to 10 MB), and once
the City has filed the request the copies here are deleted — the City holds
them now, under a case number, the same way it holds the contact details.

Every photo is re-encoded before it is kept:

  * it must actually decode as an image, so nothing else rides in;
  * HEIC becomes JPEG, which is what the City's site can show;
  * the orientation is baked in and the EXIF dropped — including the GPS
    tag a phone embeds. The request already says where the problem is, in
    the words the resident chose; the photo should not say where they stood;
  * the long side is capped, which keeps every photo far below the City's
    10 MB limit.

The bytes live in Postgres (`attempt_photos.data`) rather than the media
bucket: they are few, small after re-encoding, short-lived, and this way the
filing worker needs nothing but the database it already has.
"""
from __future__ import annotations

import io
import re

import psycopg

MAX_PHOTOS = 3
MAX_UPLOAD_BYTES = 15 * 1024 * 1024       # what we accept from a phone, before re-encoding
CITY_MAX_BYTES = 10 * 1024 * 1024         # the City's File Upload limit
MAX_SIDE = 2560
KEEP_DAYS = 7                             # an unsent request's photos are not kept forever
OPEN_STATES = ("prepared", "queued")      # photos can change only before the request is sent


class BadPhoto(ValueError):
    pass


def safe_name(name: str | None, index: int = 1) -> str:
    """A plain file name the City's form will list: letters, digits, dash, .jpg."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", (name or "").strip())
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:40]
    return f"{stem or f'photo-{index}'}.jpg"


def normalize(data: bytes, name: str | None = None, index: int = 1) -> tuple[bytes, str]:
    """Decode, straighten, strip, shrink, and re-encode as JPEG. Raises BadPhoto."""
    if not data:
        raise BadPhoto("that file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BadPhoto("that photo is too large; 15 MB is the limit")
    try:
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except Exception:                                  # HEIC support is optional at import
            pass
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)           # no exif= : metadata is not carried over
    except BadPhoto:
        raise
    except Exception:
        raise BadPhoto("that file is not a photo this site can read (JPEG, PNG or HEIC)")
    jpeg = out.getvalue()
    if len(jpeg) > CITY_MAX_BYTES:
        raise BadPhoto("that photo is too large for the City's form even after resizing")
    return jpeg, safe_name(name, index)


# ----------------------------------------------------------------- storage

def add(conn: psycopg.Connection, *, attempt_id: int, data: bytes, name: str | None) -> dict:
    n = conn.execute("SELECT count(*) AS n FROM attempt_photos WHERE attempt_id = %s "
                     "AND purged_at IS NULL", (attempt_id,)).fetchone()["n"]
    if n >= MAX_PHOTOS:
        raise BadPhoto(f"{MAX_PHOTOS} photos is the limit for one request")
    jpeg, file_name = normalize(data, name, index=n + 1)
    row = conn.execute(
        """INSERT INTO attempt_photos (attempt_id, file_name, bytes, data)
           VALUES (%s, %s, %s, %s) RETURNING photo_id, file_name, bytes, created_at""",
        (attempt_id, file_name, len(jpeg), jpeg)).fetchone()
    conn.commit()
    return row


def listing(conn: psycopg.Connection, *, attempt_id: int) -> list[dict]:
    return conn.execute(
        """SELECT photo_id, file_name, bytes, created_at FROM attempt_photos
            WHERE attempt_id = %s AND purged_at IS NULL ORDER BY photo_id""",
        (attempt_id,)).fetchall()


def remove(conn: psycopg.Connection, *, attempt_id: int, photo_id: int) -> bool:
    n = conn.execute("DELETE FROM attempt_photos WHERE attempt_id = %s AND photo_id = %s",
                     (attempt_id, photo_id)).rowcount
    conn.commit()
    return n > 0


def for_filing(conn: psycopg.Connection, *, attempt_id: int) -> list[tuple[str, bytes]]:
    """(file name, JPEG bytes) for the worker, in the order they were added."""
    rows = conn.execute(
        """SELECT file_name, data FROM attempt_photos
            WHERE attempt_id = %s AND purged_at IS NULL AND data IS NOT NULL ORDER BY photo_id""",
        (attempt_id,)).fetchall()
    return [(r["file_name"], bytes(r["data"])) for r in rows]


def purge(conn: psycopg.Connection, *, attempt_id: int) -> int:
    """Drop the bytes once the City has them. The row stays, without the photo,
    so the status board can still say how many went."""
    n = conn.execute(
        "UPDATE attempt_photos SET data = NULL, purged_at = now() "
        "WHERE attempt_id = %s AND purged_at IS NULL", (attempt_id,)).rowcount
    conn.commit()
    return n


def sweep(conn: psycopg.Connection, *, days: int = KEEP_DAYS) -> int:
    """Photos on a request nobody sent: gone after a week."""
    n = conn.execute(
        """UPDATE attempt_photos p SET data = NULL, purged_at = now()
            WHERE p.purged_at IS NULL AND p.created_at < now() - make_interval(days => %s)""",
        (days,)).rowcount
    conn.commit()
    return n
