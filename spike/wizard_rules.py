"""Rule-discovery spike: what does each answer do in the Alex311 wizard?

For the top-N categories by volume, walks the guest wizard to step 3 and, for
every list question along the path, selects EACH option in place and records:
  - alert text that appears (Validation Alert modals / banners),
  - whether the answer is a hard stop (rejected after OK) or advisory,
  - which question(s) it reveals next (first-order branching signal).
Also records the rendered question sequence, widget kind, and option lists —
including options the mined data never contains.

How it sees the page: Incap311 is LWC/shadow DOM, and step 3 mixes widget
kinds (native radios, native <select>, div[role=checkbox], date+time+AM/PM
composites, free text). So it deep-walks every shadow root, finds the
"QUESTION n *" headers, and attributes every control to the nearest header
above it by screen position. Actions go through Playwright locators (which
pierce shadow roots) with per-kind select + verify logic.

Read-only and polite: one browser, sequential, pauses, in-place probing
instead of restarting per option, and it NEVER presses "Submit Request".
Results save incrementally to docs/data/wizard-rules.json; re-running resumes.

Usage: uv run python spike/wizard_rules.py [top_n=15] [screenshot_dir]
"""
import asyncio, json, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "https://alex311.alexandriava.gov/customer/s/"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/data/wizard-rules.json"
SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
PLACEHOLDER = "Select an option"

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
  return heads;
}"""


def load_targets(n):
    rows = json.load(open(ROOT / "docs/data/question-schema.mined.json"))
    cat = json.load(open(ROOT / "docs/data/service-catalog.json"))
    by_name = {t["service_name"].lower(): t for t in cat}
    tot = {}
    for r in rows:
        tot[r["service_name"]] = r["total"]
    ranked = sorted(tot, key=lambda k: -tot[k])
    targets, skipped = [], []
    for nm in ranked:
        t = by_name.get(nm.lower())
        if t:
            groups = [c["name"] for c in (t.get("definitions") or {}).get("service_categories", [])]
            targets.append((nm, t["service_code"], groups))
        elif len(skipped) < 5:
            skipped.append(nm)
        if len(targets) == n:
            break
    return targets, skipped


def _css(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')


async def msgs(pg):
    try:
        got = await pg.locator("[class*=message], [role=alert], .slds-form-element__help, [class*=error], [class*=alert], [class*=modal] p, [class*=modal] h2").evaluate_all(
            "els => els.filter(e => e.offsetParent !== null).map(e => e.innerText.replace(/\\s+/g,' ').trim()).filter(t => t && t.length > 2 && t.length < 240)")
        return list(dict.fromkeys(got))
    except Exception:
        return []


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


async def dismiss_alerts(pg):
    """Press OK until no Validation Alert remains (a lingering modal makes the
    next probe look like a hard stop)."""
    for _ in range(5):
        if not await enabled(pg, "OK"):
            return
        await press(pg, "OK")
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
    await pg.wait_for_timeout(900)


def is_selected(h, meta, opt):
    cs = h["controls"]
    if meta["kind"] == "radio":
        return any(c["checked"] and c["val"] == opt for c in cs if c["tag"] == "input" and c["type"] == "radio")
    if meta["kind"] == "select":
        return any(c["tag"] == "select" and (c["val"] == opt or c["text"].startswith(opt)) for c in cs) or \
            any(c["tag"] == "select" and c["val"] not in ("", PLACEHOLDER) for c in cs)
    return any(c["checked"] and (c["text"] == opt or c["aria"] == opt) for c in cs if c["role"] in ("checkbox", "radio"))


async def fill_free(pg, meta):
    """Fill text / date / time / AM-PM parts of a non-list question."""
    for tag, typ, ph, aria, opts in meta.get("parts", []):
        try:
            if tag == "select":
                loc = pg.locator(f'select[aria-label="{_css(aria)}"]').first if aria else pg.locator("select", has=pg.locator('option:text-is("AM")')).first
                await loc.select_option(index=1 if opts and opts[0] == PLACEHOLDER else 0)
                continue
            loc = pg.locator(f'[aria-label="{_css(aria)}"]').first if aria else pg.locator(f'[placeholder="{_css(ph)}"]').first
            if "MM/DD/YYYY" in ph: val = "09/01/2026"
            elif ":" in ph: val = "10:00"
            elif typ == "date": val = "2026-09-01"
            elif typ == "number": val = "1"
            elif typ == "email": val = "dryrun@example.invalid"
            elif typ == "tel": val = "7035551212"
            else: val = "research dry run - not a real request"
            await loc.fill(val)
        except Exception:
            pass
        await pg.wait_for_timeout(500)


async def open_category(pg, name, code, groups):
    await pg.goto("about:blank")                    # a same-URL goto does not reset the SPA
    await pg.goto(BASE, wait_until="networkidle", timeout=60000)
    await pg.wait_for_timeout(2500)
    tile = pg.locator(f'[data-service-code="{code}"]').first
    if not await tile.count():
        for g in groups:  # not a Suggested tile: open its category group first
            gb = pg.get_by_role("button", name=g, exact=True).first
            if await gb.count():
                await gb.click(force=True)
                await pg.wait_for_timeout(2500)
                tile = pg.locator(f'[data-service-code="{code}"]').first
                if await tile.count():
                    break
    if not await tile.count():
        raise RuntimeError("category tile not found on home or in its groups")
    await tile.scroll_into_view_if_needed()
    await tile.click(force=True)
    await pg.wait_for_timeout(2500)
    if not await press(pg, "Request This Service"):
        raise RuntimeError("no 'Request This Service' button (view-only service?)")
    await pg.wait_for_timeout(3000)
    await press(pg, "Continue")                                    # step 1 -> 2
    fr = pg.locator("iframe.map-loc-mobile").first
    for fx, fy in ((0.55, 0.55), (0.45, 0.48), (0.6, 0.4)):
        box = await fr.bounding_box()
        if not box:
            raise RuntimeError("no map iframe on location step")
        await pg.mouse.click(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
        await pg.wait_for_timeout(3200)
        if await enabled(pg, "Continue"):
            break
    if not await press(pg, "Continue"):                            # step 2 -> 3
        raise RuntimeError("location gate did not enable Continue")
    await pg.wait_for_timeout(2200)


async def probe_category(browser, name, code, groups):
    rec = {"service_name": name, "service_code": code, "questions": [], "error": None}

    async def fresh():
        return await browser.new_page(viewport={"width": 1100, "height": 1600})

    pg = await fresh()
    try:
        await open_category(pg, name, code, groups)
        rec["banners"] = await msgs(pg)
        try:
            pg = await _walk(pg, fresh, rec, name, code, groups)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"   # questions gathered so far are kept
        rec.setdefault("continue_enabled_at_end", None)
    finally:
        try:
            await pg.close()
        except Exception:
            pass
    return rec


async def _walk(pg, fresh, rec, name, code, groups):
    """Probe questions in order; returns the page in use at the end."""
    done_ks, path, restarts = set(), [], 0
    for _ in range(18):
        heads = await scan(pg)
        pend = [h for h in heads if h["k"] not in done_ks and not answered(h, classify(h))]
        if not pend:
            cont = await enabled(pg, "Continue")
            last_had_hard = bool(rec["questions"]) and any(
                p.get("hard_stop") for p in rec["questions"][-1]["probes"].values())
            if not cont and last_had_hard and restarts < 3:
                # Some hard stops alter the wizard flow so the next question never
                # reveals. Rebuild from a FRESH page (the abandoned wizard's stored
                # state otherwise hides "Request This Service") and replay the path.
                restarts += 1
                try:
                    old, pg = pg, await fresh()
                    await old.close()
                    await open_category(pg, name, code, groups)
                    for kind, m, ch in path:
                        if kind == "list":
                            await choose(pg, m, ch)
                            await settle(pg)
                            await dismiss_alerts(pg)
                        else:
                            await fill_free(pg, m)
                        await press(pg, "Next", last=True)
                except Exception as e:
                    rec["note"] = f"restart {restarts} failed: {type(e).__name__}: {str(e)[:120]}"
                    rec["continue_enabled_at_end"] = False
                    break
                continue
            rec["continue_enabled_at_end"] = cont
            break
        h = pend[0]
        meta = classify(h)
        done_ks.add(h["k"])
        entry = {"order": h["k"], "question": h["text"], "required": h["required"], "input": meta["kind"],
                 "code": meta.get("code") or "", "options": meta["options"], "probes": {}, "chosen": None}
        if meta["kind"] in ("radio", "select", "checkbox", "radio-role") and meta["options"]:
            base_msgs = set(await msgs(pg))
            base_ks = {x["k"] for x in heads}
            for opt in meta["options"]:
                try:
                    await dismiss_alerts(pg)                 # leftovers from the previous probe
                    base_msgs = set(await msgs(pg))          # re-baseline so old alert text isn't "new"
                    await choose(pg, meta, opt)
                    await settle(pg)
                    await pg.wait_for_timeout(900)
                    new_msgs = [m for m in await msgs(pg) if m not in base_msgs]
                    had_ok = await enabled(pg, "OK")
                    if had_ok:
                        await dismiss_alerts(pg)
                    after = await scan(pg)
                    h2 = next((x for x in after if x["k"] == h["k"]), None)
                    still = bool(h2 and is_selected(h2, meta, opt))
                    entry["probes"][opt] = {"alert": new_msgs, "hard_stop": bool(had_ok and not still),
                                            "advisory": bool(had_ok and still),
                                            "reveals": sorted({x["k"] for x in after} - base_ks),
                                            "extra_controls": max(0, len(h2["controls"]) - len(h["controls"])) if h2 else 0}
                    if meta["kind"] == "checkbox" and still:
                        await choose(pg, meta, opt)          # toggle back off to isolate probes
                        await settle(pg)
                except Exception as e:
                    entry["probes"][opt] = {"error": f"{type(e).__name__}: {str(e)[:140]}"}
            chosen = next((o for o in meta["options"] if not entry["probes"].get(o, {}).get("hard_stop")
                           and "error" not in entry["probes"].get(o, {})), None)
            if chosen:
                # after a hard-stop probe the widget can drop the next click; verify and retry
                for _attempt in range(3):
                    try:
                        await choose(pg, meta, chosen)
                        await settle(pg)
                        if await enabled(pg, "OK"):
                            await press(pg, "OK")
                            await settle(pg)
                        h3 = next((x for x in await scan(pg) if x["k"] == h["k"]), None)
                        if h3 and is_selected(h3, meta, chosen):
                            break
                    except Exception:
                        pass
                    await pg.wait_for_timeout(900)
            entry["chosen"] = chosen
            if chosen:
                path.append(("list", meta, chosen))
        else:
            await fill_free(pg, meta)
            entry["chosen"] = "filled"
            path.append(("free", meta, None))
            entry["parts"] = [{"tag": t, "type": ty, "placeholder": ph, "aria": a} for t, ty, ph, a, _ in meta.get("parts", [])]
        await press(pg, "Next", last=True)
        rec["questions"].append(entry)
        if SHOTS:
            try:
                await pg.screenshot(path=str(SHOTS / f"rules_{code}_q{h['k']}.png"), full_page=True)
            except Exception:
                pass
    rec.setdefault("continue_enabled_at_end", await enabled(pg, "Continue"))
    rec["restarts"] = restarts
    return pg


def summarize(rec):
    qs = rec["questions"]
    hard = sum(1 for e in qs for p in e["probes"].values() if p.get("hard_stop"))
    adv = sum(1 for e in qs for p in e["probes"].values() if p.get("advisory"))
    branch = any(len({tuple(p.get("reveals", [])) for p in e["probes"].values() if "reveals" in p}) > 1 for e in qs)
    kinds = ",".join(e["input"] for e in qs)
    return f"{len(qs)} q [{kinds}] · {hard} hard-stop · {adv} advisory · branching={'yes' if branch else 'no'} · continue={rec.get('continue_enabled_at_end')}"


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    targets, skipped = load_targets(n)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "read_only": True,
           "skipped_not_in_catalog": skipped, "categories": []}
    if OUT.exists():  # resume: keep categories an earlier run finished cleanly
        try:
            done = {c["service_code"]: c for c in json.load(open(OUT))["categories"]
                    if not c.get("error") and c.get("questions")}
        except Exception:
            done = {}
        out["categories"] = [done[c] for _, c, _ in targets if c in done]
        targets = [t for t in targets if t[1] not in done]
        if done:
            print(f"resuming: {len(done)} done, {len(targets)} to go", flush=True)
    print(f"probing {len(targets)} categories; skipped (not in catalog): {skipped}", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for i, (name, code, groups) in enumerate(targets, 1):
            t0 = time.time()
            try:
                rec = await asyncio.wait_for(probe_category(browser, name, code, groups), timeout=600)
                extra = (f" · NOTE {rec['note']}" if rec.get("note") else "") + (f" · ERROR {rec['error']}" if rec.get("error") else "")
                print(f"[{i}/{len(targets)}] {name} ({code}): {summarize(rec)}{extra}  [{time.time()-t0:.0f}s]", flush=True)
            except Exception as e:
                rec = {"service_name": name, "service_code": code, "questions": [],
                       "error": f"{type(e).__name__}: {str(e)[:200]}"}
                print(f"[{i}/{len(targets)}] {name} ({code}): ERROR {rec['error']}", flush=True)
            out["categories"].append(rec)
            OUT.write_text(json.dumps(out, indent=1))
            await asyncio.sleep(2.5)
        await browser.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
