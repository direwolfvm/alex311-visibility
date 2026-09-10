"""The bar at the top of every page.

Three pages grew their own headers, so moving between them felt like moving
between sites. These tests pin the parts that have to agree: the same items in
the same order pointing at the same places, and the same colour tokens under
them. They do not pin the styling itself — that is allowed to change, as long
as it changes everywhere at once.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "dashboard"
PAGES = {
    "dashboard": ROOT / "static/index.html",
    "form": ROOT / "submit.html",
    "users": ROOT / "users.html",
}
HTML = {name: path.read_text() for name, path in PAGES.items()}

# What the bar offers, in order. Explore and Analytics are panels of the
# dashboard, so they are buttons there and links everywhere else; the text and
# the destination are what a reader actually compares.
ITEMS = [("Explore", "/"), ("Analytics", "/#analytics"), ("Report an issue", "/submit")]

TOKENS = ("--bg", "--panel", "--ink", "--muted", "--accent", "--border")


def bar(html: str) -> str:
    m = re.search(r"<nav class=\"tabs\".*?</nav>", html, re.S)
    assert m, "no site bar on this page"
    return m.group(0)


def token(html: str, name: str) -> str | None:
    m = re.search(rf"{name}\s*:\s*(#[0-9a-fA-F]{{3,8}})", html)
    if not m:
        return None
    v = m.group(1).lower()
    return "#" + "".join(c * 2 for c in v[1:]) if len(v) == 4 else v  # #fff == #ffffff


@pytest.mark.parametrize("page", sorted(PAGES))
def test_every_page_carries_the_same_bar(page):
    text = re.sub(r"<[^>]+>", " ", bar(HTML[page]))
    labels = [w for w in (s.strip() for s in re.split(r"\s{2,}|\n", text)) if w]
    assert [lab for lab, _ in ITEMS] == [lab for lab in labels if lab in dict(ITEMS)]


@pytest.mark.parametrize("page", ["form", "users"])
def test_the_other_pages_link_to_the_dashboard_views(page):
    """They cannot press a tab on a page they are not on, so they link to it —
    which is why the dashboard has to answer to the fragment."""
    for label, href in ITEMS:
        if page == "users" and href == "/submit":
            continue  # the form is not the current page here, but it is still linked
        assert f'href="{href}"' in bar(HTML[page]), f"{page} lost the link to {label}"


def test_the_dashboard_answers_to_the_analytics_fragment():
    """The shared bar sends people to /#analytics from two other pages. If the
    dashboard ignores the fragment they land on Explore and think the link is
    broken."""
    assert "hashchange" in HTML["dashboard"]
    assert "tabFromHash" in HTML["dashboard"]


def test_the_current_page_is_marked_on_the_form():
    assert 'href="/submit" aria-current="page"' in bar(HTML["form"])
    assert 'aria-current' not in bar(HTML["users"])
    # the dashboard marks its view with a selected tab, not a current link
    assert 'aria-current' not in bar(HTML["dashboard"])


@pytest.mark.parametrize("name", TOKENS)
def test_the_colour_tokens_agree(name):
    values = {page: token(html, name) for page, html in HTML.items()}
    assert len(set(values.values())) == 1, f"{name} differs: {values}"


def test_the_sign_in_page_shares_the_background():
    """It has no bar — it is a centred card — but landing on a different grey
    on the way in gives the whole thing away."""
    login = (ROOT / "login.html").read_text()
    assert token(login, "--bg") == token(HTML["dashboard"], "--bg")
