"""Regression test: the trademark mark in the header lockup must be legible.

Reported on v3.7.14: "make the logo on the extractor look bigger because TM is not
visible". The mark is only ~12% of the lockup's height, so at the old 60px header
logo it rendered about 3px of solid ink and its strokes disappeared into
anti-aliasing. Then on v3.7.15, "the logo looks too big" - so the size sits in a
band, and the point of this file is that shrinking it back cannot quietly undo the
first fix.

Three things are checked, because fixing only the first would let it regress:

  * the mark is drawn at least MIN_TM_PX tall;
  * the lockup keeps growing with the display scaling. Tk sizes text in points and
    images in raw pixels, so a fixed-pixel logo silently shrinks against its own
    surroundings on a 150% display - the same class of bug as the stat tiles;
  * the header still fits across the window, so a bigger lockup cannot push the
    Refresh button off the right edge at the 980px minimum width.

The pixels are measured through the same _logo_bitmap the app draws with, and the
size Tk actually ended up holding is asserted against it - so this cannot pass on a
bitmap the app never displayed.

Needs a display. On a headless box:  xvfb-run -a python tests/test_header_logo.py
"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui import (LOGO_HEIGHT, LOGO_MASTER_H, OrbasApp,  # noqa: E402
                     _asset_path, _logo_bitmap)

# Tk's scaling is pixels-per-point, so Windows 100% (96dpi) is 96/72 = 1.333.
SCALINGS = {"100%": 96 / 72, "125%": 120 / 72, "150%": 144 / 72, "175%": 168 / 72}
WIDTHS = (980, 1360)      # 980 is the window minsize
# Floor set by looking at the rendered mark magnified 7x, not by arithmetic: at 8px
# both strokes of the M and the gap under the T's arms still resolve, at 7px the M's
# middle vertex starts filling in. v3.7.14 shipped 3px of solid ink, which is the
# defect this file exists for.
MIN_TM_PX = 8
INK = 40                  # alpha above this counts as ink
# The lockup should stay in this band relative to a line of body text. Wide enough
# not to be fussy, tight enough that a fixed-pixel logo fails it at 175%.
RATIO_MIN, RATIO_MAX = 2.8, 8.0


def tm_height(im):
    """Height in pixels of the trademark mark in an RGBA lockup, or None.

    Located by shape rather than a hardcoded offset: the mark is the rightmost run
    of columns whose ink all sits in the top of the image. Every other column is
    either wordmark (ink through the middle) or tagline (ink along the bottom).
    """
    a = np.array(im.convert("RGBA"))[:, :, 3]
    h, _ = a.shape
    inked = a > INK
    top_only = [c for c in range(a.shape[1])
                if inked[:, c].any() and not inked[int(h * 0.45):, c].any()]
    if not top_only:
        return None
    end = start = top_only[-1]
    seen = set(top_only)
    while start - 1 in seen:
        start -= 1
    rows = np.where(inked[:, start:end + 1].any(axis=1))[0]
    return int(rows[-1] - rows[0] + 1)


def check(master, scaling, width):
    root = tk.Tk()
    root.tk.call("tk", "scaling", scaling)
    app = OrbasApp(root)
    root.geometry(f"{width}x820")
    root.update()
    root.update_idletasks()

    img = app._logo_img
    if img is None:
        root.destroy()
        return "no logo", ["header logo failed to load"]

    problems = []
    drawn = _logo_bitmap(master, img.height())
    if (drawn.width, drawn.height) != (img.width(), img.height()):
        problems.append(f"Tk holds {img.width()}x{img.height()}, "
                        f"the bitmap is {drawn.width}x{drawn.height}")

    tm = tm_height(drawn)
    if tm is None:
        problems.append("no trademark mark found in the rendered lockup")
    elif tm < MIN_TM_PX:
        problems.append(f"trademark mark is {tm}px tall, want >= {MIN_TM_PX}")

    line = tkfont.Font(root=root, font=app.font_ui).metrics("linespace")
    ratio = img.height() / line
    if not RATIO_MIN <= ratio <= RATIO_MAX:
        problems.append(f"logo is {ratio:.1f}x the text line height, want "
                        f"{RATIO_MIN}-{RATIO_MAX} - it is out of step with the text")

    # Header row: the lockup on the left, Refresh + version on the right, 18px
    # of window padding either side.
    right = app.refresh_btn.master.winfo_reqwidth()
    need = img.width() + right + 36
    if need > width:
        problems.append(f"header needs {need}px in a {width}px window")

    layout = (f"logo {img.width()}x{img.height()}, TM {tm}px, "
              f"{ratio:.1f}x text, header {need}px")
    root.destroy()
    return layout, problems


def main():
    master = _asset_path("orbas_logo.png")
    if not master:
        print("FAIL assets/orbas_logo.png not found")
        return 1

    failed = 0
    with Image.open(master) as im:
        # The master must stay comfortably above the drawn size - the app scales it
        # up to LOGO_MAX on a large scaled display, and upscaling is what smears the
        # trademark mark. Twice the design height is the minimum worth shipping.
        want = LOGO_HEIGHT * 2
        ok = im.height == LOGO_MASTER_H and im.height >= want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} assets/orbas_logo.png {im.width}x{im.height} "
              f"(want {LOGO_MASTER_H}px tall, >= 2x the {LOGO_HEIGHT}px design size)")

    for name, scaling in SCALINGS.items():
        for width in WIDTHS:
            layout, problems = check(master, scaling, width)
            failed += len(problems)
            print(f"{'FAIL' if problems else 'ok  '} {name} width={width}  ({layout})")
            for p in problems:
                print(f"       {p}")
    print(f"\n{failed} problem(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
