# Issue threads

`/threads` starts from one request and shows the others that are probably
about the same thing. The City links requests only when staff mark a
duplicate; this guesses the rest from what the mirror holds, says why, and
lets people correct it.

## How a match is scored (`src/alex311/threads.py`)

Candidates are every request within 250 m and a year of the starting one
(plus anything the City linked to it, wherever it is). Each is scored 0–1
from four signals, weighted 35 / 25 / 25 / 15:

| signal | what | how |
|---|---|---|
| where | distance | `exp(−d / 50 m)`: 1 at the same spot, ½ at ~35 m, 0.13 at 100 m |
| what kind | request type | 1 for the same type, 0.4 for the same department, else 0 |
| wording | the two descriptions | TF-IDF cosine, with idf taken from the candidates around this request, so a word every neighbour uses counts for little |
| when | days apart | `exp(−days / 30)` |

Distance is also a rule, not only a signal: same-issue reports are very
local, so **beyond 80 m a match must be described alike** (wording ≥ 0.25) or
it is not offered at all. Without this, "same type, last month" alone cleared
the threshold anywhere in the circle. On 40 random starts the median match
is now 13 m away and 97% are within 100 m.

A signal that cannot be measured (no coordinates, no description) is left
out and the rest re-weighted, rather than counted as "unlike". A pair the
City itself marked as duplicate/parent scores at least 0.95. Anything under
0.30 is not offered; at most twelve are.

**Feedback.** Signed-in people mark a pair *related* or *not related*
(`thread_feedback`, one row per person per pair, pair stored in sorted
order). Each vote moves the pair's score by 0.15, capped at ±0.45, so a few
people can correct the model and one person cannot flip it on their own.
Votes go with the account when it is deleted.

**The graph.** Edges run from the start to each match, and between two
matches when they score ≥ 0.45 against each other, so a cluster reads as a
web rather than a star. The layout is a small force simulation in the page;
the list underneath carries the same information for anyone who cannot use
the canvas.

Nothing is stored except votes; a thread is computed on request (a few
hundred rows, milliseconds).

## Endpoints

| | |
|---|---|
| `GET /api/threads/random` | a recent request with a description and a location, preferring one with company |
| `GET /api/threads/{case}` | the thread: `center`, `related[]` (each with `score`, `why`, `votes`, `my_vote`), `edges[]` |
| `PUT /submit/api/threads/{case}/{other}` | `{related: true|false|null}` — signed in; `null` withdraws |

## Ideas not done

A real embedding model for the wording signal; using the City's own
`parent`/`duplicate` links as training signal for the weights; threads over
time ("this corner has had 14 reports since 2025").
