"""A sign-in link that lands in a browser which did not ask for it.

Mail services that rewrite links (Outlook Safe Links and its kind) deliver
the phone to this page by redirect, and iOS only hands a link to an app when
it is tapped directly. The page used to prompt for an email and sign in —
using up the one-time code the app was waiting for. Now it offers a choice
and consumes nothing until the person makes it. And a refused request for
another link says when one is possible.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIN = (ROOT / "dashboard/login.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
AUTH = (ROOT / "src/alex311/firebase_auth.py").read_text()


def test_the_page_no_longer_prompts():
    assert "prompt(" not in LOGIN


def test_the_choice_is_made_from_the_url_before_firebase_loads():
    """So it appears at once, and showing it makes no sign-in call."""
    head = LOGIN.split("(async () => {")[0]
    assert "const hasLink = linkParams.get('mode') === 'signIn' && !!linkParams.get('oobCode');" in head
    assert "$('link-choice').hidden = false;" in head
    assert "signInWithEmailLink" not in head


def test_open_in_the_app_carries_the_whole_query_on_the_apps_scheme():
    assert "$('open-app').href = 'com.herbertindustries.alex311-reborn://signin' + location.search;" in LOGIN
    # alex311:// is the City's own app's scheme; the button must never use it
    assert "'alex311://" not in LOGIN
    assert ">Open in the app</a>" in LOGIN
    assert "If you asked to sign in from the app on\n        this phone, open the link there." in LOGIN   # no promise it is installed


def test_the_app_leads_on_an_iphone_and_both_show_everywhere():
    assert "/iPhone|iPad|iPod/.test(navigator.userAgent)" in LOGIN
    assert "classList.toggle('app-first', onIOS)" in LOGIN and "classList.toggle('lead', onIOS)" in LOGIN
    assert 'id="choice-app"' in LOGIN and 'id="choice-here"' in LOGIN
    # the choice comes before the page's long explanation, so a phone shows it without scrolling
    assert LOGIN.index('id="link-choice"') < LOGIN.index('class="explain"')
    assert "#link-choice:not(.app-first) { display:flex; flex-direction:column-reverse; }" in LOGIN


def test_signing_in_here_happens_only_when_asked_or_when_this_browser_requested_the_link():
    body = LOGIN.split("if (auth.isSignInWithEmailLink(location.href)) {")[1].split("$('google')")[0]
    assert "if (askedHere) {" in body and "await signInHere(askedHere);" in body
    other = body.split("} else {")[1]
    assert "$('here-go').addEventListener('click', go);" in other
    assert "signInHere(" not in other.split("const go = () => {")[0]      # nothing runs unprompted
    assert 'id="here-email"' in LOGIN                                      # an inline field, not a dialog


def test_a_refusal_says_when_and_the_limits_are_the_ones_the_app_was_told():
    assert "LINKS_PER_ADDRESS_PER_HOUR = 5" in ROUTES and "LINK_SPACING_SECONDS = 60" in ROUTES
    assert "LINKS_PER_CALLER_PER_HOUR = 20" in ROUTES
    fn = ROUTES.split("def email_sign_in_link(")[1].split("\n    @public")[0]
    assert 'headers={"Retry-After": str(wait)}' in fn and "raise HTTPException(429" in fn
    # both limits are checked before either is counted
    assert fn.index("_wait(f\"ip:{caller}\"") < fn.index("link_sends.setdefault(key, []).append")


def test_the_wait_is_until_the_oldest_send_leaves_the_window_or_the_spacing_passes():
    import math, time
    src = ROUTES.split("    def _wait(")[1].split("\n    @public")[0]
    ns = {"time": time, "math": math, "link_sends": {}}
    exec("def _wait(" + "\n".join(l[4:] if l.startswith("    ") else l for l in src.split("\n")), ns)
    wait, sends = ns["_wait"], ns["link_sends"]
    now = time.monotonic()
    assert wait("k", 5, spacing=60) == 0
    sends["k"] = [now - 10]
    assert 49 <= wait("k", 5, spacing=60) <= 50                         # spacing: 60 s apart
    sends["k"] = [now - 3000, now - 2000, now - 1500, now - 900, now - 400]
    assert 599 <= wait("k", 5, spacing=60) <= 600                       # hourly: when the oldest leaves
    sends["k"] = [now - 4000]                                           # outside the window: forgotten
    assert wait("k", 5, spacing=60) == 0 and sends["k"] == []


def test_the_email_points_app_users_at_their_phone():
    assert "Using the Alex311 Reborn app on an iPhone? Open this link on that phone." in AUTH
