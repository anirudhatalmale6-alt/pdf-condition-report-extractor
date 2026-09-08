"""Generate agency-rendered NSW condition report fixtures.

Real agency reports arrive as the NSW Schedule 2 form rendered by a web system
(PrinceXML and the like) rather than as the government's own blank PDF, and they
are shaped quite differently from it:

  * the Clean / Undamaged / Working / Tenant Agrees headings are ROTATED, which
    lands them in the text layer reversed ("naelC"),
  * those headings sit in their own strip above the grid on the first grid page,
    and are repeated as the grid's own first row on continuation pages,
  * an area heading is one cell MERGED across the full table width, which is the
    only thing distinguishing it from an unfilled item row,
  * the grid runs over many pages,
  * and the two wide comment columns SWAP owners between entry and exit: an
    entry report reads Lessor/agent then Tenant/s, an exit report the reverse.

The reports these were modelled on carry real tenant, agent and property details,
so they cannot be committed. These fixtures reproduce the layout exactly with
invented data. Regenerate with:

    python3 tools/make_agency_fixtures.py
"""
import os

from reportlab.lib.pagesizes import landscape, A4
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")

PAGE = landscape(A4)
PAGE_W, PAGE_H = PAGE

# Column edges, mirroring the real reports: item name, four narrow tick columns,
# then two wide comment columns.
COL_X = [23, 103, 114, 126, 137, 148, 487, 825]
TICK_COLS = (1, 2, 3, 4)

ROW_H = 17
HEADING_H = 44
GRID_TOP = 104          # first grid page; continuation pages start higher
GRID_TOP_CONT = 40
GRID_BOTTOM = 520

ADDRESS = ("12/45 Wattle Grove", "Riverstone, NSW", "2765")
TENANT = "Marcus Ellery"
AGENT_ENTRY = "Dana Whitfield - Kingsford Realty (Riverstone)"
AGENT_EXIT = "Owen Brackley - Meridian Property Group"

AREAS = [
    ("Entrance/hall", [
        "front door/screen door /security door", "walls/picture hooks",
        "doorway frames", "windows/screens/ window safety devices",
        "ceiling/light fittings", "blinds/curtains",
        "lights/power points/ door bell", "skirting boards",
        "floor coverings", "other"]),
    ("Lounge room", [
        "walls/picture hooks", "doors/doorway frames",
        "windows/screens/ window safety devices", "ceiling/light fittings",
        "blinds/curtains", "lights/power points", "skirting boards",
        "floor coverings", "other"]),
    ("Dining room", [
        "walls/picture hooks", "doors/doorway frames", "ceiling/light fittings",
        "blinds/curtains", "lights/power points", "floor coverings", "other"]),
    ("Kitchen", [
        "walls/picture hooks", "cupboards/drawers", "bench tops", "sink/taps",
        "stove/hotplates", "oven/griller", "range hood/exhaust fan",
        "floor coverings", "other"]),
    ("Bedroom 1", [
        "walls/picture hooks", "wardrobe/shelves", "windows/screens",
        "blinds/curtains", "lights/power points", "floor coverings", "other"]),
    ("Bathroom", [
        "walls/tiling", "bath/taps", "shower/screen", "basin/taps",
        "mirror/cabinet", "toilet/cistern/seat", "floor coverings", "other"]),
    ("Laundry", [
        "walls/tiling", "tub/taps", "washing machine taps", "blinds/curtains",
        "floor coverings", "other"]),
    ("Security/Safety", [
        "locks/keys", "smoke alarms", "window safety devices", "other"]),
    ("General", [
        "hot water system", "gutters/downpipes", "clothesline", "garbage bins",
        "external television antenna/tv points", "other"]),
    ("Balcony", ["walls/railings", "floor coverings", "lights/power points", "other"]),
]

# (clean, undamaged, working, comment). None leaves the row blank, as a real
# inspector does for items that do not apply.
ENTRY_VALUES = {
    "walls/picture hooks": ("Y", "Y", "Y", "Scuffed near the light switch, four picture hooks"),
    "floor coverings": ("Y", "N", "Y", "Small gap in the boards by the doorway"),
    "ceiling/light fittings": ("Y", "Y", "Y", "Two oyster lights, one smoke alarm"),
    "blinds/curtains": None,
    "windows/screens/ window safety devices": None,
    "other": None,
}
EXIT_VALUES = {
    "walls/picture hooks": ("N", "N", "Y", "Plaster scraped near the edge, hooks displaced"),
    "floor coverings": ("N", "N", "Y", "Visible wear across the main walkway"),
    "ceiling/light fittings": ("Y", "Y", "Y", "All in working order"),
    "blinds/curtains": None,
    "windows/screens/ window safety devices": None,
    "other": None,
}
DEFAULT_ENTRY = ("Y", "Y", "Y", "Clean and in working order at handover")
DEFAULT_EXIT = ("N", "Y", "Y", "Dust buildup noted, otherwise serviceable")


def _rotated_heading(c, text, x, y):
    """Draw a column heading so it lands in the text layer REVERSED.

    That reversal is the thing the extractor has to cope with, so a fixture
    without it would not test the fix at all.
    """
    # reportlab's rotated strings are re-normalised to reading order by
    # pdfplumber, so a plain rotate() would NOT reproduce the reversal and the
    # fixture would quietly stop testing it. Stacking the characters up the
    # column instead puts them in the text layer bottom-to-top, which is read
    # back top-to-bottom as "naelC" - exactly what the real reports produce.
    c.setFont("Helvetica", 4.5)
    # Fit the whole label inside the heading strip. A fixed step let the longest
    # label ("Tenant Agrees") run past the cell, so only its first nine
    # characters landed inside and the fixture stopped covering that column.
    step = min(4.4, (HEADING_H - 4) / max(len(text), 1))
    for i, ch in enumerate(text):
        c.drawString(x, y + i * step, ch)


def _wrap(c, text, width, font="Helvetica", size=6.5):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if c.stringWidth(trial, font, size) <= width - 4:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _title(c, kind, y):
    c.setFont("Helvetica", 7)
    c.drawString(COL_X[0], y, f"{kind} condition report: "
                              f"{ADDRESS[0]}, {ADDRESS[1]}")


def _heading_strip(c, top):
    """The four rotated headings in their own ruled strip above the grid."""
    bottom = top - HEADING_H
    c.setLineWidth(0.4)
    for idx in TICK_COLS:
        c.rect(COL_X[idx], bottom, COL_X[idx + 1] - COL_X[idx], HEADING_H)
    labels = ["Clean", "Undamaged", "Working", "Tenant Agrees"]
    for idx, label in zip(TICK_COLS, labels):
        _rotated_heading(c, label, COL_X[idx] + 8, bottom + 3)


def _heading_row(c, top):
    """The same headings repeated as the grid's own first row."""
    bottom = top - HEADING_H
    c.setLineWidth(0.4)
    for i in range(len(COL_X) - 1):
        c.rect(COL_X[i], bottom, COL_X[i + 1] - COL_X[i], HEADING_H)
    labels = ["Clean", "Undamaged", "Working", "Tenant Agrees"]
    for idx, label in zip(TICK_COLS, labels):
        _rotated_heading(c, label, COL_X[idx] + 8, bottom + 3)
    return bottom


def _column_owner_text(c, kind, y):
    """Name the two comment columns - in the order this form uses them."""
    c.setFont("Helvetica", 6)
    c.drawString(COL_X[0], y, "Insert Y/3= Yes")
    c.drawString(COL_X[0] + 60, y, "Insert N/7= No")
    first, second = (("Lessor/agent", "Comments (if any)"),
                     ("Tenant/s", "Comment on lessor/agent report"))
    if kind == "Exit":
        first, second = (("Tenant/s", "Comments (if any)"),
                         ("Lessor/agent", "Comment on tenant/s report"))
    c.drawString(COL_X[5] + 2, y, first[0])
    c.drawString(COL_X[5] + 2, y - 8, first[1])
    c.drawString(COL_X[6] + 2, y, second[0])
    c.drawString(COL_X[6] + 2, y - 8, second[1])


def _metadata_page(c, kind):
    _title(c, kind, PAGE_H - 30)
    y = PAGE_H - 70
    c.setFont("Helvetica", 7)
    for line in ("(b) At the end of the tenancy, the premises will be inspected and the "
                 "condition compared to that stated in the original condition report.",
                 "(d) A condition report must be filled out whether or not a rental bond "
                 "is paid."):
        c.drawString(COL_X[0], y, line)
        y -= 14
    y -= 10
    rows = [
        ("Address of the premises", list(ADDRESS)),
        ("Full name/s of the tenant/s", [TENANT]),
        ("Name of the lessor/agent",
         [AGENT_ENTRY if kind == "Entry" else AGENT_EXIT]),
    ]
    for label, values in rows:
        c.setFont("Helvetica", 7)
        c.drawString(COL_X[0], y, label)
        y -= 12
        for v in values:
            c.drawString(COL_X[0], y, v)
            y -= 12
        y -= 4
    c.drawString(COL_X[0], y, "The tenant/s received a copy of this report on (date):")
    y -= 12
    if kind == "Entry":
        # Laid out one component per line, as the real reports do.
        for piece in ("14", "/", "03", "/ 2025"):
            c.drawString(COL_X[0], y, piece)
            y -= 10
    c.showPage()


def _values_for(kind, item):
    table = ENTRY_VALUES if kind == "Entry" else EXIT_VALUES
    if item in table:
        return table[item]
    return DEFAULT_ENTRY if kind == "Entry" else DEFAULT_EXIT


def build(kind, path):
    c = canvas.Canvas(path, pagesize=PAGE)

    # Page 1 - instructions. Marked so the reader skips it for field lookups.
    c.setFont("Helvetica", 7)
    c.drawString(COL_X[0], PAGE_H - 30, f"{kind} condition report: {ADDRESS[0]}, {ADDRESS[1]}")
    c.drawString(COL_X[0], PAGE_H - 50, "How to complete this report")
    c.drawString(COL_X[0], PAGE_H - 64,
                 "1. Three copies of this condition report should be completed and "
                 "signed by the landlord or the landlord's agent.")
    c.showPage()

    # Page 2 - the labelled metadata block.
    _metadata_page(c, kind)

    # Grid pages.
    rows = []
    for area, items in AREAS:
        rows.append(("area", area, None))
        for item in items:
            rows.append(("item", item, _values_for(kind, item)))

    first_grid_page = True
    idx = 0
    while idx < len(rows):
        _title(c, kind, PAGE_H - 30)
        _column_owner_text(c, kind, PAGE_H - 46)

        if first_grid_page:
            strip_top = PAGE_H - 60
            _heading_strip(c, strip_top)
            y = strip_top - HEADING_H
            first_grid_page = False
        else:
            y = _heading_row(c, PAGE_H - 60)

        c.setLineWidth(0.4)
        while idx < len(rows) and y - ROW_H > PAGE_H - GRID_BOTTOM:
            kind_row, name, values = rows[idx]
            if kind_row == "area":
                # One cell merged across the full width - this is what marks an
                # area heading apart from an unfilled item row.
                c.rect(COL_X[0], y - ROW_H, COL_X[-1] - COL_X[0], ROW_H)
                c.setFont("Helvetica-Bold", 7)
                c.drawString(COL_X[0] + 3, y - ROW_H + 5, name)
                y -= ROW_H
                idx += 1
                continue

            comment = values[3] if values else ""
            body = _wrap(c, name, COL_X[1] - COL_X[0])
            note = _wrap(c, comment, COL_X[6] - COL_X[5]) if comment else [""]
            height = max(ROW_H, 8 * max(len(body), len(note)) + 6)
            if y - height <= PAGE_H - GRID_BOTTOM:
                break
            for i in range(len(COL_X) - 1):
                c.rect(COL_X[i], y - height, COL_X[i + 1] - COL_X[i], height)
            c.setFont("Helvetica", 6.5)
            for li, line in enumerate(body):
                c.drawString(COL_X[0] + 3, y - 9 - li * 8, line)
            if values:
                for col, val in zip(TICK_COLS, values[:3]):
                    c.drawString(COL_X[col] + 3, y - 9, val)
                # The comment goes in whichever column belongs to the party who
                # performed this inspection.
                target = COL_X[5] if kind == "Entry" else COL_X[6]
                for li, line in enumerate(note):
                    c.drawString(target + 3, y - 9 - li * 8, line)
            y -= height
            idx += 1

        c.setFont("Helvetica", 6)
        c.drawString(COL_X[0], 24, "Residential Tenancies Regulation 2019 "
                                   "Schedule 2: Condition report | March 2020")
        c.showPage()

    c.save()
    return path


if __name__ == "__main__":
    for kind, name in (("Entry", "NSW_agency_entry_rotated_grid.pdf"),
                       ("Exit", "NSW_agency_exit_rotated_grid.pdf")):
        out = build(kind, os.path.join(SAMPLES, name))
        print("wrote", out)
