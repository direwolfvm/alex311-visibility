"""Drive the City's Incap311 request wizard.

Everything about how that wizard actually behaves lives here: the shadow-DOM
scan, the six widget kinds, the Validation Alert modals, the service-type
suggestion, the Esri location step, and the tile-by-tile quirks discovered by
walking all 111 services.

**This module cannot submit.** It navigates and fills, and stops at the review
step. Pressing the final button is `alex311.submit_browser`'s job alone, behind
its double gate, so there is exactly one place in the codebase where a real
request can be created.

Playwright is a development dependency and is imported at module scope, so this
module must never be imported by the web service. The dashboard image does not
carry a browser; a real submission runs as a separate job.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

BASE = "https://alex311.alexandriava.gov/customer/s/"
PLACEHOLDER = "Select an option"

#: Set by the caller when it wants screenshots; None disables them.
SHOTS: Path | None = None


SCAN = r"""() => {
  const CTRL = 'input,select,textarea,[role=checkbox],[role=radio],[role=combobox],button[data-que-number]';
  const seen = new Set(), nodes = [], textNodes = [];
  const walk = root => {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let t; while ((t = tw.nextNode())) textNodes.push(t);
    root.querySelectorAll('*').forEach(e => {
      if (!seen.has(e)) { seen.add(e); nodes.push(e); }
      if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  const vis = e => !!(e.offsetParent || getComputedStyle(e).position === 'fixed');
  const up = n => n.parentElement || (n.getRootNode && n.getRootNode().host) || null;
  const top = e => { let n = e; while (n) { const r = n.getBoundingClientRect(); if (r.height > 0) return r.top + window.scrollY; n = up(n); } return -1; };
  const HRE = /^QUESTION\s+(\d+)\s*\*?\s*$/i;
  const byK = new Map();
  for (const tn of textNodes) {                       // headers are often bare text nodes
    const m = (tn.textContent || '').trim().match(HRE);
    if (!m) continue;
    const rng = document.createRange(); rng.selectNodeContents(tn);
    const rect = rng.getBoundingClientRect(); if (rect.height === 0) continue;
    const k = +m[1]; if (byK.has(k)) continue;
    const parent = tn.parentElement;
    const reqRe = new RegExp('QUESTION\\s+' + k + '\\s*\\*', 'i');
    let required = /\*/.test(tn.textContent);
    let text = '', p = parent;
    for (let i = 0; i < 5 && p; i++) {
      const it = p.innerText || '';
      if (!required && reqRe.test(it)) required = true;
      if (!text) { const lines = it.split('\n').map(s => s.trim()).filter(Boolean);
        const hi = lines.findIndex(l => /^QUESTION\s+\d+/i.test(l)); if (hi >= 0 && lines[hi + 1]) text = lines[hi + 1]; }
      p = up(p);
    }
    byK.set(k, { k, y: rect.top + window.scrollY, required, text: text.slice(0, 200), controls: [], next: null });
  }
  const heads = [...byK.values()].sort((a, b) => a.y - b.y);
  const assign = y => { let best = null; for (const h of heads) if (h.y <= y + 2) best = h; return best; };
  nodes.filter(e => e.matches(CTRL)).forEach(e => {
    const tag = e.tagName.toLowerCase(), role = e.getAttribute('role') || '', type = (e.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'file', 'button'].includes(type)) return;
    const y = top(e), h = assign(y); if (!h) return;
    if (tag === 'button') { h.next = { disabled: !!e.disabled }; return; }
    const c = { tag, role, type, vis: vis(e), name: e.name || '', val: (e.value || '').slice(0, 60),
      checked: e.checked === true || e.getAttribute('aria-checked') === 'true', ph: e.placeholder || '',
      aria: (e.getAttribute('aria-label') || '').slice(0, 160), text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80) };
    if (tag === 'select') c.opts = [...e.options].map(o => o.text.trim());
    if (/search/i.test(c.ph + ' ' + c.aria)) return;
    if (['First Name', 'Last Name', 'Email', 'Phone Number', 'Remember'].includes(c.name)) return;
    if (c.aria === 'Additional Information' || c.ph === 'Additional Information') return;
    h.controls.push(c);
  });
  // custom dropdown triggers (not native <select>): innermost visible element whose text is the placeholder
  const DD = /^Select (all applicable|an option)$/i;
  let trig = nodes.filter(e => vis(e) && !e.shadowRoot && !['OPTION', 'SELECT'].includes(e.tagName) && DD.test((e.innerText || '').trim()));
  trig.sort((a, b) => top(a) - top(b)).forEach((e, i) => {
    const h = assign(top(e)); if (!h) return;
    h.controls.push({ tag: e.tagName.toLowerCase(), role: 'dropdown', type: '', vis: true, name: '', val: '', checked: false,
      ph: '', aria: '', text: (e.innerText || '').trim(), idx: i });
  });
  return heads;
}"""


def _css(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


# The Validation Alert body lives in its own node. Read it separately and with a
# generous cap: the general scrape below is capped at 240 chars to keep whole
# wizard panels out, and several City messages are longer than that (the leaf
# collection one is 244), which silently produced rules with no explanation.


ALERT_BODY = ".pop-up-message"
ALERT_FALLBACK = ".validation-popup, .verify-popup, [class*=validation-popup]"


async def _texts(pg, sel, cap):
    try:
        return await pg.locator(sel).evaluate_all(
            "(els, cap) => els.filter(e => e.offsetParent !== null)"
            ".map(e => e.innerText.replace(/\\s+/g,' ').trim().replace(/\\s*OK$/, ''))"
            ".filter(t => t && t.length > 2 && t.length < cap)", cap)
    except Exception:
        return []


async def msgs(pg):
    body = await _texts(pg, ALERT_BODY, 900)
    if not body:
        body = await _texts(pg, ALERT_FALLBACK, 900)
    general = await _texts(pg, "[class*=message], [role=alert], .slds-form-element__help, [class*=error], "
                               "[class*=alert], [class*=modal] p, [class*=modal] h2", 240)
    # drop general entries already covered by the (longer, cleaner) alert body
    out = list(body) + [g for g in general if not any(g in b for b in body)]
    return list(dict.fromkeys(out))


async def settle(pg):
    try:
        await pg.locator(".ui-overlay").first.wait_for(state="hidden", timeout=6000)
    except Exception:
        pass


async def enabled(pg, name):
    loc = pg.get_by_role("button", name=name, exact=True)
    for i in range(await loc.count()):
        b = loc.nth(i)
        if await b.is_visible() and await b.is_enabled():
            return True
    return False


async def press(pg, name, last=False):
    assert "submit" not in name.lower(), "never submit"
    loc = pg.get_by_role("button", name=name, exact=True)
    n = await loc.count()
    for i in (range(n - 1, -1, -1) if last else range(n)):
        b = loc.nth(i)
        if await b.is_visible() and await b.is_enabled():
            await settle(pg)
            await b.click(force=True)
            await pg.wait_for_timeout(1800)
            return True
    return False


async def scan(pg):
    return await pg.evaluate(SCAN)


async def keep_current_visible(pg):
    """The 'New Service Type Suggestion' modal's dismiss control is an <a>, not a button."""
    try:
        loc = pg.get_by_text("Keep Current", exact=True).first
        return bool(await loc.count()) and await loc.is_visible()
    except Exception:
        return False


async def dismiss_alerts(pg):
    """Press OK until no Validation Alert remains (a lingering modal makes the
    next probe look like a hard stop)."""
    for _ in range(6):
        if await enabled(pg, "OK"):
            await press(pg, "OK")
        elif await keep_current_visible(pg):             # "New Service Type Suggestion" modal
            await pg.get_by_text("Keep Current", exact=True).first.click(force=True)
        else:
            return
        await settle(pg)
        await pg.wait_for_timeout(700)


def classify(h):
    """Widget kind + options + code for one question header's controls."""
    cs = h["controls"]
    radios = [c for c in cs if c["tag"] == "input" and c["type"] == "radio" and c["vis"]]
    if radios:
        return {"kind": "radio", "code": radios[0]["name"], "options": list(dict.fromkeys(r["val"] for r in radios))}
    sels = [c for c in cs if c["tag"] == "select" and c["vis"] and PLACEHOLDER in (c.get("opts") or [])]
    if sels:
        s = sels[0]
        return {"kind": "select", "code": _code_from_aria(s["aria"]), "aria": s["aria"],
                "options": [o for o in s["opts"] if o != PLACEHOLDER]}
    dd = [c for c in cs if c["role"] == "dropdown"]
    if dd:
        return {"kind": "dropdown", "code": "", "options": [], "trigger": dd[0]["text"], "idx": dd[0]["idx"]}
    rchecks = [c for c in cs if c["role"] == "checkbox" and c["vis"]]
    if rchecks:
        return {"kind": "checkbox", "code": "", "options": [c["text"] or c["aria"] for c in rchecks]}
    rradios = [c for c in cs if c["role"] == "radio" and c["vis"]]
    if rradios:
        return {"kind": "radio-role", "code": "", "options": [c["text"] or c["aria"] for c in rradios]}
    texts = [c for c in cs if c["vis"] and (c["tag"] == "textarea" or (c["tag"] == "input" and c["type"] in ("text", "date", "number", "email", "tel", "")) or c["tag"] == "select")]
    if texts:
        kind = "composite" if len(texts) > 1 else "text"
        aria = next((c["aria"] for c in texts if c["aria"]), "")
        return {"kind": kind, "code": _code_from_aria(aria), "aria": aria, "options": [], "parts": [(c["tag"], c["type"], c["ph"], c["aria"], c.get("opts")) for c in texts]}
    return {"kind": "unknown", "code": "", "options": []}


def _code_from_aria(aria):
    return ""  # the wizard exposes attribute codes only on radios; text/select questions match by text at report time


def answered(h, meta):
    cs = h["controls"]
    if meta["kind"] == "radio":
        return any(c["checked"] for c in cs if c["tag"] == "input" and c["type"] == "radio")
    if meta["kind"] == "select":
        return any(c["tag"] == "select" and c["val"] and c["val"] != PLACEHOLDER and PLACEHOLDER in (c.get("opts") or []) for c in cs)
    if meta["kind"] in ("checkbox", "radio-role"):
        return any(c["checked"] for c in cs if c["role"] in ("checkbox", "radio"))
    if meta["kind"] == "dropdown":
        return not any(c["role"] == "dropdown" for c in cs)
    if meta["kind"] in ("text", "composite"):
        vals = [c["val"] for c in cs if c["vis"] and c["tag"] in ("input", "textarea")]
        return bool(vals) and all(vals)
    return True


async def choose(pg, meta, opt):
    """Select one option of a list question. Returns nothing; verify via rescan."""
    k = meta["kind"]
    if k == "radio":
        await pg.locator(f'input[type=radio][name="{_css(meta["code"])}"][value="{_css(opt)}"]').first.click(force=True)
    elif k == "select":
        loc = pg.locator(f'select[aria-label="{_css(meta["aria"])}"]').first if meta.get("aria") else \
            pg.locator("select", has=pg.locator(f'option:text-is("{_css(opt)}")')).first
        await loc.select_option(label=opt)
    elif k == "checkbox":
        await pg.locator("[role=checkbox]", has_text=opt).first.click(force=True)
    elif k == "radio-role":
        await pg.locator("[role=radio]", has_text=opt).first.click(force=True)
    elif k == "dropdown":
        await choose_dropdown(pg, meta, opt)
    await pg.wait_for_timeout(900)


def is_selected(h, meta, opt):
    cs = h["controls"]
    if meta["kind"] == "radio":
        return any(c["checked"] and c["val"] == opt for c in cs if c["tag"] == "input" and c["type"] == "radio")
    if meta["kind"] == "select":
        return any(c["tag"] == "select" and (c["val"] == opt or c["text"].startswith(opt)) for c in cs) or \
            any(c["tag"] == "select" and c["val"] not in ("", PLACEHOLDER) for c in cs)
    if meta["kind"] == "dropdown":
        return not any(c["role"] == "dropdown" for c in cs)     # placeholder text replaced by the selection
    return any(c["checked"] and (c["text"] == opt or c["aria"] == opt) for c in cs if c["role"] in ("checkbox", "radio"))


def _value_for(question: str, ph: str, aria: str, typ: str) -> str:
    """A plausible value for a free-text field, chosen from its placeholder,
    accessible label and question text so format validation accepts it."""
    hay = f"{question} {ph} {aria}".lower()
    if typ == "email" or "email" in hay:
        return "dryrun@example.invalid"
    if typ == "tel" or "phone" in hay:
        return "7035551212"
    if "zip" in hay:
        return "22314"
    if typ == "date" or "mm/dd/yyyy" in hay:
        return "2026-09-01" if typ == "date" else "09/01/2026"
    if ph.strip() == "$" or "income" in hay or "amount" in hay or "cost" in hay or "price" in hay:
        return "50000"
    if typ == "number" or "numeric" in hay or "how many" in hay or "number of" in hay or "count" in hay:
        return "2"
    if "year" in hay and "yearly" not in hay:
        return "2026"
    return "Research dry run - not a real request. Please disregard."


DD_TRIGGER = ".Chooes_option_box"                      # Incap311 custom picklist trigger (text = placeholder or selection)


DD_ITEMS = "[role=checkbox][data-id], [role=option][data-id], [role=radio][data-id], [role=option]"


def _trigger(pg, meta):
    loc = pg.locator(DD_TRIGGER)
    return loc.nth(meta.get("idx", 0))


async def _dd_open(pg, meta):
    t = _trigger(pg, meta)
    if not await t.count():
        t = pg.get_by_text(meta["trigger"], exact=True).first
    await t.click(force=True)
    await pg.wait_for_timeout(800)
    return t


async def _dd_close(pg, t):
    """Close an open picklist. NEVER press Escape here: it closes the whole wizard modal."""
    items = pg.locator(DD_ITEMS).locator("visible=true")
    for _ in range(2):
        if not await items.count():
            return
        await t.click(force=True)                        # the trigger toggles the list
        await pg.wait_for_timeout(500)
    if await items.count():
        hdr = pg.get_by_text("Details", exact=True).first  # neutral click target: the modal title
        if await hdr.count():
            await hdr.click(force=True)
            await pg.wait_for_timeout(500)


async def dropdown_options(pg, meta):
    """Open a custom (non-native) picklist, read its options, close it."""
    t = await _dd_open(pg, meta)
    items = pg.locator(DD_ITEMS).locator("visible=true")
    opts = await items.evaluate_all("els => els.map(e => e.dataset.id || e.innerText.trim())")
    await _dd_close(pg, t)
    return [o for o in dict.fromkeys(opts) if o and len(o) < 90]


async def choose_dropdown(pg, meta, opt):
    """Toggle one option of a custom picklist (multi-selects toggle, so calling twice unticks)."""
    t = await _dd_open(pg, meta)
    item = pg.locator(f'[data-id="{_css(opt)}"]').locator("visible=true").first
    if not await item.count():
        item = pg.locator(DD_ITEMS).locator("visible=true").filter(has_text=opt).first
    await item.click(force=True)
    await pg.wait_for_timeout(500)
    await _dd_close(pg, t)


async def dropdown_checked(pg, opt):
    """aria-checked of a custom picklist option (readable while the list is closed)."""
    try:
        loc = pg.locator(f'[role=checkbox][data-id="{_css(opt)}"], [role=radio][data-id="{_css(opt)}"]').first
        if await loc.count():
            return (await loc.get_attribute("aria-checked")) == "true"
    except Exception:
        pass
    return False


async def suggestion_text(pg):
    """Text of the 'New Service Type Suggestion' modal when it is showing."""
    try:
        loc = pg.get_by_text("New Service Type Suggestion").first
        if await loc.count():
            t = await loc.evaluate("e => { let p = e; for (let i = 0; i < 6 && p.parentElement; i++) p = p.parentElement; return p.innerText; }")
            return " ".join(t.split())[:500]
    except Exception:
        pass
    return ""


def _as_mdY(value: str) -> str:
    """The wizard's date box is a text field wanting MM/DD/YYYY, not an
    <input type=date>, so an ISO date typed straight in is silently ignored."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value.strip())
    return f"{m.group(2)}/{m.group(3)}/{m.group(1)}" if m else value.strip()


def _as_12h(value: str) -> tuple[str, str | None]:
    """24-hour time -> (12-hour clock, AM/PM) for the wizard's split fields."""
    m = re.match(r"^(\d{1,2}):(\d{2})", value.strip())
    if not m:
        return value.strip(), None
    hour, minute = int(m.group(1)), m.group(2)
    if hour > 12 or (hour == 12 and "pm" not in value.lower()):
        meridiem = "PM" if hour >= 12 else "AM"
    else:
        meridiem = "PM" if "pm" in value.lower() else "AM"
    twelve = hour % 12 or 12
    return f"{twelve:02d}:{minute}", meridiem


async def fill_free(pg, meta, question="", values=None):
    """Fill text / date / time / AM-PM parts of a non-list question.

    `values` supplies what to type: a string for a single field, or a mapping
    with `date` / `time` keys for the date-and-time composites. Without it the
    driver invents something shaped like the field, which is right for a probe
    walking every service and wrong for anything filed in someone's name.
    """
    supplied = values if isinstance(values, dict) else {}
    single = values if isinstance(values, str) else None
    for tag, typ, ph, aria, opts in meta.get("parts", []):
        try:
            if tag == "select":
                loc = pg.locator(f'select[aria-label="{_css(aria)}"]').first if aria else pg.locator("select", has=pg.locator('option:text-is("AM")')).first
                _, meridiem = _as_12h(supplied["time"]) if supplied.get("time") else (None, None)
                if meridiem and opts and meridiem in opts:
                    await loc.select_option(label=meridiem)   # AM/PM must follow the hour
                else:
                    await loc.select_option(index=1 if opts and opts[0] == PLACEHOLDER else 0)
                continue
            # the scan truncates labels, so match by prefix; long labels otherwise never match exactly
            loc = pg.locator(f'[aria-label^="{_css(aria[:120])}"]').first if aria else pg.locator(f'[placeholder="{_css(ph)}"]').first
            if not await loc.count() and ph:
                loc = pg.locator(f'[placeholder="{_css(ph)}"]').first
            is_date = typ == "date" or "MM/DD/YYYY" in ph.upper() or "/" in ph
            is_time = typ == "time" or (":" in ph and "MM" not in ph.upper())
            if is_date and supplied.get("date"):
                val = _as_mdY(supplied["date"])
            elif is_time and supplied.get("time"):
                val = _as_12h(supplied["time"])[0]
            elif single is not None and not (is_date or is_time):
                val = single
            else:
                val = "10:00" if is_time else _value_for(question, ph, aria, typ)
            await loc.fill(val, timeout=5000)
            try:
                await loc.dispatch_event("change")
            except Exception:
                pass
        except Exception:
            pass
        await pg.wait_for_timeout(500)


#: Each suggestion the location search offers is one of these, including the
#: browser-geolocation row, which is the only one that is not an address.
LOC_ROW = ".loc-set"
CURRENT_LOCATION = "current location"


class LocationRejected(Exception):
    """The City's gazetteer does not recognise the address as serviceable."""


async def location_suggestions(pg, address: str) -> list[str]:
    """Type an address into the location search and read back what it offers."""
    box = pg.locator('input[placeholder="Search"]').first
    if not await box.count():
        return []
    await box.click()
    await box.fill("")
    await box.type(address, delay=60)
    await pg.wait_for_timeout(2500)
    rows = pg.locator(LOC_ROW).locator("visible=true")
    try:
        texts = await rows.evaluate_all(
            "els => els.map(e => (e.innerText || '').trim())")
    except Exception:
        return []
    return [t for t in texts if t and t.lower() != CURRENT_LOCATION]


#: The City answers an unknown address with a row in the suggestion list rather
#: than an alert, so it arrives looking exactly like a choice.
NOT_SERVICEABLE = re.compile(r"not a valid service address|spelled correctly", re.I)


async def set_location(pg, address: str, *, choose_index: int = 0) -> str | None:
    """Place the wizard's pin on a specific address. Returns what was chosen.

    The location step has a search box wired to the City's own gazetteer. It
    sits in the modal above the map rather than inside the Esri iframe, and its
    rows live in a shadow root, so this goes through Playwright locators rather
    than querySelector.

    Without this a run can only click an arbitrary point on the map, which is
    fine for a probe and useless for a submission: the request would be filed
    against whatever happened to sit under the cursor.
    """
    picks = await location_suggestions(pg, address)
    if picks and NOT_SERVICEABLE.search(picks[0]):
        raise LocationRejected(picks[0])
    if not picks or choose_index >= len(picks):
        return None
    rows = pg.locator(LOC_ROW).locator("visible=true")
    n = await rows.count()
    for i in range(n):
        row = rows.nth(i)
        text = (await row.inner_text()).strip()
        if text.lower() == CURRENT_LOCATION or text != picks[choose_index]:
            continue
        await row.click(force=True)
        await pg.wait_for_timeout(3000)
        return text
    return None


async def open_category(pg, name, code, groups, address: str | None = None):
    await pg.goto("about:blank")                    # a same-URL goto does not reset the SPA
    await pg.goto(BASE, wait_until="networkidle", timeout=60000)
    await pg.wait_for_timeout(2500)
    tile = pg.locator(f'[data-service-code="{code}"]').first
    if not await tile.count():
        # Not a Suggested tile: open its category group. 31 services belong to
        # no group in the catalog; the home page lists those under "other".
        for g in list(groups) + ["other"]:
            gb = pg.get_by_role("button", name=g, exact=True).first
            if await gb.count():
                await gb.click(force=True)
                tile = pg.locator(f'[data-service-code="{code}"]').first
                try:
                    await tile.wait_for(state="attached", timeout=8000)   # group panels render lazily
                    break
                except Exception:
                    continue
    if not await tile.count():                       # last resort: the search box surfaces service chips
        box = pg.get_by_placeholder("Search Service Requests").first
        if await box.count():
            await box.fill(name)
            await pg.wait_for_timeout(2500)
            tile = pg.locator(f'[data-service-code="{code}"]').first
    if not await tile.count():
        raise RuntimeError("category tile not found on home, in its groups, or via search")
    await tile.scroll_into_view_if_needed()
    await tile.click(force=True)
    await pg.wait_for_timeout(2500)
    if not await press(pg, "Request This Service"):
        raise RuntimeError("no 'Request This Service' button (view-only service?)")
    await pg.wait_for_timeout(3000)
    await press(pg, "Continue")                                    # step 1 -> 2
    fr = pg.locator("iframe.map-loc-mobile").first
    try:
        await fr.wait_for(state="visible", timeout=8000)
        has_map = True
    except Exception:
        has_map = False                                            # some services have no Location step
    if has_map and address:
        # a real request needs the resident's location, not a point the probe
        # happened to click; LocationRejected propagates to the caller
        picked = await set_location(pg, address)
        if picked is None:
            raise RuntimeError(f"the location search offered nothing for {address!r}")
        if not await press(pg, "Continue"):
            raise RuntimeError("location chosen but Continue stayed disabled")
    elif has_map:
        for fx, fy in ((0.55, 0.55), (0.45, 0.48), (0.6, 0.4)):
            box = await fr.bounding_box()
            if not box:
                break
            await pg.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
            await pg.wait_for_timeout(3200)
            if await enabled(pg, "Continue"):
                break
        if not await press(pg, "Continue"):                        # step 2 -> 3
            raise RuntimeError("location gate did not enable Continue")
    await pg.wait_for_timeout(2200)
