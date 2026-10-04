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


# The probe itself moved into the package (alex311.walk), where the nightly
# job runs it; this script stays as the way to walk into a file by hand.
from alex311.walk import probe_category, summarize  # noqa: E402


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
