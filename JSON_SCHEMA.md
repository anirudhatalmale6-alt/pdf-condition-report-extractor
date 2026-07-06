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
  "total_pages": 15,
  "extraction_timestamp": "2026-07-06T...",
  "extractor_version": "2.3.0"
}
```

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
