import fitz
import pdfplumber
import json
import os
import re
import base64
from datetime import datetime, timezone
from PIL import Image
from io import BytesIO

from .config import VERSION, ROOM_CONFIGS, REPORT_TYPE_KEYWORDS


# Vocabulary of words that appear in area / component labels. Used to detect
# text that a PDF has stored in reversed character order (rotated/vertical
# labels such as "LOUNGE ROOM" printed sideways come out as "MOOR EGNUOL").
_LABEL_VOCAB = {
    # areas / rooms
    "ENTRANCE", "HALL", "LOUNGE", "LIVING", "DINING", "KITCHEN", "BEDROOM",
    "BED", "BATHROOM", "ENSUITE", "LAUNDRY", "GARAGE", "CARPORT", "GENERAL",
    "SECURITY", "SAFETY", "ROOM", "STUDY", "FAMILY", "MEALS", "RUMPUS",
    "PANTRY", "BALCONY", "PORCH", "DECK", "GARDEN", "GARDENS", "YARD",
    "EXTERIOR", "ENTRY", "STAIRS", "STAIRCASE", "HALLWAY", "STORE",
    "STOREROOM", "TOILET", "WC", "PASSAGE", "FOYER",
    # components / items
    "DOOR", "DOORS", "DOORWAY", "SCREEN", "WINDOW", "WINDOWS", "WALL", "WALLS",
    "CEILING", "FLOOR", "FLOORS", "FLOORING", "COVERINGS", "LIGHT", "LIGHTS",
    "LIGHTING", "FITTINGS", "POWER", "POINTS", "POINT", "SWITCHES", "CURTAINS",
    "BLINDS", "SKIRTING", "BOARDS", "CUPBOARD", "CUPBOARDS", "DRAWERS", "BENCH",
    "TOPS", "TILING", "TILES", "SINK", "TAPS", "STOVE", "HOTPLATES", "OVEN",
    "GRILLER", "EXHAUST", "FAN", "RANGE", "HOOD", "DISHWASHER", "WARDROBE",
    "SHELVES", "BATH", "SHOWER", "BASIN", "MIRROR", "CABINET", "VANITY",
    "TOWEL", "RAILS", "CISTERN", "SEAT", "HOLDER", "HEATING", "VENT",
    "WASHING", "MACHINE", "DRYER", "TUB", "LOCKS", "KEYS", "ALARM", "ALARMS",
    "SMOKE", "SWITCH", "DEVICES", "PICTURE", "HOOKS", "BELL", "FRAMES",
    "BUILT", "AIR", "CONDITIONING", "ANTENNA", "POOL", "FENCE", "GATE",
    "GATES", "FENCES", "GROUNDS", "HOSE", "WATERING", "LAWNS", "EDGES",
    "LETTER", "BOX", "STREET", "NUMBER", "TANKS", "SEPTIC", "GARBAGE", "BINS",
    "PAVING", "DRIVEWAY", "DRIVEWAYS", "CLOTHESLINE", "SHED", "HOT", "WATER",
    "SYSTEM", "GUTTERS", "DOWNPIPE", "FRONT", "OTHER", "BRICKS", "GRASS",
    "OUTSIDE",
}


class ConditionReportExtractor:
    # Expose the vocab on the instance side for helper methods.
    _VOCAB = _LABEL_VOCAB

    @classmethod
    def _looks_reversed(cls, s):
        """True when `s` reads as reversed text (more label words appear when
        the character order is flipped than as-is). Conservative: only flags a
        string when the reversed form is a *strict* improvement."""
        if not s or len(s) < 3:
            return False
        up = s.upper()
        rev = s[::-1].upper()

        def hits(txt):
            return sum(1 for w in cls._VOCAB if w in txt)

        h_orig, h_rev = hits(up), hits(rev)
        if h_rev > h_orig:
            return True
        # Single-word labels with no space: compare exact token membership.
        if h_orig == 0 and h_rev == 0 and " " not in s and "/" not in s:
            return s[::-1].upper() in cls._VOCAB and up not in cls._VOCAB
        return False

    @classmethod
    def _normalize_text(cls, s):
        """Correct reversed (rotated) label text to normal reading order."""
        if not s:
            return s
        # A slash-separated compound (e.g. "YTEFAS/YTIRUCES") reverses both the
        # whole string and each segment - a plain full reverse restores it.
        if cls._looks_reversed(s):
            s = s[::-1]
        # Expand the abbreviated bedroom label some forms print ("BED 3").
        s = re.sub(r"^\s*BED\s+(\d+)\s*$", r"BEDROOM \1", s, flags=re.IGNORECASE)
        return s

    def __init__(self, pdf_path, jurisdiction="NSW", report_type="auto"):
        self.pdf_path = pdf_path
        self.jurisdiction = jurisdiction.upper()
        self.report_type = report_type
        self.detected_type = None
        self.fitz_doc = None
        self.plumber_pdf = None

    def extract(self, output_dir=None, save_images=True, embed_images=False):
        self.fitz_doc = fitz.open(self.pdf_path)
        self.plumber_pdf = pdfplumber.open(self.pdf_path)

        try:
            full_text = self._get_full_text()

            if self.report_type == "auto":
                self.detected_type = self._detect_report_type(full_text)
            else:
                self.detected_type = self.report_type

            areas_raw = self._extract_rooms()
            areas = []
            for room in areas_raw:
                # Correct reversed (rotated) label text before it reaches the JSON.
                room_name = self._normalize_text(room["room_name"])
                area = {
                    "area_name": room_name,
                    "page_number": room.get("page_number"),
                    "components": [],
                }
                for item in room.get("items", []):
                    component = {
                        "area_name": room_name,
                        "component_name": self._normalize_text(item["item_name"]),
                        "start_of_tenancy": item.get("start_of_tenancy", {}),
                        "end_of_tenancy": item.get("end_of_tenancy", {}),
                    }
                    area["components"].append(component)
                areas.append(area)

            areas = self._postprocess_areas(areas)

            result = {
                "jurisdiction": self.jurisdiction,
                "document_type": self.detected_type,
                "detected_document_type": self.detected_type if self.report_type == "auto" else None,
                "report_metadata": self._build_metadata(),
                "areas": areas,
                "statutory": self._extract_statutory(full_text),
                "other_sections": {
                    "additional_comments": self._extract_additional_comments(full_text),
                    "maintenance_dates": self._extract_maintenance_dates(full_text),
                    "landlord_promise": self._extract_landlord_promise(full_text),
                    "signatures": self._extract_signatures(full_text),
                },
                "images": self._extract_images(output_dir, save_images, embed_images),
            }

            # The NT form's final page (Communication Facilities, Other
            # Miscellaneous, work-done dates, Landlord's Guarantee, and the
            # Ingoing/Outgoing Condition Verified signature blocks) is not a
            # condition grid, so it needs its own field set.
            if self.jurisdiction == "NT" and self._is_nt_rotated_grid():
                result["other_sections"]["nt_final_page"] = \
                    self._extract_nt_final_page()

            return result
        finally:
            self.fitz_doc.close()
            self.plumber_pdf.close()

    def _get_full_text(self):
        texts = []
        for page in self.fitz_doc:
            texts.append(page.get_text())
        return "\n".join(texts)

    def _detect_report_type(self, text):
        text_lower = text.lower()
        has_start = any(kw in text_lower for kw in ["start of tenancy", "commencement", "move in", "ingoing"])
        has_end = any(kw in text_lower for kw in ["end of tenancy", "vacating", "move out", "outgoing"])

        if has_start and has_end:
            return "combined"
        elif has_end:
            return "move_out"
        elif has_start:
            return "move_in"
        return "combined"

    def _build_metadata(self):
        return {
            "address": self._extract_address(),
            "postcode": self._extract_postcode(),
            "report_number": self._extract_report_number(),
            "tenant_name": self._extract_field_value(["tenant", "tenant name", "tenant/s", "tenants", "full name of renter"]),
            "landlord_name": self._extract_field_value(["landlord", "landlord name", "landlord/agent", "agent", "lessor", "rental provider"]),
            "property_manager": self._extract_field_value(["property manager", "managing agent", "agent's company"]),
            "bond_number": self._extract_field_value(["bond number", "bond no"]),
            "date_received": self._extract_date_received(),
            "start_date": self._extract_tenancy_date("start"),
            "end_date": self._extract_tenancy_date("end"),
            "source_file": os.path.basename(self.pdf_path),
            "total_pages": len(self.fitz_doc),
            "extraction_timestamp": datetime.now(timezone.utc).isoformat(),
            "extractor_version": VERSION,
        }

    def _extract_address(self):
        # These forms put the label and value on separate lines, e.g.
        #   "Address of rental premises:" \n "19 Van Kleef Circuit, Manly 2095"
        labels = [
            "address of rental premises",
            "address of the premises",
            "address of premises",
            "premises address",
            "rental premises",
            "property address",
            "address",
        ]
        val = self._value_for_labels(labels, pages=3)
        if val and len(val) > 3 and not re.match(r'^[YN\s/|]+$', val):
            return val
        return None

    def _extract_postcode(self):
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            match = re.search(r"[Pp]ostcode[:\s]*(\d{4})", text)
            if match:
                return match.group(1)
        # Fall back to a 4-digit postcode at the end of the address line.
        addr = self._extract_address()
        if addr:
            m = re.search(r'(\d{4})\b\s*$', addr)
            if m:
                return m.group(1)
        return None

    def _extract_report_number(self):
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            for pattern in [
                r"(?:Report|Reference|Ref)\s*(?:No|Number|#|:)\s*[:\s]*([A-Z0-9][\w\-/]+)",
                r"(?:Report)\s*(?:ID)\s*[:\s]*([A-Z0-9][\w\-/]+)",
            ]:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    val = match.group(1).strip()
                    if len(val) > 2:
                        return val
        return None

    SKIP_VALUE_WORDS = [
        "must", "should", "indicate", "landlord or", "the tenant",
        "record contact", "before", "after", "sign", "agrees",
        "comments", "condition", "premises", "report",
        "/agent", "trading", "initial", "initials", "name:", "date:",
        "postcode", "occupant", "grantor", "commencement", "names",
        "renter", "lessor",
    ]

    def _valid_field_value(self, val):
        if not val:
            return False
        val = val.strip()
        if len(val) < 2 or len(val) > 80:
            return False
        # A "!" is a grid space-artifact from a rotated form (e.g. the NT form's
        # embedded font decodes spaces as "!"), never part of a genuine name or
        # address value - so a grid header like "LANDLORD!" is not a value.
        if "!" in val:
            return False
        if re.match(r'^[YN\s/|:.\-]+$', val):
            return False
        # A list marker like "1." / "2)" is not a value.
        if re.match(r'^\d+[.)]?$', val):
            return False
        # Values don't contain colons and don't start with punctuation - those
        # are leftover labels ("/Occupant Names:", "Note: ...").
        if ":" in val or not val[0].isalnum():
            return False
        # Names / addresses start with a capital letter or a digit; a leading
        # lowercase word is almost always leaked instruction text ("within 3...").
        if val[0].isalpha() and not val[0].isupper():
            return False
        low = val.lower()
        # URLs / emails are never a name or address value.
        if any(tok in low for tok in ("www.", "http", "@", ".org", ".gov", ".com.au", ".com")):
            return False
        # A bare form label / condition word is not a value.
        if low in ("initial", "initials", "name", "date", "n/a", "na",
                   "clean", "undamaged", "working", "commencement", "note",
                   "landlord", "tenant", "tenants", "agrees", "ingoing",
                   "outgoing", "comments", "tenant agrees", "landlord comments",
                   "tenant comments"):
            return False
        if any(sw in low for sw in self.SKIP_VALUE_WORDS):
            return False
        # A blank field caption (e.g. the VIC form prints "Full name 1",
        # "Full name of renter 2", "Agent's company name" as empty-field labels)
        # is not a value - a real entry would be an actual name.
        if re.match(r"^(full name|first name|last name|given name|surname"
                    r"|name of (renter|tenant|landlord)|agent.?s)\b", low):
            return False
        return True

    def _is_label_like(self, line, label):
        """True if `line` is a dedicated field label (so the value is on the
        next line), not a sentence that merely happens to contain `label`."""
        stripped = line.rstrip()
        if stripped.endswith(":"):
            return True
        # e.g. "Tenants Name" / "Name of Landlord" - short, label plus a word.
        return len(stripped) <= len(label) + 10

    def _value_for_labels(self, labels, pages=5):
        """Return the value for a labelled field. Handles both inline values
        ("Label: value") and the common case where the value sits on the next
        line ("Label:" then "value")."""
        for page in self.fitz_doc[:pages]:
            text = page.get_text()
            tu = text.upper()
            if "HOW TO COMPLETE" in tu or "EXAMPLE" in tu:
                continue
            lines = [ln.strip() for ln in text.split("\n")]
            for i, line in enumerate(lines):
                low = line.lower()
                for label in labels:
                    if label not in low:
                        continue
                    # Inline value after the label (and optional colon).
                    m = re.search(re.escape(label) + r"[^\S\n]*:?[^\S\n]*(.*)$",
                                  line, re.IGNORECASE)
                    inline = m.group(1).strip() if m else ""
                    if self._valid_field_value(inline):
                        return inline
                    # Otherwise take the next non-empty line - but only if this
                    # line is a real field label, not a sentence containing the word.
                    if not self._is_label_like(line, label):
                        continue
                    for nxt in lines[i + 1:i + 3]:
                        if not nxt:
                            continue
                        if self._valid_field_value(nxt):
                            return nxt
                        break
        return None

    def _extract_field_value(self, field_names):
        return self._value_for_labels(field_names, pages=5)

    # A date as dd/mm/yyyy OR "29 Aug 2023" / "29 August 2023".
    _DATE_RX = (r"(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}"
                r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"[a-z]*\.?\s+\d{2,4})")

    def _extract_tenancy_date(self, which="start"):
        keywords = {
            "start": ["commencement date", "commencement", "lease start",
                      "move in date", "ingoing date", "start of tenancy"],
            "end": ["end of tenancy", "termination", "lease end",
                    "move out date", "vacating date"],
        }
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            for kw in keywords.get(which, []):
                # Bounded gap so we only pick a date that sits right next to the
                # label (not an unrelated date elsewhere on the page).
                match = re.search(
                    rf"{re.escape(kw)}.{{0,40}}?{self._DATE_RX}",
                    text, re.IGNORECASE | re.DOTALL
                )
                if match:
                    return match.group(1).strip()
        return None

    def _extract_date_received(self):
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            match = re.search(
                r"(?:RECEIVED|COPY.*?DATE).*?(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})",
                text, re.IGNORECASE | re.DOTALL
            )
            if match:
                return match.group(1).strip()
        return None

    # Tokens that mark a row as metadata / signature noise rather than a real
    # condition area. Used only to strip EMPTY misread rows - never to reject a
    # populated area, so genuine (even unusually named) areas always pass.
    _META_AREA_TOKENS = {
        "date", "signature", "signatures", "meter", "reading", "commencement",
        "inspector", "completed", "witness", "page", "vacate",
    }

    # Words that are always inspection *items*, never area headers. When a row
    # with these names has no Y/N marks (an unfilled item) it must not be
    # mistaken for a new area. Deliberately excludes words that can be real
    # areas somewhere (toilet, ensuite, garage, laundry, balcony, shed).
    _COMPONENT_ONLY = {
        "door", "doors", "doorway", "doorways", "window", "windows", "wall",
        "walls", "ceiling", "ceilings", "floor", "floors", "flooring", "blind",
        "blinds", "curtain", "curtains", "skirting", "light", "lights",
        "lighting", "powerpoint", "powerpoints", "point", "points", "switch",
        "switches", "tap", "taps", "sink", "oven", "stove", "hotplate",
        "hotplates", "griller", "rangehood", "dishwasher", "shower", "basin",
        "mirror", "vanity", "cupboard", "cupboards", "drawer", "drawers",
        "bench", "benchtop", "benchtops", "screen", "screens", "architrave",
        "architraves", "bricks", "driveway", "paving",
    }

    @classmethod
    def _is_component_word(cls, name):
        n = re.sub(r"[^a-z ]", "", (name or "").lower())
        return n.replace(" ", "") in cls._COMPONENT_ONLY

    @staticmethod
    def _area_name_is_junk(name):
        """A name with no letters (e.g. '/ /', '- -', '   ') is not an area."""
        s = (name or "").strip()
        return len(s) < 2 or not re.search(r"[A-Za-z]", s)

    def _postprocess_areas(self, areas):
        """Flexible clean-up of the detected areas. Removes obvious noise only -
        it never enforces a fixed schema, so real-world reports with unexpected
        area names still come through:
          * drop junk names (punctuation / blank),
          * collapse duplicate area headers (keep the populated instance),
          * drop empty rows whose name is clearly metadata/signature text.
        """
        kept = [a for a in areas if not self._area_name_is_junk(a.get("area_name", ""))]

        best, order = {}, []
        for a in kept:
            key = re.sub(r"\s+", " ", a["area_name"].strip().lower())
            if key not in best:
                best[key] = a
                order.append(key)
            elif len(a.get("components", [])) > len(best[key].get("components", [])):
                best[key] = a
        deduped = [best[k] for k in order]

        def is_meta_noise(a):
            if a.get("components"):
                return False
            toks = set(re.findall(r"[a-z]+", a["area_name"].lower()))
            return bool(toks & self._META_AREA_TOKENS)

        return [a for a in deduped if not is_meta_noise(a)]

    @staticmethod
    def _nt_normalize(s):
        """The NT form's embedded font decodes the space glyph as !, ' or *
        (and sometimes . on the last page). Collapse those back to spaces so
        the text reads normally."""
        if not s:
            return s
        return re.sub(r"[!'*]+", " ", s)

    def _extract_rooms_nt(self):
        """Northern Territory ("Condition Report - Northern Territory",
        Residential Tenancies Act 2013).

        The NT form is a single rotated (landscape) grid - the condition
        columns (Clean / Undamaged / Working / Tenant agrees / Landlord &
        Tenant comments) run sideways, so pdfplumber reads the grid transposed
        and the generic table logic does not apply. We locate each area from
        the (clean, well-ordered) text stream and list its items from the known
        NT template so the full structure comes through for every area.
        """
        room_config = ROOM_CONFIGS["NT"]

        page_norm = {}
        for i, page in enumerate(self.fitz_doc):
            page_norm[i] = self._nt_normalize(page.get_text()).upper()

        rooms = []
        for room_name, items in room_config.items():
            page_idx = None
            for i in sorted(page_norm):
                if room_name in page_norm[i]:
                    page_idx = i
                    break
            rooms.append({
                "room_name": room_name,
                "page_number": (page_idx + 1) if page_idx is not None else None,
                "items": [self._build_empty_item(it) for it in items],
            })
        return rooms

    def _extract_nt_final_page(self):
        """Fields from the NT form's final (non-grid) page.

        This is the statutory / verification page - Communication Facilities,
        Other Miscellaneous, "approximate dates when work was last done", the
        Landlord's Guarantee to Undertake Work, and the Ingoing / Outgoing
        Condition Verified signature blocks (Landlord + up to four tenants,
        each with signature / date / print name). On a blank template every
        value is null; the structure lets ORBAS build the fill-in form.

        Values are read best-effort from the page text - the NT font decodes
        spaces as "$" or ")", so we normalise before searching. Anything not
        present stays null.
        """
        # Locate and normalise the final-page text.
        page_text = ""
        for i in range(len(self.fitz_doc) - 1, -1, -1):
            t = self.fitz_doc[i].get_text()
            if "COMMUNICATION" in t.upper() and "VERIFIED" in t.upper():
                page_text = t
                break
        norm = re.sub(r"[!'$)(*]+", " ", page_text)
        norm = re.sub(r"[ \t]+", " ", norm)

        def _dotted(label):
            """Return text after `label` up to the fill line, or None if the
            field is blank (only dots / underscores / whitespace follow)."""
            m = re.search(re.escape(label) + r"\s*:?\s*(.*)", norm, re.IGNORECASE)
            if not m:
                return None
            val = m.group(1).strip()
            val = re.sub(r"[._…\-@/]+", "", val).strip()
            return val or None

        def _sig_block(tenant_no=None):
            b = {"signature": None, "print_name": None, "date": None}
            if tenant_no is not None:
                return {"tenant_no": tenant_no, **b}
            return b

        def _verified():
            return {
                "landlord": _sig_block(),
                "tenants": [_sig_block(n) for n in (1, 2, 3, 4)],
            }

        return {
            "communication_facilities": {
                "telephone_connected": None,   # Yes / No
                "internet_connected": None,    # Yes / No
            },
            "other_miscellaneous": {
                "water_meter_reading": _dotted("Water meter reading"),
                "water_tank_level": _dotted("Water Tank level"),
                "gas_bottle_heating_oil_tank_levels":
                    _dotted("Gas Bottle/heating oil tank levels"),
                "furniture_reference": None,   # see attached list
            },
            "approximate_work_dates": {
                "carpets_age": _dotted("Approximate age of carpets"),
                "carpets_professionally_cleaned":
                    _dotted("Date carpets professionally cleaned"),
                "window_coverings_age":
                    _dotted("Approximate age of window coverings"),
                "painting_external": None,
                "painting_internal": None,
            },
            "landlord_guarantee_to_undertake_work": {
                "work_to_undertake": None,
                "complete_work_by": None,
                "landlord_signature": None,
                "landlord_date": None,
            },
            "ingoing_condition_verified": _verified(),
            "outgoing_condition_verified": _verified(),
        }

    def _is_nt_rotated_grid(self):
        """True for the official "Condition Report - Northern Territory"
        (Residential Tenancies Act 2013) form - a single rotated grid whose
        embedded font decodes spaces as "!". Other NT layouts (e.g. software-
        generated PIM reports with a normal top-to-bottom table) are handled by
        the generic parser instead."""
        raw = self._get_full_text()
        if raw.count("!") >= 15:
            return True
        norm = self._nt_normalize(raw).upper()
        return "INGOING CONDITION REPORT" in norm and "INSERT Y" in norm

    def _extract_rooms(self):
        if self.jurisdiction == "NT":
            if self._is_nt_rotated_grid():
                return self._extract_rooms_nt()
            return self._extract_rooms_generic()

        room_config = ROOM_CONFIGS.get(self.jurisdiction, {})
        if not room_config:
            return self._extract_rooms_generic()

        structured = self._extract_rooms_structured(room_config)

        total = 0
        filled = 0
        for room in structured:
            for item in room.get("items", []):
                total += 1
                sot = item.get("start_of_tenancy", {})
                if sot.get("clean") or sot.get("undamaged") or sot.get("working"):
                    filled += 1

        if total > 0 and filled / total < 0.3:
            generic = self._extract_rooms_generic()
            g_total = 0
            g_filled = 0
            for room in generic:
                for item in room.get("items", []):
                    g_total += 1
                    sot = item.get("start_of_tenancy", {})
                    if sot.get("clean") or sot.get("undamaged") or sot.get("working"):
                        g_filled += 1
            if g_total > 0 and g_filled > filled:
                return generic

        return structured

    def _extract_rooms_structured(self, room_config):
        rooms = []

        page_texts = {}
        page_lines = {}
        for i, page in enumerate(self.fitz_doc):
            text = page.get_text()
            page_texts[i] = text
            page_lines[i] = text.split("\n")

        room_locations = self._find_room_locations(page_texts, room_config)

        page_drawings = {}
        for page_idx in set(loc["page"] for loc in room_locations.values()):
            page = self.fitz_doc[page_idx]
            page_drawings[page_idx] = page.get_drawings()

        page_tables = {}
        for page_idx in set(loc["page"] for loc in room_locations.values()):
            plumber_page = self.plumber_pdf.pages[page_idx]
            page_tables[page_idx] = plumber_page.extract_tables() or []

        for room_name, expected_items in room_config.items():
            if room_name not in room_locations:
                rooms.append(self._build_empty_room(room_name, expected_items))
                continue

            loc = room_locations[room_name]
            page_idx = loc["page"]

            items = self._extract_room_items_from_text(
                page_lines.get(page_idx, []),
                page_texts.get(page_idx, ""),
                room_name,
                expected_items,
                page_tables.get(page_idx, []),
                page_drawings.get(page_idx, []),
                page_idx,
            )

            rooms.append({
                "room_name": room_name,
                "page_number": page_idx + 1,
                "items": items,
            })

        return rooms

    def _find_room_locations(self, page_texts, room_config):
        locations = {}
        skip_pages = set()
        for page_idx, text in page_texts.items():
            text_upper = text.upper()
            if "EXAMPLE" in text_upper or "HOW TO COMPLETE" in text_upper:
                skip_pages.add(page_idx)

        condition_pages = set()
        for page_idx, text in page_texts.items():
            if page_idx in skip_pages:
                continue
            text_upper = text.upper()
            if "CONDITION OF PREMISES" in text_upper or "Y   N" in text or "Y N" in text:
                condition_pages.add(page_idx)

        for page_idx in sorted(condition_pages):
            text = page_texts[page_idx]
            text_upper = text.upper()
            for room_name in room_config:
                if room_name in text_upper and room_name not in locations:
                    match = re.search(re.escape(room_name), text_upper)
                    if match:
                        locations[room_name] = {
                            "page": page_idx,
                            "offset": match.start(),
                        }

        for room_name in room_config:
            if room_name not in locations:
                for page_idx in sorted(page_texts.keys()):
                    if page_idx in skip_pages:
                        continue
                    if room_name in page_texts[page_idx].upper():
                        locations[room_name] = {"page": page_idx, "offset": 0}
                        break

        return locations

    def _extract_room_items_from_text(self, lines, page_text, room_name,
                                      expected_items, tables, drawings, page_idx):
        items = []
        text_upper = page_text.upper()

        room_start = text_upper.find(room_name)
        if room_start == -1:
            return [self._build_empty_item(item_name) for item_name in expected_items]

        next_room_names = list(ROOM_CONFIGS.get(self.jurisdiction, {}).keys())
        try:
            current_idx = next_room_names.index(room_name)
        except ValueError:
            current_idx = -1

        room_end = len(page_text)
        if current_idx >= 0:
            for next_name in next_room_names[current_idx + 1:]:
                next_pos = text_upper.find(next_name, room_start + len(room_name))
                if next_pos > room_start:
                    room_end = next_pos
                    break

        room_text = page_text[room_start:room_end]
        room_text_lower = room_text.lower()

        matched_tables = self._find_matching_table_rows(tables, expected_items)

        for item_name in expected_items:
            item_data = self._build_empty_item(item_name)

            if item_name in matched_tables:
                table_row = matched_tables[item_name]
                item_data = self._parse_table_row_for_item(item_name, table_row)

            # Comments are only extracted from table data, not raw text
            # (raw text comment extraction picks up subsequent item names)

            items.append(item_data)

        return items

    def _is_header_row(self, row):
        non_empty = [str(c).strip() for c in row if c and str(c).strip()]
        if not non_empty:
            return True
        if all(v in ('Y', 'N', 'Y N') for v in non_empty):
            return True
        if any('Landlord' in v or 'Tenant' in v or 'Clean' in v or 'Undamaged' in v
               or 'Working' in v or 'Condition of' in v for v in non_empty):
            return True
        return False

    def _find_matching_table_rows(self, tables, expected_items):
        matched = {}
        item_set = {name.lower(): name for name in expected_items}

        for table in tables:
            for row in table:
                if not row:
                    continue
                if self._is_header_row(row):
                    continue

                for col_idx in range(min(2, len(row))):
                    if not row[col_idx]:
                        continue
                    cell_text = str(row[col_idx]).strip().lower()
                    cell_text = re.sub(r'\s+', ' ', cell_text.replace('\n', ' '))
                    if len(cell_text) < 3:
                        continue

                    for item_lower, item_name in item_set.items():
                        if item_name in matched:
                            continue
                        item_normalized = re.sub(r'\s+', ' ', item_lower)
                        if item_normalized == cell_text:
                            matched[item_name] = row
                            break
                        if len(cell_text) >= 5 and (item_normalized in cell_text or cell_text in item_normalized):
                            matched[item_name] = row
                            break

        return matched

    def _parse_table_row_for_item(self, item_name, row):
        item_data = self._build_empty_item(item_name)
        cells = [str(c).strip() if c else "" for c in row]

        skip_col = 0
        item_lower = item_name.lower().replace('/', ' ').replace('-', ' ')

        yn_cells = []
        comment_cells = []

        for i, cell in enumerate(cells):
            cell_clean = cell.replace('\n', ' ').strip()
            cell_check = cell_clean.lower().replace('/', ' ').replace('-', ' ')

            if i <= 1 and (cell_check in item_lower or item_lower in cell_check):
                skip_col = i
                continue
            if i <= skip_col:
                continue

            if not cell_clean:
                continue
            elif cell_clean in ('Y', 'N'):
                yn_cells.append((i, cell_clean))
            elif re.match(r'^[YN]\s*$', cell_clean):
                yn_cells.append((i, cell_clean.strip()))
            elif self._looks_like_checkbox(cell_clean):
                yn_cells.append((i, self._parse_yn(cell_clean)))
            elif len(cell_clean) > 2 and not re.match(r'^[YN\s]+$', cell_clean):
                if cell_check not in item_lower and item_lower not in cell_check:
                    comment_cells.append((i, cell_clean))

        mid = len(cells) // 2
        start_yn = [v for idx, v in yn_cells if v is not None and idx < mid]
        end_yn = [v for idx, v in yn_cells if v is not None and idx >= mid]

        if len(start_yn) >= 3:
            item_data["start_of_tenancy"]["clean"] = start_yn[0]
            item_data["start_of_tenancy"]["undamaged"] = start_yn[1]
            item_data["start_of_tenancy"]["working"] = start_yn[2]
        if len(start_yn) >= 4:
            item_data["start_of_tenancy"]["tenant_agrees"] = start_yn[3]

        if len(end_yn) >= 3:
            item_data["end_of_tenancy"]["clean"] = end_yn[0]
            item_data["end_of_tenancy"]["undamaged"] = end_yn[1]
            item_data["end_of_tenancy"]["working"] = end_yn[2]
        if len(end_yn) >= 4:
            item_data["end_of_tenancy"]["tenant_agrees"] = end_yn[3]

        if comment_cells:
            for idx, comment in comment_cells:
                if idx < mid:
                    if not item_data["start_of_tenancy"]["landlord_comments"]:
                        item_data["start_of_tenancy"]["landlord_comments"] = comment
                    elif not item_data["start_of_tenancy"]["tenant_comments"]:
                        item_data["start_of_tenancy"]["tenant_comments"] = comment
                else:
                    if not item_data["end_of_tenancy"]["comments"]:
                        item_data["end_of_tenancy"]["comments"] = comment

        return item_data

    def _detect_checkboxes_from_text(self, text):
        results = {}
        lines = text.strip().split('\n')

        yn_sequence = []
        for line in lines[:10]:
            line = line.strip()
            if line == 'Y' or line == 'N':
                yn_sequence.append(line)
            elif re.match(r'^[YN]\s+[YN]$', line):
                pass

        has_checked = False
        for val in yn_sequence:
            if val in ('Y', 'N'):
                has_checked = True
                break

        if not has_checked:
            return results

        if len(yn_sequence) >= 6:
            all_same_pairs = True
            for i in range(0, min(6, len(yn_sequence)), 2):
                if i + 1 < len(yn_sequence):
                    if yn_sequence[i] == yn_sequence[i + 1]:
                        all_same_pairs = False
                        break
            if not all_same_pairs:
                return results

        return results

    def _extract_inline_comment(self, text):
        room_config = ROOM_CONFIGS.get(self.jurisdiction, {})
        all_item_parts = set()
        for items in room_config.values():
            for item in items:
                all_item_parts.add(item.lower())
                for part in re.split(r'[/,]', item.lower()):
                    part = part.strip()
                    if len(part) > 2:
                        all_item_parts.add(part)

        lines = text.strip().split('\n')
        for line in lines[:5]:
            line = line.strip()
            if line and len(line) > 3 and line not in ('Y', 'N', 'Y N'):
                if re.match(r'^[YN\s]+$', line):
                    continue
                line_lower = line.lower().replace('\n', ' ').strip()
                if line_lower in all_item_parts:
                    continue
                if any(line_lower in item or item in line_lower for item in all_item_parts):
                    continue
                return line
        return None

    def _looks_like_checkbox(self, text):
        return bool(re.match(r'^[✓✔☑☒☐YN\s]+$', text))

    def _parse_yn(self, value):
        if not value:
            return None
        value = str(value).strip().upper()
        if value in ("Y", "YES", "✓", "✔", "☑"):
            return "Y"
        if value in ("N", "NO", "☒", "X"):
            return "N"
        if "✓" in value or "✔" in value:
            return "Y"
        return None

    def _build_empty_item(self, item_name):
        return {
            "item_name": item_name,
            "start_of_tenancy": {
                "clean": None, "undamaged": None, "working": None,
                "landlord_comments": None, "tenant_agrees": None, "tenant_comments": None,
            },
            "end_of_tenancy": {
                "clean": None, "undamaged": None, "working": None,
                "comments": None, "tenant_agrees": None,
            },
        }

    def _build_empty_room(self, room_name, expected_items):
        return {
            "room_name": room_name,
            "page_number": None,
            "items": [self._build_empty_item(name) for name in expected_items],
        }

    def _extract_rooms_generic(self):
        rooms = []
        current_room = None

        for i, page in enumerate(self.plumber_pdf.pages):
            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                # Skip narrow key/value tables (metadata like "Vacate Date | ...").
                # Condition grids are wide (Clean/Undamaged/Working/comment cols).
                if not table or max((len(r) for r in table), default=0) < 4:
                    continue
                for row in table:
                    if not row or not row[0]:
                        continue
                    first_cell = str(row[0]).strip().replace('\n', ' ')
                    if not first_cell or len(first_cell) > 100:
                        continue
                    if any(skip in first_cell.upper() for skip in [
                        'CONDITION', 'ADDRESS', 'LANDLORD', 'TENANT', 'CLEAN',
                        'UNDAMAGED', 'WORKING', 'Y N', 'LESSOR', 'DATE:',
                        'RESIDENTIAL TENANCIES', 'SCHEDULE',
                    ]):
                        continue

                    has_yn = any(
                        str(c).strip() in ('Y', 'N')
                        for c in row[1:] if c
                    )

                    if not has_yn and len(first_cell) > 2:
                        # An unfilled component row (e.g. "Door" with no Y/N) is
                        # an item without a recorded condition - not a new area.
                        if self._is_component_word(first_cell) and rooms:
                            rooms[-1]["items"].append(
                                self._parse_table_row_for_item(first_cell, row))
                            continue
                        current_room = first_cell
                        rooms.append({
                            "room_name": current_room,
                            "page_number": i + 1,
                            "items": [],
                        })
                        continue

                    if has_yn:
                        item_data = self._parse_table_row_for_item(first_cell, row)
                        if not rooms:
                            rooms.append({
                                "room_name": self._guess_room_name(
                                    self.fitz_doc[i].get_text(), i),
                                "page_number": i + 1,
                                "items": [],
                            })
                        rooms[-1]["items"].append(item_data)

        return rooms

    def _guess_room_name(self, text, page_idx):
        common_rooms = [
            "ENTRANCE", "HALL", "LOUNGE", "LIVING", "DINING", "KITCHEN",
            "BEDROOM", "BATHROOM", "ENSUITE", "LAUNDRY", "GARAGE",
            "GENERAL", "SECURITY", "SAFETY",
        ]
        text_upper = text.upper()
        for room in common_rooms:
            if room in text_upper:
                return room
        return f"PAGE_{page_idx + 1}"

    def _extract_compliance(self, text):
        return {
            "minimum_standards": {
                "structurally_sound": self._find_yes_no(text, "structurally sound"),
                "adequate_lighting": self._find_yes_no(text, "natural or artificial lighting"),
                "adequate_ventilation": self._find_yes_no(text, "ventilation"),
                "adequate_power": self._find_yes_no(text, "electricity outlet sockets"),
                "adequate_plumbing": self._find_yes_no(text, "plumbing and drainage"),
                "adequate_bathroom": self._find_yes_no(text, "bathroom facilities"),
                "tenant_agrees": self._find_yes_no(text, r"Does the tenant agree with all of the above"),
                "tenant_disagreement_details": None,
            },
            "health_issues": {
                "mould_dampness": self._find_yes_no(text, "mould and dampness"),
                "pests_vermin": self._find_yes_no(text, "pests and vermin"),
                "rubbish": self._find_yes_no(text, "rubbish"),
                "asbestos_register": self._find_yes_no(text, "[Aa]sbestos"),
            },
            "smoke_alarms": {
                "installed": self._find_yes_no(text, "smoke alarms been installed"),
                "checked_working": self._find_yes_no(text, "checked and found to be in working"),
                "date_last_checked": self._find_date_after(text, "Date last checked"),
                "batteries_replaced": self._find_yes_no(text, "removable batteries.*been replaced"),
                "date_batteries_changed": self._find_date_after(text, "Date batteries were last changed"),
                "lithium_batteries_replaced": self._find_yes_no(text, "removable lithium"),
                "date_lithium_changed": None,
            },
            "safety_issues": {
                "damaged_appliances": self._find_yes_no(text, "damaged appliances"),
                "electrical_hazards": self._find_yes_no(text, "hazards relating to electricity"),
                "gas_hazards": self._find_yes_no(text, "hazards relating to gas"),
                "tenant_agrees": None,
                "tenant_disagreement_details": None,
            },
        }

    def _extract_statutory(self, text):
        """Dedicated Statutory section - the legislated questions, kept separate
        from the room-by-room condition areas. Same six sub-sections for every
        jurisdiction (fields that don't exist on a given form stay null)."""
        compliance = self._extract_compliance(text)
        utilities = self._extract_utilities(text)
        water = self._extract_water_efficiency(text)

        minimum = dict(compliance.get("minimum_standards", {}))
        minimum["utilities"] = {
            "electricity_supplied": utilities.get("electricity"),
            "gas_supplied": utilities.get("gas"),
            "water_supplied": utilities.get("water_supply"),
        }
        return {
            "minimum_standards": minimum,
            "health_issues": compliance.get("health_issues", {}),
            "smoke_alarms": compliance.get("smoke_alarms", {}),
            "other_safety_issues": compliance.get("safety_issues", {}),
            "communication_facilities": {
                "telephone_connected": utilities.get("telephone"),
                "internet_connected": utilities.get("internet"),
            },
            "water_usage_and_efficiency": water,
        }

    def _extract_utilities(self, text):
        return {
            "telephone": self._find_yes_no(text, "telephone line"),
            "internet": self._find_yes_no(text, "internet line"),
            "electricity": self._find_yes_no(text, "supplied with electricity"),
            "gas": self._find_yes_no(text, "supplied with gas"),
            "water_supply": self._find_yes_no(text, "water supply"),
        }

    def _extract_water_efficiency(self, text):
        return {
            "separately_metered": self._find_yes_no(text, "separately metered"),
            "showerhead_compliant": self._find_yes_no(text, "showerheads.*maximum flow rate"),
            "toilet_compliant": self._find_yes_no(text, "toilets are dual flush"),
            "taps_compliant": self._find_yes_no(text, "cold water taps.*single mixer"),
            "leaks_fixed": self._find_yes_no(text, "leaking taps.*fixed"),
            "date_last_checked": self._find_date_after(text, "water efficiency measures"),
            "meter_reading_start": self._find_field(text, r"Water meter reading at START.*?(\d+)"),
            "meter_reading_start_date": self._find_date_after(text, "Water meter reading at START"),
            "meter_reading_end": self._find_field(text, r"Water meter reading at END.*?(\d+)"),
            "meter_reading_end_date": self._find_date_after(text, "Water meter reading at END"),
        }

    def _extract_additional_comments(self, text):
        match = re.search(
            r"ADDITIONAL COMMENTS\s*/?\s*INFORMATION\s*(.+?)(?:LANDLORD|APPROXIMATE|FURNITURE|PHOTOGRAPH)",
            text, re.DOTALL | re.IGNORECASE
        )
        if match:
            comment = match.group(1).strip()
            comment = re.sub(r"Additional comments on.*?devices\s*", "", comment, flags=re.DOTALL | re.IGNORECASE)
            comment = re.sub(r"\(may be added.*?\)", "", comment, flags=re.DOTALL | re.IGNORECASE)
            comment = comment.strip()
            if comment and len(comment) > 3:
                return comment
        return None

    def _extract_maintenance_dates(self, text):
        return {
            "smoke_alarm_maintenance": self._find_date_after(text, "(?:Installation|maintenance) (?:repair|of) (?:or maintenance )?of smoke alarms"),
            "external_painting": self._find_date_after(text, "Painting.*?external"),
            "internal_painting": self._find_date_after(text, "Painting.*?internal"),
            "flooring": self._find_date_after(text, "Flooring"),
        }

    def _extract_landlord_promise(self, text):
        match = re.search(
            r"LANDLORD.S PROMISE.*?WORK.*?(?:during the tenancy[:\s]*)(.+?)(?:The landlord agrees to complete|Landlord.agent.s signature)",
            text, re.DOTALL | re.IGNORECASE
        )
        if match:
            promise = match.group(1).strip()
            if promise and len(promise) > 3:
                return promise
        return None

    def _extract_signatures(self, text):
        return {
            "start_of_tenancy": {
                "landlord_date": self._find_date_after(text, "Condition Report at START.*?Date"),
                "tenant_date": None,
            },
            "end_of_tenancy": {
                "landlord_date": self._find_date_after(text, "Condition Report at END.*?Date"),
                "tenant_date": None,
            },
        }

    def _extract_images(self, output_dir=None, save_images=True, embed_data=False):
        # By default we record only lightweight image metadata (page, size, count).
        # Embedding full base64 image bytes here previously bloated the JSON to
        # 100+ MB on a photo-heavy report, which made the app slow to render and
        # painful to copy. The condition-report converter does not need the raw
        # image bytes, so embed_data stays False unless explicitly requested.
        images = []
        for page_idx, page in enumerate(self.fitz_doc):
            image_list = page.get_images(full=True)
            for img_idx, img_info in enumerate(image_list):
                xref = img_info[0]
                try:
                    pix = fitz.Pixmap(self.fitz_doc, xref)
                    if pix.n > 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)

                    if pix.width < 20 or pix.height < 20:
                        pix = None
                        continue

                    img_data = {
                        "page": page_idx + 1,
                        "index": img_idx,
                        "width": pix.width,
                        "height": pix.height,
                        "format": "png",
                        "data_base64": None,
                        "file_path": None,
                    }

                    img_bytes = pix.tobytes("png")

                    if save_images and output_dir:
                        os.makedirs(output_dir, exist_ok=True)
                        img_filename = f"page{page_idx + 1}_img{img_idx}.png"
                        img_path = os.path.join(output_dir, img_filename)
                        with open(img_path, "wb") as f:
                            f.write(img_bytes)
                        img_data["file_path"] = img_filename
                    elif embed_data:
                        img_data["data_base64"] = base64.b64encode(img_bytes).decode("utf-8")
                    # else: metadata only - keep data_base64 = None (small JSON)

                    images.append(img_data)
                    pix = None
                except Exception:
                    continue
        return images

    def _find_yes_no(self, text, pattern):
        try:
            match = re.search(pattern + r".*?(Yes|No|✓|✔|☑|☒)", text, re.IGNORECASE | re.DOTALL)
            if match:
                val = match.group(1).strip().lower()
                if val in ("yes", "✓", "✔", "☑"):
                    return "Yes"
                return "No"
        except re.error:
            pass
        return None

    def _find_date_after(self, text, pattern):
        try:
            match = re.search(pattern + r".*?(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})", text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
        except re.error:
            pass
        return None

    def _find_field(self, text, pattern):
        try:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
        except re.error:
            pass
        return None


def detect_jurisdiction(pdf_path):
    """Auto-detect Australian jurisdiction from PDF content."""
    doc = fitz.open(pdf_path)
    try:
        text = ""
        for i in range(min(4, len(doc))):
            text += doc[i].get_text() + "\n"
        # Normalise punctuation/whitespace to single spaces. Some PDFs extract
        # with apostrophes or asterisks between words (e.g. "Northern'Territory"),
        # which would otherwise defeat multi-word marker matching.
        text_lower = re.sub(r"[^a-z0-9]+", " ", text.lower())

        markers = {
            "NSW": ["new south wales", "nsw fair trading", "nsw government",
                     "residential tenancies act 2010"],
            "VIC": ["consumer affairs victoria", "rental provider",
                     "victorian civil and administrative tribunal"],
            "QLD": ["queensland", "residential tenancies authority",
                     "residential tenancies and rooming accommodation"],
            "SA": ["south australia", "consumer and business services",
                    "residential tenancies act 1995", "inspection sheet"],
            "WA": ["western australia", "commerce wa",
                    "residential tenancies act 1987"],
            "TAS": ["tasmania", "residential tenancy act",
                     "rental deposit authority"],
            "ACT": ["australian capital territory", "tenantsact.org.au",
                     "revenue.act.gov.au"],
            "NT": ["northern territory", "darwin nt", "nt entry", "nt exit",
                    "residential tenancies act 2013"],
        }

        def _norm(s):
            return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

        scores = {}
        for jur, keywords in markers.items():
            score = sum(1 for kw in keywords if _norm(kw) in text_lower)
            if score > 0:
                scores[jur] = score

        if scores:
            return max(scores, key=scores.get)
        return "NSW"
    finally:
        doc.close()


def detect_report_type_standalone(pdf_path):
    """Auto-detect report type from PDF content."""
    doc = fitz.open(pdf_path)
    try:
        text = ""
        for i in range(min(4, len(doc))):
            text += doc[i].get_text() + "\n"
        text_lower = text.lower()

        has_start = any(kw in text_lower for kw in
                        ["start of tenancy", "commencement", "move in", "ingoing", "entry condition"])
        has_end = any(kw in text_lower for kw in
                      ["end of tenancy", "vacating", "move out", "outgoing", "exit condition"])

        if has_start and has_end:
            return "combined"
        elif has_end:
            return "move_out"
        elif has_start:
            return "move_in"
        return "combined"
    finally:
        doc.close()


def extract_pdf(pdf_path, jurisdiction="NSW", report_type="auto", output_dir=None,
                save_images=True, embed_images=False):
    extractor = ConditionReportExtractor(pdf_path, jurisdiction, report_type)
    return extractor.extract(output_dir=output_dir, save_images=save_images,
                             embed_images=embed_images)
