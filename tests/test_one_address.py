"""The site has one name, whatever domain a request arrives on.

Pinned: a page asked for under another host is sent to SITE_ORIGIN with its
path and query intact and its method preserved (308); API calls, the iOS
association file and the health check are answered where they arrive; and
nothing happens when SITE_ORIGIN is unset (development) or the host is local.
"""
import pytest
from fastapi.testclient import TestClient

from dashboard import app as A


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SITE_ORIGIN", "https://alex311-reborn.com")
    return TestClient(A.app, base_url="https://alex311visibility.me")


def test_pages_on_the_old_domain_are_sent_to_the_new_one(client):
    r = client.get("/explore?category=Noise", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "https://alex311-reborn.com/explore?category=Noise"
    r = client.get("/r/26-00039807", follow_redirects=False)
    assert r.headers["location"] == "https://alex311-reborn.com/r/26-00039807"


def test_the_app_and_apple_are_answered_where_they_ask(client, monkeypatch):
    monkeypatch.setenv("APPLE_APP_IDS", "LAKT4757H4.com.herbertindustries.Alex311-Reborn")
    r = client.get("/.well-known/apple-app-site-association", follow_redirects=False)
    assert r.status_code == 200 and "LAKT4757H4" in r.text
    for path in ("/submit/api/registry/version", "/api/stats"):
        assert client.get(path, follow_redirects=False).status_code != 308, path
    assert client.post("/submit/api/email-link", json={}, follow_redirects=False).status_code != 308


def test_the_rules_without_a_server():
    import os
    os.environ["SITE_ORIGIN"] = "https://alex311-reborn.com"
    try:
        assert A.wants_canonical("GET", "alex311visibility.me", "/") is True
        assert A.wants_canonical("GET", "www.alex311-reborn.com", "/explore") is True
        assert A.wants_canonical("GET", "alex311-dashboard-650621702399.us-east4.run.app", "/privacy") is True
        assert A.wants_canonical("HEAD", "ALEX311-REBORN.COM", "/") is False             # already there
        assert A.wants_canonical("POST", "alex311visibility.me", "/explore") is False     # pages only
        assert A.wants_canonical("GET", "localhost:8311", "/explore") is False
        assert A.wants_canonical("GET", "alex311visibility.me", "/api/healthz") is False
        assert A.wants_canonical("GET", "alex311visibility.me", "/submit/api/my") is False
        assert A.wants_canonical("GET", "alex311visibility.me", "/.well-known/apple-app-site-association") is False
    finally:
        del os.environ["SITE_ORIGIN"]
    assert A.wants_canonical("GET", "alex311visibility.me", "/") is False                 # no SITE_ORIGIN: no opinion
