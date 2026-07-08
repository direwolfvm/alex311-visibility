"""Generate raster icon fallbacks matching dashboard/static/favicon.svg."""
from PIL import Image, ImageDraw, ImageFont

BLUE = (29, 78, 216, 255)   # #1d4ed8
WHITE = (255, 255, 255, 255)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]


def font_at(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            # index 1 in Helvetica.ttc is bold; ttf files ignore index errors
            if path.endswith(".ttc"):
                return ImageFont.truetype(path, size, index=1)
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise SystemExit("no usable bold font found")


def render(px: int) -> Image.Image:
    s = px / 64.0  # design coordinates are the SVG's 64x64 viewBox
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=14 * s, fill=BLUE)
    # pin: circle head + triangular tail meeting at the bottom point
    cx, cy, r = 32 * s, 26 * s, 18.5 * s
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)
    d.polygon([(cx - r * 0.62, cy + r * 0.72), (cx + r * 0.62, cy + r * 0.72),
               (cx, 55 * s)], fill=WHITE)
    f = font_at(max(8, round(15.5 * s)))
    d.text((cx, cy), "311", font=f, fill=BLUE, anchor="mm")
    return img


if __name__ == "__main__":
    out = "dashboard/static/"
    render(180).save(out + "apple-touch-icon.png")
    render(32).save(out + "favicon.ico",
                    sizes=[(16, 16), (32, 32)],
                    append_images=[render(16)])
    print("wrote apple-touch-icon.png, favicon.ico")
