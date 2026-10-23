"""Issue threads: which other requests is this one about?

A 311 request is one person's report of one thing on one day. The thing
itself — a dead tree, a dark block, a car that keeps blocking a sidewalk —
usually has more than one report, and the City's own record only links them
when staff mark a duplicate. This module guesses the rest, from what the
mirror holds, and lets people say whether the guess was right.

Four signals, each scored 0..1, combined with weights that add to one:

  proximity   how near (exp decay, half at ~35 m; nothing beyond `RADIUS_M`)
  category    the same request type, or the same department
  text        TF-IDF cosine over the two descriptions (idf from the
              candidates around this request, so "tree" counts for little in
              a cluster of tree reports and a lot among potholes)
  time        how close in time (exp decay, half at a month)

Same-issue reports are very local — the same pothole, the same block —
so distance is also a rule, not just a signal: beyond `NEAR_M` a match must
be described alike (`FAR_NEEDS_TEXT`) or it is not offered at all, however
recent and whatever its type. Without that, "same type, last month" alone
passed the threshold anywhere inside the circle.

On top of that: a request the City itself marked as a duplicate of (or the
parent of) another is related to it, full stop; and what people said —
"related" / "not related" votes on the pair — moves the score, by a little
per vote and by at most half the scale, so a crowd can correct the model
without one person being able to.

Everything here is read-only against `service_requests`; votes go to
`thread_feedback`. A thread is computed on request, not stored: the mirror
changes every six hours and the model is cheap (a few hundred rows and some
arithmetic).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import timedelta

RADIUS_M = 250              # candidates come from inside this circle
NEAR_M = 80                 # beyond this, a match must also read alike...
FAR_NEEDS_TEXT = 0.25       # ...this much (TF-IDF cosine)
PROXIMITY_SCALE_M = 50      # exp(-d / 50): 1 at the spot, 0.37 at 50 m, 0.13 at 100 m
WINDOW_DAYS = 365           # and from inside this many days either side
MAX_CANDIDATES = 400
MAX_RELATED = 12
THRESHOLD = 0.30            # below this a candidate is not offered
PAIR_THRESHOLD = 0.45       # for an edge between two related requests (not the center)
WEIGHTS = {"proximity": 0.35, "category": 0.25, "text": 0.25, "time": 0.15}
VOTE_STEP, VOTE_CAP = 0.15, 0.45

STOP = set("""a an and are as at be been but by for from has have he her his i if in into is it its
me my no nor not of on or our she so than that the their them then there these they this to
too was we were what when where which who will with would you your please also very just
there's it's i'm don't can't there is are am been being do does did had having""".split())


# ------------------------------------------------------------------ signals

def distance_m(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def proximity(d: float | None) -> float | None:
    return None if d is None else math.exp(-d / PROXIMITY_SCALE_M)


def category(a: dict, b: dict) -> float:
    if a.get("service_code") and a.get("service_code") == b.get("service_code"):
        return 1.0
    if a.get("service_name") and a.get("service_name") == b.get("service_name"):
        return 1.0
    da, db = a.get("primary_service_department"), b.get("primary_service_department")
    return 0.4 if da and da == db else 0.0


def closeness_in_time(a: dict, b: dict) -> float | None:
    ta, tb = a.get("requested_datetime"), b.get("requested_datetime")
    if not ta or not tb:
        return None
    return math.exp(-abs((ta - tb).total_seconds()) / timedelta(days=30).total_seconds())


def tokens(text: str | None) -> list[str]:
    """Words that carry meaning: lowercase, no punctuation, no stop words,
    nothing under three letters, a crude stem so 'trees' meets 'tree'."""
    out = []
    for w in re.findall(r"[a-z0-9']+", (text or "").lower()):
        w = w.strip("'")
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if len(w) > len(suffix) + 3 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


class TextModel:
    """TF-IDF over a small corpus: the request and its candidates."""

    def __init__(self, docs: dict[str, str | None]):
        self.tf = {k: Counter(tokens(v)) for k, v in docs.items()}
        df = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        n = max(1, len(self.tf))
        self.idf = {w: math.log((n + 1) / (d + 1)) + 1 for w, d in df.items()}
        self.vec = {k: self._vec(c) for k, c in self.tf.items()}

    def _vec(self, c: Counter) -> dict[str, float]:
        total = sum(c.values()) or 1
        return {w: (n / total) * self.idf[w] for w, n in c.items()}

    def similarity(self, a: str, b: str) -> float | None:
        va, vb = self.vec.get(a) or {}, self.vec.get(b) or {}
        if not va or not vb:
            return None
        dot = sum(x * vb[w] for w, x in va.items() if w in vb)
        na, nb = math.sqrt(sum(x * x for x in va.values())), math.sqrt(sum(x * x for x in vb.values()))
        return dot / (na * nb) if na and nb else 0.0

    def shared(self, a: str, b: str, n: int = 4) -> list[str]:
        va, vb = self.vec.get(a) or {}, self.vec.get(b) or {}
        both = [(x * vb[w], w) for w, x in va.items() if w in vb]
        return [w for _, w in sorted(both, reverse=True)[:n]]


def combine(parts: dict[str, float | None]) -> float:
    """Weighted mean of the signals that could be measured. A missing
    signal (no coordinates, no description) is left out and the others are
    re-weighted, so a request with no description is judged by where and what
    it is rather than treated as unlike everything."""
    have = {k: v for k, v in parts.items() if v is not None}
    if not have:
        return 0.0
    w = sum(WEIGHTS[k] for k in have)
    return sum(WEIGHTS[k] * v for k, v in have.items()) / w


def city_linked(a: dict, b: dict) -> bool:
    ids = {a.get("duplicate_parent_service_request_id"), a.get("parent_service_request_id")}
    jds = {b.get("duplicate_parent_service_request_id"), b.get("parent_service_request_id")}
    return (b["service_request_id"] in ids or a["service_request_id"] in jds
            or bool((ids & jds) - {None}))


def vote_shift(related: int, not_related: int) -> float:
    net = related - not_related
    return max(-VOTE_CAP, min(VOTE_CAP, net * VOTE_STEP))


# ------------------------------------------------------------- the database

COLS = ("service_request_id, service_name, service_code, status, address, lat, long, "
        "requested_datetime, closed_datetime, description, primary_service_department, "
        "parent_service_request_id, duplicate_parent_service_request_id, media_count")


def get(conn, case: str) -> dict | None:
    return conn.execute(f"SELECT {COLS} FROM service_requests WHERE service_request_id = %s",
                        (case,)).fetchone()


def candidates(conn, seed: dict) -> list[dict]:
    """Everything near this request in space and time, plus anything the
    City linked to it wherever it is."""
    lat, lon, when = seed.get("lat"), seed.get("long"), seed.get("requested_datetime")
    rows: list[dict] = []
    if lat is not None and lon is not None:
        dlat = RADIUS_M / 111_000
        dlon = RADIUS_M / (111_000 * max(0.2, math.cos(math.radians(lat))))
        params = {"id": seed["service_request_id"], "lat0": lat - dlat, "lat1": lat + dlat,
                  "lon0": lon - dlon, "lon1": lon + dlon, "n": MAX_CANDIDATES}
        time_sql = ""
        if when:
            time_sql = "AND requested_datetime BETWEEN %(t0)s AND %(t1)s"
            params |= {"t0": when - timedelta(days=WINDOW_DAYS), "t1": when + timedelta(days=WINDOW_DAYS)}
        rows = conn.execute(
            f"""SELECT {COLS} FROM service_requests
                 WHERE service_request_id <> %(id)s
                   AND lat BETWEEN %(lat0)s AND %(lat1)s AND long BETWEEN %(lon0)s AND %(lon1)s
                   {time_sql}
                 ORDER BY requested_datetime DESC NULLS LAST LIMIT %(n)s""", params).fetchall()
    linked = {seed.get("duplicate_parent_service_request_id"), seed.get("parent_service_request_id")} - {None}
    seen = {r["service_request_id"] for r in rows}
    extra = conn.execute(
        f"""SELECT {COLS} FROM service_requests
             WHERE service_request_id <> %s
               AND (service_request_id = ANY(%s)
                    OR duplicate_parent_service_request_id = %s OR parent_service_request_id = %s)""",
        (seed["service_request_id"], list(linked), seed["service_request_id"], seed["service_request_id"])).fetchall()
    rows += [r for r in extra if r["service_request_id"] not in seen]
    # the box has corners the circle does not
    return [r for r in rows if r["service_request_id"] in linked or r in extra
            or (distance_m(lat, lon, r.get("lat"), r.get("long")) or 0) <= RADIUS_M]


def votes(conn, center: str, others: list[str]) -> dict[str, dict]:
    """Per pair with `center`: how many said related, how many not."""
    if not others:
        return {}
    rows = conn.execute(
        """SELECT CASE WHEN case_a = %s THEN case_b ELSE case_a END AS other,
                  count(*) FILTER (WHERE related) AS related,
                  count(*) FILTER (WHERE NOT related) AS not_related
             FROM thread_feedback
            WHERE (case_a = %s AND case_b = ANY(%s)) OR (case_b = %s AND case_a = ANY(%s))
            GROUP BY other""", (center, center, others, center, others)).fetchall()
    return {r["other"]: {"related": r["related"], "not_related": r["not_related"]} for r in rows}


def my_votes(conn, user_id: str, center: str) -> dict[str, bool]:
    rows = conn.execute(
        """SELECT CASE WHEN case_a = %s THEN case_b ELSE case_a END AS other, related
             FROM thread_feedback WHERE user_id = %s AND (case_a = %s OR case_b = %s)""",
        (center, user_id, center, center)).fetchall()
    return {r["other"]: r["related"] for r in rows}


def pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def vote(conn, *, user_id: str, case_a: str, case_b: str, related: bool | None) -> None:
    """Record (or withdraw, with related=None) one person's view of a pair."""
    a, b = pair(case_a, case_b)
    if a == b:
        raise ValueError("a request is not related to itself")
    if related is None:
        conn.execute("DELETE FROM thread_feedback WHERE user_id = %s AND case_a = %s AND case_b = %s",
                     (user_id, a, b))
    else:
        conn.execute(
            """INSERT INTO thread_feedback (user_id, case_a, case_b, related)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, case_a, case_b) DO UPDATE SET related = EXCLUDED.related, created_at = now()""",
            (user_id, a, b, related))
    conn.commit()


# --------------------------------------------------------------- the thread

def _public(r: dict) -> dict:
    return {k: r.get(k) for k in ("service_request_id", "service_name", "status", "address", "lat", "long",
                                  "requested_datetime", "closed_datetime", "description", "media_count",
                                  "primary_service_department")}


def score_pair(a: dict, b: dict, text: TextModel) -> tuple[float, dict]:
    d = distance_m(a.get("lat"), a.get("long"), b.get("lat"), b.get("long"))
    parts = {"proximity": proximity(d), "category": category(a, b),
             "text": text.similarity(a["service_request_id"], b["service_request_id"]),
             "time": closeness_in_time(a, b)}
    s = combine(parts)
    why = {"distance_m": None if d is None else round(d), "same_type": category(a, b) == 1.0,
           "same_department": 0 < category(a, b) < 1,
           "text_similarity": None if parts["text"] is None else round(parts["text"], 2),
           "shared_words": text.shared(a["service_request_id"], b["service_request_id"]),
           "days_apart": (None if parts["time"] is None
                          else abs((a["requested_datetime"] - b["requested_datetime"]).days)),
           "city_linked": city_linked(a, b)}
    if why["city_linked"]:
        s = max(s, 0.95)
    elif d is not None and d > NEAR_M and (parts["text"] or 0.0) < FAR_NEEDS_TEXT:
        s = 0.0                              # too far apart to be the same thing, unless the words say so
        why["too_far"] = True
    return s, why


def thread(conn, case: str, *, user_id: str | None = None) -> dict | None:
    """The request, the requests it is probably about, and the edges between
    them, with every score explained."""
    seed = get(conn, case)
    if seed is None:
        return None
    cands = candidates(conn, seed)
    # the description only: the request type is its own signal, and a type
    # name in every document would make every pair of a kind look alike
    text = TextModel({r["service_request_id"]: r.get("description") for r in [seed] + cands})
    tallies = votes(conn, case, [c["service_request_id"] for c in cands])
    mine = my_votes(conn, user_id, case) if user_id else {}
    scored = []
    for c in cands:
        s, why = score_pair(seed, c, text)
        v = tallies.get(c["service_request_id"], {"related": 0, "not_related": 0})
        shift = vote_shift(v["related"], v["not_related"])
        final = max(0.0, min(1.0, s + shift))
        if final >= THRESHOLD:
            scored.append((final, s, why, v, c))
    scored.sort(key=lambda t: -t[0])
    related = []
    for final, s, why, v, c in scored[:MAX_RELATED]:
        related.append(_public(c) | {"score": round(final, 3), "model_score": round(s, 3), "why": why,
                                     "votes": v, "my_vote": mine.get(c["service_request_id"])})
    # edges among the related, so the picture is a web and not a star
    edges = [{"a": case, "b": r["service_request_id"], "score": r["score"]} for r in related]
    by_id = {c["service_request_id"]: c for c in cands}
    for i, r1 in enumerate(related):
        for r2 in related[i + 1:]:
            s, _ = score_pair(by_id[r1["service_request_id"]], by_id[r2["service_request_id"]], text)
            if s >= PAIR_THRESHOLD:
                edges.append({"a": r1["service_request_id"], "b": r2["service_request_id"], "score": round(s, 3)})
    return {"center": _public(seed), "related": related, "edges": edges,
            "considered": len(cands), "radius_m": RADIUS_M, "near_m": NEAR_M, "window_days": WINDOW_DAYS,
            "weights": WEIGHTS}


def random_case(conn, *, tries: int = 6) -> str | None:
    """A request worth starting from: recent, with a description and a
    location, and — when one can be found in a few tries — with company."""
    best = None
    for _ in range(tries):
        row = conn.execute(
            """SELECT service_request_id FROM service_requests
                WHERE description IS NOT NULL AND lat IS NOT NULL
                  AND requested_datetime > now() - interval '120 days'
                ORDER BY random() LIMIT 1""").fetchone()
        if row is None:
            return None
        t = thread(conn, row["service_request_id"])
        if t and len(t["related"]) >= 2:
            return row["service_request_id"]
        best = best or row["service_request_id"]
    return best
