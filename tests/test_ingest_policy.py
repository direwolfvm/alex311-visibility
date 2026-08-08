"""When a fully-refused run should page someone, and when it should not.

The portal rations queries most weekends. Those runs read nothing, self-heal
on the next pass, and must stay quiet; a refusal that has actually kept us
from ingesting for a day is a real problem and must not.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alex311 import db, ingest


def _last_ok(hours_ago):
    def fake(conn):
        if hours_ago is None:
            return None
        return datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return fake


def test_refused_run_is_tolerated_when_a_recent_ingest_succeeded(monkeypatch):
    monkeypatch.setattr(db, "last_successful_ingest", _last_ok(6))
    ingest._require_not_stale(None, capped_windows=4)  # must not raise


def test_refused_run_fails_once_data_is_stale(monkeypatch):
    monkeypatch.setattr(db, "last_successful_ingest", _last_ok(30))
    with pytest.raises(RuntimeError, match="stale"):
        ingest._require_not_stale(None, capped_windows=4)


def test_refused_run_fails_when_nothing_ever_ingested(monkeypatch):
    monkeypatch.setattr(db, "last_successful_ingest", _last_ok(None))
    with pytest.raises(RuntimeError, match="stale"):
        ingest._require_not_stale(None, capped_windows=1)


def test_health_freshness_tolerates_recent_ingest(monkeypatch):
    from alex311 import health
    monkeypatch.setattr(db, "last_successful_ingest", _last_ok(3))
    health.check_freshness(None)  # must not raise


def test_health_freshness_fails_on_stale_data(monkeypatch):
    from alex311 import health
    monkeypatch.setattr(db, "last_successful_ingest", _last_ok(48))
    with pytest.raises(RuntimeError, match="stale"):
        health.check_freshness(None)
