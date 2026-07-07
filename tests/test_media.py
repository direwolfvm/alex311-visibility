import io

import pytest

from alex311.ingest import _jpeg_name, heic_to_jpeg, is_heic


def _make_heic_bytes() -> bytes:
    from PIL import Image
    from pillow_heif import register_heif_opener

    register_heif_opener()
    img = Image.new("RGB", (32, 20), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="HEIF")
    return buf.getvalue()


def test_heic_roundtrip_to_jpeg():
    heic = _make_heic_bytes()
    assert is_heic(None, None, heic)  # magic-byte detection alone
    jpeg = heic_to_jpeg(heic)
    assert jpeg[:3] == b"\xff\xd8\xff"

    from PIL import Image
    out = Image.open(io.BytesIO(jpeg))
    assert out.format == "JPEG"
    assert out.size == (32, 20)
    assert "exif" not in out.info  # EXIF (incl. any GPS) must be stripped


@pytest.mark.parametrize("mime,fname,expected", [
    ("image/heic", None, True),
    ("image/heif; charset=binary", None, True),
    (None, "IMG_1234.HEIC", True),
    (None, "IMG_1234.heif", True),
    ("image/jpeg", "photo.jpg", False),
    (None, "archive.heic.zip", False),
])
def test_is_heic_by_mime_and_name(mime, fname, expected):
    assert is_heic(mime, fname, b"") is expected


def test_jpeg_name_swaps_extension():
    assert _jpeg_name("26-1/m1_IMG_1.HEIC") == "26-1/m1_IMG_1.jpg"
    assert _jpeg_name("26-1/m1_photo") == "26-1/m1_photo.jpg"
    assert _jpeg_name("26-1/m1_a.b.heic") == "26-1/m1_a.b.jpg"
