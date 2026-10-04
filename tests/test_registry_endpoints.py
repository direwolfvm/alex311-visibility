"""The registry over HTTP, for a client that must not need a rebuild to follow the City.

The City changes its form without notice. Every registry response carries a
version (a fingerprint of the content) and an ETag; a client keeps its copy
while that holds and gets a 304 for asking. The whole registry is available
in one call, and whether a copy is current can be asked without a session.
"""
import gzip
import json

import pytest
from fastapi.testclient import TestClient

from dashboard import registry as R


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SUBMIT_USER", "alex311user")
    monkeypatch.setenv("SUBMIT_PASSWORD", "localtest")
    from dashboard.app import app
    return TestClient(app)          # no lifespan: these routes never touch the database


AUTH = ("alex311user", "localtest")


def test_the_version_is_a_fingerprint_of_the_content(tmp_path):
    a = tmp_path / "a.json"; a.write_text('{"generated": "x", "services": []}')
    b = tmp_path / "b.json"; b.write_text('{"generated": "x", "services": [{"service_code": "NEW"}]}')
    va, vb = R.registry_version.__wrapped__(str(a)), R.registry_version.__wrapped__(str(b))
    assert va != vb and len(va) == 16
    assert R.registry_version.__wrapped__(str(a)) == va          # same content, same version


def test_anyone_can_ask_whether_their_copy_is_current(client):
    r = client.get("/submit/api/registry/version")              # no credentials
    assert r.status_code == 200
    body = r.json()
    assert body == {"version": R.registry_version(), "schema": R.SCHEMA,
                    "generated": R.load_registry()["generated"],
                    "services": len(R.load_registry()["services"])}
    assert r.headers["etag"] == f'"{R.registry_version()}"'
    # and the probe answers 304 like the rest, so "always send If-None-Match" works everywhere
    assert client.get("/submit/api/registry/version",
                      headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_the_registry_itself_still_needs_the_gate(client):
    for path in ("/submit/api/registry", "/submit/api/registry/full", "/submit/api/service/TESMISCO"):
        assert client.get(path).status_code == 401, path


def test_the_index_carries_its_version_and_answers_304_when_current(client):
    r = client.get("/submit/api/registry", auth=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == R.registry_version() and body["schema"] == R.SCHEMA
    assert len(body["services"]) == len(R.load_registry()["services"])
    assert "questions" not in body["services"][0]               # the light list, as before
    etag = r.headers["etag"]
    assert r.headers["x-registry-version"] == R.registry_version()
    again = client.get("/submit/api/registry", auth=AUTH, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    stale = client.get("/submit/api/registry", auth=AUTH, headers={"If-None-Match": '"an-older-version"'})
    assert stale.status_code == 200


def test_the_whole_registry_comes_in_one_call_compressed(client):
    r = client.get("/submit/api/registry/full", auth=AUTH)      # the test client asks for gzip
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    body = r.json()
    reg = R.load_registry()
    assert body["version"] == R.registry_version() and body["schema"] == R.SCHEMA
    assert [s["service_code"] for s in body["services"]] == [s["service_code"] for s in reg["services"]]
    assert body["services"][0]["questions"] == reg["services"][0]["questions"]
    assert len(R.full_payload(True)) < len(R.full_payload(False)) / 5
    assert json.loads(gzip.decompress(R.full_payload(True))) == json.loads(R.full_payload(False))
    plain = client.get("/submit/api/registry/full", auth=AUTH, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json()["version"] == body["version"]
    assert client.get("/submit/api/registry/full", auth=AUTH,
                      headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_one_service_is_versioned_the_same_way(client):
    r = client.get("/submit/api/service/TESMISCO", auth=AUTH)
    assert r.status_code == 200 and r.json()["service_code"] == "TESMISCO"
    assert r.headers["etag"] == f'"{R.registry_version()}"'
    assert client.get("/submit/api/service/TESMISCO", auth=AUTH,
                      headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.get("/submit/api/service/NOPE", auth=AUTH).status_code == 404
    # a type the City retired is simply not there any more
    assert client.get("/submit/api/service/TESTSGNL", auth=AUTH).status_code == 404
