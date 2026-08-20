"""Rebuild the app's brand assets from the approved ORBAS primary lockup.

Everything the app shows comes from one artwork file, so a new lockup is a single
command rather than three hand-edited images that can drift apart:

    python tools/make_brand_assets.py path/to/ORBAS_Primary_Approved_Tagline.jpg

Produces, in assets/:
    orbas_logo.png   header wordmark, transparent, 60px tall
    orbas_icon.png   256x256 monogram tile, transparent
    orbas.ico        multi-size Windows icon (title bar, taskbar, exe)

The supplied artwork sits on a solid white background, so the background is keyed
out on a ramp rather than a hard threshold - a hard cut leaves a white fringe on
every curve once the lockup is scaled down to header size. White inside the
letterforms goes transparent too, which is what the approved lockup wants: the
counters read as holes on whatever the app paints behind them.
"""

import os
import sys

from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(HERE, "assets")

LOGO_HEIGHT = 60          # header lockup, used at native size by the Tk label
ICON_SIZE = 256
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Alpha ramp, measured as 255 - min(r, g, b): 0 is white, and every brand colour
# (navy, green, yellow) sits well above OPAQUE_AT. The gap absorbs both JPEG noise
# and the artwork's own anti-aliasing.
CLEAR_BELOW = 6
OPAQUE_AT = 40


def keyed(im):
    """RGBA copy of `im` with its white background keyed out."""
    im = im.convert("RGB")
    r, g, b = im.split()
    # Darkest of the three channels: white is 255, every inked pixel is lower.
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    span = OPAQUE_AT - CLEAR_BELOW
    alpha = lo.point(lambda v: _alpha(255 - v, span))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    return out


def _alpha(dist, span):
    if dist <= CLEAR_BELOW:
        return 0
    if dist >= OPAQUE_AT:
        return 255
    return int(round((dist - CLEAR_BELOW) * 255 / span))


def trimmed(im):
    """Crop away fully transparent margins."""
    box = im.split()[-1].getbbox()
    return im.crop(box) if box else im


def monogram(im):
    """The dark rounded square on the left of the lockup, as a square crop.

    Found by its own colour rather than by fixed offsets, so a re-drawn lockup with
    different padding still crops correctly.
    """
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def navy(x, y):
        r, g, b = px[x, y]
        return r < 60 and g < 70 and b < 90

    # Column-by-column navy counts. Inside the tile a column is navy for most of
    # its height; the navy tagline further right is only a few pixels tall. A
    # single row scan is not enough - the green "o" and yellow "b" interrupt any
    # run of navy across the middle of the tile.
    counts = [sum(1 for y in range(h) if navy(x, y)) for x in range(w)]
    floor = max(counts) * 0.35
    x0 = next(x for x, c in enumerate(counts) if c > floor)
    x1 = x0
    while x1 + 1 < w and counts[x1 + 1] > floor:
        x1 += 1

    rows = [y for y in range(h) if any(navy(x, y) for x in range(x0, x1 + 1))]
    y0, y1 = rows[0], rows[-1]

    # Square it off around the centre, so rounded corners are not clipped.
    side = max(x1 - x0, y1 - y0) + 1
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    return im.crop((cx - side // 2, cy - side // 2,
                    cx - side // 2 + side, cy - side // 2 + side))


def main(src):
    art = keyed(Image.open(src))

    logo = trimmed(art)
    w = max(1, round(logo.width * LOGO_HEIGHT / logo.height))
    logo = logo.resize((w, LOGO_HEIGHT), Image.LANCZOS)
    logo.save(os.path.join(ASSETS, "orbas_logo.png"))

    icon = monogram(art).resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    icon.save(os.path.join(ASSETS, "orbas_icon.png"))
    icon.save(os.path.join(ASSETS, "orbas.ico"),
              sizes=[(s, s) for s in ICO_SIZES])

    print(f"orbas_logo.png  {logo.width}x{logo.height}")
    print(f"orbas_icon.png  {ICON_SIZE}x{ICON_SIZE}")
    print(f"orbas.ico       {', '.join(str(s) for s in ICO_SIZES)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
