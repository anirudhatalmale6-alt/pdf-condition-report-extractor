# ORBAS PDF Extractor — Technical Documentation

**Product:** ORBAS PDF Extractor (Online Rental Bond Assessment System)
**Type:** Native Windows desktop application (Python)
**Purpose:** Extract Australian residential tenancy condition report PDFs into
structured, machine-readable JSON for downstream use in the ORBAS web platform
(converter, CRUD, reconciliation, reporting).
**Jurisdictions supported:** NSW, VIC, QLD, SA, WA, TAS, ACT, NT (all 8
Australian states/territories).

---

## 1. Overview

The application is a self-contained desktop tool. A user selects (or drags in) a
condition report PDF, verifies a product key, and the tool extracts the property
metadata, the room-by-room condition areas/components, and the statutory
questions into a single JSON document that can be copied to the clipboard and
pasted into the ORBAS web platform.

All processing happens **locally on the user's machine** — the PDF and its
contents are never uploaded anywhere. The only network call is an optional
product-key validation.

Design priorities, in order: **reliability** (must never hang or crash),
**speed** (instant startup, fast extraction), and **flexibility** (real-world
reports vary widely, so extraction is deliberately not locked to a rigid
per-jurisdiction template).

---

## 2. Architecture

```
+-------------------------------------------------------------+
|                     ORBAS.exe (desktop)                     |
|                                                             |
|   +------------------+       +---------------------------+  |
|   |   GUI (Tkinter)  | <---> |  Extraction engine        |  |
|   |   src/gui.py     | queue |  src/extractor.py         |  |
|   |                  |       |                           |  |
|   | - file select    |       |  PyMuPDF (fitz)  -- text  |  |
|   | - drag & drop     |       |  pdfplumber      -- tables|  |
|   | - product key     |       |  Pillow          -- images|  |
|   | - JSON view/copy  |       |                           |  |
|   +------------------+       +---------------------------+  |
|            |                             |                  |
|            v                             v                  |
|   product-key check (requests)     structured JSON output   |
+-------------------------------------------------------------+
```

### 2.1 Native Tkinter UI
The interface is built with Python's built-in **Tkinter** toolkit (no embedded
browser, no local web server). This was a deliberate choice after an earlier
WebView-based UI proved heavy and unstable. Tkinter gives:
- instant startup and a tiny footprint;
- no "Not Responding" freezes;
- native OS clipboard access (instant copy, no external process).

### 2.2 Threading model (never blocks the UI)
Long-running work — PDF extraction and product-key validation — runs on a
background `threading.Thread`. Results are pushed onto a `queue.Queue`, which the
Tkinter main loop polls with `root.after(...)`. Because the UI thread is never
occupied by heavy work, **the window can never freeze**, no matter how large the
PDF.

### 2.3 Data flow
1. User selects/drops a PDF and verifies a product key.
2. On "Extract PDF", the engine opens the PDF once with both PyMuPDF and
   pdfplumber.
3. It detects the jurisdiction and document type, extracts metadata, areas,
   statutory questions and image references.
4. The assembled Python dictionary is serialised to indented JSON and shown in
   the output pane; the user copies it with one click.

---

## 3. Technology stack

| Component | Library / Tool | Version | Purpose |
|-----------|---------------|---------|---------|
| Language | Python | 3.12 | Application language |
| PDF text & layout | **PyMuPDF** (`fitz`) | >= 1.24.0 | Fast text extraction, page/line geometry, rotated-text handling, embedded images |
| PDF tables | **pdfplumber** | >= 0.11.0 | Table/grid extraction (the condition matrices with Clean/Undamaged/Working columns) |
| Images | **Pillow** (PIL) | >= 10.0.0 | Image handling, icon/logo asset processing |
| Networking | **requests** | >= 2.31.0 | Product-key validation call |
| Drag & drop | **tkinterdnd2** | >= 0.4.0 | OS-level drag-and-drop of a PDF onto the window |
| GUI | **Tkinter** | stdlib | Native desktop interface |
| Packaging | **PyInstaller** | latest | Bundles Python + libraries into a Windows app folder |
| CI / build | **GitHub Actions** | — | Automated Windows build on every version tag |
| Compression | PowerShell `Compress-Archive` | — | Produces the distributable `.zip` |

Python standard-library modules used: `tkinter`, `threading`, `queue`, `json`,
`os`, `re`, `sys`, `base64`, `io`, `datetime`, `ctypes` (Windows DPI awareness).

---

## 4. Project structure

```
pdf-extractor/
├── main.py                 # Entry point (launches GUI, or CLI if args given)
├── orbas.spec              # PyInstaller build specification (onedir + icon + assets)
├── requirements.txt        # Python dependencies
├── assets/                 # Branding: logo, window icon (.png), exe icon (.ico)
├── schemas/                # JSON schema references
├── samples/                # Sample condition report PDFs (one per jurisdiction)
├── src/
│   ├── gui.py              # Tkinter GUI (class OrbasApp)
│   ├── extractor.py        # Extraction engine (class ConditionReportExtractor)
│   ├── config.py           # App name/version, jurisdiction list, room templates
│   ├── license.py          # Product-key validation
│   ├── cli.py              # Command-line interface (batch/headless use)
│   └── cloud_sync.py       # (reserved) sync helper
├── .github/workflows/build.yml   # CI: build Windows app on version tags
└── TECHNICAL_DOCUMENTATION.md     # This document
```

---

## 5. Extraction pipeline (`src/extractor.py`)

The core class is `ConditionReportExtractor`. The pipeline:

1. **Open** the PDF once with PyMuPDF (`fitz`) for text and with pdfplumber for
   tables.
2. **Detect document type** — move-in, move-out, or combined — from keyword
   analysis of the full text.
3. **Metadata** (`_build_metadata`) — property address, postcode, tenant name,
   landlord/agent name, property manager, bond number, dates (received, tenancy
   start/end), source file, page count. Each value passes validation (section 6).
4. **Areas & components** (`_extract_rooms`) — the room-by-room condition grid:
   - A **structured** pass matches known area/item layouts where available.
   - A **generic** pass reads the condition tables directly for forms/agency
     reports that don't match a known layout.
   - A **flexible clean-up** pass (`_postprocess_areas`) removes noise without
     imposing a fixed schema (section 7.2).
5. **Statutory section** (`_extract_statutory`) — the legislated questions,
   kept separate from the condition areas (section 8).
6. **Other sections** — additional comments, maintenance dates, landlord's
   promise to undertake work, signatures.
7. **Images** — lightweight references (page, size). Image bytes are **not**
   embedded by default, which keeps the JSON small and fast.
8. **Assemble** the final dictionary and return it.

---

## 6. Data validation

Validation happens at several layers. Note (per client direction): the tool does
**not** enforce a strict per-jurisdiction schema, because real-world uploads use
different area, component and section names. Validation focuses on *data quality*
and *noise rejection*, not on forcing documents into a fixed template.

1. **Product key** — extraction is disabled until a key is verified.
2. **File** — only a real `.pdf` file is accepted (browse or drag-drop).
3. **Jurisdiction & document type** — auto-detected, with manual override.
4. **Field-value validation** (`_valid_field_value`) — before any metadata value
   is written to JSON it is checked, and rejected if it is: too short/long, a
   Y/N mark, a list number, a bare form label, instruction text, a URL/email, or
   otherwise not a genuine value. This prevents form boilerplate leaking into
   the data.
5. **Format checks** — postcodes must be 4 digits; dates must match a real date
   pattern (`dd/mm/yyyy` or `29 Aug 2023`).
6. **Completeness check** — after extraction the app confirms the key fields
   (address, tenant, landlord) were found and shows a "Validation Passed" or
   "Validation Review" status, listing anything missing.

---

## 7. Robustness features

### 7.1 Reversed / rotated text correction
Some official forms (e.g. the blank NSW form) print area names **sideways**
(rotated 90°). Depending on rotation direction, a naive extraction reads such
text in reverse character order (e.g. `LOUNGE ROOM` → `MOOR EGNUOL`). The engine
detects this with a conservative vocabulary check (`_normalize_text` /
`_looks_reversed`) and restores the correct reading order **only** when the
reversed form is a clear improvement — so normal text is never altered. Applied
to both area and component names, across all jurisdictions.

### 7.2 Flexible area clean-up (no rigid schema)
`_postprocess_areas` removes obvious noise while preserving any genuine,
even unusually named, area:
- drops junk names (punctuation/blank);
- collapses duplicate area headers, keeping the populated instance;
- treats component-only words (e.g. "Door", "Floor") that appear without a
  condition mark as unfilled *items*, not as new areas;
- ignores narrow key/value metadata tables when locating areas.

This is intentionally name-agnostic: it strips noise, it never rejects content.

---

## 8. JSON output schema

Top-level shape (abbreviated):

```json
{
  "jurisdiction": "NSW",
  "document_type": "combined",
  "detected_document_type": "combined",
  "report_metadata": {
    "address": "...", "postcode": "2095",
    "tenant_name": "...", "landlord_name": "...",
    "property_manager": null, "bond_number": null,
    "date_received": null, "start_date": "29 Aug 2023", "end_date": null,
    "source_file": "report.pdf", "total_pages": 15,
    "extraction_timestamp": "...", "extractor_version": "3.3.0"
  },
  "areas": [
    {
      "area_name": "LOUNGE ROOM",
      "page_number": 3,
      "components": [
        {
          "area_name": "LOUNGE ROOM",
          "component_name": "Walls / picture hooks",
          "start_of_tenancy": { "clean": "Y", "undamaged": "Y", "working": "Y",
                                "landlord_comments": "...", "tenant_agrees": null,
                                "tenant_comments": null },
          "end_of_tenancy":   { "clean": "Y", "undamaged": "Y", "working": "Y",
                                "landlord_comments": "...", "tenant_agrees": null }
        }
      ]
    }
  ],
  "statutory": {
    "minimum_standards": { "...": "...", "utilities": { "electricity_supplied": "Yes", "gas_supplied": "No", "water_supplied": "No" } },
    "health_issues": { "...": "..." },
    "smoke_alarms": { "...": "..." },
    "other_safety_issues": { "...": "..." },
    "communication_facilities": { "telephone_connected": "Yes", "internet_connected": "Yes" },
    "water_usage_and_efficiency": { "...": "..." }
  },
  "other_sections": {
    "additional_comments": null,
    "maintenance_dates": { "...": "..." },
    "landlord_promise": null,
    "signatures": { "...": "..." }
  },
  "images": [ { "page": 1, "width": 800, "height": 600, "data_base64": null } ]
}
```

Notes:
- Each `component` repeats its `area_name` so the record survives when the
  downstream converter flattens areas → components into a table.
- `statutory` is a dedicated top-level section (six sub-sections) kept separate
  from the condition `areas`, consistent across all jurisdictions.
- Fields that don't exist on a given form are `null` rather than omitted.

---

## 9. Product key / licensing

- `src/license.py` validates the product key. Demo/offline keys verify instantly
  (no network round-trip); other keys are validated against the ORBAS API
  endpoint `https://app.orbas.com.au/api/license/validate`.
- **Activation payload** (`POST`, JSON): `license_key`, `email`, `product_code`
  (`ORBAS_EXTRACTOR`), `device_id` (auto — SHA-256 of MAC + host/OS, non-reversible),
  `device_name` (auto-detected), `app_version`. The user only types their email and
  key; device fields are generated on the machine.
- **Response** (`HTTP 200`, JSON): `{ success, valid, reason, message }`. The app
  treats `valid` (falling back to `active`/`success`) as the verdict and shows the
  server `message` on failure.
- The endpoint sits behind a WAF that rejects the default `python-requests`
  User-Agent with `403`, so requests send a named `User-Agent: ORBAS-Extractor/<version>`.
- **Silent re-validation on every launch:** after a successful activation the
  key + email are saved to a per-user file (`%APPDATA%/ORBAS/activation.json` on
  Windows). On each startup the app re-validates that key live against the server
  in the background. Because the request always carries the auto-generated
  `device_id`, the server enforces **one unit per device, bound to one active
  subscription** — if the subscription is inactive, the key is used on another
  device, or the key is revoked, validation fails, the stored activation is
  cleared, and the user is asked to re-enter a key. No "valid" flag is ever cached
  locally, so a machine can never self-authorise offline.
- The verification runs on a background thread so the UI stays responsive.
- Extraction is disabled until a key is verified.

---

## 10. Build & packaging

- **PyInstaller** builds a **one-folder** (`onedir`) Windows application from
  `orbas.spec`. One-folder is used instead of one-file because it starts
  instantly and is far less likely to be quarantined by Windows Defender /
  SmartScreen than a lone unsigned single `.exe`. UPX compression is disabled to
  further reduce antivirus false positives.
- The build bundles the branding `assets/` and `schemas/`, the `tkinterdnd2`
  native drag-and-drop binaries, and sets the ORBAS `.exe` icon.
- **GitHub Actions** (`.github/workflows/build.yml`) builds automatically on any
  `v*` version tag: installs dependencies, runs PyInstaller, zips
  `dist/ORBAS/` into `ORBAS-Windows.zip`, and uploads it as a build artifact.
  Tagged releases are published on the repository's Releases page.

### Running from source (developers)
```bash
pip install -r requirements.txt
python main.py            # launches the GUI
python main.py <file.pdf> # CLI extraction (see src/cli.py)
```

### Running the packaged app (end users)
1. Download `ORBAS-vX.Y.Z-Windows.zip` from the Releases page.
2. Unzip, open the `ORBAS` folder, run `ORBAS.exe`.
3. On first launch, Windows SmartScreen may ask — "More info" → "Run anyway".

---

## 11. Known limitations

- The tool reads **digitally generated** PDFs (real text/tables). Scanned,
  image-only PDFs would require OCR, which is out of current scope.
- Some agency-generated reports group areas visually (bold headings) rather than
  as table rows; where a form provides no structural area boundaries at all, the
  items are still extracted but area grouping for that specific document may be
  approximate. Official blank forms for all 8 jurisdictions extract cleanly.

---

## 12. Version

This document reflects **v3.3.0**. The current version is always shown in the
app header and recorded in each JSON export as `report_metadata.extractor_version`.
