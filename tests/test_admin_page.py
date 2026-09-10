"""The admin page, now a status board rather than a review desk.

Testers objected to moderation, and it was removed: a tester's own press sends
their request to the City. That makes two things load-bearing. The tester needs
to see what happened to their request, because nobody will tell them. And an
administrator needs to see what the system did, because approving each one is
no longer how they would find out.

What these pin, then, is that the release step is really gone, that nothing
quietly reintroduces it, and that the instrumentation which replaced it
actually answers the questions it exists to answer.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "dashboard/admin.html").read_text()
ROUTES = (ROOT / "dashboard/submit.py").read_text()
DB = (ROOT / "src/alex311/db.py").read_text()
FORM = (ROOT / "dashboard/submit.html").read_text()
WORKER = (ROOT / "src/alex311/submit_worker.py").read_text()


# --------------------------------------------------- the review desk is gone

def test_there_is_no_way_to_release_somebody_elses_request():
    """The endpoint is gone, not hidden. A button that only administrators can
    see is still a moderation step to whoever is waiting on it."""
    assert '@router.post("/api/approve/{attempt_id}")' not in ROUTES
    assert "data-release" not in PAGE
    assert "armRelease" not in PAGE


def test_the_board_says_it_is_not_a_queue_to_work():
    lede = PAGE.split("<h2>Last 30 days")[1].split("</p>")[0]
    assert "not a queue to work through" in lede


def test_the_one_lever_left_is_for_abuse_and_says_so():
    """Blocking a submitter is not moderating a request. Letting one through
    and rejecting one both went, because there is nothing to let through."""
    flagged = PAGE.split("function renderHeld")[1].split("\n}")[0]
    assert 'data-mod="block_submitter"' in flagged
    assert 'data-mod="approve"' not in PAGE and 'data-mod="reject"' not in PAGE
    assert "for abuse, not for a duplicate" in PAGE


def test_retrying_a_failure_is_not_moderation_by_another_name():
    """It only applies to rows the worker could not file, and the person who
    made the request already sent it."""
    retry = ROUTES.split('@router.post("/api/retry/{attempt_id}")')[1].split("@router")[0]
    assert "admin_only" in retry
    fn = DB.split("def retry_attempt(")[1].split("\ndef ")[0]
    assert "submit_state = 'failed'" in fn


# ------------------------------------------------------- what replaced it

def test_the_board_answers_did_it_work_and_how_fast():
    for key in ("filed", "median_seconds", "slowest_seconds", "last_filed_at"):
        assert key in DB.split("def instrumentation(")[1].split("\ndef ")[0]
    for label in ("filed with the City", "in flight", "failed", "refused by policy",
                  "typical time to a case number"):
        assert label in PAGE


def test_the_board_keeps_up_with_what_testers_are_doing():
    """A request goes from sent to filed in about a minute, so a board someone
    has to reload is a board that is always wrong."""
    assert "setInterval" in PAGE and "REFRESH_MS" in PAGE
    assert "document.hidden" in PAGE          # not while nobody is looking


def test_in_flight_is_the_thing_worth_watching():
    """It is where a request sits when the job never picked it up, which is the
    failure this design can actually have."""
    assert "IN_FLIGHT = ['queued', 'approved', 'filing']" in PAGE
    assert "the job did not pick up" in PAGE


# ---------------------------------------------- feedback for the tester

def test_the_tester_is_told_what_happened_because_nobody_else_will():
    for piece in ("id=\"track\"", "function watch(", "city_case_number", "Filing with the City"):
        assert piece in FORM


def test_a_failure_tells_them_nothing_was_sent():
    """The worst outcome is a tester assuming the City has it when it does
    not, and reporting nothing themselves."""
    assert "nothing was sent to the City" in FORM


# ------------------------------------------------------ filing starts itself

def test_the_web_service_starts_the_job_rather_than_waiting_for_a_person():
    assert "job_runner.kick" in ROUTES
    assert "background.add_task" in ROUTES


def test_a_job_that_will_not_start_does_not_lose_the_request():
    """The request is released either way; a kick that fails is a slower
    filing, not a lost one."""
    runner = (ROOT / "src/alex311/job_runner.py").read_text()
    assert "return" in runner.split("def kick(")[1]
    assert "raise" not in runner.split("def kick(")[1]


def test_only_one_worker_drives_a_browser_at_the_city():
    """Several executions can be alive at once now that each send starts one."""
    assert "pg_try_advisory_lock" in WORKER
    assert "take_the_floor" in WORKER
    assert "RECHECK_SECONDS" in WORKER          # closes the stranding window


# ------------------------------------------------------------ still gated

def test_the_admin_views_are_still_administrator_only():
    for name in ("def submission_queue(", "def queue(limit", "def users_list(",
                 "def admin_ui(", "def instrument(", "def retry("):
        head = ROUTES.split(name)[1].split(")")[0]
        assert "admin_only" in head, f"{name} is not administrator-only"


def test_both_jobs_still_live_behind_the_one_tab():
    assert 'id="panel-status"' in PAGE and 'id="panel-users"' in PAGE
    assert PAGE.count('role="tabpanel"') == 2
    assert PAGE.count('role="tab"') == 2


def test_the_old_address_for_user_management_still_works():
    users = ROUTES.split('@router.get("/users")')[1].split("@router")[0]
    assert "/submit/admin#users" in users
    assert "admin_only" in users


def test_the_public_page_can_ask_who_is_there_without_being_refused():
    fn = ROUTES.split("def whoami_portal(")[1].split("@app.exception_handler")[0]
    assert "except Unauthenticated" in fn
    assert 'return {"user": None}' in fn
    assert '@public.get("/api/whoami")' in ROUTES
