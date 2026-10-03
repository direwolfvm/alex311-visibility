"""The two pages the public and App Review open without signing in, and the
promise the privacy page makes about deleting an account.

/privacy and /support are server-rendered, need no sign-in, and say what is
true: who runs the site, that it is not the City, what a report sends, what
is deleted and when, and how to reach a person. Deleting an account now
removes the sign-in record too — from this site's own tenant only.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "dashboard/app.py").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
PRIVACY = (ROOT / "dashboard/static/privacy.html").read_text()
SUPPORT = (ROOT / "dashboard/static/support.html").read_text()


@pytest.mark.parametrize("fn", ["privacy", "support"])
def test_the_pages_are_whole_in_the_first_response(fn, monkeypatch):
    from dashboard import app as A
    monkeypatch.setenv("SITE_CONTACT_EMAIL", "support@alex311visibility.me")
    monkeypatch.setenv("SITE_OPERATOR", "Herbert Industries")
    r = getattr(A, fn)()
    html = r.body.decode()
    assert r.status_code == 200 and r.media_type == "text/html"
    assert "{{" not in html                                  # nothing left for a script to fill in
    assert "support@alex311visibility.me" in html and "Herbert Industries" in html
    assert "not\n      run by, affiliated with, or\n      endorsed" in html or "not run by, affiliated with, or" in html.replace("\n      ", " ")


def test_they_are_public_routes_ahead_of_the_static_mount():
    for route in ('@app.get("/privacy", response_class=HTMLResponse)', '@app.get("/support", response_class=HTMLResponse)'):
        assert route in APP and APP.index(route) < APP.index("app.mount(")
    home = (ROOT / "dashboard/static/home.html").read_text()
    assert 'href="/privacy"' in home and 'href="/support"' in home


def test_the_privacy_page_says_what_the_app_store_listing_needs_it_to():
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", PRIVACY))
    for must in (
        "an account id",                                          # what an account holds
        "Google's Identity Platform, which keeps the email address you sign in with",
        "first and last name, email address and phone number",    # what a report sends
        "deleted as soon as the City has filed the request",
        "removes the hidden location and device data",            # photos
        "Notes you write with a rating are private",
        "Watches",
        "device token Apple gives the app",                       # push tokens
        "No advertising. No tracking",
        "Delete my account", "Delete account",                    # website and app
        "its sign-in record",
        "not directed to children under 13",
    ):
        assert must in text, must
    assert "mailto:{{PRIVACY_EMAIL}}" in PRIVACY


def test_the_support_page_points_at_the_city_and_at_a_person():
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", SUPPORT))
    for must in ("This is not the City.", "In an emergency, call 911.", "alex311.alexandriava.gov",
                 "311, or 703-746-4311", "How do I sign in?", "Why does a report need my name, email and phone number?",
                 "How do I stop the emails?", "How do I delete my account?"):
        assert must in text, must
    assert "mailto:{{CONTACT_EMAIL}}" in SUPPORT


def test_deleting_an_account_removes_the_sign_in_from_the_tenant_only(monkeypatch):
    from alex311 import firebase_auth as fb
    class Creds:
        token = "t"
        def refresh(self, _): pass
    import google.auth, requests
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (Creds(), "p"))
    seen = {}
    class Resp:
        status_code = 200
        def json(self): return {}
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None, headers=None: (seen.update(url=url, body=json), Resp())[1])
    # no tenant configured: refuse, rather than touch the pool another application lives in
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "permitting-ai-helper")
    monkeypatch.delenv("FIREBASE_TENANT_ID", raising=False)
    with pytest.raises(fb.NotConfigured):
        fb.delete_tenant_user("uid-1")
    assert not seen
    monkeypatch.setenv("FIREBASE_TENANT_ID", "alex311-qfnem")
    assert fb.delete_tenant_user("uid-1") is True
    assert seen["url"].endswith("/projects/permitting-ai-helper/tenants/alex311-qfnem/accounts:delete")
    assert seen["body"] == {"localId": "uid-1"}
    # already gone is fine; anything else is reported, not raised
    class Gone:
        status_code = 400
        def json(self): return {"error": {"message": "USER_NOT_FOUND"}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Gone())
    assert fb.delete_tenant_user("uid-1") is True
    class Denied:
        status_code = 403
        def json(self): return {"error": {"message": "PERMISSION_DENIED"}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Denied())
    assert fb.delete_tenant_user("uid-1") is False


def test_the_route_removes_both_and_says_which():
    fn = ROUTES.split('@router.delete("/api/account")')[1].split("\n    @router")[0]
    assert "adb.delete_account(conn, user_id=user_id)" in fn and "fb.delete_tenant_user(uid)" in fn
    assert fn.index("adb.delete_account") < fn.index("fb.delete_tenant_user")   # our data first
    assert '"sign_in_removed": sign_in_removed' in fn
