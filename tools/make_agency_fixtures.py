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
import io
import os

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.utils import ImageReader
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

# The tenant's own column. In practice tenants often do not reply at all - a
# response is not compulsory - so the real reports have this column empty
# throughout. It still has to be captured when it IS filled, and a fixture with
# it blank everywhere would not prove that, so a few rows carry one here.
TENANT_REPLIES = {
    "walls/picture hooks": "Marks were already there at move-in, noted on the entry report",
    "floor coverings": "Wear is from normal use over three years",
}


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


# The statutory questions are answered by TICKING one of two drawn boxes. Both
# words are printed against every question either way, so a fixture has to carry
# the boxes and the tick marks - reproducing only the text would let a reader
# that guesses from the words pass. One question is deliberately left UNANSWERED,
# because a blank must stay null rather than defaulting to "No".
STATUTORY = [
    ("Are the premises structurally sound?", "Yes"),
    ("Does the premises have adequate ventilation?", "Yes"),
    ("Does the premises have adequate plumbing and drainage?", "Yes"),
    ("Are there any signs of mould and dampness?", "No"),
    ("Are there any pests and vermin?", "No"),
    ("Have smoke alarms been installed in the residential premises?", "Yes"),
    ("Does the tenant agree with all of the above?", None),
]


def _checkbox(c, x, y, ticked):
    """An empty 14pt box, with a curved check mark inside it when ticked."""
    c.setLineWidth(0.6)
    c.rect(x, y, 14, 14)
    if not ticked:
        return
    # Curves, so the mark is distinguishable from the box by shape alone.
    p = c.beginPath()
    p.moveTo(x + 3, y + 7)
    p.curveTo(x + 4, y + 6, x + 5, y + 4, x + 6, y + 3)
    p.curveTo(x + 8, y + 6, x + 9, y + 9, x + 11, y + 12)
    p.curveTo(x + 10, y + 12, x + 8, y + 9, x + 6, y + 5)
    p.curveTo(x + 5, y + 6, x + 4, y + 7, x + 3, y + 7)
    c.drawPath(p, stroke=1, fill=1)


def _statutory_page(c, kind):
    """A Minimum Standards / Health Issues page answered with ticks."""
    _title(c, kind, PAGE_H - 30)
    c.setFont("Helvetica", 8)
    c.drawString(COL_X[0], PAGE_H - 56, "Minimum Standards")
    c.setFont("Helvetica", 7)
    c.drawString(COL_X[0], PAGE_H - 70,
                 "The landlord must indicate whether the following apply to the "
                 "residential premises:")
    y = PAGE_H - 96
    for question, answer in STATUTORY:
        c.setFont("Helvetica", 7)
        c.drawString(COL_X[0] + 10, y + 4, question)
        # Boxes sit to the right of the question, each labelled after its box.
        _checkbox(c, 400, y, answer == "Yes")
        c.drawString(418, y + 4, "Yes")
        _checkbox(c, 440, y, answer == "No")
        c.drawString(458, y + 4, "No")
        y -= 34
    c.showPage()


# Photo pages caption every picture with the area it belongs to and who took
# it - the only thing tying a photo to a room. One photo per page is
# deliberately almost FLAT (a close-up of a plain wall, which is what a damage
# photo usually looks like): those were being discarded as clip-art, and a
# fixture of colourful images would not catch that returning.
PHOTO_PAGES = [
    ("Entrance/hall", 6),
    ("Kitchen", 6),
]


def _photo_bytes(seed, flat=False):
    """A JPEG the size the real reports use - noisy, or nearly featureless.

    The dimensions matter: the flatness test only applies to SMALL rasters, so
    a fixture of thumbnails would not exercise the case that mattered - a
    full-size close-up of a plain wall being discarded as clip-art.
    """
    from PIL import Image
    import random
    w, h = 640, 853
    rnd = random.Random(seed)
    if flat:
        # A plain painted wall: a handful of near-identical tones.
        # Seed-dependent, or every flat photo would be byte-identical, share
        # one xref and be emitted only once.
        tone = 224 + (seed % 12)
        base = bytes(bytearray(
            (tone + (i % 3)) for i in range(w * h * 3)))
    else:
        base = bytes(bytearray(rnd.randrange(256) for _ in range(w * h * 3 // 64)))
        base = (base * 64)[:w * h * 3]
    img = Image.frombytes("RGB", (w, h), base)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


def _photo_page(c, kind, area, count, start, total, seed_base=0):
    _title(c, kind, PAGE_H - 30)
    c.setFont("Helvetica", 8)
    c.drawString(COL_X[0], PAGE_H - 50, "Condition report - Photos")
    x, y = COL_X[0], PAGE_H - 90
    for i in range(count):
        n = start + i
        # The last photo on each page is the flat one.
        flat = (i == count - 1)
        c.drawImage(ImageReader(_photo_bytes(seed_base + n * 7 + (0 if kind == "Entry" else 3),
                                             flat=flat)),
                    x, y - 150, width=113, height=150)
        c.setFont("Helvetica", 6)
        c.drawString(x, y - 160, "{} ({}) - {} of {}".format(area, "Agent", n, total))
        x += 140
        if x > 700:
            x = COL_X[0]
            y -= 190
    c.setFont("Helvetica", 6)
    c.drawString(COL_X[0], 24, "Lessor/agent initials    Tenant/s initials")
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

    # Page 3 - the statutory questions, answered by tick.
    _statutory_page(c, kind)

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
            # On an exit report the tenant may reply in their own column.
            reply = TENANT_REPLIES.get(name, "") if (kind == "Exit" and values) else ""
            body = _wrap(c, name, COL_X[1] - COL_X[0])
            note = _wrap(c, comment, COL_X[6] - COL_X[5]) if comment else [""]
            reply_lines = _wrap(c, reply, COL_X[6] - COL_X[5]) if reply else [""]
            height = max(ROW_H, 8 * max(len(body), len(note), len(reply_lines)) + 6)
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
                # performed this inspection - which is column 5 on the entry
                # form and column 6 on the exit form, because the two swap.
                target = COL_X[5] if kind == "Entry" else COL_X[6]
                for li, line in enumerate(note):
                    c.drawString(target + 3, y - 9 - li * 8, line)
                if reply:
                    for li, line in enumerate(reply_lines):
                        c.drawString(COL_X[5] + 3, y - 9 - li * 8, line)
            y -= height
            idx += 1

        c.setFont("Helvetica", 6)
        c.drawString(COL_X[0], 24, "Residential Tenancies Regulation 2019 "
                                   "Schedule 2: Condition report | March 2020")
        c.showPage()

    # Photo pages, captioned with the area each picture evidences.
    for page_no, (area, count) in enumerate(PHOTO_PAGES):
        # Distinct seeds per page: identical images share one xref, and a
        # picture drawn on several pages is taken for a repeated logo and
        # dropped - which silently halved the fixture's photo count.
        _photo_page(c, kind, area, count, 1, count, seed_base=100 * (page_no + 1))

    c.save()
    return path


if __name__ == "__main__":
    for kind, name in (("Entry", "NSW_agency_entry_rotated_grid.pdf"),
                       ("Exit", "NSW_agency_exit_rotated_grid.pdf")):
        out = build(kind, os.path.join(SAMPLES, name))
        print("wrote", out)
