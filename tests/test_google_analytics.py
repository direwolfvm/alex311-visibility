"""Google Analytics is on every page of the website, and the privacy page says so.

The tag counts visits. What is pinned: every page carries it with the same
measurement id; the privacy page no longer promises there is no analytics
code and instead says what is measured, what is not sent, and that the app
is not included.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "dashboard"
PAGES = sorted(list((ROOT / "static").glob("*.html")) + list(ROOT.glob("*.html")))
ID = "G-2QR8498BRE"


def test_every_page_carries_the_same_tag():
    assert len(PAGES) >= 11
    for p in PAGES:
        html = p.read_text()
        assert f'src="https://www.googletagmanager.com/gtag/js?id={ID}"' in html, p.name
        assert f"gtag('config', '{ID}')" in html, p.name
        assert html.index("googletagmanager") < html.index("</head>"), p.name      # in the head, as Google asks
        assert html.count("googletagmanager.com/gtag/js") == 1, p.name


def test_the_privacy_page_tells_the_truth_about_it():
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (ROOT / "static/privacy.html").read_text()))
    assert "no cookies are set" not in text and "no analytics or advertising code" not in text
    assert "Google Analytics" in text and "_ga" in text
    assert "does not send Google anything you type, your account id, or the contents of a report" in text
    assert "The iPhone app does not include Google Analytics" in text
    assert "No advertising. No tracking across apps or websites." in text
