"""Replay real 311 history through the anti-abuse policy.

The policy's thresholds are only worth anything if they fire on the patterns
that actually occurred and stay quiet on the ones that are simply a busy
street. This replays the city's own records in chronological order through
`alex311.abuse.evaluate` — the same function the endpoint calls — and reports
what would have happened.

What it can and cannot show: the city's channel is anonymous, so no historical
record carries a submitter. The identity rules (per-submitter limits, targeting
concentration, duplicate text by one author) are therefore untestable here and
are excluded from the counts. Everything address-shaped is exercised for real.

Usage: DATABASE_URL=... uv run python spike/backtest_abuse.py [--days 90]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta

from alex311 import db
from alex311.abuse import DEFAULT_POLICY, Event, evaluate

# Cases the research doc calls out by name; the policy is supposed to separate
# the first two from the second two.
WATCH = {
    "80 S EARLEY STREET": "burst campaign (52 noise complaints in one day)",
    "493 N ARMISTEAD STREET": "persistent single target",
    "400 KING STREET": "legitimately busy commercial block",
    "1437 JANNEYS LANE": "genuine recurring defect, many reporters",
}


def load(conn, days: int) -> list[Event]:
    rows = conn.execute(
        """SELECT address, service_name, description, requested_datetime, closed_datetime
             FROM service_requests
            WHERE requested_datetime > now() - make_interval(days => %s)
              AND address IS NOT NULL AND requested_datetime IS NOT NULL
            ORDER BY requested_datetime""",
        (days,),
    ).fetchall()
    return [Event(at=r["requested_datetime"], address=r["address"],
                  category=r["service_name"] or "", description=r["description"] or "",
                  closed_at=r["closed_datetime"], source="city") for r in rows]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backtest_abuse", description=__doc__)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--show", type=int, default=12, help="how many flagged addresses to list")
    a = p.parse_args(argv)

    conn = db.connect()
    events = load(conn, a.days)
    print(f"replaying {len(events):,} requests over {a.days} days "
          f"through the live policy\n")

    # Address rules only look at events sharing the address key, so feeding
    # each evaluation just that bucket is equivalent and keeps this O(n·k).
    by_key: dict[str, list[Event]] = defaultdict(list)
    flagged_by_rule: dict[str, int] = defaultdict(int)
    flagged_addresses: dict[str, dict] = defaultdict(lambda: {"n": 0, "first": None, "rules": set()})
    reviewed = noticed = 0
    watch_hits: dict[str, list] = defaultdict(list)

    for i, e in enumerate(events, 1):
        key = e.key
        d = evaluate(e, by_key[key], policy=DEFAULT_POLICY, now=e.at)
        # identity rules cannot fire on anonymous history; assert that so a
        # future change to the engine does not silently invalidate this report
        assert not any(f.rule.startswith("submitter") or f.rule in
                       ("targeting_concentration", "duplicate_text") for f in d.findings)
        if d.outcome == "notice":
            noticed += 1
        if d.outcome in ("review", "block"):
            reviewed += 1
            rec = flagged_addresses[key]
            rec["n"] += 1
            rec["first"] = rec["first"] or e.at
            for f in d.findings:
                flagged_by_rule[f.rule] += 1
                rec["rules"].add(f.rule)
        if d.outcome == "notice":
            for f in d.findings:
                flagged_by_rule[f.rule] += 1
        if key in WATCH:
            watch_hits[key].append((i, len(by_key[key]) + 1, d.outcome,
                                    [f.rule for f in d.findings]))
        by_key[key].append(e)

    pct = 100 * reviewed / len(events) if events else 0
    print(f"held for review: {reviewed:,} of {len(events):,} ({pct:.1f}%)")
    print(f"duplicate notices (shown, not held): {noticed:,} "
          f"({100 * noticed / len(events):.1f}%)")
    print(f"addresses involved: {len(flagged_addresses):,} of {len(by_key):,}\n")

    print("by rule")
    for rule, n in sorted(flagged_by_rule.items(), key=lambda kv: -kv[1]):
        print(f"   {rule:32} {n:6,}")

    print(f"\ntop flagged addresses")
    for key, rec in sorted(flagged_addresses.items(), key=lambda kv: -kv[1]["n"])[:a.show]:
        print(f"   {rec['n']:5}  {key[:44]:46} {', '.join(sorted(rec['rules']))}")

    print("\nthe cases the research doc names")
    for key, what in WATCH.items():
        hits = watch_hits.get(key)
        if not hits:
            print(f"   {key}: no records in this window ({what})")
            continue
        held = [h for h in hits if h[2] in ("review", "block")]
        first = f"request #{held[0][1]} at this address" if held else "never"
        print(f"   {key}")
        print(f"      {what}")
        notes = [h for h in hits if h[2] == "notice"]
        print(f"      {len(hits)} requests, {len(held)} held, {len(notes)} duplicate notices; "
              f"first held: {first}")
        if held:
            print(f"      rules: {', '.join(sorted({r for h in held for r in h[3]}))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
