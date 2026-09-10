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

PAGE = (Path(__file__).resolve().parents[1] / "dashboard/submit.html").read_text()


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
