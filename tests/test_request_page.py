"""The record page for one request.

A case number used to lead to the City's own page and nowhere else. That page
is getting less useful — it cannot display the HEIC photographs a large share
of reporters take on their phones, among other things — so the number now leads
here, and the City's copy is linked from this page rather than replaced by it.

The photographs are the reason it exists, so most of what is pinned here is
about them: that they are shown, that they are not shipped at full phone
resolution, and that a file which is not a picture is still offered.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "dashboard/static/request.html").read_text()
INDEX = (ROOT / "dashboard/static/index.html").read_text()
APP = (ROOT / "dashboard/app.py").read_text()


# ------------------------------------------------------- getting to the page

def test_a_case_number_is_a_url_somebody_can_send():
    """Not a query string on a page that reads it from state: the id is in the
    path, so the link survives being pasted into a message."""
    assert '@app.get("/r/{service_request_id}"' in APP


def test_the_dashboard_sends_people_here_rather_than_to_the_city():
    for link in ('<a href="/r/${encodeURIComponent(r.service_request_id)}">',
                 '<td><a href="/r/${encodeURIComponent(r.service_request_id)}"'):
        assert link in INDEX
    assert "target=\"_blank\">${r.service_request_id}</a>" not in INDEX


def test_the_official_record_is_still_one_click_away():
    """Ours is a mirror. Anything that matters legally is on theirs, so the
    link out is on the page, not removed from the product."""
    assert "d.report_url" in PAGE
    assert "Open the official City record" in PAGE
    assert "the authoritative copy" in PAGE


def test_the_page_is_routed_before_the_static_mount():
    """Starlette matches in order and the mount at / swallows everything, so a
    route added after it never runs."""
    assert APP.index('@app.get("/r/{service_request_id}"') < APP.index('app.mount(')


# --------------------------------------------------------------- the photos

def test_photos_that_the_citys_page_cannot_show_are_shown_here():
    """The ingest converts HEIC to JPEG, which is the whole reason this page can
    do something the City's cannot. The page says so where the photos are."""
    assert "heic" in PAGE.lower()
    assert "not open on the City's own page" in PAGE
    assert "HEIC is already JPEG by the time it reaches here" in APP


def test_a_gallery_does_not_ship_full_phone_resolution():
    """One phone photograph off this dataset is routinely 6 MB. A dozen of them
    unresized is a page nobody waits for."""
    assert "?w=320" in PAGE                    # thumbnails
    assert "?w=1200" in PAGE                   # the lightbox
    assert "/api/media/${m.media_id}`" in PAGE  # and the original, on request


def test_only_a_few_sizes_can_be_asked_for():
    """An open width parameter lets a visitor make us resize the same 6 MB
    picture a thousand different ways."""
    assert "THUMB_WIDTHS = (160, 320, 640, 1200)" in APP
    fn = APP.split("def media(")[1].split("\n@app")[0]
    assert "if w in THUMB_WIDTHS" in fn


def test_the_thumbnail_cache_is_keyed_before_the_download_not_after():
    """Keyed on the bytes, every hit still fetches the 3 MB original from
    storage first — which is most of the time it takes."""
    assert "def _thumbnail(stored_path: str, width: int)" in APP
    assert "functools.lru_cache" in APP.split("def _thumbnail(")[0].rsplit("\n\n", 1)[-1]


def test_a_file_that_is_not_a_picture_is_still_offered():
    """137 PDFs, 39 Word files and 33 videos are attached to these records."""
    assert "Attachments" in PAGE
    assert "files.map" in PAGE
    assert "stored_bytes" in APP.split("def request_detail(")[1].split("\n@app")[0]


def test_a_corrupt_image_is_served_rather_than_lost():
    fn = APP.split("def media(")[1].split("\n@app")[0]
    assert "except Exception" in fn
    assert "is still a file" in fn


# ---------------------------------------------------------------- the record

def test_it_shows_what_the_city_said_and_how_it_closed():
    for label in ("What the City said", "How it was closed", "Description"):
        assert label in PAGE


def test_a_closed_request_says_how_long_it_took():
    """Two timestamps make the reader do the subtraction."""
    assert "function span(" in PAGE
    assert "after ${span(d.requested_datetime, d.closed_datetime)}" in PAGE


def test_requests_the_city_tied_together_are_linked_both_ways():
    """Their record says a request is a duplicate without saying of what."""
    detail = APP.split("def request_detail(")[1].split("\n@app")[0]
    assert "duplicate_parent_service_request_id = %(id)s" in detail
    assert "parent_service_request_id = %(id)s" in detail
    assert "Tied to this request" in PAGE


def test_a_number_that_is_not_in_the_mirror_says_so_plainly():
    assert "notfound" in PAGE
    assert "may be newer than the last ingest" in PAGE


def test_a_record_we_only_half_have_admits_it():
    assert "d.enriched_at" in PAGE
    assert "Only the summary of this" in PAGE


# ------------------------------------------------------------------- chrome

def test_the_map_is_told_its_size_after_the_markup_is_written():
    """Leaflet measures its container once. This one is drawn into markup that
    was written a moment earlier, and the same mistake cost a day on the
    submission page."""
    assert "map.invalidateSize()" in PAGE
    assert "requestAnimationFrame" not in PAGE


# ---------------------------------------------------- serving other people's files

def test_an_html_attachment_is_not_rendered_in_our_own_origin():
    """Anyone can attach a file to a 311 request and five in this mirror are
    .html. Served inline they would run somebody else's script on our domain,
    next to the sign-in this site now has. They are handed over instead."""
    from dashboard.app import _delivery

    served, head = _delivery("text/html", "evil.html")
    assert served == "application/octet-stream"
    assert head["Content-Disposition"].startswith("attachment")
    assert head["X-Content-Type-Options"] == "nosniff"


def test_a_photograph_is_still_shown_rather_than_downloaded():
    from dashboard.app import _delivery

    served, head = _delivery("image/jpeg", "photo.jpg")
    assert served == "image/jpeg"
    assert "Content-Disposition" not in head


def test_a_filename_cannot_break_out_of_the_header():
    from dashboard.app import _delivery

    _served, head = _delivery("application/zip", 'a"; x=y.zip')
    assert head["Content-Disposition"].count('"') == 2


def test_the_map_credits_the_people_whose_tiles_it_uses():
    """Turning attribution off to tidy a 190px map is using someone's service
    without the one thing they ask for."""
    assert "attributionControl: false" not in PAGE
    assert "OpenStreetMap contributors" in PAGE
