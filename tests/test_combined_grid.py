"""Regression test: a combined START/END condition grid must be read by its own
column headings, and every area in the report must survive.

Reported on v3.7.16 against a combined NSW report whose START columns are filled
and whose END columns are deliberately blank. The app returned 2 areas and 19
rows out of 12 areas and 145 rows, and put START data into END on every row it
did keep - inventing move-out evidence, which on a bond assessment is worse than
missing it.

Five things were wrong and each one is asserted here:

  1. Every area came back named "Item". Each area is its own table, the area
     name sits on the line ABOVE it, and the table's own first column is headed
     "Item" - so all 12 areas got the same name and the duplicate-area clean-up
     merged them into one.
  2. The START/END split was `len(cells) // 2`. With 13 columns and the item
     name in column 0 the boundary lands one column early, so the last START
     column was recorded as END data.
  3. The order inside each block was assumed to be Clean, Undamaged, Working,
     Tenant agrees. This form runs Tenant agrees first, so values shifted.
  4. A one-character cell was dropped when the item name contained that letter
     ("balcony" contains a "y"), so rows corrupted differently depending on
     spelling.
  5. Every header field was null because the page-skip guard matched the bare
     word "EXAMPLE", and the dummy data uses "24 Example Street".

The truth side is read from the PDF's own header row rather than typed here, so
this cannot drift from the document, and the per-field comparison is what
catches a shift - counting rows alone would pass while every value sat in the
wrong column.
"""
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pdfplumber  # noqa: E402

from src.extractor import detect_jurisdiction, extract_pdf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")
FIXTURE = os.path.join(SAMPLES, "NSW_combined_start_populated_end_blank.pdf")

# Column order in the fixture's header row, after the item-name column.
START_COLUMNS = ("tenant_agrees", "tenant_comments", "clean", "undamaged",
                 "working", "landlord_comments")

# Area / item counts for every other report we hold. A change to the grid
# reader that "fixes" the combined form by breaking one of these is not a fix -
# this is the half of the test that stops one report being traded for another.
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


def pdf_truth():
    """Item rows straight from the PDF, keyed by item name."""
    rows = {}
    total = 0
    with pdfplumber.open(FIXTURE) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or (table[0][0] or "").strip() != "Item":
                    continue
                for row in table[2:]:
                    name = (row[0] or "").replace("\n", " ").strip()
                    if not name:
                        continue
                    total += 1
                    cells = [(c or "").replace("\n", " ").strip()
                             for c in row[1:7]]
                    rows.setdefault(name, dict(zip(START_COLUMNS, cells)))
                    # Everything past the START block must be blank in this
                    # fixture - that is what makes invented END data visible.
                    assert not any((c or "").strip() for c in row[7:]), name
    return rows, total


def main():
    failed = 0

    truth, row_total = pdf_truth()
    result = extract_pdf(FIXTURE, jurisdiction=detect_jurisdiction(FIXTURE) or "NSW")
    areas = result["areas"]
    items = sum(len(a["components"]) for a in areas)

    ok = len(areas) == 12 and items == row_total
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} structure: {len(areas)} areas / {items} rows "
          f"(want 12 / {row_total})")

    generic = [a["area_name"] for a in areas
               if a["area_name"].strip().lower() in ("item", "items", "party")]
    ok = not generic
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} no area named after a column heading"
          f"{'' if ok else ': ' + ', '.join(generic)}")

    got = {}
    for area in areas:
        for comp in area["components"]:
            got.setdefault(comp["component_name"], comp)

    mismatches, invented, missing = [], [], []
    for name, expected in truth.items():
        comp = got.get(name)
        if not comp:
            missing.append(name)
            continue
        start = comp["start_of_tenancy"]
        for field, want in expected.items():
            have = start.get(field) or ""
            if want != have:
                mismatches.append(f"{name}.{field}: want {want!r} got {have!r}")
        end = comp["end_of_tenancy"]
        if any(end.get(k) for k in
               ("clean", "undamaged", "working", "comments", "tenant_agrees")):
            invented.append(name)

    for label, bad in (("rows missing", missing),
                       ("field mismatches", mismatches),
                       ("rows with invented END data", invented)):
        ok = not bad
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {len(bad)}")
        for entry in bad[:5]:
            print(f"       {entry}")

    meta = result["report_metadata"]
    want_meta = {
        "address": "24 Example Street, Parramatta NSW 2150",
        "postcode": "2150",
        "tenant_name": "Daniel Nguyen & Priya Shah",
        "landlord_name": "Harbour Example Realty Pty Ltd",
        "property_manager": "Amelia Carter",
        "start_date": "03/09/2026",
    }
    for field, want in want_meta.items():
        have = meta.get(field)
        ok = have == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} metadata {field}: {have!r}"
              f"{'' if ok else f' (want {want!r})'}")

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

    print(f"\n{failed} problem(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
