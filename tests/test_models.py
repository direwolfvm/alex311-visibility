from datetime import datetime, timezone

from alex311 import models


def test_list_record_columns_defensive():
    cols = models.list_record_columns({
        "service_request_id": "26-00000001",
        "id": "a0x",
        "status": "open",
        "service_name": "Potholes",
        "lat": "38.81",
        "long": None,
        "address": "  1 KING ST  ",
        "requested_datetime": "2026-06-30T12:00:00.000Z",
        "closed_datetime": "",
        "expected_datetime": "not-a-date",
        "media_url": [{"id": "m1"}, {"id": "m2"}],
    })
    assert cols["lat"] == 38.81
    assert cols["long"] is None
    assert cols["address"] == "1 KING ST"
    assert cols["requested_datetime"] == datetime(2026, 6, 30, 12, tzinfo=timezone.utc)
    assert cols["closed_datetime"] is None
    assert cols["expected_datetime"] is None
    assert cols["media_count"] == 2


def test_media_rows_skips_malformed():
    rows = models.media_rows("26-1", {"media_url": [
        {"id": "m1", "fileName": "a.jpg", "url": "https://x/y", "private": False,
         "mime_type": "image/jpeg", "created_datetime": "2026-06-30T12:00:00.000Z"},
        {"fileName": "no-id.jpg"},
        "garbage",
    ]})
    assert len(rows) == 1
    assert rows[0]["media_id"] == "m1"
    assert rows[0]["private"] is False


def test_validate_detects_drift():
    assert models.validate_list_record({"service_request_id": "x"}) == [
        "address", "requested_datetime", "service_name", "status",
    ]
    assert models.validate_detail({
        "service_request_id": "x", "status": "open",
        "description": None, "media_url": [],
    }) == []
