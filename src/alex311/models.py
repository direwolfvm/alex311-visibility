"""Typed extraction from raw Alex311 records into flat DB columns.

The API returns loosely-typed JSON (datetimes as strings, lat/long as strings
or numbers, empty strings for missing values). Everything here is defensive:
a malformed field becomes NULL, never an exception — raw JSON is stored
alongside, so nothing is lost.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

LIST_REQUIRED_KEYS = {
    "service_request_id", "status", "service_name",
    "requested_datetime", "address",
}
DETAIL_REQUIRED_KEYS = {"service_request_id", "status", "description", "media_url"}


def parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str | None:
    """Non-empty string or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def list_record_columns(rec: dict) -> dict:
    """Flat columns available from the list endpoint."""
    return {
        "service_request_id": rec["service_request_id"],
        "sf_id": _s(rec.get("id")),
        "status": _s(rec.get("status")),
        "service_name": _s(rec.get("service_name")),
        "service_code": _s(rec.get("service_code")),
        "lat": parse_float(rec.get("lat")),
        "long": parse_float(rec.get("long")),
        "address": _s(rec.get("address")),
        "requested_datetime": parse_dt(rec.get("requested_datetime")),
        "expected_datetime": parse_dt(rec.get("expected_datetime")),
        "updated_datetime": parse_dt(rec.get("updated_datetime")),
        "last_updated_datetime": parse_dt(rec.get("last_updated_datetime")),
        "closed_datetime": parse_dt(rec.get("closed_datetime")),
        "canceled_datetime": parse_dt(rec.get("canceled_datetime")),
        "media_count": len(rec.get("media_url") or []),
    }


def detail_columns(detail: dict) -> dict:
    """Extra flat columns available only from the detail endpoint."""
    owner = detail.get("owner")
    if isinstance(owner, dict):
        owner = owner.get("name") or owner.get("Name")
    return {
        "description": _s(detail.get("description")),
        "zipcode": _s(detail.get("zipcode")),
        "origin": _s(detail.get("origin")),
        "source": _s(detail.get("source")),
        "priority": _s(detail.get("priority")),
        "primary_service_department": _s(detail.get("primary_service_department")),
        "agency_responsible": _s(detail.get("agency_responsible")),
        "status_notes": _s(detail.get("status_notes")),
        "closure_details": _s(detail.get("closure_details")),
        "parent_service_request_id": _s(detail.get("parent_service_request_id")),
        "duplicate_parent_service_request_id": _s(
            detail.get("duplicate_parent_service_request_id")
        ),
        "owner": _s(owner),
    }


def media_rows(service_request_id: str, detail_or_rec: dict) -> list[dict]:
    rows = []
    for m in detail_or_rec.get("media_url") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        rows.append({
            "media_id": m["id"],
            "service_request_id": service_request_id,
            "file_name": _s(m.get("fileName")),
            "mime_type": _s(m.get("mime_type")),
            "private": bool(m.get("private")),
            "source_url": _s(m.get("url")),
            "created_datetime": parse_dt(m.get("created_datetime")),
        })
    return rows


def validate_list_record(rec: dict) -> list[str]:
    """Names of expected keys missing from a list record (schema drift check)."""
    return sorted(LIST_REQUIRED_KEYS - rec.keys())


def validate_detail(detail: dict) -> list[str]:
    return sorted(DETAIL_REQUIRED_KEYS - detail.keys())
