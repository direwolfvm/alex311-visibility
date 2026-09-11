"""The submission harness: the gates, and the value conversions.

This is the only code in the project that can create a real city record, so the
tests that matter most are the ones proving it will not.
"""
import os

import pytest

from alex311 import submit_browser as sb
from alex311.wizard import _as_12h, _as_mdY


# --------------------------------------------------------------- the gates

@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(sb.ENV_GATE, raising=False)


def test_a_plain_run_is_a_dry_run():
    assert sb.gates_open(False)[0] is False


def test_live_alone_is_not_enough(monkeypatch):
    """The environment gate exists so that a stray --live cannot file anything."""
    assert sb.gates_open(True)[0] is False


def test_the_environment_alone_is_not_enough(monkeypatch):
    monkeypatch.setenv(sb.ENV_GATE, "1")
    assert sb.gates_open(False)[0] is False


def test_both_gates_together_open_it(monkeypatch):
    monkeypatch.setenv(sb.ENV_GATE, "1")
    allowed, why = sb.gates_open(True)
    assert allowed is True and "both gates" in why


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "TRUE", " 1"])
def test_only_the_exact_string_one_opens_the_environment_gate(monkeypatch, value):
    monkeypatch.setenv(sb.ENV_GATE, value)
    assert sb.gates_open(True)[0] is False


def test_the_gate_reason_is_reported_not_swallowed(monkeypatch):
    assert sb.ENV_GATE in sb.gates_open(True)[1]


# ------------------------------------------------- one place can press submit

def test_the_wizard_driver_cannot_press_submit():
    """`press` is how every other module clicks a wizard button, and it refuses.

    The single click that files a request lives in this harness alone, so there
    is exactly one line in the project to audit."""
    from pathlib import Path
    driver = (Path(sb.__file__).parent / "wizard.py").read_text()
    assert 'assert "submit" not in name.lower()' in driver


def test_submit_is_looked_for_inside_the_wizard_only():
    """The page behind the modal has a "Submit General Request" button of its
    own. A bare name match found that one, and reported a dry run as ready
    while it was actually sitting on the contact step."""
    from pathlib import Path
    src = Path(sb.__file__).read_text()
    assert "_within_modal(\"button\")" in src
    assert 'pg.get_by_role("button", name=re.compile(r"^Submit"' not in src


def test_the_modal_scoping_applies_to_every_alternative():
    """IN_MODAL is a selector list, so appending a descendant to the string
    attaches it only to the last alternative."""
    sel = sb._within_modal("[role=checkbox]")
    assert sel.count("[role=checkbox]") == len(sb.IN_MODAL.split(","))


# ----------------------------------------------- what actually gets typed in

@pytest.mark.parametrize("iso,typed", [
    ("2026-09-08", "09/08/2026"),
    ("2026-01-01", "01/01/2026"),
    ("09/08/2026", "09/08/2026"),      # already in the wizard's format
    ("not a date", "not a date"),
])
def test_dates_are_typed_in_the_format_the_wizard_wants(iso, typed):
    """The date box is a text input labelled MM/DD/YYYY, not an <input
    type=date>: an ISO date typed straight in is silently ignored and the
    request is filed with whatever the field already held."""
    assert _as_mdY(iso) == typed


@pytest.mark.parametrize("given,clock,meridiem", [
    ("06:30", "06:30", "AM"),
    ("18:45", "06:45", "PM"),
    ("12:15", "12:15", "PM"),
    ("00:30", "12:30", "AM"),
    ("09:00", "09:00", "AM"),
    ("23:59", "11:59", "PM"),
])
def test_times_are_split_into_a_clock_and_a_meridiem(given, clock, meridiem):
    """The wizard splits time across a text box and an AM/PM select, so an
    evening time filed as AM is twelve hours wrong."""
    assert _as_12h(given) == (clock, meridiem)


# ------------------------------------------------------------- result shape

def test_a_result_only_counts_as_submitted_at_that_exact_stage():
    for stage in ("ready_not_submitted", "needs_contact", "at_submit",
                  "address_not_serviceable", "needs_answers", "error"):
        assert sb.SubmitResult(True, False, stage, "X").submitted is False
    assert sb.SubmitResult(True, True, "submitted", "X").submitted is True


def test_bookkeeping_never_blocks_a_filing(monkeypatch):
    """A database that is unreachable must not stop an operator from filing."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/nope")
    r = sb.SubmitResult(True, True, "at_submit", "TESMISCO", "Missed Collection")
    sb._record(r, address="100 King St", description="x", live=True)   # must not raise
    assert r.attempt_id is None


def test_nothing_is_recorded_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = sb.SubmitResult(True, True, "at_submit", "TESMISCO", "Missed Collection")
    sb._record(r, address="100 King St", description="x", live=True)
    assert r.attempt_id is None and r.policy == {}


def test_the_consent_box_is_ticked_before_the_contact_fields_are_filled():
    """On services where contact is optional the City disables the four inputs
    until the consent box is ticked. Filling first waits on a disabled field
    and times out — which is how the first real request failed, three times.
    The order is the fix, so the order is what this pins."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/alex311/submit_browser.py").read_text()
    body = src.split("async def fill_contact(")[1].split("\nasync def ")[0]
    tick = body.index('box.click(force=True)')
    fill = body.index('loc.fill(str(value)')
    assert tick < fill, "the consent tick has to come before the fields are filled"
