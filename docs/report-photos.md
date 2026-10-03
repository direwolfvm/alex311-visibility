# Photos on a report

Up to three photos can ride along with a request sent from here (web form or
the iOS app). They are held briefly, attached on the City's own form by the
filing worker, and deleted here once the City has filed the request.

## For a client (the web form, the iOS app)

All under the signed-in session (`alex311_portal` cookie), against a request
that has been *prepared* (`POST /submit/api/precheck` returned an
`attempt_id`) and not yet sent.

| | |
|---|---|
| `POST /submit/api/attempt/{attempt_id}/photos?name=<file name>` | Body **is the image bytes** (no multipart); any `Content-Type`. JPEG, PNG, HEIC/HEIF and anything else Pillow decodes. ≤ 15 MB. Returns `{photo_id, file_name, bytes, created_at}`. |
| `GET /submit/api/attempt/{attempt_id}/photos` | `{photos: [...], max: 3}` |
| `DELETE /submit/api/attempt/{attempt_id}/photos/{photo_id}` | `{removed: true}` |
| `GET /submit/api/status/{attempt_id}` | now also `photos` (count) and `photo_note` (e.g. "2 photos attached", or what the City's form said) |

Errors: `400` with a sentence (not a photo, a fourth photo), `403` someone
else's request, `404` no such request, `409` already sent, `413` over 15 MB.

Upload **before** `POST /submit/api/queue`; after that the request is closed
to changes.

## What happens to a photo

1. **Re-encoded on the way in** (`alex311.photos.normalize`): it has to decode
   as an image; HEIC becomes JPEG; orientation is baked in; **EXIF is dropped,
   including the phone's GPS tag**; the long side is capped at 2560 px. The
   stored name is a plain `.jpg`.
2. **Held in Postgres** (`attempt_photos.data`), so the filing worker needs
   nothing but the database it already has.
3. **Attached at the City's File Upload step** (Step 1 of 5; a
   `c-web-file-upload-new` component with a real file input, 10 MB limit) —
   **on a live filing only**. Selecting a file there may upload it to the City
   at once, which is a write, so a rehearsal attaches nothing and says so.
4. A photo the City's form will not take does **not** stop the request; the
   note records what happened and the status shows it.
5. **Purged when the request is filed** — bytes set to NULL, the row kept so
   the count survives — the same moment the contact details are purged.
   Photos on a request nobody sent are purged after 7 days.

## Not yet verified

The attach step has not run against the City's live form: doing so would
upload a file to the City outside a real filing. The first real request with
a photo is the test; check `photo_note` on it.
