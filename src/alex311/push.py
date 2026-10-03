"""Push notifications to the iOS app: "your request changed".

The app's background refresh is best-effort — iOS decides when. A push is
the City's status change reaching the person when the mirror learns of it:
after each ingest, every request someone sent or follows whose status is
not the one they were last told about gets one notification per device,
and the link remembers what was said (`request_links.notified_status`).

The first time a link's request is seen, nothing is sent — that status is
the baseline, not news. A request filed from here is "open" the first time
the mirror sees it, and the person already knows that.

Scope, deliberately: requests you sent or follow. Watches (a type, an
address, an area) stay on the daily digest; a push per new pothole in a
neighborhood is noise, not news.

Apple's side: token-based auth. A JWT signed ES256 with the team's APNs key
(`.p8`), reused for under an hour, sent over HTTP/2 to api.push.apple.com —
or the sandbox host for a development build, which is why a device says
which it is when it registers. A token Apple says is gone (410, or 400
BadDeviceToken) is switched off rather than retried.

Configuration (all four, or `configured()` is False and nothing is sent):

    APNS_KEY       the .p8 file's text
    APNS_KEY_ID    the key's ten-character id
    APNS_TEAM_ID   the Apple team id
    APNS_TOPIC     the app's bundle id
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

import psycopg

log = logging.getLogger("alex311.push")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_TTL = 50 * 60                 # Apple accepts a provider token for an hour
MAX_DEVICES = 10                    # per account
DEAD = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}

_jwt: tuple[float, str] | None = None


def configured() -> bool:
    return all(os.environ.get(k) for k in ("APNS_KEY", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def provider_token(now: float | None = None) -> str:
    """The JWT Apple wants: ES256 over {iss: team, iat}, kid in the header."""
    global _jwt
    now = now or time.time()
    if _jwt and now - _jwt[0] < TOKEN_TTL:
        return _jwt[1]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = serialization.load_pem_private_key(os.environ["APNS_KEY"].encode(), password=None)
    head = _b64(json.dumps({"alg": "ES256", "kid": os.environ["APNS_KEY_ID"]}).encode())
    body = _b64(json.dumps({"iss": os.environ["APNS_TEAM_ID"], "iat": int(now)}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    _jwt = (now, token)
    return token


def send(device_token: str, environment: str, payload: dict, *, client=None) -> tuple[int, str]:
    """One notification to one device. Returns (HTTP status, Apple's reason or '')."""
    import httpx
    own = client is None
    client = client or httpx.Client(http2=True, timeout=15)
    try:
        r = client.post(
            f"{HOSTS.get(environment, HOSTS['production'])}/3/device/{device_token}",
            content=json.dumps(payload).encode(),
            headers={"authorization": f"bearer {provider_token()}",
                     "apns-topic": os.environ["APNS_TOPIC"], "apns-push-type": "alert",
                     "apns-priority": "10", "content-type": "application/json"})
        reason = ""
        if r.status_code != 200:
            try:
                reason = r.json().get("reason", "")
            except Exception:
                reason = ""
        return r.status_code, reason
    finally:
        if own:
            client.close()


# ----------------------------------------------------------------- devices

class BadDevice(ValueError):
    pass


def register(conn: psycopg.Connection, *, user_id: str, token: str, environment: str) -> dict:
    """Remember a device for an account. A token belongs to one account at a
    time: signing in as someone else on the same phone moves it."""
    token = "".join(token.split()).lower()
    if not (32 <= len(token) <= 200) or any(c not in "0123456789abcdef" for c in token):
        raise BadDevice("that is not a device token")
    if environment not in HOSTS:
        raise BadDevice("environment must be production or sandbox")
    conn.execute("SELECT set_config('app.role', 'admin', true)")     # the token may be another account's row
    conn.execute(
        """INSERT INTO push_devices (user_id, token, environment)
           VALUES (%s, %s, %s)
           ON CONFLICT (token) DO UPDATE
             SET user_id = EXCLUDED.user_id, environment = EXCLUDED.environment,
                 last_seen_at = now(), disabled_at = NULL""",
        (user_id, token, environment))
    # an account's oldest devices fall off rather than growing without bound
    conn.execute(
        """DELETE FROM push_devices WHERE user_id = %s AND device_id NOT IN (
               SELECT device_id FROM push_devices WHERE user_id = %s
                ORDER BY last_seen_at DESC LIMIT %s)""", (user_id, user_id, MAX_DEVICES))
    conn.commit()
    return {"registered": True, "environment": environment}


def unregister(conn: psycopg.Connection, *, user_id: str, token: str) -> bool:
    conn.execute("SELECT set_config('app.user_id', %s, true), set_config('app.role', 'user', true)",
                 (user_id,))
    n = conn.execute("DELETE FROM push_devices WHERE user_id = %s AND token = %s",
                     (user_id, "".join(token.split()).lower())).rowcount
    conn.commit()
    return n > 0


# ----------------------------------------------------------------- the pass

def message(row: dict, site_origin: str) -> dict:
    status = (row["status"] or "updated").lower()
    what = row["service_name"] or "Your request"
    mine = row["relation"] == "mine"
    return {
        "aps": {"alert": {"title": f"{what}: {status}",
                          "body": (f"{'Your request' if mine else 'A request you follow'} "
                                   f"{row['service_request_id']}"
                                   + (f" at {row['address']}" if row["address"] else "")
                                   + f" is now {status}.")},
                "sound": "default", "thread-id": row["service_request_id"]},
        "case": row["service_request_id"],
        "url": f"{site_origin}/r/{row['service_request_id']}",
    }


def notify(conn: psycopg.Connection, *, site_origin: str = "https://alex311visibility.me",
           send_fn=None) -> dict:
    """One pass: tell each account about each linked request whose status is
    not the one they were last told. Returns counts."""
    send_fn = send_fn or send
    admin = "SELECT set_config('app.role', 'admin', false)"    # the job reads every link and device
    conn.execute(admin)
    changed = conn.execute(
        """SELECT l.user_id, l.service_request_id, l.relation, l.notified_status,
                  r.status, r.service_name, r.address
             FROM request_links l JOIN service_requests r USING (service_request_id)
            WHERE r.status IS NOT NULL AND r.status IS DISTINCT FROM l.notified_status
            ORDER BY l.user_id""").fetchall()
    counts = {"changed": len(changed), "baselined": 0, "sent": 0, "failed": 0, "devices_off": 0}
    for row in changed:
        if row["notified_status"] is not None:
            devices = conn.execute(
                "SELECT token, environment FROM push_devices WHERE user_id = %s AND disabled_at IS NULL",
                (row["user_id"],)).fetchall()
            payload = message(row, site_origin)
            for d in devices:
                try:
                    status, reason = send_fn(d["token"], d["environment"], payload)
                except Exception as e:
                    log.warning("push failed: %s", type(e).__name__)
                    counts["failed"] += 1
                    continue
                if status == 200:
                    counts["sent"] += 1
                elif status == 410 or reason in DEAD:
                    conn.execute("UPDATE push_devices SET disabled_at = now() WHERE token = %s",
                                 (d["token"],))
                    counts["devices_off"] += 1
                else:
                    log.warning("apns said %s %s", status, reason)
                    counts["failed"] += 1
        else:
            counts["baselined"] += 1
        # what they have now been told (or, the first time, simply what it is)
        conn.execute(
            "UPDATE request_links SET notified_status = %s WHERE user_id = %s AND service_request_id = %s",
            (row["status"], row["user_id"], row["service_request_id"]))
        conn.commit()
        conn.execute(admin)
    log.info("push pass: %s", counts)
    return counts
