"""Universal Links: the file that lets iOS open this site's links in the app.

Apple fetches /.well-known/apple-app-site-association through its CDN, over
HTTPS, with no redirect, and wants JSON. It must name the app and exactly
the paths the app handles — the emailed sign-in link, a request record, the
digest's My requests link — and nothing that should stay in the browser.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.py").read_text()


def test_the_route_is_registered_ahead_of_the_static_mount():
    assert '@app.get("/.well-known/apple-app-site-association")' in APP
    assert APP.index('"/.well-known/apple-app-site-association"') < APP.index("app.mount(")


def test_without_an_app_id_it_is_a_404(monkeypatch):
    from fastapi import HTTPException
    from dashboard import app as A
    monkeypatch.delenv("APPLE_APP_IDS", raising=False)
    with pytest.raises(HTTPException) as e:
        A.apple_app_site_association()
    assert e.value.status_code == 404


def test_it_names_the_app_and_only_the_paths_the_app_handles(monkeypatch):
    from dashboard import app as A
    monkeypatch.setenv("APPLE_APP_IDS", "ABCDE12345.me.alex311visibility.app")
    r = A.apple_app_site_association()
    assert r.media_type == "application/json"
    body = json.loads(r.body)
    d = body["applinks"]["details"][0]
    assert d["appIDs"] == ["ABCDE12345.me.alex311visibility.app"]
    paths = [(c["/"], c.get("?")) for c in d["components"]]
    assert paths == [("/submit/login", {"mode": "signIn"}), ("/r/*", None), ("/submit/my", None)]
    # the digest's confirm and unsubscribe links stay in the browser
    assert "digest" not in json.dumps(d["components"]).replace("the digest's My requests link", "")


def test_the_server_trusts_cloud_runs_forwarded_scheme():
    """Behind Cloud Run the app sees plain HTTP; without this the session
    cookie's `secure=request.url.scheme == "https"` is always false."""
    docker = (ROOT / "Dockerfile").read_text()
    assert '"--proxy-headers", "--forwarded-allow-ips", "*"' in docker
