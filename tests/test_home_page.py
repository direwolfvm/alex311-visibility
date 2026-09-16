"""The front door.

Visitors used to land on the map with its filters open. Now they land on a
page that says what the site is and gives the three things a resident can do
here equal room: see what has been reported, send a request to the City, and
say whether it was actually addressed. The map moved to /explore.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = (ROOT / "dashboard/static/home.html").read_text()
APP = (ROOT / "dashboard/app.py").read_text()


def test_the_root_is_the_front_door_and_the_map_is_explore():
    assert '@app.get("/", response_class=FileResponse)' in APP
    assert 'STATIC / "home.html"' in APP
    assert '@app.get("/explore", response_class=FileResponse)' in APP
    assert 'STATIC / "explore.html"' in APP
    assert (ROOT / "dashboard/static/explore.html").exists()
    assert not (ROOT / "dashboard/static/index.html").exists()
    # both routes are registered before the catch-all static mount
    assert APP.index('@app.get("/", response_class') < APP.index('app.mount(')


def test_the_three_things_get_equal_room():
    """One section, three articles, one each for seeing, reporting and rating —
    not a dashboard with a form bolted on."""
    three = re.search(r'<section class="three".*?</section>', HOME, re.S).group(0)
    assert three.count('<article class="do">') == 3
    labels = re.findall(r'<span class="n">(\w+)</span>', three)
    assert labels == ["See", "Report", "Rate"]
    assert 'href="/explore"' in three and 'href="/explore#analytics"' in three
    assert 'href="/submit"' in three
    assert 'href="/submit/my"' in three


def test_reporting_is_described_honestly():
    """It really files with the City and really is in beta; both are said."""
    assert "real City case number" in HOME
    assert re.search(r'<a href="/submit">Report an issue</a>\s*<span class="tab-tag">beta', HOME)


def test_rating_is_described_the_way_the_record_page_does_it():
    """The owner's score is the outcome of record; others' is a community view
    kept apart; it is private unless shown; names never appear."""
    assert "outcome of record" in HOME
    assert "community view" in HOME and "kept separate" in HOME
    assert "Private unless you choose to show your score" in HOME
    assert "Names are never shown" in HOME


def test_it_shows_the_live_thing():
    """Numbers and the newest cases come from the same APIs the dashboard uses,
    and the cases link to record pages here rather than the City's site."""
    assert "fetch('/api/meta')" in HOME
    assert "fetch('/api/analytics')" in HOME
    assert "fetch('/api/requests?sort=requested&dir=desc&limit=6')" in HOME
    assert 'href="/r/${encodeURIComponent(r.service_request_id)}"' in HOME
    assert "report_url" not in HOME


def test_it_says_what_it_is_not():
    assert "not an official City of Alexandria service" in HOME
    assert "call 911" in HOME
    assert 'href="https://alex311.alexandriava.gov"' in HOME
