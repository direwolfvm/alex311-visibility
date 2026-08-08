"""Unit tests for Alex311Client against a mock Aura server (no network)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from alex311.client import (
    Alex311Client,
    AuraActionError,
    BootstrapError,
    VolumeTooLargeError,
)

FWUID = "testFWUID-abc123"

BOOTSTRAP_HTML = f"""<html><head></head><body>
<script>
window.Aura = {{}};
var auraConfig = {json.dumps({
    "context": {
        "mode": "PROD",
        "fwuid": FWUID,
        "app": "siteforce:communityApp",
        "loaded": {"APPLICATION@markup://siteforce:communityApp": "1656_x"},
    },
    "attributes": {"authenticated": "false"},
})};
if (auraConfig) {{}}
</script></body></html>"""


def _record(srid: str, **extra) -> dict:
    return {
        "service_request_id": srid,
        "id": f"sf{srid}",
        "status": "open",
        "service_name": "Litter and Illegal Dumping",
        "requested_datetime": "2026-06-30T12:00:00.000Z",
        "address": "100 TEST ST",
        "media_url": [],
        **extra,
    }


def _aura_success(payload) -> dict:
    return {
        "actions": [{
            "id": "1;a",
            "state": "SUCCESS",
            "returnValue": {"returnValue": json.dumps(payload), "cacheable": False},
        }]
    }


def _aura_error(message: str) -> dict:
    return {"actions": [{"id": "1;a", "state": "ERROR",
                         "error": [{"message": message}]}]}


class FakePortal:
    """Configurable stand-in for the Aura endpoint."""

    def __init__(self):
        self.fwuid = FWUID
        self.bootstrap_hits = 0
        self.aura_hits = 0
        # windows: list of (start_iso, end_iso, [records]) consulted in order
        self.records: list[dict] = []
        self.volume_threshold: int | None = None  # error if window has > N
        self.refusals_left = 0   # ration the next N list calls regardless of size
        self.details: dict[str, dict] = {}

    def _window_records(self, params: dict) -> list[dict]:
        start, end = params["start_date"], params["end_date"]
        return [r for r in self.records
                if start <= r["requested_datetime"] < end]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sfsites/aura"):
            self.aura_hits += 1
            form = dict(httpx.QueryParams(request.content.decode()))
            ctx = json.loads(form["aura.context"])
            if ctx["fwuid"] != self.fwuid:
                return httpx.Response(200, json={
                    "exceptionEvent": True,
                    "event": {"descriptor": "markup://aura:clientOutOfSync"},
                })
            msg = json.loads(form["message"])
            inner = msg["actions"][0]["params"]["params"]
            inner_params = json.loads(inner["params"])[0]
            if inner["method"] == "getServiceRequestList_v2":
                if self.refusals_left > 0:   # portal rationing, size-independent
                    self.refusals_left -= 1
                    return httpx.Response(200, json=_aura_error(
                        "Data volume for selected duration is too large to show."))
                recs = self._window_records(inner_params)
                if self.volume_threshold is not None and len(recs) > self.volume_threshold:
                    return httpx.Response(200, json=_aura_error(
                        "Data volume for selected duration is too large to show."))
                page, size = inner_params["page"], inner_params["page_size"]
                page_recs = recs[(page - 1) * size: page * size]
                return httpx.Response(200, text="*/ " + json.dumps(_aura_success(page_recs)))
            if inner["method"] == "getServiceRequest":
                srid = inner_params["serviceRequestId"]
                if srid not in self.details:
                    return httpx.Response(200, json=_aura_error("not found"))
                return httpx.Response(200, json=_aura_success(self.details[srid]))
            return httpx.Response(200, json=_aura_error(f"unknown method {inner['method']}"))
        # bootstrap page
        self.bootstrap_hits += 1
        html = BOOTSTRAP_HTML.replace(FWUID, self.fwuid)
        return httpx.Response(200, text=html)


@pytest.fixture()
def portal():
    return FakePortal()


@pytest.fixture()
def client(portal):
    c = Alex311Client(
        transport=httpx.MockTransport(portal.handler), min_interval=0.0
    )
    yield c
    c.close()


def _dt(day: int, hour: int = 12) -> str:
    return f"2026-06-{day:02d}T{hour:02d}:00:00.000Z"


WINDOW_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_bootstrap_parses_config(client, portal):
    session = client.bootstrap()
    assert session.context["fwuid"] == FWUID
    assert session.token == "null"  # guests have no token
    assert portal.bootstrap_hits == 1


def test_bootstrap_error_on_missing_config(portal):
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with Alex311Client(transport=httpx.MockTransport(handler), min_interval=0) as c:
        with pytest.raises(BootstrapError):
            c.bootstrap()


def test_pagination_exhausts_window(client, portal):
    portal.records = [_record(f"26-{i:08d}", requested_datetime=_dt(10)) for i in range(120)]
    got = list(client.iter_window(WINDOW_START, WINDOW_END))
    assert len(got) == 120  # 50 + 50 + 20 across three pages


def test_fetch_range_dedupes(client, portal):
    portal.records = [_record("26-00000001", requested_datetime=_dt(10))]
    results = client.fetch_range(
        WINDOW_START, WINDOW_END, window=timedelta(days=10)
    )
    assert list(results) == ["26-00000001"]


def test_volume_error_splits_window(client, portal):
    # 30 records spread across the month; server refuses windows holding >10
    portal.records = [
        _record(f"26-{i:08d}", requested_datetime=_dt(1 + (i % 28)))
        for i in range(30)
    ]
    portal.volume_threshold = 10
    results = client.fetch_range(
        WINDOW_START, WINDOW_END, window=timedelta(days=30)
    )
    assert len(results) == 30


def test_iter_window_still_raises_volume_error(client, portal):
    """The raw primitive is unchanged; only fetch_range's policy is tolerant."""
    portal.records = [
        _record(f"26-{i:08d}", requested_datetime=_dt(10)) for i in range(50)
    ]
    portal.volume_threshold = 5
    with pytest.raises(VolumeTooLargeError):
        list(client.iter_window(WINDOW_START, WINDOW_END))


def test_floor_volume_cap_is_retried_then_succeeds(client, portal):
    """The portal rations a couple of calls, then serves — as it does in life."""
    portal.records = [_record("26-00000001", requested_datetime=_dt(10))]
    portal.refusals_left = 2
    capped = []
    results = client.fetch_range(
        WINDOW_START, WINDOW_END, window=timedelta(days=30),
        min_window=timedelta(days=30), volume_retry_delay=0,
        on_capped=lambda s, e: capped.append((s, e)))
    assert list(results) == ["26-00000001"]
    assert capped == []  # recovered on retry, nothing to report


def test_capped_window_is_skipped_not_fatal(client, portal):
    """One unreadable window must not cost us the readable ones."""
    portal.records = (
        [_record(f"26-{i:08d}", requested_datetime=_dt(10)) for i in range(50)]
        + [_record("26-99999999", requested_datetime=_dt(20))]
    )
    portal.volume_threshold = 5  # the day-10 cluster is never servable
    capped = []
    results = client.fetch_range(
        WINDOW_START, WINDOW_END, window=timedelta(days=10),
        min_window=timedelta(days=5), volume_retry_delay=0,
        on_capped=lambda s, e: capped.append((s, e)))
    assert "26-99999999" in results   # the good window still landed
    assert capped                      # and the bad one was reported, not raised


def test_circuit_breaker_stops_hammering_a_refusing_portal(client, portal):
    portal.records = [
        _record(f"26-{i:08d}", requested_datetime=_dt(1 + (i % 28)))
        for i in range(60)
    ]
    portal.volume_threshold = 0   # refuse every window that holds anything
    capped = []
    before = portal.aura_hits
    results = client.fetch_range(
        WINDOW_START, WINDOW_END, window=timedelta(days=1),
        min_window=timedelta(days=1), volume_retry_delay=0, give_up_after=2,
        on_capped=lambda s, e: capped.append((s, e)))
    assert results == {}
    assert len(capped) == 30          # every window accounted for, none silent
    assert portal.aura_hits - before <= 10   # gave up instead of grinding


def test_out_of_sync_triggers_rebootstrap(client, portal):
    portal.records = [_record("26-00000001", requested_datetime=_dt(10))]
    client.bootstrap()
    portal.fwuid = "rotated-after-redeploy"  # simulate portal redeploy
    got = list(client.iter_window(WINDOW_START, WINDOW_END))
    assert [r["service_request_id"] for r in got] == ["26-00000001"]
    assert portal.bootstrap_hits == 2


def test_detail_fetch(client, portal):
    portal.details["26-00000001"] = {
        "service_request_id": "26-00000001",
        "status": "open",
        "description": "broken limb",
        "contact": {},
        "media_url": [],
    }
    detail = client.get_detail("26-00000001")
    assert detail["description"] == "broken limb"


def test_aura_error_surfaces_message(client, portal):
    with pytest.raises(AuraActionError, match="not found"):
        client.get_detail("26-99999999")


def test_deep_link():
    url = Alex311Client.deep_link("26-00013327")
    assert url == (
        "https://alex311.alexandriava.gov/customer/s/service-request-details"
        "?c__prePageName=service_requests__c&c__srNumber=26-00013327"
        "&servicerequested=all"
    )
