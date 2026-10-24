"""App Links: the file that lets Android open this site's links in the app.

Android fetches /.well-known/assetlinks.json over HTTPS, with no redirect,
and wants a JSON list naming the package and the signing certificate's
SHA-256 fingerprint. Pinned: public, ahead of the static mount, answered on
every host without a redirect, a 404 until configured, never a placeholder.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.py").read_text()
PRINT = "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89"


def test_the_route_is_registered_ahead_of_the_static_mount_and_beside_apples():
    assert '@app.get("/.well-known/assetlinks.json")' in APP
    assert APP.index('"/.well-known/assetlinks.json"') < APP.index("app.mount(")
    assert '@app.get("/.well-known/apple-app-site-association")' in APP          # the iOS one stays


@pytest.mark.parametrize("env", [{}, {"ANDROID_PACKAGE": "com.herbertindustries.alex311"},
                                 {"ANDROID_SHA256_CERT_FINGERPRINTS": PRINT},
                                 {"ANDROID_PACKAGE": "com.herbertindustries.alex311", "ANDROID_SHA256_CERT_FINGERPRINTS": " , "}])
def test_until_both_are_configured_it_is_a_404(monkeypatch, env):
    from fastapi import HTTPException
    from dashboard import app as A
    monkeypatch.delenv("ANDROID_PACKAGE", raising=False)
    monkeypatch.delenv("ANDROID_SHA256_CERT_FINGERPRINTS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(HTTPException) as e:
        A.android_asset_links()
    assert e.value.status_code == 404


def test_it_names_the_package_and_the_signing_certificates(monkeypatch):
    from dashboard import app as A
    monkeypatch.setenv("ANDROID_PACKAGE", "com.herbertindustries.alex311")
    monkeypatch.setenv("ANDROID_SHA256_CERT_FINGERPRINTS", f"{PRINT.lower()}, {PRINT.replace('A', 'F')}")
    r = A.android_asset_links()
    assert r.media_type == "application/json" and r.headers["cache-control"] == "public, max-age=3600"
    body = json.loads(r.body)
    assert body == [{"relation": ["delegate_permission/common.handle_all_urls"],
                     "target": {"namespace": "android_app", "package_name": "com.herbertindustries.alex311",
                                "sha256_cert_fingerprints": [PRINT, PRINT.replace("A", "F")]}}]
    assert "REPLACE" not in json.dumps(body)


def test_it_is_answered_on_every_host_without_a_redirect(monkeypatch):
    """The Android client's canonical origin is the original domain; the file
    must be there (and on the new one) rather than redirected away."""
    from fastapi.testclient import TestClient
    from dashboard import app as A
    monkeypatch.setenv("SITE_ORIGIN", "https://alex311-reborn.com")
    monkeypatch.setenv("ANDROID_PACKAGE", "com.herbertindustries.alex311")
    monkeypatch.setenv("ANDROID_SHA256_CERT_FINGERPRINTS", PRINT)
    for host in ("https://alex311visibility.me", "https://alex311-reborn.com"):
        r = TestClient(A.app, base_url=host).get("/.well-known/assetlinks.json", follow_redirects=False)
        assert r.status_code == 200 and r.json()[0]["target"]["package_name"] == "com.herbertindustries.alex311", host
