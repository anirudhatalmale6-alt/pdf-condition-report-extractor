"""Regression test: no summary stat tile may wrap mid-word or overflow its tile.

Reported on v3.7.12: the DOC TYPE tile showed "combine / d" on two lines. The cause
was a fixed pixel wrap limit on a row whose text grows with the user's Windows
display scaling - so it broke on scaled displays and looked fine on unscaled ones.

The check therefore sweeps window width x display scaling. Tk's 'scaling' factor is
the same knob Windows sets from the display setting, so this reproduces a 150% laptop
rather than approximating one.

Needs a display. On a headless box:  xvfb-run -a python tests/test_stat_tiles.py
"""

import os
import sys
import tkinter as tk
from tkinter import font as tkfont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui import OrbasApp  # noqa: E402

WIDTHS = (980, 1160, 1360, 1600)     # 980 is the window minsize
SCALINGS = (1.0, 1.33, 1.5, 1.75)    # Windows 100% / 133% / 150% / 175%

RESULT = {
    "jurisdiction": "NSW",
    "document_type": "combined",     # the value that broke
    "report_metadata": {
        "address": "19 Van Kleef Circuit, Manly 2095",
        "tenant_name": "Mr Mary Lewis",
        "landlord_name": "Mark John",
        "file_format": "digital",
        "source_file": "NSW-PCR-Exit (1).pdf",
        "total_pages": 15,
    },
    "areas": [{"components": [{} for _ in range(8)]} for _ in range(16)],
}


def _tile_labels(widget, out):
    """Every label inside a gridded cell - i.e. the stat tiles, values and captions."""
    for child in widget.winfo_children():
        if isinstance(child, tk.Frame) and child.grid_info():
            out += [c for c in child.winfo_children() if isinstance(c, tk.Label)]
        _tile_labels(child, out)
    return out


def check(scaling, width):
    root = tk.Tk()
    root.tk.call("tk", "scaling", scaling)
    app = OrbasApp(root)
    root.geometry(f"{width}x820")
    app.pdf_size_mb = 0.96
    app.extracted_json = "x" * 856269
    app._show_summary(RESULT)
    root.update()
    root.update_idletasks()

    problems = []
    for lbl in _tile_labels(app.meta_card, []):
        text = lbl.cget("text")
        line = tkfont.Font(root=root, font=lbl.cget("font")).metrics("linespace")
        # A Label adds ~4px of border/padding around the text, so a plain
        # reqheight/linespace ratio rounds a single line up to two at small sizes.
        lines = max(1, int((lbl.winfo_reqheight() - 4 + line // 2) / line))
        overflow = lbl.winfo_reqwidth() - lbl.master.winfo_width()
        if lines > 1 and " " not in text:
            problems.append(f"{text!r} split mid-word onto {lines} lines")
        elif overflow > 6:
            problems.append(f"{text!r} overflows its tile by {overflow}px")

    layout = f"{app._stat_cols} across, {app._stat_fit[0]}pt"
    root.destroy()
    return layout, problems


def main():
    failed = 0
    for scaling in SCALINGS:
        for width in WIDTHS:
            layout, problems = check(scaling, width)
            failed += len(problems)
            print(f"{'FAIL' if problems else 'ok  '} scaling={scaling} "
                  f"width={width}  ({layout})")
            for p in problems:
                print(f"       {p}")
    print(f"\n{failed} problem(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
