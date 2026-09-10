"""Usability behaviour of the gated submission page.

The geocoder is built on the City's own records rather than an external
service, so the interesting logic is how spelling variants of one address fold
together. The rest are static guards on the page: features that are easy to
delete by accident and hard to notice missing.
"""
import re
from pathlib import Path

import pytest

from dashboard.submit import merge_candidates

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "dashboard/submit.html").read_text()


def row(address, seen, lat=38.8, long=-77.1):
    return {"address": address, "seen": seen, "lat": lat, "long": long}


# ------------------------------------------------ folding address variants

def test_one_address_written_two_ways_is_one_candidate():
    """51 records of JANNEY'S LN and 48 of JANNEY'S LA are one place, and
    offering both would split the confidence signal as well as confuse."""
    got = merge_candidates([row("1437 JANNEY'S LN", 51), row("1437 JANNEY'S LA", 48)],
                           key="1437 JANNEYS LANE")
    assert len(got) == 1
    assert got[0]["seen"] == 99
    assert got[0]["exact"] is True


def test_the_more_common_spelling_represents_the_group():
    got = merge_candidates([row("MOUNT VERNON AV", 234), row("MOUNT VERNON AVE", 276)],
                           key="MOUNT VERNON AVENUE")
    assert got[0]["address"] == "MOUNT VERNON AVE"
    assert got[0]["seen"] == 510


def test_an_exact_match_outranks_a_more_common_near_match():
    got = merge_candidates([row("400 KING ST", 3), row("KING ST & N WEST ST", 90)],
                           key="400 KING STREET")
    assert got[0]["address"] == "400 KING ST"
    assert got[0]["exact"] is True and got[1]["exact"] is False


def test_near_matches_are_ordered_by_how_often_the_City_used_them():
    got = merge_candidates([row("KING ST & N WEST ST", 6), row("KING ST & N HAMPTON DR", 8)],
                           key="NOTHING MATCHES")
    assert [c["seen"] for c in got] == [8, 6]
    assert all(c["exact"] is False for c in got)


def test_distinct_addresses_stay_distinct():
    got = merge_candidates([row("100 KING ST", 4), row("200 KING ST", 4)], key="100 KING STREET")
    assert len(got) == 2


def test_no_rows_means_no_candidates():
    assert merge_candidates([], key="ANY") == []


# ------------------------------------------------------- page-level guards

def test_the_picker_collapses_once_a_type_is_chosen():
    """110 other request types are noise after the choice is made."""
    assert "state.picking" in PAGE
    assert 'id="change-svc"' in PAGE


def test_date_questions_are_seeded_with_now():
    """Seeded into state.answers, not just the input, so validation and the
    summary both see the value."""
    assert "function nowParts()" in PAGE
    assert "function seedDateDefaults" in PAGE
    assert "seedDateDefaults(vis);" in PAGE


def test_seeding_never_overwrites_an_answer_someone_typed():
    body = PAGE.split("function seedDateDefaults")[1].split("function renderQuestions")[0]
    assert "state.answers[key] !== undefined" in body and "return;" in body


@pytest.mark.parametrize("feature,needle", [
    ("use my location", "navigator.geolocation"),
    ("address lookup", "/submit/api/geocode"),
    ("reverse lookup after locating", "/submit/api/reverse"),
    ("a way in without either", "map.on('click'"),
])
def test_every_route_to_a_location_exists(feature, needle):
    """Three ways to set a location, because each one fails for someone: no GPS
    permission, an address with no 311 history, or a phone that cannot do either."""
    assert needle in PAGE, f"missing {feature}"


def test_a_denied_location_explains_what_to_do_instead():
    assert "PERMISSION_DENIED" in PAGE
    assert "Type an address or tap the map instead." in PAGE


def test_address_lookup_is_debounced():
    """It fires on every keystroke otherwise, once per letter typed."""
    assert re.search(r"addrTimer\s*=\s*setTimeout\(findAddress,\s*\d+\)", PAGE)


def test_the_new_controls_are_named_for_screen_readers():
    assert 'class="sr-only" for="address"' in PAGE
    assert 'aria-label="Address matches"' in PAGE
    assert 'id="loc-status" class="hint" role="status"' in PAGE


# --------------------------------------- naming the pin, and the city limit

def test_a_dropped_pin_looks_up_its_own_address():
    """Geolocation filled the address box and a map tap did not, which is the
    inconsistency that made a pin look like it had failed."""
    assert "function fillAddressFromPoint" in PAGE
    assert "lookup: true" in PAGE
    assert re.search(r"map\.on\('click'.*lookup: true", PAGE)


def test_the_address_lookup_never_overwrites_a_typed_address():
    body = PAGE.split("async function fillAddressFromPoint")[1].split("}")[0]
    assert "$('address').value.trim()" in body and "return;" in body


def test_points_outside_alexandria_are_flagged():
    """The map pans anywhere, so a pin lands in Arlington or DC easily, and the
    City closes those without action."""
    assert "const CITY_BOX" in PAGE and "inAlexandria" in PAGE
    assert "state.outsideCity" in PAGE


def test_an_out_of_city_point_is_never_reported_as_ready():
    """Warning that the City cannot act on a point while offering the handoff
    would be telling the resident two contradictory things."""
    assert "const ready = v.ok && state.lat != null && !state.outsideCity;" in PAGE


def test_the_city_box_covers_alexandria_and_excludes_the_district():
    """Calibrated against 33,832 geocoded requests: this rectangle holds 99.85%
    of them, and excludes a point across the river in DC."""
    box = re.search(r"const CITY_BOX = \{minLat: ([\d.]+), maxLat: ([\d.]+), "
                    r"minLong: (-[\d.]+), maxLong: (-[\d.]+)\}", PAGE)
    assert box, "CITY_BOX not found"
    min_lat, max_lat, min_long, max_long = (float(g) for g in box.groups())

    def inside(lat, long):
        return min_lat <= lat <= max_lat and min_long <= long <= max_long

    assert inside(38.8046, -77.0469), "Old Town is in Alexandria"
    assert inside(38.8246, -77.1255), "the West End is in Alexandria"
    assert inside(38.7671, -77.1436), "the southern tip is in Alexandria"
    assert not inside(38.8997, -77.0382), "that point is across the river in DC"
    assert not inside(38.8816, -77.0910), "that is Arlington"


# ------------------------------------------------ having us file it for you

def test_the_form_records_an_attempt_so_there_is_something_to_queue():
    """The abuse policy runs in precheck, and the row it writes is what a
    reviewer later approves. Validation alone leaves nothing to act on."""
    assert "/submit/api/precheck" in PAGE
    assert "async function precheck()" in PAGE
    assert "await precheck();" in PAGE


def test_there_is_a_way_to_ask_us_to_file_it():
    assert 'id="queue-btn"' in PAGE and "/submit/api/queue" in PAGE


def test_filing_asks_for_all_four_contact_fields():
    """The City refuses some request types without them, and the worker will
    not invent them, so a request queued without them only fails later."""
    for field in ("c-first", "c-last", "c-email", "c-phone"):
        assert f'id="{field}"' in PAGE
    assert "the City needs all four" in PAGE


def test_the_filing_offer_is_hidden_until_the_request_is_fit_to_send():
    assert "$('file-block').hidden = !(ready && state.attempt && !blocked)" in PAGE


def test_a_blocked_request_is_never_offered_for_filing():
    assert "state.attempt.outcome === 'block'" in PAGE


def test_choosing_a_different_type_clears_the_recorded_attempt():
    """Otherwise the queue button would file the previous request."""
    assert "state.valid = null; state.attempt = null;" in PAGE


def test_the_page_no_longer_claims_it_cannot_file():
    assert "does not file with the City yet" not in PAGE
    assert "dry-run only in this prototype" not in PAGE


def test_the_page_says_what_approval_actually_causes():
    """"A reviewer approves it" reads like a formality on the way to filing. It
    is the moment a real request is created, and nothing recalls it."""
    assert "cannot be taken back" in PAGE
    assert "point of no return" in PAGE
    assert "reviewer approves each one first" not in PAGE


LOGIN = (Path(__file__).resolve().parents[1] / "dashboard/login.html").read_text()


def test_the_login_page_says_this_is_a_test():
    """Anyone can reach it from the public dashboard now, so it has to explain
    itself to someone who arrived by curiosity."""
    assert "test feature" in LOGIN
    assert "What this is" in LOGIN and "Why it needs a sign-in" in LOGIN


def test_the_login_page_disclaims_the_City_and_points_at_the_real_portal():
    assert "Not an official City of Alexandria service" in LOGIN
    assert "alex311.alexandriava.gov" in LOGIN
    assert "nothing here is required to use it" in LOGIN


def test_the_login_page_does_not_promise_public_sign_up():
    assert "no public" in LOGIN.lower()


def test_you_can_get_back_to_the_dashboard_from_both_pages():
    assert 'href="/"' in LOGIN and 'href="/"' in PAGE


def test_sending_is_the_testers_own_action_and_the_page_admits_it():
    """The administrator step was removed on purpose: testers found waiting for
    one worse than not having the feature. What matters now is that the page
    does not imply somebody is still checking, because nobody is."""
    assert "reviewer" not in PAGE
    for promise in ("until an administrator", "has to release", "waits for"):
        assert promise not in PAGE, f"the page still implies review: {promise!r}"
    assert PAGE.lower().count("nobody reviews it") >= 2  # the notice and the offer


def test_the_only_gate_left_is_named_where_someone_will_read_it():
    """One key now, and it is an environment variable on a Cloud Run job. A
    person changing that file has to be told, since nothing else stops a
    tester's press from reaching the City."""
    worker = (ROOT / "src/alex311/submit_worker.py").read_text()
    assert "**One key" in worker
    assert "ALEX311_ALLOW_LIVE_SUBMIT" in worker.split("**One key")[1][:600]


def test_sending_still_takes_two_presses():
    """Not review — confirmation. It is irreversible and immediate, so the
    second press says what it does."""
    assert "armSend" in PAGE
    assert "Confirm: send this to the City" in PAGE


def test_a_blocked_request_is_refused_and_a_flagged_one_is_argued_with():
    """`review` used to park a request until somebody looked. With nobody
    looking it becomes a warning the person can overrule, which is feedback
    rather than moderation — and the overrule is recorded."""
    src = (ROOT / "dashboard/submit.py").read_text()
    send = src.split('@router.post("/api/queue")')[1].split("@router.get")[0]
    assert "abuse.BLOCK" in send and "abuse.REVIEW" in send
    assert "body.acknowledged" in send
    assert "record_moderation" in send


def test_the_tester_can_watch_their_own_request_and_only_their_own():
    """Ownership is who pressed send, not the resident-identity cookie. That
    cookie is optional here, and a check that skipped when it was missing
    handed every signed-in tester the address on anybody's request — which it
    did, until someone tried it."""
    src = (ROOT / "dashboard/submit.py").read_text()
    status = src.split('@router.get("/api/status/{attempt_id}")')[1].split("@router")[0]
    assert 'row["approved_by"] is not None and row["approved_by"] == actor' in status
    assert "if not (owner or an_admin)" in status
    assert "sent by someone else" in status
    assert "city_case_number" in status
    assert "watch(" in PAGE and "/submit/api/status/" in PAGE


def test_the_queue_views_are_still_not_open_to_every_tester():
    """Sending is self-service now. Reading what everybody else sent is not:
    those views carry other residents' addresses and what they reported."""
    src = (ROOT / "dashboard/submit.py").read_text()
    for endpoint in ("def submission_queue(", "def queue(limit", "def instrument("):
        line = src[src.index(endpoint):src.index(endpoint) + 160]
        assert "admin_only" in line, f"{endpoint} is not restricted"
