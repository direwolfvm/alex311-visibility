"""Offering the City's spelling, and using it whether or not they take it.

A tester reported that getting an address accepted was hard. The reason was
not the lookup: it was that the lookup's result went nowhere. Someone could
type a correct address, see it matched, and still have the request filed in
wording the City's own gazetteer does not recognise — because the text typed
into that box was theirs, verbatim.

So two things are pinned here. The form *offers* the City's spelling rather
than replacing what the resident wrote. And when they keep their own wording,
the City is still handed its own, because that is the only thing its address
search answers to.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "dashboard/submit.html").read_text()
API = (ROOT / "dashboard/submit.py").read_text()
WORKER = (ROOT / "src/alex311/submit_worker.py").read_text()
DB = (ROOT / "src/alex311/db.py").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


# ------------------------------------------------------- the two are separate

def test_what_the_resident_typed_and_what_the_city_gets_are_different_columns():
    """Overwriting the resident's wording would be the easy fix and the wrong
    one: they should recognise their own address when they look at it."""
    assert "ADD COLUMN IF NOT EXISTS city_address TEXT" in SCHEMA
    assert "city_address: str | None = None" in API
    fn = DB.split("def record_attempt(")[1].split("\ndef ")[0]
    assert "city_address" in fn


def test_the_form_never_replaces_the_box_unless_the_resident_asks():
    """The suggestion writes to the box only inside the click handler for the
    button that offers it."""
    offer = FORM.split("function offerSpelling(")[1].split("\nfunction ")[0]
    assert "take-spelling" in offer
    assert offer.index("take-spelling") < offer.index("$('address').value = sug.address")


def test_declining_the_suggestion_still_files_under_the_citys_spelling():
    """This is the whole point. `setCityAddress` is called on every exact
    lookup, before and independently of the offer being accepted — so keeping
    your own wording still sends the City its own."""
    fn = FORM.split("async function findAddress()")[1].split("\n$('find-addr')")[0]
    assert "setCityAddress(data.suggestion ? data.suggestion.address : null)" in fn
    assert fn.index("setCityAddress(data.suggestion") < fn.index("offerSpelling(data.suggestion)")


def test_the_quiet_translation_is_disclosed_rather_than_hidden():
    """Translating silently would be a different kind of unpleasant surprise —
    a City confirmation naming an address they never typed."""
    assert "we will send the City" in FORM
    assert "Filed with the City as:" in FORM


# --------------------------------------------------------- it reaches the City

def test_the_worker_types_the_citys_spelling_into_the_citys_box():
    fn = WORKER.split("def file_one(")[1].split("\ndef ")[0]
    assert 'row.get("city_address") or row.get("address") or ""' in fn


def test_a_request_with_no_match_still_files_under_what_they_typed():
    """Coverage is partial — an address that never had a 311 request is not in
    our records. Those must not become unfilable."""
    fn = WORKER.split("def file_one(")[1].split("\ndef ")[0]
    assert 'or row.get("address") or ""' in fn
    find = FORM.split("async function findAddress()")[1].split("\n$('find-addr')")[0]
    assert "setCityAddress(null)" in find


def test_editing_the_address_drops_a_translation_that_no_longer_describes_it():
    handler = FORM.split("$('address').addEventListener('input'")[1].split("});")[0]
    assert "setCityAddress(null)" in handler


# ------------------------------------------------------------ the suggestion

def test_the_api_says_whether_its_spelling_differs_from_what_was_typed():
    """No point interrupting someone who typed it the City's way already."""
    fn = API.split("def geocode(")[1].split("\n    @router")[0]
    assert '"differs"' in fn
    assert '"suggestion": suggestion' in fn


def test_the_offer_is_silent_when_the_spellings_agree():
    offer = FORM.split("function offerSpelling(")[1].split("\nfunction ")[0]
    assert "if (!sug || !sug.differs)" in offer


def test_a_near_miss_is_never_presented_as_the_same_address():
    """"500 north st" finds "500 NORTH VIEW TER" on a shared prefix. Saying
    "the City files this address as 500 NORTH VIEW TER" asserts an identity
    that is false, and it was doing exactly that in production."""
    fn = API.split("def geocode(")[1].split("\n    @router")[0]
    assert 'cands and cands[0]["exact"]' in fn


def test_a_near_miss_is_never_quietly_filed_under_either():
    """The silent half is the dangerous half: nobody would notice until the
    City's confirmation named a street they had never heard of."""
    find = FORM.split("async function findAddress()")[1].split("\n$('find-addr')")[0]
    assert "setCityAddress(data.suggestion ? data.suggestion.address : null)" in find


def test_near_misses_still_reach_the_person_as_a_question():
    """They are not discarded — the candidate list asks rather than tells, and
    picking one is an explicit choice."""
    find = FORM.split("async function findAddress()")[1].split("\n$('find-addr')")[0]
    assert "Pick the closest match" in find
    assert "cands.map" in find
