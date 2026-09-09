"""Regression test: combined START/END condition grids, read by column heading.

Three fixtures, one per real-world case an agency produces:

  * start populated / end blank  - the move-in report
  * start blank / end populated  - the move-out report
  * both populated               - the full combined report

They matter as a set. Each one fills a different half of the grid, so a reader
that quietly assumes where START stops passes one and fails another - which is
exactly what happened:

  1. Every area came back named "Item". Each area is its own table, the area
     name sits on the line ABOVE it, and the table's own first column is headed
     "Item" - so all 12 areas got the same name and the duplicate-area clean-up
     merged them into one.
  2. The START/END split was `len(cells) // 2`. With 13 columns and the item
     name in column 0 the boundary lands one column early, so the last START
     column was recorded as END data - inventing move-out evidence.
  3. The order inside each block was assumed to be Clean, Undamaged, Working,
     Tenant agrees. These forms run Tenant agrees first, so values shifted.
  4. A one-character cell was dropped when the item name contained that letter
     ("balcony" contains a "y"), so rows corrupted differently by spelling.
  5. The page-skip guard matched a bare "EXAMPLE", and the dummy data uses
     "24 Example Street", so every header field came back null.
  6. end_of_tenancy had no tenant_comments key, so that column was read off the
     row and then dropped on the way in - invisible until a fixture populated
     the END half.
  7. The structured-vs-generic choice scored only the START half, so a move-out
     report scored zero on every parser and came back as an empty skeleton.

Truth is read from each PDF's own header row rather than typed here, so the
expectations cannot drift from the documents. Comparing field by field is the
point: counting rows alone passes while every value sits one column across.
"""
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber  # noqa: E402

from src.extractor import detect_jurisdiction, extract_pdf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")

FIXTURES = [
    ("NSW_combined_start_populated_end_blank.pdf", "move-in: START filled, END blank"),
    ("NSW_combined_start_blank_end_populated.pdf", "move-out: START blank, END filled"),
    ("NSW_combined_both_populated.pdf", "combined: both halves filled"),
]

# What "Auto Detect" must report for the Document Type. All three fixtures are
# the SAME combined form printed with different halves filled in, so keyword
# matching on the pre-printed headings answers "combined" for every one of them.
# The type has to come from which half carries data.
AUTO_DOC_TYPE = {
    "NSW_combined_start_populated_end_blank.pdf": "move_in",
    "NSW_combined_start_blank_end_populated.pdf": "move_out",
    "NSW_combined_both_populated.pdf": "combined",
}

# Blank official templates on Auto Detect - a no-regression baseline, so a later
# change to detection cannot quietly restate what these forms are. A single-block
# form is named by its layout (note QLD's entry and exit forms come out move_in
# and move_out respectively); a blank combined form stays combined, because there
# is no data to say otherwise and guessing would be worse than the honest answer.
BLANK_DOC_TYPE = {
    "ACT_condition_report.pdf": "combined",
    "NSW_condition_report.pdf": "combined",
    "NT_condition_report.pdf": "move_in",
    "QLD_condition_report_1a.pdf": "move_in",
    "QLD_exit_condition_report_14a.pdf": "move_out",
    "SA_condition_report.pdf": "move_in",
    "TAS_condition_report.pdf": "combined",
    "VIC_condition_report.pdf": "combined",
    "WA_condition_report.pdf": "move_in",
}

META = {
    "address": "24 Example Street, Parramatta NSW 2150",
    "postcode": "2150",
    "tenant_name": "Daniel Nguyen & Priya Shah",
    "landlord_name": "Harbour Example Realty Pty Ltd",
    "property_manager": "Amelia Carter",
}

# Area / item counts for the official templates. A grid reader that "fixes" the
# combined forms by breaking one of these is not a fix - this half of the test
# is what stops one report being traded for another.
EXPECTED = {
    "ACT_condition_report.pdf": (15, 151),
    "NSW_condition_report.pdf": (12, 146),
    "NT_condition_report.pdf": (1, 141),
    "QLD_condition_report_1a.pdf": (14, 134),
    "QLD_exit_condition_report_14a.pdf": (14, 134),
    "SA_condition_report.pdf": (15, 151),
    "TAS_condition_report.pdf": (12, 140),
    "VIC_condition_report.pdf": (14, 195),
    "WA_condition_report.pdf": (9, 88),
}

_ABBR = {"c": "clean", "u": "undamaged", "w": "working"}


def _field(cell):
    n = " ".join(re.sub(r"[^a-z ]", " ", (cell or "").lower()).split())
    if n in _ABBR:
        return _ABBR[n]
    if "tenant" in n and "agree" in n:
        return "tenant_agrees"
    if "tenant" in n and "comment" in n:
        return "tenant_comments"
    if "landlord" in n or "agent" in n:
        return "landlord_comments"
    if "comment" in n:
        return "comments"
    return None


def truth_from_pdf(path):
    """Every item row, keyed by name, mapped through the table's OWN headers."""
    rows, count, areas = {}, 0, 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or (table[0][0] or "").strip() != "Item":
                    continue
                areas += 1

                span = table[0]
                marks = [(i, (c or "").upper())
                         for i, c in enumerate(span) if (c or "").strip()]
                blocks = []
                for pos, (i, txt) in enumerate(marks):
                    block = ("start" if "START" in txt
                             else "end" if "END" in txt else None)
                    if not block:
                        continue
                    stop = marks[pos + 1][0] if pos + 1 < len(marks) else len(span)
                    blocks.append((i, stop, block))

                colmap = {}
                for i, cell in enumerate(table[1]):
                    field = _field(cell)
                    if not field:
                        continue
                    block = next((b for lo, hi, b in blocks if lo <= i < hi), "start")
                    if block == "end" and field == "landlord_comments":
                        field = "comments"
                    if block == "start" and field == "comments":
                        field = "landlord_comments"
                    colmap[i] = (block, field)

                for row in table[2:]:
                    name = (row[0] or "").replace("\n", " ").strip()
                    if not name:
                        continue
                    count += 1
                    record = {"start": {}, "end": {}}
                    for i, (block, field) in colmap.items():
                        if i < len(row):
                            record[block][field] = (row[i] or "").replace("\n", " ").strip()
                    rows.setdefault(name, record)
    return rows, count, areas


def check_fixture(name, label):
    path = os.path.join(SAMPLES, name)
    failed = 0
    print(f"--- {name}  ({label})")
    if not os.path.exists(path):
        print("FAIL fixture missing")
        return 1

    truth, row_count, area_count = truth_from_pdf(path)
    result = extract_pdf(path, jurisdiction=detect_jurisdiction(path) or "NSW")
    areas = result["areas"]
    items = sum(len(a["components"]) for a in areas)

    ok = (len(areas), items) == (area_count, row_count)
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {len(areas)} areas / {items} rows "
          f"(PDF has {area_count} / {row_count})")

    generic = [a["area_name"] for a in areas
               if a["area_name"].strip().lower() in ("item", "items", "party")]
    ok = not generic
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} no area named after a column heading"
          f"{'' if ok else ': ' + ', '.join(generic)}")

    # Auto Detect: extract_pdf defaults to report_type="auto".
    want_type = AUTO_DOC_TYPE[name]
    got_type = result.get("document_type")
    ok = got_type == want_type
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} auto-detected doc type: {got_type!r}"
          f"{'' if ok else f' (want {want_type!r})'}")

    got = {}
    for area in areas:
        for comp in area["components"]:
            got.setdefault(comp["component_name"], comp)

    missing, mismatch = [], []
    for name_, record in truth.items():
        comp = got.get(name_)
        if not comp:
            missing.append(name_)
            continue
        for block, key in (("start", "start_of_tenancy"), ("end", "end_of_tenancy")):
            for field, want in record[block].items():
                have = comp[key].get(field) or ""
                if want != have:
                    mismatch.append(f"{name_} [{block}.{field}] "
                                    f"want {want!r} got {have!r}")

    for lab, bad in (("rows missing", missing), ("field mismatches", mismatch)):
        ok = not bad
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {lab}: {len(bad)}")
        for entry in bad[:5]:
            print(f"       {entry}")

    meta = result["report_metadata"]
    for field, want in META.items():
        have = meta.get(field)
        ok = have == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} metadata {field}: {have!r}"
              f"{'' if ok else f' (want {want!r})'}")
    return failed


# Agency-rendered NSW forms (see tools/make_agency_fixtures.py). Their headings
# are rotated - so the text layer hands them over reversed - and live in a strip
# above the grid, repeated as the grid's own first row on continuation pages.
# Before this was handled the reader fell through to a positional guess that
# produced its built-in 146-item checklist with EVERY tick dropped, which looked
# exactly like a clean extraction of a standard form.
AGENCY = [
    ("NSW_agency_entry_rotated_grid.pdf", "move_in", "start", "landlord_comments"),
    ("NSW_agency_exit_rotated_grid.pdf", "move_out", "end", "comments"),
]


def agency_truth(path):
    """(areas, item rows, rows carrying a tick) read from the PDF itself."""
    areas = items = ticks = 0
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                rows = table.extract()
                # Six-plus columns is what makes a table a condition grid. Do
                # NOT also require several rows: a grid that runs over a page
                # boundary can leave a two-row continuation table carrying a
                # real item, and skipping it made this helper undercount by one
                # - nearly sending me to "fix" an extractor that was right.
                if not rows or max(len(r) for r in rows) < 6:
                    continue
                for row_obj, row in zip(table.rows, rows):
                    name = re.sub(r"\s+", " ", str(row[0] or "")).strip()
                    if not re.search(r"[A-Za-z]{2,}", name):
                        continue
                    cells = [c for c in row_obj.cells if c]
                    width = table.bbox[2] - table.bbox[0]
                    if len(cells) == 1 and (cells[0][2] - cells[0][0]) / width >= 0.8:
                        areas += 1
                        continue
                    items += 1
                    if any((c or "").strip() in ("Y", "N") for c in row[1:5]):
                        ticks += 1
    return areas, items, ticks


def check_agency(name, want_type, want_block, want_comment_field):
    path = os.path.join(SAMPLES, name)
    failed = 0
    print(f"--- {name}")
    if not os.path.exists(path):
        print("FAIL fixture missing")
        return 1

    want_areas, want_items, want_ticks = agency_truth(path)
    result = extract_pdf(path, jurisdiction=detect_jurisdiction(path) or "NSW")
    areas = result["areas"]
    items = sum(len(a["components"]) for a in areas)
    ticks = sum(1 for a in areas for c in a["components"]
                for b in ("start_of_tenancy", "end_of_tenancy")
                if c[b].get("clean") or c[b].get("undamaged") or c[b].get("working"))

    for label, got, want in (("areas", len(areas), want_areas),
                             ("item rows", items, want_items),
                             ("rows with a tick", ticks, want_ticks)):
        ok = got == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got} (PDF has {want})")

    ok = result["document_type"] == want_type
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} doc type {result['document_type']!r}"
          f"{'' if ok else f' (want {want_type!r})'}")

    # The observations must land on the correct side of the tenancy, and the
    # comment must be attributed to whoever actually wrote it - the two comment
    # columns swap owners between the entry and exit forms.
    other = "end_of_tenancy" if want_block == "start" else "start_of_tenancy"
    mine = "start_of_tenancy" if want_block == "start" else "end_of_tenancy"
    stray = sum(1 for a in areas for c in a["components"] if any(c[other].values()))
    ok = stray == 0
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} nothing filed under {other}: {stray} row(s)")

    owned = sum(1 for a in areas for c in a["components"]
                if c[mine].get(want_comment_field))
    ok = owned > 0
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} comments attributed to {want_comment_field}: {owned}")

    # A tenant's reply is not compulsory, so it is usually absent - but it must
    # be captured, and kept apart from the agent's, when it is there.
    tenant = sum(1 for a in areas for c in a["components"]
                 if c[mine].get("tenant_comments"))
    want_tenant = want_block == "end"
    ok = bool(tenant) == want_tenant
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} tenant comments captured: {tenant}"
          f"{'' if ok else ' (want %s)' % ('some' if want_tenant else 'none')}")

    # The statutory questions are answered by a tick, not by a printed word.
    st = result["statutory"]
    checks = [
        ("structurally_sound", st["minimum_standards"].get("structurally_sound"), "Yes"),
        ("adequate_ventilation", st["minimum_standards"].get("adequate_ventilation"), "Yes"),
        ("adequate_plumbing", st["minimum_standards"].get("adequate_plumbing"), "Yes"),
        ("mould_dampness", st["health_issues"].get("mould_dampness"), "No"),
        ("pests_vermin", st["health_issues"].get("pests_vermin"), "No"),
        ("smoke installed", st["smoke_alarms"].get("installed"), "Yes"),
        # Left blank on the form: it must stay null, never default to "No".
        ("tenant_agrees (blank)", st["minimum_standards"].get("tenant_agrees"), None),
    ]
    wrong = [(n, g, w) for n, g, w in checks if g != w]
    failed += len(wrong)
    print(f"{'ok  ' if not wrong else 'FAIL'} statutory read from ticks: "
          f"{len(checks) - len(wrong)}/{len(checks)}")
    for name, got, want in wrong:
        print(f"     FAIL {name}: got {got!r}, want {want!r}")

    # Every photo must carry the area it evidences, or an importer has nothing
    # to file it against. One photo per page is a close-up of a plain wall,
    # which is what damage evidence usually looks like - those were being
    # discarded as clip-art, so the count matters as much as the labels.
    images = result["images"]
    ok = len(images) == 12
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} photos found: {len(images)}"
          f"{'' if ok else ' (want 12, incl. the flat wall close-ups)'}")

    unlabelled = [i for i in images if not i.get("area")]
    ok = images and not unlabelled
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} photos carrying an area: "
          f"{len(images) - len(unlabelled)}/{len(images)}")

    named = {i["area"] for i in images if i.get("area")}
    stray = named - {a["area_name"] for a in areas}
    ok = not stray
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} photo areas match real areas"
          f"{'' if ok else ': ' + ', '.join(sorted(stray))}")

    captioned = [i for i in images if i.get("caption") and i.get("photographer")]
    ok = len(captioned) == len(images)
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} photos with caption and photographer: "
          f"{len(captioned)}/{len(images)}")
    return failed


def check_blank_grid():
    """A report whose grid was left entirely blank, with only photos taken.

    Nothing is ticked and nothing is commented, so both readings score zero -
    and the reader used to settle that tie by returning its own built-in
    checklist: 146 rows under ITS OWN uppercase area names for a form that
    lists 143 under its own. A tidy, entirely fictional skeleton. The document's
    structure has to win.
    """
    name = "NSW_agency_exit_blank_grid.pdf"
    path = os.path.join(SAMPLES, name)
    failed = 0
    print(f"--- {name}  (grid blank, photos only)")
    if not os.path.exists(path):
        print("FAIL fixture missing")
        return 1

    want_areas, want_items, _ = agency_truth(path)
    result = extract_pdf(path, jurisdiction=detect_jurisdiction(path) or "NSW")
    areas = result["areas"]
    items = sum(len(a["components"]) for a in areas)

    for label, got, want in (("areas", len(areas), want_areas),
                             ("item rows", items, want_items)):
        ok = got == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got} (PDF has {want})")

    # The document names its areas "Entrance/hall"; the built-in checklist uses
    # "ENTRANCE/HALL". Upper-case names mean the checklist was returned instead.
    shouty = [a["area_name"] for a in areas
              if a["area_name"].isupper() and len(a["area_name"]) > 3]
    ok = not shouty
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} areas came from the document"
          f"{'' if ok else ': got built-in names ' + ', '.join(shouty[:3])}")

    ok = result["document_type"] == "move_out"
    failed += not ok
    note = "" if ok else " (want 'move_out' - the title says Exit)"
    print(f"{'ok  ' if ok else 'FAIL'} doc type {result['document_type']!r}{note}")

    labelled = sum(1 for i in result["images"] if i.get("area"))
    ok = result["images"] and labelled == len(result["images"])
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} photos carrying an area: "
          f"{labelled}/{len(result['images'])}")
    return failed


def main():
    failed = 0
    failed += check_blank_grid()
    print()
    for name, label in FIXTURES:
        failed += check_fixture(name, label)
        print()

    for name, want_type, want_block, want_comment in AGENCY:
        failed += check_agency(name, want_type, want_block, want_comment)
        print()

    for name, (want_areas, want_items) in sorted(EXPECTED.items()):
        path = os.path.join(SAMPLES, name)
        if not os.path.exists(path):
            print(f"FAIL {name}: sample missing")
            failed += 1
            continue
        other = extract_pdf(path, jurisdiction=detect_jurisdiction(path) or "NSW")
        n_areas = len(other["areas"])
        n_items = sum(len(a["components"]) for a in other["areas"])
        ok = (n_areas, n_items) == (want_areas, want_items)
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {n_areas} areas / {n_items} items "
              f"(want {want_areas} / {want_items})")

        # A blank form has answered nothing. The official NSW PDF is a fillable
        # AcroForm whose statutory section sits on page 10, and it was reporting
        # 27 invented answers - "Yes" across the board on an empty document.
        stat = other.get("statutory") or {}
        answered = []
        stack = [("", stat)]
        while stack:
            prefix, node = stack.pop()
            for key, value in node.items():
                if isinstance(value, dict):
                    stack.append((prefix + key + ".", value))
                elif value is not None:
                    answered.append(prefix + key)
        ok = not answered
        failed += not ok
        print(f"{'     ok  ' if ok else '     FAIL'} statutory blank: "
              f"{len(answered)} answered"
              f"{'' if ok else ' -> ' + ', '.join(answered[:6])}")

        want_type = BLANK_DOC_TYPE.get(name)
        if want_type:
            got_type = other.get("document_type")
            ok = got_type == want_type
            failed += not ok
            print(f"{'     ok  ' if ok else '     FAIL'} doc type {got_type!r}"
                  f"{'' if ok else f' (want {want_type!r})'}")

    print(f"\n{failed} problem(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
