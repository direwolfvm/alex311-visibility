"""Read-only walk of the Alex311 guest submission wizard (research tooling).

Drives Home -> category tile -> "Request This Service" -> File Upload -> Esri
map (click inside iframe.map-loc-mobile) -> answers every progressively
revealed step-3 question -> contact -> review, dumping fields, buttons, and
validation messages at each stage. It NEVER presses "Submit Request".

Incap311 is LWC/shadow-DOM: query through Playwright locators (which pierce
shadow roots), never document.querySelectorAll. A transient .ui-overlay
intercepts clicks after each answer; wait for it to hide, then click(force).

Usage: uv run python spike/wizard_walk.py <output-dir> "<Category Name>"
"""
import asyncio, sys
from playwright.async_api import async_playwright
BASE = "https://alex311.alexandriava.gov/customer/s/"
S = sys.argv[1]; CAT = sys.argv[2]
VIS = "els => els.filter(e => e.offsetParent !== null)"
async def q(pg, sel, fn):
    try: return await pg.locator(sel).evaluate_all(f"els => ({VIS})(els).{fn}")
    except Exception: return []
async def msgs(pg):
    return list(dict.fromkeys(await q(pg, "[class*=message], [role=alert], .slds-form-element__help, [class*=error], [class*=alert]",
        "map(e=>e.innerText.replace(/\\s+/g,' ').trim()).filter(t=>t&&t.length>2&&t.length<200)")))
async def dump(pg, tag):
    btns = await q(pg, "button", "map(e=>e.innerText.trim()+(e.disabled?'[disabled]':'')).filter(t=>t&&t.length<32&&!/Search|×|More|☰/.test(t))")
    fields = await q(pg, "input:not([type=hidden]):not([placeholder*='Search']), textarea, select",
        "map(e=>({t:e.type||e.tagName.toLowerCase(),name:e.name||'',ph:e.placeholder||'',aria:(e.getAttribute('aria-label')||'').slice(0,40),req:!!(e.required||e.getAttribute('aria-required')==='true'),val:(e.value||'').slice(0,20)}))")
    labels = await q(pg, "label, legend, .slds-form-element__label, [class*=question], h3, h4",
        "map(e=>e.innerText.replace(/\\s+/g,' ').trim()).filter(t=>t&&t.length>6&&t.length<140&&!/Search Service/.test(t))")
    print(f"\n### {tag}\n  buttons: {btns[:12]}")
    for f in fields[:16]: print(f"  input : {f}")
    for t in dict.fromkeys(labels): print(f"  label : {t}")
    for m in await msgs(pg): print(f"  MSG   : {m}")
    await pg.screenshot(path=f"{S}/wiz5_{tag.replace(' ','_')}.png", full_page=True)
async def settle(pg):
    try: await pg.locator(".ui-overlay").first.wait_for(state="hidden", timeout=6000)
    except Exception: pass
async def press(pg, name, last=False):
    assert "submit" not in name.lower()
    loc = pg.get_by_role("button", name=name, exact=True)
    n = await loc.count()
    for i in (range(n-1, -1, -1) if last else range(n)):
        b = loc.nth(i)
        if await b.is_enabled(): await settle(pg); await b.click(force=True); await pg.wait_for_timeout(2200); return True
    return False
async def answer_one_unanswered(pg):
    radios = await q(pg, "input[type=radio]", "map(e=>({name:e.name,val:e.value,checked:e.checked}))")
    groups = {}
    for r in radios: groups.setdefault(r["name"], []).append(r)
    for name, opts in groups.items():
        if not any(o["checked"] for o in opts):
            vals = [o["val"] for o in opts]; pick = "No" if "No" in vals else vals[-1]
            await pg.locator(f"input[type=radio][name='{name}'][value='{pick}']").first.check(force=True)
            await pg.wait_for_timeout(900); return f"radio {name}={pick}"
    for sel, how in [("select", "select")]:
        s = pg.locator(sel)
        for i in range(await s.count()):
            e = s.nth(i)
            if await e.is_visible() and (await e.input_value()) == "":
                await e.select_option(index=1); await pg.wait_for_timeout(900); return how
    cbs = pg.locator("[role=combobox]:not([aria-label*='Search'])")
    for i in range(await cbs.count()):
        e = cbs.nth(i)
        if await e.is_visible() and not (await e.input_value() if await e.evaluate("e=>e.tagName==='INPUT'") else await e.inner_text()).strip():
            await e.click(force=True); await pg.wait_for_timeout(600)
            o = pg.locator("[role=option]").last
            if await o.count(): await o.click(force=True); await pg.wait_for_timeout(900); return "combobox"
    d = pg.locator("input[type=date], input[type=datetime-local]")
    for i in range(await d.count()):
        e = d.nth(i)
        if await e.is_visible() and (await e.input_value()) == "": await e.fill("2026-09-01"); await pg.wait_for_timeout(700); return "date"
    t = pg.locator("textarea, input[type=text]:not([placeholder*='Search'])")
    for i in range(await t.count()):
        e = t.nth(i)
        if await e.is_visible() and (await e.input_value()).strip() == "":
            await e.fill("research dry run - not a real request"); await pg.wait_for_timeout(700); return "text"
    return None
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True); pg = await b.new_page(viewport={"width":1100,"height":1600})
        await pg.goto(BASE, wait_until="networkidle", timeout=60000); await pg.wait_for_timeout(3000)
        t = pg.locator("[data-service-code]", has_text=CAT).first
        await t.scroll_into_view_if_needed(); await t.click(force=True); await pg.wait_for_timeout(2500)
        await pg.get_by_role("button", name="Request This Service").first.click(); await pg.wait_for_timeout(4500)
        await press(pg, "Continue")
        fr = pg.locator("iframe.map-loc-mobile").first; box = await fr.bounding_box()
        await pg.mouse.click(box["x"]+box["width"]*0.55, box["y"]+box["height"]*0.55); await pg.wait_for_timeout(3500)
        await press(pg, "Continue")
        for i in range(1, 16):
            how = await answer_one_unanswered(pg); print(f"  [{i}] answered: {how}")
            if await press(pg, "OK"): print("     (dismissed a validation alert)")
            await press(pg, "Next", last=True)
            counter = [m for m in await msgs(pg) if "mandatory" in m.lower()]
            print(f"     counter: {counter}")
            cont = pg.get_by_role("button", name="Continue", exact=True).first
            if await cont.count() and await cont.is_enabled():
                await dump(pg, "step3 all answered"); print(f"  >>> step 3 needed {i} answers"); break
            if how is None: print("  nothing left to answer but Continue still disabled"); await dump(pg, "step3 stuck"); break
        if await press(pg, "Continue"):
            await dump(pg, "step4 contact")
            if await press(pg, "Continue"): await dump(pg, "step5 review")
            else: await dump(pg, "step4 after Continue (blocked)")
        print("\n(stopped before any Submit)")
        await b.close()
asyncio.run(main())
