"""Anti-abuse policy for the gated submission layer.

Relaying residents' requests to the City means we become the sender of record,
so the volume and the targeting risk are ours to manage. This module is the
policy engine: given a proposed submission and the history around it, it
returns allow / review / block with the reasons spelled out.

Three properties are deliberate:

* **Pure.** No database, no framework, no clock of its own. Everything it needs
  arrives as arguments, which is what lets `spike/backtest_abuse.py` replay 90
  days of real requests through the exact code the endpoint runs.
* **Review, not block.** Only bot signals block outright. Every human-looking
  signal routes to a queue, because the failure mode of a false positive is a
  resident whose real problem goes unreported.
* **Thresholds are data, not code.** `Policy` holds every number, and each one
  is calibrated in docs/generic-form-layer-research.md §3 against what the
  city's own history actually looks like.

The identity rules only mean something once submissions carry a verified
submitter; the address rules work regardless, and are the ones the historical
backtest can exercise, because the city's own channel is anonymous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Iterable, Sequence

# --------------------------------------------------------------- addresses

#: Alexandria's data spells the same street both ways — "1437 JANNEY'S LN" and
#: "1437 JANNEY'S LA" are 51 and 48 records of one address; "MOUNT VERNON AVE"
#: and "MOUNT VERNON AV" are 276 and 234. Per-address caps are meaningless
#: until those collapse to one key.
_SUFFIXES = {
    "ST": "STREET", "STR": "STREET", "STREET": "STREET",
    "AV": "AVENUE", "AVE": "AVENUE", "AVEN": "AVENUE", "AVENUE": "AVENUE",
    "RD": "ROAD", "ROAD": "ROAD",
    "DR": "DRIVE", "DRV": "DRIVE", "DRIVE": "DRIVE",
    "LN": "LANE", "LA": "LANE", "LANE": "LANE",
    "CT": "COURT", "COURT": "COURT",
    "PL": "PLACE", "PLACE": "PLACE",
    "BV": "BOULEVARD", "BLVD": "BOULEVARD", "BOULEVARD": "BOULEVARD",
    "TER": "TERRACE", "TERR": "TERRACE", "TERRACE": "TERRACE",
    "CIR": "CIRCLE", "CIRCLE": "CIRCLE",
    "PKWY": "PARKWAY", "PKY": "PARKWAY", "PARKWAY": "PARKWAY",
    "HWY": "HIGHWAY", "HIGHWAY": "HIGHWAY",
    "WY": "WAY", "WAY": "WAY",
    "SQ": "SQUARE", "SQUARE": "SQUARE",
    "RUN": "RUN", "ROW": "ROW", "MEWS": "MEWS", "WALK": "WALK",
}
_DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
               "N": "N", "S": "S", "E": "E", "W": "W"}


def normalize_address(raw: str | None) -> str:
    """A stable key for one location, tolerant of the portal's spellings.

    Intersections ("A ST & B RD") normalise each side and sort them, so the
    same corner keys the same however it was typed.
    """
    if not raw:
        return ""
    text = re.sub(r"[.,']", "", raw.upper()).strip()
    if "&" in text:
        parts = sorted(normalize_address(p) for p in text.split("&"))
        return " & ".join(p for p in parts if p)
    words = [w for w in re.split(r"\s+", text) if w]
    out = []
    for i, w in enumerate(words):
        if i and w in _DIRECTIONS and i < len(words) - 1:
            out.append(_DIRECTIONS[w])
        elif w in _SUFFIXES:
            out.append(_SUFFIXES[w])
        else:
            out.append(w)
    return " ".join(out)


def sql_prefix(raw: str | None) -> str:
    """A loose, uppercase prefix for narrowing an address lookup in SQL.

    Everything up to (not including) the street-type word, because that word is
    exactly what varies: "1437 JANNEY'S LN" and "1437 JANNEY'S LA" are one
    address. Punctuation is stripped here, and SQL_ADDRESS_EXPR strips the same
    characters from the column, so "JANNEY'S" and "JANNEYS" meet in the middle.
    The result is deliberately loose; `normalize_address` matches exactly
    afterwards.
    """
    if not raw:
        return ""
    text = re.sub(r"[.,']", "", raw.upper())
    text = re.sub(r"\s+", " ", text).strip()
    if "&" in text:
        text = text.split("&")[0].strip()
    keep: list[str] = []
    for w in text.split(" "):
        if keep and w in _SUFFIXES:
            break
        keep.append(w)
    return " ".join(keep)


#: The column-side half of the pair above. Kept next to the function it must
#: agree with, because a mismatch here silently returns no history at all.
#: The doubled quote is SQL's escape for the apostrophe inside the character class.
SQL_ADDRESS_EXPR = "upper(regexp_replace(address, '[.,'']', '', 'g'))"


def normalize_text(raw: str | None) -> str:
    """Lowercased, punctuation-free text for near-duplicate comparison."""
    return re.sub(r"[^a-z0-9 ]+", " ", (raw or "").lower()).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize_text(a), normalize_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ----------------------------------------------------------------- policy

@dataclass(frozen=True)
class Policy:
    """Every threshold, in one place. Calibrated in the research doc §3.

    The daily address cap is the load-bearing one: only three addresses in the
    city exceeded five requests in a day across a whole quarter, so a cap of
    three catches the burst campaign with almost no collateral.
    """
    submitter_per_day: int = 5
    submitter_per_week: int = 15
    submitter_address_per_week: int = 3
    #: a second request for the same address and category inside this window
    #: is a duplicate unless the first one is already closed
    same_category_repeat_days: int = 7
    address_per_day: int = 3
    address_per_week: int = 8
    #: targeting: this share of one submitter's requests landing on one address
    targeting_share: float = 0.60
    targeting_min_events: int = 3
    #: an address getting this many requests in a week whose descriptions
    #: barely differ looks like one voice, not a neighbourhood
    burst_events: int = 5
    burst_days: int = 7
    burst_diversity: float = 0.50
    #: two of a submitter's own descriptions this alike are the same report
    duplicate_text_ratio: float = 0.90
    #: ...but only once there is enough text to judge. 13% of real descriptions
    #: are under 40 characters, and at that length "pothole on my street" and
    #: "potholes on my street" score 0.98 while describing different things.
    duplicate_text_min_chars: int = 40
    #: cooldown grows each time a submitter is sent to review in this window
    cooldown_lookback_days: int = 30


DEFAULT_POLICY = Policy()

#: Outcomes, weakest first. NOTICE exists because the backtest showed the
#: duplicate rule firing on 400 King Street — a busy commercial block where
#: repeat reports are normal. Telling a resident "this is already reported,
#: want to add to it?" is a dedupe prompt; sending them to a moderator is not.
ALLOW, NOTICE, REVIEW, BLOCK = "allow", "notice", "review", "block"
_RANK = {ALLOW: 0, NOTICE: 1, REVIEW: 2, BLOCK: 3}


@dataclass(frozen=True)
class Event:
    """One request, ours or the city's.

    `submitter_id` is None for records that came from the city's own anonymous
    channel — which is all of the historical data, and the reason the identity
    rules cannot be backtested.
    """
    at: datetime
    address: str
    category: str
    submitter_id: str | None = None
    description: str = ""
    closed_at: datetime | None = None
    source: str = "city"          # "city" | "ours"

    @property
    def key(self) -> str:
        return normalize_address(self.address)


@dataclass(frozen=True)
class Finding:
    rule: str
    outcome: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class Decision:
    outcome: str
    findings: list[Finding] = field(default_factory=list)
    cooldown_until: datetime | None = None

    @property
    def allowed(self) -> bool:
        """True when the resident may proceed. A notice is shown, not enforced."""
        return self.outcome in (ALLOW, NOTICE)

    @property
    def needs_review(self) -> bool:
        return self.outcome == REVIEW

    @property
    def reasons(self) -> list[str]:
        return [f.message for f in self.findings]


# ------------------------------------------------------------------ rules

def _within(events: Iterable[Event], now: datetime, days: float) -> list[Event]:
    cutoff = now - timedelta(days=days)
    return [e for e in events if cutoff <= e.at <= now]


def _diversity(events: Sequence[Event]) -> float:
    """Distinct descriptions over total. One voice filing the same complaint
    scores near zero; a street where many people report different things is
    near one."""
    texts = [normalize_text(e.description) for e in events if normalize_text(e.description)]
    if not texts:
        return 1.0        # nothing to judge; do not accuse on missing data
    return len(set(texts)) / len(texts)


def evaluate(proposed: Event, history: Sequence[Event], *,
             policy: Policy = DEFAULT_POLICY, now: datetime | None = None,
             honeypot_filled: bool = False) -> Decision:
    """Decide what to do with one proposed submission."""
    now = now or proposed.at
    findings: list[Finding] = []
    key = proposed.key

    if honeypot_filled:
        # a field no human can see was filled in; nothing else matters
        return Decision(BLOCK, [Finding(
            "honeypot", BLOCK, "Automated submission detected.")])

    prior = [e for e in history if e.at <= now]
    at_address = [e for e in prior if e.key == key]
    mine = [e for e in prior if e.submitter_id and e.submitter_id == proposed.submitter_id]

    # --- volume, per submitter -------------------------------------------
    if proposed.submitter_id:
        today = len(_within(mine, now, 1))
        if today >= policy.submitter_per_day:
            findings.append(Finding("submitter_daily", REVIEW,
                f"You have submitted {today} requests today "
                f"(limit {policy.submitter_per_day}).", {"count": today}))
        week = len(_within(mine, now, 7))
        if week >= policy.submitter_per_week:
            findings.append(Finding("submitter_weekly", REVIEW,
                f"You have submitted {week} requests this week "
                f"(limit {policy.submitter_per_week}).", {"count": week}))

        mine_here = [e for e in _within(mine, now, 7) if e.key == key]
        if len(mine_here) >= policy.submitter_address_per_week:
            findings.append(Finding("submitter_address_weekly", REVIEW,
                f"You have submitted {len(mine_here)} requests about this address "
                f"in the last week.", {"count": len(mine_here), "address": key}))

        # --- targeting: is this submitter's activity aimed at one address? --
        if len(mine) + 1 >= policy.targeting_min_events:
            here = sum(1 for e in mine if e.key == key) + 1
            share = here / (len(mine) + 1)
            if share >= policy.targeting_share:
                findings.append(Finding("targeting_concentration", REVIEW,
                    f"{round(share * 100)}% of your requests concern this one address.",
                    {"share": round(share, 3), "at_address": here, "total": len(mine) + 1}))

        # --- same words, again ---------------------------------------------
        # Short text is not evidence: two brief reports of different problems
        # look alike simply because there are few ways to write them.
        long_enough = len(normalize_text(proposed.description)) >= policy.duplicate_text_min_chars
        for e in _within(mine, now, policy.same_category_repeat_days) if long_enough else []:
            if similarity(e.description, proposed.description) >= policy.duplicate_text_ratio:
                findings.append(Finding("duplicate_text", REVIEW,
                    "This reads almost exactly like a request you already submitted.",
                    {"previous_at": e.at.isoformat()}))
                break

    # --- duplicate of an open request at this address ---------------------
    same_cat = [e for e in _within(at_address, now, policy.same_category_repeat_days)
                if e.category == proposed.category]
    open_same = [e for e in same_cat if e.closed_at is None]
    if open_same:
        findings.append(Finding("open_duplicate", NOTICE,
            f"{len(open_same)} open request(s) of this type are already filed for "
            f"this address in the last {policy.same_category_repeat_days} days.",
            {"count": len(open_same)}))

    # --- volume, per address, across everyone ------------------------------
    day_here = len(_within(at_address, now, 1))
    if day_here >= policy.address_per_day:
        findings.append(Finding("address_daily", REVIEW,
            f"This address has had {day_here} requests today "
            f"(limit {policy.address_per_day}).", {"count": day_here}))
    week_here = len(_within(at_address, now, 7))
    if week_here >= policy.address_per_week:
        findings.append(Finding("address_weekly", REVIEW,
            f"This address has had {week_here} requests this week "
            f"(limit {policy.address_per_week}).", {"count": week_here}))

    # --- burst with one voice ---------------------------------------------
    burst = _within(at_address, now, policy.burst_days)
    if len(burst) + 1 >= policy.burst_events:
        div = _diversity(list(burst) + [proposed])
        if div < policy.burst_diversity:
            findings.append(Finding("address_burst_low_diversity", REVIEW,
                f"This address has {len(burst) + 1} near-identical requests in "
                f"{policy.burst_days} days.",
                {"count": len(burst) + 1, "diversity": round(div, 3)}))

    outcome = max((f.outcome for f in findings), key=lambda o: _RANK[o], default=ALLOW)
    decision = Decision(outcome, findings)

    if outcome == REVIEW and proposed.submitter_id:
        decision.cooldown_until = _cooldown(mine, now, policy)
    return decision


def _cooldown(mine: Sequence[Event], now: datetime, policy: Policy) -> datetime:
    """Exponential backoff on repeat offenders, capped at a day.

    Counts the submitter's recent volume rather than their prior flags, because
    the queue decides those later; this only slows a runaway loop down.
    """
    recent = len(_within(mine, now, policy.cooldown_lookback_days))
    minutes = min(15 * (2 ** max(0, recent // 5)), 24 * 60)
    return now + timedelta(minutes=minutes)


def summarize(decision: Decision) -> str:
    lead = {ALLOW: "No concerns.", NOTICE: "Worth checking first:",
            REVIEW: "Held for review:", BLOCK: "Blocked:"}[decision.outcome]
    return lead if decision.outcome == ALLOW else lead + " " + " ".join(decision.reasons)


__all__ = ["Policy", "DEFAULT_POLICY", "Event", "Finding", "Decision", "evaluate",
           "normalize_address", "normalize_text", "sql_prefix", "SQL_ADDRESS_EXPR",
           "similarity", "summarize",
           "ALLOW", "NOTICE", "REVIEW", "BLOCK", "replace"]
