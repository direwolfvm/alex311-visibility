# The form registry, for a client

The registry describes the City's request form: every request type, its
questions, and what each answer does. The City changes that form without
notice — in October 2026 it retired two request types — and a client must
follow without shipping a build. Nothing about the registry should be
compiled into the app except the logic that interprets it.

## Endpoints

| | sign-in | |
|---|---|---|
| `GET /submit/api/registry/version` | none | `{version, schema, generated, services}` — "is my copy current?" |
| `GET /submit/api/registry` | session | the picker's list: one light entry per request type, plus `version`, `schema`, `generated` |
| `GET /submit/api/registry/full` | session | every request type **with its questions and rules**, in one response (~360 KB, ~38 KB gzipped) |
| `GET /submit/api/service/{code}` | session | one request type in full; `404` if the City no longer offers it |

Every response carries `ETag: "<version>"` (and `X-Registry-Version`,
`X-Registry-Schema`). Send it back as `If-None-Match` and the answer is
`304 Not Modified` with no body while the copy is current.

## How a client should use it

1. Keep the last `/registry/full` response on disk, with its `version`.
2. At launch — and when the app returns to the foreground after a while —
   `GET /registry/version` (no session needed). If `version` differs from the
   stored one, fetch `/registry/full` again once signed in. Or skip the probe
   and always send `If-None-Match`; a `304` costs almost nothing.
3. **`schema`** is the version of the registry's *shape* (what a question, a
   rule and `reveals` look like). It is `1`. If the server ever reports a
   number the app does not know, the app's form logic may be wrong for it:
   send people to the website to report, rather than guess.
4. A request type that vanishes from the registry has been retired by the
   City. Drop it from the picker; a draft for it cannot be filed.

`version` is a fingerprint of the content, not a counter: compare for
equality only.

## The server still decides

A client mirrors the registry's rules for instant feedback, but
`POST /submit/api/validate` and `/precheck` apply the server's current
registry regardless of what the client holds. A stale copy can make the form
ask the wrong questions; it cannot get a wrong request filed.
