"""The admin page: moderation and user management behind one tab.

Releasing a request is the only irreversible thing this prototype can do, so
most of what is pinned here is about that button: what it says, how many
presses it takes, and whether the person pressing it can see what they are
releasing.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "dashboard/admin.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
DB = (ROOT / "src/alex311/db.py").read_text()
FORM = (ROOT / "dashboard/submit.html").read_text()


# ------------------------------------------------------------- the release

def test_releasing_takes_two_presses():
    """A button reading "Release" beside a list is too easy to press by habit,
    and there is no undo once the City has the request."""
    assert "armRelease" in PAGE
    assert "if (!rel.dataset.armed) return armRelease" in PAGE


def test_the_second_press_says_what_it_does():
    """Not "Confirm" — what is being confirmed."""
    arm = PAGE.split("function armRelease")[1].split("async function")[0]
    assert "file this with the city" in arm.lower()


def test_the_page_says_releasing_cannot_be_recalled():
    lede = PAGE.split('<h2>Waiting for release</h2>')[1].split("</p>")[0]
    for phrase in ("live action", "cannot be recalled", "real staff"):
        assert phrase in lede, f"the warning no longer mentions {phrase!r}"


def test_you_can_see_what_you_are_releasing():
    """Address and description are the two fields worth re-reading before a
    request becomes a City record, and contact goes to the City as typed."""
    details = PAGE.split("function details(")[1].split("\n}")[0]
    for field in ("r.address", "r.description", "contactLine(r.contact)"):
        assert field in details


def test_the_policys_own_verdict_travels_with_the_request():
    """Some of these were held once already. Releasing without seeing why is
    the mistake this view exists to prevent."""
    assert "findingsBlock" in PAGE
    assert "r.findings" in PAGE


# ---------------------------------------------------------------- the lists

def test_only_queued_requests_offer_a_release_button():
    boot = PAGE.split("async function load()")[1]
    assert "renderWaiting(rows.filter(r => r.submit_state === 'queued'))" in boot
    assert "renderHistory(rows.filter(r => r.submit_state !== 'queued'))" in boot


def test_the_empty_state_explains_the_gate_rather_than_saying_nothing():
    empty = PAGE.split("Nothing is waiting.")[1].split("</p>")[0]
    assert "nothing reaches the City until you release it" in empty


def test_history_reaches_past_the_moment_of_release():
    """A history that stops at "approved" cannot answer the question the page
    is for: did releasing it actually produce a City case number?"""
    states = DB.split("def submission_queue(")[1].split(")")[0]
    assert "filed" in states
    assert "city_case_number" in DB.split("def submission_queue(")[1].split("fetchall")[0]


def test_the_queue_carries_what_a_release_decision_turns_on():
    query = DB.split("def submission_queue(")[1].split("fetchall")[0]
    for column in ("contact", "findings", "address", "description"):
        assert column in query


# ------------------------------------------------------------ getting there

def test_both_jobs_live_behind_the_one_tab():
    assert 'id="panel-moderation"' in PAGE and 'id="panel-users"' in PAGE
    assert PAGE.count('role="tabpanel"') == 2
    assert PAGE.count('role="tab"') == 2


def test_the_old_address_for_user_management_still_works():
    """It was linked from the form and may be in somebody's history."""
    users = ROUTES.split('@router.get("/users")')[1].split("@router")[0]
    assert "/submit/admin#users" in users
    assert "admin_only" in users


def test_the_page_opens_on_the_panel_the_link_asked_for():
    assert "panelFromHash" in PAGE and "hashchange" in PAGE


def test_the_form_no_longer_offers_its_own_way_in():
    """Two doors to user management is one more than needs maintaining."""
    assert "/submit/users" not in FORM
    assert not (ROOT / "dashboard/users.html").exists()


def test_signing_out_is_still_reachable_from_here():
    assert "/submit/logout" in PAGE


# ------------------------------------------------------- who may see it all

def test_the_page_asks_for_admin_data_and_the_routes_check():
    for endpoint in ("/submission-queue", "/review-queue", "/users"):
        assert f"api('{endpoint}')" in PAGE
    for name in ("def submission_queue(", "def queue(limit", "def users_list(",
                 "def admin_ui("):
        head = ROUTES.split(name)[1].split(")")[0] if name.endswith("(") else \
            ROUTES.split(name)[1].split("\n")[0]
        assert "admin_only" in head, f"{name} is not administrator-only"


def test_the_public_page_can_ask_who_is_there_without_being_refused():
    """The dashboard is public and asks whoami to decide whether to show the
    Admin tab. A 401 there would be noise on every anonymous visit."""
    fn = ROUTES.split("def whoami_portal(")[1].split("@app.exception_handler")[0]
    assert "except Unauthenticated" in fn
    assert 'return {"user": None}' in fn
    # and it has to sit on the public router, or the gate answers first and the
    # try/except above never runs
    assert '@public.get("/api/whoami")' in ROUTES
