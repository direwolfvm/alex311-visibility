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

Usage: uv run python spike/wizard_rules.py [top_n=15 | all] [screenshot_dir]
"""
import asyncio, json, os, sys, time
from pathlib import Path

from playwright.async_api import async_playwright

# The wizard driver moved into the package so the submission harness and this
# probe share one set of selectors. This module keeps only what makes it a
# *probe*: trying every option and recording what the City does about it.
from alex311 import wizard
from alex311.wizard import (BASE, PLACEHOLDER, answered, choose, choose_dropdown,
                            classify, dismiss_alerts, dropdown_checked, dropdown_options,
                            enabled, fill_free, is_selected, keep_current_visible,
                            msgs, open_category, press, scan, settle, suggestion_text)

BASE = "https://alex311.alexandriava.gov/customer/s/"
ROOT = Path(__file__).resolve().parents[1]
# ALEX311_RULES_OUT lets a drift run write (and resume from) a scratch file
# instead of the committed one, so the weekly re-crawl can be diffed against it.
OUT = Path(os.environ.get("ALEX311_RULES_OUT") or ROOT / "docs/data/wizard-rules.json")
wizard.SHOTS = SHOTS = Path(sys.argv[2]) if len(sys.argv) > 2 else None
PLACEHOLDER = "Select an option"

def load_targets(n):
    """Top-n categories by mined volume, or the whole catalog when n is None:
    mined categories first (by volume), then every other catalog service."""
    rows = json.load(open(ROOT / "docs/data/question-schema.mined.json"))
    cat = json.load(open(ROOT / "docs/data/service-catalog.json"))
    by_name = {t["service_name"].lower(): t for t in cat}
    tot = {}
    for r in rows:
        tot[r["service_name"]] = r["total"]
    ranked = sorted(tot, key=lambda k: -tot[k])

    def entry(t):
        groups = [c["name"] for c in (t.get("definitions") or {}).get("service_categories", [])]
        return (t["service_name"], t["service_code"], groups)

    targets, skipped, seen = [], [], set()
    for nm in ranked:
        t = by_name.get(nm.lower())
        if t:
            targets.append(entry(t)); seen.add(t["service_code"])
        elif len(skipped) < 5:
            skipped.append(nm)
        if n is not None and len(targets) == n:
            return targets, skipped
    if n is None:
        for t in cat:
            if t["service_code"] not in seen:
                targets.append(entry(t))
    return targets, skipped


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
            await dismiss_alerts(pg)
            cont = await enabled(pg, "Continue")
            if not cont and not rec["questions"]:
                # No questions at all: for some services the free-text box is
                # the required field. Fill it and see whether Continue enables.
                ta = pg.locator('textarea[aria-label="Additional Information"], textarea[placeholder="Additional Information"]').first
                if await ta.count():
                    await ta.fill("research dry run - not a real request")
                    await pg.wait_for_timeout(1200)
                    cont = await enabled(pg, "Continue")
                    rec["additional_information_required"] = bool(cont)
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
            if not cont and SHOTS:
                try:
                    await pg.screenshot(path=str(SHOTS / f"rules_{code}_stuck.png"), full_page=True)
                except Exception:
                    pass
            break
        h = pend[0]
        meta = classify(h)
        if meta["kind"] == "dropdown":
            try:
                meta["options"] = await dropdown_options(pg, meta)
            except Exception as e:
                print(f"  dropdown enumeration failed: {e}", flush=True)
        done_ks.add(h["k"])
        entry = {"order": h["k"], "question": h["text"], "required": h["required"], "input": meta["kind"],
                 "code": meta.get("code") or "", "options": meta["options"], "probes": {}, "chosen": None}
        if meta["kind"] == "dropdown":                   # "Select all applicable" = multi-select picklist
            entry["multi"] = meta.get("trigger", "").lower().startswith("select all")
        if meta["kind"] in ("radio", "select", "checkbox", "radio-role", "dropdown") and meta["options"]:
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
                    suggests = await suggestion_text(pg) if await keep_current_visible(pg) else ""
                    if had_ok or suggests:
                        await dismiss_alerts(pg)
                    after = await scan(pg)
                    h2 = next((x for x in after if x["k"] == h["k"]), None)
                    still = await dropdown_checked(pg, opt) if meta["kind"] == "dropdown" else bool(h2 and is_selected(h2, meta, opt))
                    entry["probes"][opt] = {"alert": new_msgs, "hard_stop": bool(had_ok and not still),
                                            "advisory": bool(had_ok and still), "suggests": suggests,
                                            "reveals": sorted({x["k"] for x in after} - base_ks),
                                            "extra_controls": max(0, len(h2["controls"]) - len(h["controls"])) if h2 else 0}
                    if meta["kind"] in ("checkbox", "dropdown") and still:
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
                        await dismiss_alerts(pg)
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
            await fill_free(pg, meta, h["text"])
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
    arg = sys.argv[1] if len(sys.argv) > 1 else "15"
    n = None if arg == "all" else int(arg)
    targets, skipped = load_targets(n)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "read_only": True,
           "skipped_not_in_catalog": skipped, "categories": []}
    if OUT.exists():  # resume: keep categories an earlier run finished cleanly
        try:
            PERMANENT = ("view-only",)
            done = {c["service_code"]: c for c in json.load(open(OUT))["categories"]
                    if (not c.get("error") and c.get("continue_enabled_at_end") is not None)
                    or any(k in (c.get("error") or "") for k in PERMANENT)}
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
