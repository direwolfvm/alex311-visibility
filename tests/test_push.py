"""Push notifications: "your request changed", to the iOS app.

Pinned here: the provider token is a real ES256 JWT Apple could verify; a
status change on a linked request reaches each of the account's devices once
and is then remembered; the first sighting is a baseline and sends nothing;
a token Apple says is gone is switched off; devices are private to their
account and move when someone else signs in on the phone.
"""
import base64
import json
import os
from pathlib import Path

import pytest

from alex311 import push as P

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
INGEST = (ROOT / "src/alex311/ingest.py").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


@pytest.fixture()
def apns_key(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setenv("APNS_KEY", pem); monkeypatch.setenv("APNS_KEY_ID", "KEYID12345")
    monkeypatch.setenv("APNS_TEAM_ID", "TEAMID1234"); monkeypatch.setenv("APNS_TOPIC", "com.herbertindustries.Alex311-Reborn")
    monkeypatch.setattr(P, "_jwt", None)
    return key


def _unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_it_is_inert_until_all_four_settings_exist(monkeypatch):
    for k in ("APNS_KEY", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC"):
        monkeypatch.delenv(k, raising=False)
    assert P.configured() is False
    monkeypatch.setenv("APNS_KEY", "x"); monkeypatch.setenv("APNS_KEY_ID", "x"); monkeypatch.setenv("APNS_TEAM_ID", "x")
    assert P.configured() is False
    monkeypatch.setenv("APNS_TOPIC", "x")
    assert P.configured() is True
    assert "if push.configured():" in INGEST and "push pass failed" in INGEST   # and never fails an ingest


def test_the_provider_token_is_a_jwt_apple_could_verify(apns_key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    tok = P.provider_token(now=1_800_000_000)
    head, body, sig = tok.split(".")
    assert json.loads(_unb64(head)) == {"alg": "ES256", "kid": "KEYID12345"}
    assert json.loads(_unb64(body)) == {"iss": "TEAMID1234", "iat": 1_800_000_000}
    raw = _unb64(sig); assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    apns_key.public_key().verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong
    # reused within the hour, renewed after
    assert P.provider_token(now=1_800_000_000 + 60) == tok
    assert P.provider_token(now=1_800_000_000 + 3600) != tok


def test_a_send_goes_to_the_right_host_with_the_right_headers(apns_key):
    seen = {}
    class Resp:
        status_code = 200
        def json(self): return {}
    class Client:
        def post(self, url, content=None, headers=None):
            seen.update(url=url, body=json.loads(content), headers=headers); return Resp()
    assert P.send("ab" * 32, "sandbox", {"aps": {"alert": "x"}}, client=Client()) == (200, "")
    assert seen["url"] == "https://api.sandbox.push.apple.com/3/device/" + "ab" * 32
    assert seen["headers"]["apns-topic"] == "com.herbertindustries.Alex311-Reborn"
    assert seen["headers"]["apns-push-type"] == "alert" and seen["headers"]["authorization"].startswith("bearer ")
    P.send("ab" * 32, "production", {}, client=Client())
    assert seen["url"].startswith("https://api.push.apple.com/3/device/")


def test_the_message_says_whose_it_is_and_carries_the_case():
    row = {"service_request_id": "26-00036546", "relation": "mine", "status": "Closed",
           "service_name": "Noise Issues", "address": "500 N PITT ST"}
    m = P.message(row, "https://alex311visibility.me")
    assert m["aps"]["alert"]["title"] == "Noise Issues: closed"
    assert m["aps"]["alert"]["body"] == "Your request 26-00036546 at 500 N PITT ST is now closed."
    assert m["case"] == "26-00036546" and m["url"] == "https://alex311visibility.me/r/26-00036546"
    row["relation"] = "following"
    assert P.message(row, "https://s")["aps"]["alert"]["body"].startswith("A request you follow 26-00036546")


def test_routes_schema_and_scope():
    for r in ('@router.post("/api/devices")', '@router.delete("/api/devices/{token}")'):
        assert r in ROUTES
    for fn in ("device_register", "device_unregister"):
        assert "user_id = _account(request)" in ROUTES.split(f"def {fn}(")[1].split("\n    @router")[0]
    assert "ALTER TABLE push_devices FORCE ROW LEVEL SECURITY" in SCHEMA
    assert "ADD COLUMN IF NOT EXISTS notified_status TEXT" in SCHEMA
    # requests you sent or follow — not watches
    body = (ROOT / "src/alex311/push.py").read_text().split("def notify(")[1]
    assert "FROM request_links l JOIN service_requests r" in body and "watches" not in body


@pytest.mark.parametrize("token, env", [("xyz", "production"), ("ab" * 32, "staging"), ("g" * 64, "production")])
def test_a_bad_registration_is_refused(token, env):
    class Conn:
        def execute(self, *a): raise AssertionError("must not reach the database")
    with pytest.raises(P.BadDevice):
        P.register(Conn(), user_id="pu_1", token=token, environment=env)


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@needs_db
def test_a_change_is_told_once_per_device_and_a_dead_token_is_switched_off():
    from alex311 import db, portal_auth as pa
    owner = db.connect()
    owner.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'PU-%'")
    owner.execute("DELETE FROM portal_users WHERE email LIKE 'pu-%@test.io'"); owner.commit()
    a = pa.create_user(owner, "pu-a@test.io", "user", created_by="t")
    b = pa.create_user(owner, "pu-b@test.io", "user", created_by="t")
    owner.execute("""INSERT INTO service_requests (service_request_id, service_name, address, status, requested_datetime)
                     VALUES ('PU-1', 'Noise Issues', '500 N PITT ST', 'Open', now())"""); owner.commit()
    app = db.connect(os.environ.get("APP_DATABASE_URL") or DB)
    try:
        db.link_request(app, user_id=a.user_id, service_request_id="PU-1", relation="mine")
        db.link_request(app, user_id=b.user_id, service_request_id="PU-1", relation="following")
        good, dead = "aa" * 32, "bb" * 32
        P.register(app, user_id=a.user_id, token=good, environment="production")
        P.register(app, user_id=a.user_id, token=dead, environment="sandbox")
        P.register(app, user_id=b.user_id, token="cc" * 32, environment="production")
        sent = []
        def fake(token, env, payload):
            sent.append((token, env, payload["aps"]["alert"]["body"]))
            return (410, "Unregistered") if token == dead else (200, "")
        only = lambda c: {k: v for k, v in c.items()}
        # first sighting: a baseline, not news
        c = P.notify(owner, send_fn=fake)
        assert c["baselined"] >= 2 and not sent
        # nothing changed: nothing said
        assert P.notify(owner, send_fn=fake)["changed"] == 0 and not sent
        # the City closes it
        owner.execute("SELECT set_config('app.role', '', false)")
        owner.execute("UPDATE service_requests SET status = 'Closed' WHERE service_request_id = 'PU-1'"); owner.commit()
        c = P.notify(owner, send_fn=fake)
        assert c["sent"] == 2 and c["devices_off"] == 1
        assert sorted(t for t, _, _ in sent) == [good, dead, "cc" * 32]
        assert [b_ for t, _, b_ in sent if t == good] == ["Your request PU-1 at 500 N PITT ST is now closed."]
        assert [b_ for t, _, b_ in sent if t == "cc" * 32][0].startswith("A request you follow PU-1")
        # told once: a second pass is silent, and the dead token stays off
        sent.clear()
        assert P.notify(owner, send_fn=fake)["changed"] == 0 and not sent
        owner.execute("SELECT set_config('app.role', 'admin', false)")
        assert owner.execute("SELECT disabled_at IS NOT NULL AS off FROM push_devices WHERE token = %s",
                             (dead,)).fetchone()["off"] is True
        # private: b cannot remove a's device; signing in on a's phone as b moves the token
        assert P.unregister(app, user_id=b.user_id, token=good) is False
        P.register(app, user_id=b.user_id, token=good, environment="production")
        assert owner.execute("SELECT user_id FROM push_devices WHERE token = %s", (good,)).fetchone()["user_id"] == b.user_id
        assert P.unregister(app, user_id=b.user_id, token=good) is True
    finally:
        app.rollback(); app.close()
        owner.rollback()
        owner.execute("DELETE FROM service_requests WHERE service_request_id LIKE 'PU-%'")
        owner.execute("DELETE FROM portal_users WHERE email LIKE 'pu-%@test.io'"); owner.commit(); owner.close()
