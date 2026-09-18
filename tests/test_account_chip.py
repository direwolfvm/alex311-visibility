"""The account chip and the account page.

The top-right corner used to say "name · Sign out". It is now a chip with the
person's initial and label that opens a menu, and behind it an account page:
who you are here, what the site holds, sign out everywhere, delete.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHIP = (ROOT / "dashboard/static/account-chip.js").read_text()
ACCOUNT = (ROOT / "dashboard/account.html").read_text()
MY = (ROOT / "dashboard/my.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
AUTH = (ROOT / "src/alex311/portal_auth.py").read_text()


def test_the_chip_is_a_menu_button_with_the_expected_items():
    assert "btn.setAttribute('aria-haspopup', 'menu')" in CHIP
    assert "btn.setAttribute('aria-expanded', 'false')" in CHIP
    assert "menu.setAttribute('role', 'menu')" in CHIP
    assert "['My requests', '/submit/my'], ['Account', '/submit/account']" in CHIP
    assert "if (admin) items.push(['Admin', '/submit/admin'])" in CHIP
    assert "out.href = '/submit/logout'" in CHIP
    # keyboard: arrows move, Escape closes and returns focus, Tab leaves
    for key in ("'ArrowDown'", "'ArrowUp'", "'Escape'", "'Tab'"):
        assert key in CHIP


def test_a_visitor_gets_a_sign_in_link_and_the_label_is_never_html():
    assert "el('a', 'chip chip-in', 'Sign in')" in CHIP and "a.href = '/submit/login'" in CHIP
    assert not re.search(r"\.innerHTML\s*=", CHIP)   # labels are user data: textContent only
    assert "n.textContent = text" in CHIP


def test_the_chip_reveals_the_bar_links_the_pages_used_to_reveal():
    assert "if (user && my) my.hidden = false;" in CHIP
    assert "if (user && user.role === 'admin' && admin) admin.hidden = false;" in CHIP
    assert "window.alex311 = Object.assign(window.alex311 || {}, {whoami});" in CHIP


def test_the_account_page_is_gated_and_says_what_it_is_not():
    assert 'def account_ui(actor: str = Depends(gate))' in ROUTES
    assert 'account_page = Path(__file__).parent / "account.html"' in ROUTES
    assert "not your Alex311 account with the City, and does not connect to one" in ACCOUNT
    assert "The name shows only to you" in ACCOUNT and "never on the public site" in ACCOUNT
    assert 'href="/submit/my"' in ACCOUNT
    assert "await api('/whoami')" in ACCOUNT      # not window.alex311: deferred scripts run later


def test_sign_out_everywhere_revokes_every_session():
    assert '@router.post("/api/logout-all")' in ROUTES
    fn = ROUTES.split("def logout_everywhere(")[1].split("\n    @router")[0]
    assert "user_id = _account(request)" in fn and "pa.logout_all(conn, user_id)" in fn
    assert "resp.delete_cookie(pa.SESSION_COOKIE" in fn
    body = AUTH.split("def logout_all(")[1].split("\ndef ")[0]
    assert "WHERE user_id = %s AND revoked_at IS NULL" in body
    assert "Confirm: sign out on every device" in ACCOUNT


def test_whoami_says_how_the_person_signs_in():
    fn = ROUTES.split("def whoami_portal(")[1].split("@app.exception_handler")[0]
    for key in ('"firebase_linked"', '"policy_version"'):
        assert key in fn
    assert "policy_version: str | None = None" in AUTH
    assert "Google or an emailed link" in ACCOUNT


def test_deleting_moved_off_my_requests():
    assert "delete-account" not in MY
    assert 'id="delete-account"' in ACCOUNT


def test_a_display_name_is_optional_private_and_kept_tidy():
    """The one profile field. It changes what the chip and the account page
    call the person, and nothing public: public_scores never carries a name."""
    import pytest
    from alex311 import portal_auth as pa
    assert pa.clean_display_name(None) is None and pa.clean_display_name("   ") is None
    assert pa.clean_display_name("  Jordan   E. ") == "Jordan E."
    assert pa.clean_display_name("Mary-Jane O'Neil") == "Mary-Jane O'Neil"
    with pytest.raises(pa.BadName):
        pa.clean_display_name("x" * 41)
    with pytest.raises(pa.BadName):
        pa.clean_display_name("<script>")
    assert pa.PortalUser("pu_abcdef123456", "a@b.co", "user", display_name="Jordan").label == "Jordan"
    assert pa.PortalUser("pu_abcdef123456", "a@b.co", "user").label == "a@b.co"
    assert pa.PortalUser("pu_abcdef123456", None, "user").label == "account 123456"
    assert '@router.put("/api/profile")' in ROUTES
    fn = ROUTES.split("def set_profile(")[1].split("\n    @router")[0]
    assert "pa.clean_display_name(body.display_name)" in fn and "pa.set_display_name(conn, user_id, name)" in fn
    assert "ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS display_name TEXT;" in (ROOT / "src/alex311/schema.sql").read_text()
    assert 'id="display-name"' in ACCOUNT and "api('/profile', {method: 'PUT'" in ACCOUNT
    db = (ROOT / "src/alex311/db.py").read_text()
    assert "display_name" not in db.split("def public_scores(")[1].split("\ndef ")[0]
