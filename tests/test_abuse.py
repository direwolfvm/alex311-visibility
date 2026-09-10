"""The anti-abuse policy: what it catches, and what it deliberately does not.

The false-positive cases matter as much as the true ones. A resident whose
pothole report is held for review because their street is busy is a worse
outcome than a spammer getting one extra request through, so the tests below
pin both directions.
"""
from datetime import datetime, timedelta, timezone

import pytest

from alex311.abuse import (ALLOW, BLOCK, NOTICE, REVIEW, DEFAULT_POLICY, Event, Policy,
                           evaluate, normalize_address, similarity, sql_prefix, summarize)

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def ev(minutes=0, days=0, address="80 S EARLEY ST", category="Noise Issues",
       submitter=None, text="loud trucks at all hours", closed=None):
    return Event(at=T0 + timedelta(days=days, minutes=minutes), address=address,
                 category=category, submitter_id=submitter, description=text,
                 closed_at=closed)


# ------------------------------------------------------------- addresses

@pytest.mark.parametrize("a,b", [
    ("1437 JANNEY'S LN", "1437 JANNEY'S LA"),      # both spellings, one address
    ("MOUNT VERNON AVE", "MOUNT VERNON AV"),
    ("3512 OLD DOMINION BV", "3512 Old Dominion Boulevard"),
    ("100 King St.", "100 KING STREET"),
    ("N BEAUREGARD ST & SEMINARY RD", "Seminary Rd & N Beauregard St"),
])
def test_spelling_variants_collapse_to_one_key(a, b):
    assert normalize_address(a) == normalize_address(b)


def test_different_addresses_stay_different():
    assert normalize_address("100 KING ST") != normalize_address("200 KING ST")
    assert normalize_address("100 N KING ST") != normalize_address("100 S KING ST")


@pytest.mark.parametrize("raw", ["1437 JANNEY'S LN", "1437 JANNEY'S LA", "1437 JANNEYS LANE"])
def test_sql_prefix_matches_however_the_address_was_stored(raw):
    """SQL narrows with this prefix, so it has to match every stored spelling."""
    import re
    stored = re.sub(r"[.,']", "", raw.upper())
    assert stored.startswith(sql_prefix("1437 JANNEY'S LN"))


# ----------------------------------------------------------- the quiet path

def test_a_first_report_is_simply_allowed():
    d = evaluate(ev(), [])
    assert d.outcome == ALLOW and d.allowed and summarize(d) == "No concerns."


def test_a_busy_street_with_varied_reports_is_not_held():
    """400 King Street: many requests, many categories, many voices."""
    history = [ev(days=-i, category=f"Category {i}", text=f"distinct problem {i}")
               for i in range(1, 8)]
    d = evaluate(ev(address="400 KING ST", category="Pothole", text="new pothole"),
                 [e for e in history])
    assert d.outcome == ALLOW


def test_recurring_defect_reported_by_different_people_is_not_a_burst():
    """One category, many reporters, each describing it their own way."""
    history = [ev(days=-i, text=f"signal out, reported by neighbour {i}") for i in range(1, 6)]
    d = evaluate(ev(text="the light is dark again tonight"), history)
    assert "address_burst_low_diversity" not in [f.rule for f in d.findings]


# ------------------------------------------------------------ what it catches

def test_a_same_day_burst_at_one_address_is_held():
    history = [ev(minutes=-30 * i) for i in range(1, DEFAULT_POLICY.address_per_day + 1)]
    d = evaluate(ev(), history)
    assert d.outcome == REVIEW
    assert "address_daily" in [f.rule for f in d.findings]


def test_near_identical_text_repeated_at_one_address_is_held():
    history = [ev(days=-i, text="loud trucks at all hours") for i in range(1, 6)]
    d = evaluate(ev(text="loud trucks at all hours"), history)
    rules = [f.rule for f in d.findings]
    assert "address_burst_low_diversity" in rules and d.outcome == REVIEW


def test_a_honeypot_blocks_outright_and_skips_everything_else():
    d = evaluate(ev(), [], honeypot_filled=True)
    assert d.outcome == BLOCK and not d.allowed
    assert [f.rule for f in d.findings] == ["honeypot"]


# ------------------------------------------------------ duplicates are notices

def test_an_open_duplicate_is_a_notice_not_a_hold():
    """The backtest showed this firing on a legitimately busy block. Telling a
    resident their issue is already reported is a prompt, not a moderation."""
    d = evaluate(ev(), [ev(days=-1)])
    assert d.outcome == NOTICE
    assert d.allowed, "a duplicate notice must never stop a resident"
    assert [f.rule for f in d.findings] == ["open_duplicate"]


def test_a_closed_earlier_request_is_not_a_duplicate():
    d = evaluate(ev(), [ev(days=-2, closed=T0 - timedelta(days=1))])
    assert d.outcome == ALLOW


def test_a_duplicate_older_than_the_window_is_not_a_duplicate():
    old = DEFAULT_POLICY.same_category_repeat_days + 2
    assert evaluate(ev(), [ev(days=-old)]).outcome == ALLOW


def test_a_different_category_at_the_same_address_is_not_a_duplicate():
    d = evaluate(ev(category="Pothole"), [ev(days=-1, category="Noise Issues")])
    assert d.outcome == ALLOW


# ------------------------------------------------------------ identity rules

def test_per_submitter_daily_limit():
    mine = [ev(minutes=-10 * i, address=f"{i} KING ST", category=f"C{i}",
               submitter="u1", text=f"thing {i}")
            for i in range(1, DEFAULT_POLICY.submitter_per_day + 1)]
    d = evaluate(ev(address="900 KING ST", category="Cx", submitter="u1", text="another"), mine)
    assert "submitter_daily" in [f.rule for f in d.findings] and d.outcome == REVIEW


def test_targeting_one_address_is_held():
    """A submitter whose activity concentrates on a single address."""
    mine = [ev(days=-i, submitter="u1", category=f"C{i}", text=f"complaint {i}")
            for i in range(1, 4)]
    d = evaluate(ev(submitter="u1", category="Cx", text="another complaint"), mine)
    assert "targeting_concentration" in [f.rule for f in d.findings]


def test_a_submitter_spread_across_the_city_is_not_targeting():
    mine = [ev(days=-i, submitter="u1", address=f"{i}00 KING ST",
               category=f"C{i}", text=f"complaint {i}") for i in range(1, 6)]
    d = evaluate(ev(address="700 KING ST", category="Cx", submitter="u1", text="new"), mine)
    assert "targeting_concentration" not in [f.rule for f in d.findings]


REPOST = ("The garbage truck idles outside my window from four in the morning "
          "and the noise is unbearable every single weekday")


def test_a_submitter_reposting_their_own_words_is_held():
    mine = [ev(days=-1, submitter="u1", address="1 A ST", text=REPOST)]
    d = evaluate(ev(submitter="u1", address="2 B ST", category="Other", text=REPOST), mine)
    assert "duplicate_text" in [f.rule for f in d.findings]


def test_identity_rules_stay_silent_without_a_submitter():
    """All historical city data is anonymous; those rules must not fire on it."""
    anon = [ev(days=-i, text=f"complaint {i}") for i in range(1, 4)]
    d = evaluate(ev(), anon)
    assert not any(f.rule.startswith("submitter") or f.rule in
                   ("targeting_concentration", "duplicate_text") for f in d.findings)


def test_review_sets_a_cooldown_only_when_a_submitter_is_known():
    mine = [ev(minutes=-10 * i, submitter="u1", address=f"{i} KING ST",
               category=f"C{i}", text=f"t{i}") for i in range(1, 7)]
    with_id = evaluate(ev(address="99 KING ST", category="Cx", submitter="u1", text="x"), mine)
    assert with_id.cooldown_until is not None
    anon = evaluate(ev(), [ev(minutes=-30 * i) for i in range(1, 4)])
    assert anon.outcome == REVIEW and anon.cooldown_until is None


# ----------------------------------------------------------------- tuning

def test_thresholds_are_data_so_an_operator_can_retune_without_a_deploy():
    strict = Policy(address_per_day=2)
    history = [ev(minutes=-30), ev(minutes=-60)]
    assert evaluate(ev(), history, policy=strict).outcome == REVIEW
    assert "address_daily" not in [
        f.rule for f in evaluate(ev(), history, policy=DEFAULT_POLICY).findings]


def test_similarity_is_insensitive_to_case_and_punctuation():
    assert similarity("Loud trucks!", "loud trucks") > 0.95
    assert similarity("loud trucks", "broken streetlight") < 0.5


def test_short_descriptions_do_not_trigger_the_duplicate_text_rule():
    """13% of real descriptions are under 40 characters, and at that length two
    different problems score above the similarity threshold by coincidence."""
    mine = [ev(days=-1, submitter="u1", address="1 A ST", text="pothole on my street")]
    d = evaluate(ev(submitter="u1", address="2 B ST", category="Other",
                    text="potholes on my street"), mine)
    assert "duplicate_text" not in [f.rule for f in d.findings]

