"""Photos on a report: held briefly, sent to the City with the request, then gone.

What is pinned here: every photo is re-encoded (so only an image gets in,
and the phone's GPS tag does not ride along), the limits, that the bytes
are purged once the City has the request, that only the person who
prepared a request can touch its photos and only before it is sent, and
that the worker attaches on a live filing only — never on a rehearsal.
"""
import asyncio
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from alex311 import photos as P

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / "dashboard/submit.py").read_text()
BROWSER = (ROOT / "src/alex311/submit_browser.py").read_text()
WORKER = (ROOT / "src/alex311/submit_worker.py").read_text()
WIZARD = (ROOT / "src/alex311/wizard.py").read_text()
FORM = (ROOT / "dashboard/submit.html").read_text()
SCHEMA = (ROOT / "src/alex311/schema.sql").read_text()


def picture(fmt="PNG", size=(4000, 3000), exif=None) -> bytes:
    out = io.BytesIO()
    img = Image.new("RGB", size, (200, 30, 30))
    img.save(out, format=fmt, **({"exif": exif} if exif else {}))
    return out.getvalue()


# ------------------------------------------------------------- re-encoding

def test_any_image_becomes_a_small_jpeg_with_a_plain_name():
    jpeg, name = P.normalize(picture("PNG"), "My Pothole (1).PNG")
    assert jpeg[:3] == b"\xff\xd8\xff"
    img = Image.open(io.BytesIO(jpeg))
    assert max(img.size) == P.MAX_SIDE and len(jpeg) < P.CITY_MAX_BYTES
    assert name == "My-Pothole-1.jpg"
    assert P.safe_name("", 2) == "photo-2.jpg" and P.safe_name("../../etc/passwd") == "etc-passwd.jpg"


def test_the_phones_location_does_not_ride_along():
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                              # Make
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (38.0, 48.0, 15.0), "W", (77.0, 2.0, 49.0)
    src = picture("JPEG", size=(800, 600), exif=exif.tobytes())
    assert Image.open(io.BytesIO(src)).getexif().get_ifd(0x8825), "the fixture should carry GPS"
    jpeg, _ = P.normalize(src, "a.jpg")
    out = Image.open(io.BytesIO(jpeg)).getexif()
    assert not out.get_ifd(0x8825) and 0x010F not in out


@pytest.mark.parametrize("data, why", [
    (b"", "empty"),
    (b"%PDF-1.4 not a photo", "not a photo"),
    (b"<html><script>alert(1)</script></html>", "not a photo"),
])
def test_only_a_photo_gets_in(data, why):
    with pytest.raises(P.BadPhoto) as e:
        P.normalize(data, "x.jpg")
    assert why in str(e.value)


def test_an_oversized_upload_is_refused_before_decoding():
    with pytest.raises(P.BadPhoto) as e:
        P.normalize(b"\xff\xd8\xff" + b"0" * (P.MAX_UPLOAD_BYTES + 1), "big.jpg")
    assert "too large" in str(e.value)


# ------------------------------------------------------------- routes, form, worker

def test_the_endpoints_check_whose_request_it_is_and_that_it_is_unsent():
    for r in ('@router.get("/api/attempt/{attempt_id}/photos")',
              '@router.post("/api/attempt/{attempt_id}/photos")',
              '@router.delete("/api/attempt/{attempt_id}/photos/{photo_id}")'):
        assert r in ROUTES
    own = ROUTES.split("def _own_open_attempt(")[1].split("\n    @router")[0]
    assert 'row["submitter_id"] != _submitter(request)' in own and "raise HTTPException(403" in own
    add = ROUTES.split("async def photos_add(")[1].split("\n    @router")[0]
    assert "photos.OPEN_STATES" in add and "raise HTTPException(409" in add
    assert "raise HTTPException(413" in add and "run_in_threadpool" in add
    assert "photos.OPEN_STATES" in ROUTES.split("def photos_remove(")[1].split("\n    @router")[0]


def test_the_status_a_person_watches_says_what_happened_to_their_photos():
    fn = ROUTES.split('@router.get("/api/status/{attempt_id}")')[1].split("\n    @router")[0]
    assert '"photos": row.get("photos") or 0, "photo_note": row.get("photo_note")' in fn


def test_the_form_uploads_what_was_picked_and_says_so():
    assert 'id="photos" accept="image/*,.heic,.heif" multiple' in FORM
    assert "kept on your device in this prototype" not in FORM
    assert "/submit/api/attempt/${id}/photos?name=" in FORM and "body: f" in FORM
    assert "if (!(await syncPhotos()))" in FORM          # a failed photo stops the send, visibly
    assert "st.photo_note" in FORM


def test_the_worker_attaches_on_a_live_filing_only_and_purges_after():
    assert "on_file_upload=None" in WIZARD and "await on_file_upload(pg)" in WIZARD
    assert WIZARD.index("await on_file_upload(pg)") < WIZARD.index('await press(pg, "Continue")                                    # step 1 -> 2')
    run = BROWSER.split("async def _run(")[1].split("\ndef ")[0]
    assert "if photos and allowed:" in run                 # the gate decides, not the caller
    assert "a rehearsal uploads nothing" in run
    assert "photos.for_filing(conn, attempt_id=attempt_id)" in WORKER
    fin = WORKER.split("def finish(")[1].split("\ndef ")[0]
    assert 'if state == "filed":' in fin and "photos.purge(conn, attempt_id=attempt_id)" in fin
    assert "CREATE TABLE IF NOT EXISTS attempt_photos" in SCHEMA and "ON DELETE CASCADE" in SCHEMA.split("attempt_photos")[1]


class FakeLocator:
    def __init__(self, page, kind, text=None):
        self.page, self.kind, self.text = page, kind, text
    @property
    def first(self): return self
    async def count(self):
        if self.kind == "input": return 1 if self.page.has_input else 0
        if self.kind == "text": return 1 if self.text in self.page.listed else 0
        return 1
    async def set_input_files(self, paths):
        self.page.given = list(paths)
        self.page.listed = [Path(p).name for p in paths][: self.page.accepts]
    def get_by_text(self, text, exact=False): return FakeLocator(self.page, "text", text)


class FakePage:
    def __init__(self, has_input=True, accepts=99):
        self.has_input, self.accepts, self.listed, self.given = has_input, accepts, [], None
    def locator(self, sel): return FakeLocator(self, "input" if "input" in sel else "host")
    async def wait_for_timeout(self, ms): pass


def _attach(page, paths, monkeypatch):
    from alex311 import submit_browser as sb
    async def none(*a, **k): return []
    async def settle(pg): return None
    monkeypatch.setattr(sb.wizard, "msgs", none); monkeypatch.setattr(sb.wizard, "settle", settle)
    result = sb.SubmitResult(True, True, "in_wizard", "X")
    asyncio.run(sb.attach_photos(page, paths, result))
    return result


def test_attaching_reports_what_the_citys_form_took(monkeypatch):
    r = _attach(FakePage(), ["/tmp/a.jpg", "/tmp/b.jpg"], monkeypatch)
    assert r.photos_attached == 2 and r.photo_note == "2 photos attached"
    r = _attach(FakePage(has_input=False), ["/tmp/a.jpg"], monkeypatch)
    assert r.photos_attached == 0 and "no upload box" in r.photo_note      # and the filing goes on


def test_a_partial_attach_says_so(monkeypatch):
    import alex311.submit_browser as sb
    page = FakePage(accepts=1)
    r = _attach(page, ["/tmp/a.jpg", "/tmp/b.jpg"], monkeypatch)
    assert r.photos_attached == 1 and "took 1 of 2 photos" in r.photo_note


# ------------------------------------------------------- against the database

DB = os.environ.get("DATABASE_URL", "")
needs_db = pytest.mark.skipif(not DB or ("localhost" not in DB and "127.0.0.1" not in DB),
                              reason="needs a local DATABASE_URL")


@needs_db
def test_three_photos_at_most_then_purged_when_the_city_has_them():
    from alex311 import db
    conn = db.connect()
    aid = conn.execute(
        """INSERT INTO submission_attempts (service_code, outcome, submit_state)
           VALUES ('TESMISCO', 'allow', 'prepared') RETURNING attempt_id""").fetchone()["attempt_id"]
    conn.commit()
    try:
        for i in range(3):
            row = P.add(conn, attempt_id=aid, data=picture("PNG", size=(300, 200)), name=f"p{i}.png")
            assert row["file_name"] == f"p{i}.jpg" and row["bytes"] > 0
        with pytest.raises(P.BadPhoto):
            P.add(conn, attempt_id=aid, data=picture("PNG", size=(300, 200)), name="four.png")
        listed = P.listing(conn, attempt_id=aid)
        assert [p["file_name"] for p in listed] == ["p0.jpg", "p1.jpg", "p2.jpg"]
        assert P.remove(conn, attempt_id=aid, photo_id=listed[1]["photo_id"]) is True
        files = P.for_filing(conn, attempt_id=aid)
        assert [n for n, _ in files] == ["p0.jpg", "p2.jpg"] and all(b[:3] == b"\xff\xd8\xff" for _, b in files)
        # the worker's finish(filed) purges: bytes gone, the count remains for the status board
        from alex311 import submit_worker as w
        w.finish(conn, aid, state="filed", case_number="26-00000001", photo_note="2 photos attached")
        assert P.for_filing(conn, attempt_id=aid) == []
        left = conn.execute("SELECT count(*) AS n, count(data) AS with_bytes FROM attempt_photos WHERE attempt_id = %s", (aid,)).fetchone()
        assert left["n"] == 2 and left["with_bytes"] == 0
        st = db.attempt_status(conn, aid)
        assert st["photo_note"] == "2 photos attached" and st["photos"] == 2
    finally:
        conn.execute("DELETE FROM submission_attempts WHERE attempt_id = %s", (aid,)); conn.commit(); conn.close()


@needs_db
def test_photos_on_a_request_nobody_sent_go_after_a_week():
    from alex311 import db
    conn = db.connect()
    aid = conn.execute(
        "INSERT INTO submission_attempts (service_code, outcome, submit_state) "
        "VALUES ('TESMISCO', 'allow', 'prepared') RETURNING attempt_id").fetchone()["attempt_id"]
    conn.commit()
    try:
        P.add(conn, attempt_id=aid, data=picture("PNG", size=(300, 200)), name="old.png")
        P.add(conn, attempt_id=aid, data=picture("PNG", size=(300, 200)), name="new.png")
        conn.execute("UPDATE attempt_photos SET created_at = now() - interval '8 days' "
                     "WHERE attempt_id = %s AND file_name = 'old.jpg'", (aid,)); conn.commit()
        assert P.sweep(conn) >= 1
        assert [n for n, _ in P.for_filing(conn, attempt_id=aid)] == ["new.jpg"]
    finally:
        conn.execute("DELETE FROM submission_attempts WHERE attempt_id = %s", (aid,)); conn.commit(); conn.close()
