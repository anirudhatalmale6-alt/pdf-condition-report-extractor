# ORBAS PDF Extractor - JSON Output Structure

This document describes the JSON structure the extractor produces. It is meant as a
reference for tweaking field names / shape to match the ORBAS converter.

## Top level

```json
{
  "jurisdiction": "NSW",
  "document_type": "combined",
  "detected_document_type": "combined",
  "report_metadata": { ... },
  "areas": [ ... ],
  "other_sections": { ... },
  "images": [ ... ]
}
```

| Field | Meaning |
|-------|---------|
| `jurisdiction` | State/territory (NSW, VIC, QLD, SA, WA, TAS, ACT, NT) |
| `document_type` | `move_in`, `move_out`, or `combined` |
| `detected_document_type` | What auto-detection decided (null if user picked manually) |
| `report_metadata` | Property/tenant/landlord details (see below) |
| `areas` | The room-by-room condition data (the main table) |
| `other_sections` | Compliance, utilities, water efficiency, signatures, etc. |
| `images` | Photos embedded in the PDF (base64) |

## report_metadata

```json
"report_metadata": {
  "address": "19 Van Kleef Circuit, Manly",
  "postcode": "2095",
  "report_number": null,
  "tenant_name": "Mr Mary Lewis",
  "landlord_name": "Mark John",
  "property_manager": null,
  "bond_number": null,
  "date_received": null,
  "start_date": null,
  "end_date": null,
  "source_file": "NSW-PCR-Exit.pdf",
  "source_software": "Property Inspection Manager",
  "total_pages": 15,
  "extraction_timestamp": "2026-07-06T...",
  "extractor_version": "2.3.0"
}
```

`source_software` (v3.7.27+) names the inspection app that produced the report, read
from the file properties, the web links inside the PDF (e.g. photo links) or a
"Powered by" line. Recognised: Inspection Express, Property Inspection Manager,
SnapInspect, Inspect Real Estate, PropertyMe, InspectPro, PCR Pro. `null` = not
recognised (an official form filled by hand, or an app not yet known) - such a
report is still extracted, but deserves a check.

## areas  (the main condition table)

Each area = one room/section. Each component = one line item in that room.
`area_name` is now repeated inside every component so it survives when the
converter flattens `areas > components` into a flat table.

```json
"areas": [
  {
    "area_name": "Front Gardens",
    "page_number": 3,
    "components": [
      {
        "area_name": "Front Gardens",
        "component_name": "Driveway",
        "start_of_tenancy": {
          "clean": "N",
          "undamaged": "Y",
          "working": "Y",
          "landlord_comments": null,
          "tenant_agrees": null,
          "tenant_comments": null
        },
        "end_of_tenancy": {
          "clean": "Y",
          "undamaged": "Y",
          "working": "Y",
          "comments": null,
          "tenant_agrees": null
        }
      }
    ]
  }
]
```

Values for clean/undamaged/working are `"Y"`, `"N"`, or `null` (blank in the PDF).

### Column-name mapping (this is the part to confirm)

The converter currently shows these as `Start Of Tenancy > Clean` etc. If you want
different column headings, tell me the exact labels and I will rename the JSON keys.
Current keys:

| JSON key path | Suggested column label |
|---------------|------------------------|
| `area_name` | Area |
| `component_name` | Item / Component |
| `start_of_tenancy.clean` | Start - Clean |
| `start_of_tenancy.undamaged` | Start - Undamaged |
| `start_of_tenancy.working` | Start - Working |
| `start_of_tenancy.landlord_comments` | Start - Landlord Comments |
| `start_of_tenancy.tenant_agrees` | Start - Tenant Agrees |
| `start_of_tenancy.tenant_comments` | Start - Tenant Comments |
| `end_of_tenancy.clean` | End - Clean |
| `end_of_tenancy.undamaged` | End - Undamaged |
| `end_of_tenancy.working` | End - Working |
| `end_of_tenancy.comments` | End - Comments |
| `end_of_tenancy.tenant_agrees` | End - Tenant Agrees |

## other_sections

Contains compliance (minimum standards, health issues, smoke alarms, safety),
utilities, water efficiency, additional comments, maintenance dates, landlord
promise, and signatures. All nested objects with Yes/No/date values.

## images  (one entry per inspection photo)

Fields an importer uses to file each photo (v3.7.30+):

| Field | Meaning |
|---|---|
| `area` | Exactly an `areas[].area_name` of the same JSON, or `null` for a report-level image (cover page, keys page) |
| `label` | `"<area>: <item>"`, e.g. `"Bathroom: wash basin/taps"`. The part after `"<area>: "` is exactly a `components[].component_name` of that area, or `"Overall"` = a photo of the whole room (attach to the area). Report-level images: `"Cover page"`, `"Keys page"`, `"Signature page"` |
| `phase` | `"move_in"` or `"move_out"`: the inspection the photo was taken at. `null` = belongs to the report as a whole (cover/keys page, or a combined report from software that does not say) |
| `caption` | Human-readable, e.g. `"Bathroom: wash basin/taps (photo 3 of 4) - Exit inspection"` |
| `data_base64` | The JPEG itself when photos are included, otherwise `null` |
| `page`, `index` | Where the photo sits in the PDF |
