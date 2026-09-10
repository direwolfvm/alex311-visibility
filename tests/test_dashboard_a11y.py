"""Guards for the dashboard's accessibility and responsive markup.

These are static checks on the shipped HTML, not a substitute for a real audit,
but they catch the regressions that actually happened here: a control losing its
label, a generated image losing its alt text, a colour dropping below contrast,
a click handler on something the keyboard cannot reach.
"""
import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "dashboard/static/index.html").read_text()


def contrast(fg: str, bg: str) -> float:
    """WCAG relative-luminance contrast ratio for two #rrggbb colours."""
    def lum(h):
        parts = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        f = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]
    a, b = lum(fg), lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def css_var(name: str) -> str:
    m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", HTML)
    assert m, f"--{name} not found"
    return m.group(1)


# ----------------------------------------------------------------- structure

def test_page_has_landmarks_and_a_skip_link():
    assert "<main" in HTML and 'class="skip-link"' in HTML
    assert 'href="#main"' in HTML and 'id="main"' in HTML


def test_tabs_expose_tab_semantics():
    assert 'role="tablist"' in HTML
    assert HTML.count('role="tab"') == 2
    assert HTML.count('role="tabpanel"') == 2
    assert 'aria-selected' in HTML and "aria-controls" in HTML


def test_filter_controls_are_labelled():
    for control in ("f-start", "f-end", "f-status", "f-q"):
        assert f'for="{control}"' in HTML, f"{control} has no <label for>"


def test_category_checkbox_ids_do_not_come_from_service_names():
    """Service names contain spaces and slashes, so a name-derived id never
    matched its label's `for` and the labels silently did nothing."""
    assert 'id="cat-${i}"' in HTML and 'for="cat-${i}"' in HTML
    assert 'id="c-${c.service_name}"' not in HTML


def test_sortable_headers_are_buttons_with_aria_sort():
    assert HTML.count('aria-sort=') >= 7
    assert re.search(r'th data-sort="case"[^>]*><button', HTML)


def test_table_has_a_caption_and_the_live_region_exists():
    assert "<caption" in HTML
    assert 'role="status"' in HTML and 'aria-live="polite"' in HTML


def test_lightbox_is_a_modal_dialog_with_named_controls():
    assert 'role="dialog"' in HTML and 'aria-modal="true"' in HTML
    for label in ("Close photo viewer", "Previous photo", "Next photo"):
        assert f'aria-label="{label}"' in HTML


def test_every_chart_canvas_is_described():
    ids = re.findall(r'<canvas id="([\w-]+)"([^>]*)>', HTML)
    assert ids, "no charts found"
    for cid, attrs in ids:
        assert 'role="img"' in attrs and "aria-label=" in attrs, f"{cid} undescribed"


# --------------------------------------------------------- generated markup

def test_generated_thumbnails_are_buttons_carrying_alt_text():
    """They open a lightbox, so they must be reachable and activatable by
    keyboard; an <img> with a click handler is neither."""
    assert 'class="thumb-btn"' in HTML
    assert "tabindex=\"0\"\n              role=\"button\"" not in HTML
    # every generated <img ...> in the script carries an alt
    for img in re.findall(r"<img[^>]*?>", HTML.split("<script>")[-1]):
        assert "alt=" in img, f"generated image without alt: {img[:80]}"


def test_row_expander_reports_its_state_and_target():
    assert 'aria-expanded="false"' in HTML
    assert "Show details for" in HTML and "'Hide'" in HTML


# ------------------------------------------------------------------ colour

@pytest.mark.parametrize("var,bg,what", [
    ("muted", "bg", "muted text on the page background"),
    ("muted", "panel", "muted text on a card"),
    ("accent", "panel", "links"),
])
def test_text_colours_meet_wcag_aa(var, bg, what):
    assert contrast(css_var(var), css_var(bg)) >= 4.5, what


@pytest.mark.parametrize("var", ["open", "closed"])
def test_status_badges_meet_wcag_aa_against_white(var):
    """Badge text is small and bold, which does not qualify as large text."""
    assert contrast("#ffffff", css_var(var)) >= 4.5


def test_map_markers_keep_the_brighter_graphic_colours():
    """Markers are graphics, not text, so they are not held to the text ratio
    and stay legible against the basemap."""
    assert "--open-dot: #d97706" in HTML and "--closed-dot: #059669" in HTML


# -------------------------------------------------------------- responsive

def test_has_breakpoints_for_phone_and_tablet():
    widths = set(re.findall(r"@media \(max-width:\s*(\d+)px\)", HTML))
    assert {"640", "900"} <= widths


def test_respects_reduced_motion_and_coarse_pointers():
    assert "prefers-reduced-motion" in HTML
    assert "(pointer: coarse)" in HTML


def test_focus_is_visible():
    assert ":focus-visible" in HTML


# ------------------------------------------------ map sizing (regression)

SUBMIT = (Path(__file__).resolve().parents[1] / "dashboard/submit.html").read_text()


@pytest.mark.parametrize("page,name", [(HTML, "dashboard"), (SUBMIT, "submit form")])
def test_the_map_is_told_to_re_measure(page, name):
    """Leaflet measures its container once. Both pages build the map while its
    container is hidden or a different size, which paints one column of tiles
    and leaves the rest grey until invalidateSize runs."""
    assert "invalidateSize" in page, f"{name} never re-measures its map"


@pytest.mark.parametrize("page,name", [(HTML, "dashboard"), (SUBMIT, "submit form")])
def test_re_measuring_does_not_wait_on_an_animation_frame(page, name):
    """requestAnimationFrame is suspended while the page is not being painted,
    so a map that waits for one stays 0x0 in a background tab. invalidateSize
    forces the layout it needs, so it can be called directly."""
    assert "requestAnimationFrame(() => map.invalidateSize())" not in page
    assert "requestAnimationFrame(remeasureMap)" not in page


# ------------------------------------------------ reaching the prototype

def test_the_dashboard_links_to_the_submission_prototype():
    assert 'href="/submit"' in HTML and "Report an issue" in HTML


def test_that_link_is_a_link_and_not_a_fake_tab():
    """It navigates to another page rather than swapping a panel here. Giving it
    role=tab would promise arrow-key behaviour it cannot honour, and would put a
    third item in a tablist that only has two panels."""
    assert HTML.count('role="tab"') == 2
    assert HTML.count('role="tabpanel"') == 2
    link = re.search(r'<a class="tab-link"[^>]*>', HTML)
    assert link and "role=" not in link.group(0)
