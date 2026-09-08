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
from . import ocr as ocr_engine


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

# Phrases that mark a whole page as form INSTRUCTIONS rather than data. Kept
# deliberately narrow: these must not match a value a real report could carry.
_INSTRUCTION_PAGE_MARKERS = (
    "HOW TO COMPLETE",
    "EXAMPLE ONLY",
    "SAMPLE ONLY",
    "THIS IS AN EXAMPLE",
)

# Condition-grid column headings, mapped to the field they fill. The grids are
# read by NAME rather than by position because the column order is not stable:
# the official NSW blank form runs Clean/Undamaged/Working first, an agency
# report orders its END block differently from its own START block, and other
# forms put "Tenant agrees" ahead of Clean. Anything positional is wrong on at
# least one of them.
_HEADER_FIELDS = (
    ("tenant_agrees", ("tenant agrees", "tenant agree")),
    ("tenant_comments", ("tenant comments", "tenant comment")),
    ("landlord_comments", ("landlord", "agent comments", "lessor")),
    ("clean", ("clean",)),
    ("undamaged", ("undamaged", "undamage")),
    ("working", ("working",)),
    ("comments", ("comments", "comment")),
)

_ABBREVIATED_HEADERS = {
    "c": "clean",
    "u": "undamaged",
    "w": "working",
    "ta": "tenant_agrees",
}

# Column-0 headings that mean "this table lists items, and the area it belongs
# to is named above the table" rather than "column 0 holds the area name".
_ITEM_COLUMN_HEADS = {
    "item", "items", "component", "components", "description", "detail",
    "details", "area item", "item description",
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
        # OCR fallback state (populated lazily only for scanned/image PDFs).
        self._scanned = None          # tri-state: None until probed
        self._ocr_cache = {}          # page.number -> OCR text
        self._ocr_used = False        # True once any OCR text is actually used
        # Column maps recovered from a rotated heading strip, keyed by the
        # grid's column positions so a multi-page grid keeps reading correctly
        # on the continuation pages, which print no headings of their own.
        self._detached_map_cache = {}
        # Statutory checkbox rows and line geometry, per page.
        self._checkbox_cache = {}
        self._line_cache = {}
        # Master switch for the scanned/image OCR path. When False the extractor
        # is purely a digital-PDF reader (OCR never runs, no scanned_ticks /
        # ocr_status in the output). Digital reports are identical either way -
        # OCR only ever engages on pages with no text layer - but the switch lets
        # the OCR feature be set aside entirely while digital extraction is the
        # focus. Set from extract(enable_ocr=...).
        self._ocr_enabled = True

    # ------------------------------------------------------------------
    # Scanned-PDF (OCR) support
    # ------------------------------------------------------------------
    def _is_scanned(self):
        """True when this PDF is a scan/photo of a form with no text layer.

        Detected by: most pages carry a full-page raster but return (almost) no
        selectable text. Probed once and cached. When OCR is not available the
        document is treated as not-scanned so behaviour is unchanged.
        """
        if self._scanned is not None:
            return self._scanned
        if not self._ocr_enabled:
            self._scanned = False
            return False
        pages = list(self.fitz_doc)
        if not pages:
            self._scanned = False
            return False
        empty_image_pages = 0
        for page in pages:
            has_text = len(page.get_text().strip()) >= 20
            has_image = bool(page.get_images(full=True))
            if not has_text and has_image:
                empty_image_pages += 1
        # A scanned report: at least half its pages are image-only.
        looks_scanned = empty_image_pages >= max(1, len(pages) // 2)
        self._scanned = looks_scanned and ocr_engine.is_available()
        return self._scanned

    # A single raster covering essentially the whole page (with no text layer)
    # is a scanned form page - as opposed to a photo page, which carries several
    # smaller images. Only scanned form pages are OCR'd, so a digital report's
    # photo pages are never OCR'd and its behaviour/speed are unchanged.
    _SCANNED_PAGE_COV = 0.85

    def _is_scanned_page(self, page):
        """True when this individual page is a scanned image of a form."""
        if page.get_text().strip():
            return False
        page_area = abs(page.rect.width * page.rect.height)
        if not page_area:
            return False
        for im in page.get_images(full=True):
            for r in page.get_image_rects(im[0]):
                if abs(r.width * r.height) / page_area >= self._SCANNED_PAGE_COV:
                    return True
        return False

    def _has_scanned_page(self):
        """True when at least one page is a scanned form page and OCR is usable.
        Covers both fully-scanned and mixed (part digital, part scanned) PDFs."""
        if getattr(self, "_has_scanned_cache", None) is not None:
            return self._has_scanned_cache
        val = False
        if self._ocr_enabled and ocr_engine.is_available():
            val = any(self._is_scanned_page(p) for p in self.fitz_doc)
        self._has_scanned_cache = val
        return val

    def _file_format(self):
        """Classify the source as 'digital', 'scanned' or 'mixed' so the user
        knows immediately what kind of file they are extracting. A digital
        report with photo pages is still 'digital' - only full-page scanned
        form pages count as scanned."""
        if getattr(self, "_file_format_cache", None) is not None:
            return self._file_format_cache
        text_pages = 0
        scanned_pages = 0
        for p in self.fitz_doc:
            if p.get_text().strip():
                text_pages += 1
            elif self._is_scanned_page(p):
                scanned_pages += 1
        if scanned_pages == 0:
            fmt = "digital"
        elif text_pages == 0:
            fmt = "scanned"
        else:
            fmt = "mixed"
        self._file_format_cache = fmt
        return fmt

    def _page_text(self, page):
        """Selectable text for a page, or OCR'd text when the page is a scan.

        For normal digital PDFs this is just page.get_text(); the OCR path only
        engages for scanned (full-page image) pages - whether the whole document
        is scanned or only some pages are (a mixed PDF) - so a digital report's
        text and photo pages are completely unaffected.
        """
        native = page.get_text()
        if native.strip():
            return native
        if not self._ocr_enabled:
            return native
        if not self._is_scanned_page(page):
            return native
        idx = page.number
        if idx not in self._ocr_cache:
            self._ocr_cache[idx] = ocr_engine.ocr_page_text(page)
        text = self._ocr_cache[idx]
        if text.strip():
            self._ocr_used = True
        return text

    def extract(self, output_dir=None, save_images=True, embed_images=False,
                enable_ocr=True):
        self._ocr_enabled = enable_ocr
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

            # Only now are the recorded conditions available, so an auto
            # detection that saw a combined LAYOUT can be settled against what
            # was actually filled in.
            if self.report_type == "auto":
                self.detected_type = self._refine_report_type(self.detected_type, areas)

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

            # Scanned/image-only reports are read via OCR. Flag it, and always
            # carry the full OCR text per page so every value (comments etc.) is
            # available to the converter even where the grid can't be fully
            # rebuilt from a scan.
            result["ocr_used"] = self._ocr_used
            # Always report the OCR engine's status so a scanned PDF that comes
            # back empty is diagnosable (e.g. engine not found) instead of
            # silently yielding nulls.
            if self._ocr_enabled and (
                    self._file_format() in ("scanned", "mixed") or self._ocr_used):
                result["ocr_status"] = ocr_engine.status()
                # Best-effort Y/N tick reader for scanned condition grids, with a
                # per-cell confidence so nothing uncertain is trusted silently.
                ticks = self._extract_scanned_ticks()
                if ticks:
                    result["scanned_ticks"] = ticks
                    # Write the reads into each component's start_of_tenancy so
                    # the Y/N show directly in the converter grid (not only in the
                    # separate scanned_ticks section).
                    self._apply_scanned_ticks(result["areas"], ticks)
            if self._ocr_used:
                result["ocr_pages"] = [
                    {"page": idx + 1, "text": self._ocr_cache[idx]}
                    for idx in sorted(self._ocr_cache)
                    if self._ocr_cache[idx].strip()
                ]

            # The NT form's final page (Communication Facilities, Other
            # Miscellaneous, work-done dates, Landlord's Guarantee, and the
            # Ingoing/Outgoing Condition Verified signature blocks) is not a
            # condition grid - it is the form's statutory section. Surface it
            # under "statutory" so it renders in the Statutory Q&A view.
            if self.jurisdiction == "NT" and self._is_nt_rotated_grid():
                result["statutory"] = self._extract_nt_final_page()

            return result
        finally:
            self.fitz_doc.close()
            self.plumber_pdf.close()

    def _get_full_text(self):
        texts = []
        for page in self.fitz_doc:
            texts.append(self._page_text(page))
        return "\n".join(texts)

    def _detect_report_type(self, text):
        """Which blocks the FORM prints - not which ones were filled in.

        These keywords match the pre-printed column headings, so a combined
        NSW form answers "combined" whether or not either half carries data.
        That is the layout, and it is only half the answer - see
        _refine_report_type, which decides from the data itself.
        """
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

    # A side has genuinely been filled in only if it carries values on a real
    # share of the components. A bare count would let one misread cell in an
    # otherwise blank half flip the whole document's type.
    _SIDE_USED_MIN_ROWS = 3
    _SIDE_USED_MIN_SHARE = 0.05

    @staticmethod
    def _side_has_data(side):
        """True if an inspector actually recorded something on this side."""
        if not side:
            return False
        if side.get("clean") or side.get("undamaged") or side.get("working"):
            return True
        for key in ("comments", "tenant_comments", "tenant_agrees"):
            val = side.get(key)
            if isinstance(val, str) and val.strip():
                return True
            elif val:
                return True
        return False

    def _refine_report_type(self, layout, areas):
        """Turn the form's LAYOUT into what this document actually is.

        Agencies hand out one combined move-in/move-out form and use it three
        ways: fill the move-in half at the start, fill the move-out half at the
        end, or fill both. All three print identical headings, so keyword
        detection called every one of them "combined" - including a move-out
        report with the entire move-in half deliberately blank. Deciding from
        the recorded conditions instead separates them.
        """
        if layout != "combined":
            return layout

        total = start = end = 0
        for area in areas:
            for comp in area.get("components", []):
                total += 1
                if self._side_has_data(comp.get("start_of_tenancy")):
                    start += 1
                if self._side_has_data(comp.get("end_of_tenancy")):
                    end += 1
        if not total:
            return layout

        floor = max(self._SIDE_USED_MIN_ROWS, total * self._SIDE_USED_MIN_SHARE)
        used_start, used_end = start >= floor, end >= floor
        if used_start and used_end:
            return "combined"
        if used_end:
            return "move_out"
        if used_start:
            return "move_in"
        # A blank form of a combined layout is still a combined form.
        return layout

    @staticmethod
    def _clean_scanned_value(val):
        """Trim OCR/table noise off a scanned header value (border pipes, stray
        brackets, leftover label words) without altering the real content."""
        if not val:
            return None
        val = val.strip(" \t|[]()<>:;.,-_")
        # Drop a leftover label word that OCR merged onto the value.
        val = re.sub(r"^(PREMISES|DATE|NAME)\s*:?\s*", "", val, flags=re.IGNORECASE)
        val = re.sub(r"\s{2,}", " ", val).strip(" |[]():")
        return val or None

    def _scanned_header(self):
        """REINSW-style scanned condition reports print the address, tenant and
        commencement date across a single header row. OCR flattens that row to
        one line - parse it here by field boundaries. Returns {} for digital
        PDFs (which use the normal label parsing)."""
        if getattr(self, "_scanned_header_cache", None) is not None:
            return self._scanned_header_cache
        hdr = {}
        if self._has_scanned_page():
            for page in self.fitz_doc[:4]:
                found = False
                for line in self._page_text(page).split("\n"):
                    U = line.upper()
                    # Anchor on the two labels OCR reads most reliably on this
                    # row: "TENANT:" and "COMMENC(EMENT)". The "PREMISES:" label
                    # itself is often mangled by OCR, so we don't depend on it.
                    # Requiring both TENANT: and COMMENC avoids matching the
                    # instructional sentences that only mention these words.
                    if not (re.search(r"TENANT\s*:", U) and re.search(r"COMMENC", U)):
                        continue
                    # Address = the digit-led run just before "TENANT:" (e.g.
                    # "808/23 MAIN STREET WALLIS TOWN"), which skips any garbled
                    # "PREMISES:" label OCR left in front of it.
                    m = re.search(r"(\d[\w/].*?)\s*TENANT\s*:", line, re.IGNORECASE)
                    if not m:
                        m = re.search(r"PREMISES\s*:\s*(.*?)\s*TENANT\s*:",
                                      line, re.IGNORECASE)
                    addr = self._clean_scanned_value(m.group(1)) if m else None
                    if addr and re.match(r"^\d", addr) and re.search(r"[A-Za-z]", addr):
                        hdr["address"] = addr
                    # Tenant = between "TENANT:" and "(COMMENCEMENT".
                    m = re.search(r"TENANT\s*:\s*\|?\s*(.*?)\s*\(?\s*COMMENC",
                                  line, re.IGNORECASE) \
                        or re.search(r"TENANT\s*:\s*\|?\s*(.*)$", line, re.IGNORECASE)
                    if m:
                        tn = self._clean_scanned_value(m.group(1))
                        if tn and re.search(r"[A-Za-z]", tn):
                            hdr["tenant_name"] = tn
                    m = re.search(r"COMMENC\w*\s*(?:DATE)?\s*:?\s*\|?\s*"
                                  r"(\d{1,2}\s*[/ ]\s*\d{1,2}\s*[/ ]\s*\d{2,4})",
                                  line, re.IGNORECASE)
                    if m:
                        hdr["commencement"] = re.sub(r"\s*[/ ]\s*", "/",
                                                     m.group(1).strip())
                    if hdr.get("address") or hdr.get("tenant_name"):
                        found = True
                        break
                if found:
                    break
        self._scanned_header_cache = hdr
        return hdr

    @staticmethod
    def _norm_date(d):
        """Collapse OCR spacing inside a date ("04/08 /12" -> "04/08/12")."""
        if not d:
            return d
        return re.sub(r"\s*/\s*", "/", d).strip()

    def _build_metadata(self):
        sh = self._scanned_header()
        return {
            "address": self._extract_address() or sh.get("address"),
            "postcode": self._extract_postcode(),
            "report_number": self._extract_report_number(),
            # Precise form labels first. These are tried in order, so the exact
            # wording of the NSW Schedule 2 form wins over a bare "tenant" or
            # "landlord", which also occur in the explanatory prose.
            "tenant_name": (self._extract_numbered_tenants()
                            or self._extract_field_value([
                                "full name/s of the tenant/s", "full name of the tenant",
                                "full name/s of tenant/s", "name/s of the tenant/s",
                                "full name of renter", "tenant name", "tenant/s", "tenants",
                                "tenant"])
                            or sh.get("tenant_name")),
            "landlord_name": self._extract_field_value([
                "name of the lessor/agent", "name of lessor/agent",
                "name of the landlord/agent", "name of the landlord",
                "landlord name", "landlord/agent", "rental provider",
                "landlord", "lessor", "agent"]),
            "property_manager": self._extract_field_value(["property manager", "managing agent", "agent's company"]),
            "bond_number": self._extract_field_value(["bond number", "bond no"]),
            "date_received": self._extract_date_received(),
            "start_date": self._norm_date(self._extract_tenancy_date("start") or sh.get("commencement")),
            "end_date": self._norm_date(self._extract_tenancy_date("end")),
            "file_format": self._file_format(),
            "source_file": os.path.basename(self.pdf_path),
            "total_pages": len(self.fitz_doc),
            "extraction_timestamp": datetime.now(timezone.utc).isoformat(),
            "extractor_version": VERSION,
        }

    def _extract_numbered_tenants(self):
        """Join the tenants from forms that list them one per field ("Tenant 1",
        "Tenant 2"). Every one of them is a party to the tenancy, so returning
        only the first would drop a name the bond assessment needs."""
        names = []
        for n in range(1, 5):
            value = self._value_for_labels(["tenant %d" % n], pages=3)
            if value and value not in names:
                names.append(value)
        return " & ".join(names) if names else None

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
            return self._complete_address(val)
        return None

    # A bare 4-digit line under an address is its postcode.
    _POSTCODE_LINE = re.compile(r"^\d{4}$")

    def _complete_address(self, first_line):
        """Gather the rest of a stacked address.

        Agency systems print the address over three lines - street, then
        "Suburb, STATE", then the postcode on its own. Taking only the line
        after the label returned "510/3 George St" for a property in Warwick
        Farm NSW 2170, which is not enough to identify a property and left the
        postcode null as well.
        """
        for page in self.fitz_doc[:3]:
            lines = [ln.strip() for ln in self._page_text(page).split("\n")]
            try:
                i = lines.index(first_line)
            except ValueError:
                continue
            parts = [first_line]
            for nxt in lines[i + 1:i + 4]:
                if not nxt:
                    continue
                if self._POSTCODE_LINE.match(nxt):
                    parts.append(nxt)
                    break
                # A following label ends the address.
                if not self._valid_field_value(nxt) or self._is_label_like(nxt, ""):
                    break
                parts.append(nxt)
            if len(parts) == 1:
                continue
            # "Street, Suburb STATE 2170" - the postcode joins its own line
            # without a comma so the result reads as a normal address.
            out = ", ".join(parts[:-1]) if self._POSTCODE_LINE.match(parts[-1]) else ", ".join(parts)
            if self._POSTCODE_LINE.match(parts[-1]):
                out = f"{out} {parts[-1]}"
            return out
        return first_line

    def _extract_postcode(self):
        for page in self.fitz_doc[:3]:
            text = self._page_text(page)
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
            text = self._page_text(page)
            # The number word is required. Allowing a bare "Report:" made the
            # title line "Entry condition report: 510/3 George St, ..." yield a
            # report number of "510/3" - the unit and street number.
            for pattern in [
                r"(?:Report|Reference|Ref)\s*(?:No\.?|Number|#)\s*[:\s]*([A-Z0-9][\w\-/]+)",
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
        line ("Label:" then "value").

        Labels are tried in the order given, each across the whole document
        before the next is considered, so a precise label always beats a loose
        one. Scanning line-by-line and accepting whichever label happened to
        appear first let a prose sentence containing "landlord" outrank the
        actual "Name of the lessor/agent" field, and a real agency report came
        back with its landlord recorded as "Have the removable batteries in all
        the smoke alarms been".
        """
        for label in labels:
            found = self._value_for_one_label(label, pages)
            if found is not None:
                return found
        return None

    def _value_for_one_label(self, label, pages=5):
        for page in self.fitz_doc[:pages]:
            text = self._page_text(page)
            tu = text.upper()
            # Skip a genuine instruction page, but match the PHRASE. A bare
            # "EXAMPLE" also occurs in real values - "24 Example Street",
            # "Harbour Example Realty" - and skipping the whole page on that
            # threw away every field on it: address, both tenants, landlord,
            # manager and start date all came back null.
            if any(p in tu for p in _INSTRUCTION_PAGE_MARKERS):
                continue
            lines = [ln.strip() for ln in text.split("\n")]
            for i, line in enumerate(lines):
                if label not in line.lower():
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
                      "move in date", "ingoing date", "start of tenancy",
                      "tenancy start date", "tenancy start",
                      "agreement start date", "date tenancy commenced"],
            "end": ["end of tenancy", "termination", "lease end",
                    "move out date", "vacating date", "tenancy end date",
                    "tenancy end", "date tenancy ended", "vacate date",
                    "date of vacating", "move-out inspection",
                    "move out inspection"],
        }
        for page in self.fitz_doc[:3]:
            text = self._page_text(page)
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
            text = self._page_text(page)
            match = re.search(
                r"(?:RECEIVED|COPY.*?DATE).*?(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})",
                text, re.IGNORECASE | re.DOTALL
            )
            if match:
                # These forms lay the date out one component per line
                # ("26\n/\n11\n/ 2024"), which reached the JSON with its
                # newlines intact.
                return re.sub(r"\s+", "", match.group(1))
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
        total, filled = self._condition_fill(structured)

        if total > 0 and filled / total < 0.3:
            generic = self._extract_rooms_generic()
            g_total, g_filled = self._condition_fill(generic)
            if g_total > 0 and g_filled > filled:
                return generic

        return structured

    @staticmethod
    def _condition_fill(rooms):
        """(items, items carrying a condition) across BOTH ends of the tenancy.

        Counting only the start of tenancy made a move-out report - where the
        move-in half is blank by design - score zero on every parser, so the
        one that could actually read the grid never won and the report came back
        as an empty skeleton.
        """
        total = filled = 0
        for room in rooms:
            for item in room.get("items", []):
                total += 1
                for block in ("start_of_tenancy", "end_of_tenancy"):
                    side = item.get(block, {})
                    if side.get("clean") or side.get("undamaged") or side.get("working"):
                        filled += 1
                        break
        return total, filled

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

    @staticmethod
    def _header_field(cell):
        """Map a column heading to the field it fills, or None.

        Rotated headings often come out of the text layer reversed ("naelC" for
        "Clean"), so each candidate is tried in both reading directions. The
        reversed try is last and cannot invent a match: no field name reads as
        another field name backwards.
        """
        raw = cell or ""
        for text in (raw, raw[::-1]):
            n = re.sub(r"[^a-z ]", " ", text.lower())
            n = " ".join(n.split())
            if not n:
                continue
            # Grids that abbreviate the three condition columns and carry a
            # legend ("Key: C = Clean; U = Undamaged; W = Working"). Exact match
            # only - a substring test on a single letter would hit almost every
            # heading.
            if n in _ABBREVIATED_HEADERS:
                return _ABBREVIATED_HEADERS[n]
            for field, needles in _HEADER_FIELDS:
                if any(needle in n for needle in needles):
                    return field
            # A rotated heading can also arrive one character per line
            # ("n\na\ne\nl\nC"), which leaves a space between every letter and
            # matches nothing above. Compare with all spacing removed.
            tight = n.replace(" ", "")
            if len(tight) > 2:
                for field, needles in _HEADER_FIELDS:
                    if any(needle.replace(" ", "") in tight for needle in needles):
                        return field
        return None

    @classmethod
    def _table_column_map(cls, table):
        """Read a condition grid's own header rows and return

            (colmap, first_data_row)

        where colmap is {column index: ("start"|"end", field)}. Returns
        (None, 0) when the table has no usable header, in which case the caller
        falls back to the older positional reading.

        Two header rows are involved: a span row naming the blocks ("Condition
        of premises at START of tenancy" / "... at END ...") and a field row
        naming the columns. The span row is what decides where START stops -
        halving the column count gets this wrong whenever the item name shares
        the row, which is how a START landlord comment ended up recorded as
        END-of-tenancy evidence.
        """
        if not table or len(table) < 2:
            return None, 0

        field_row = span_row = None
        for idx, row in enumerate(table[:4]):
            named = sum(1 for c in row if cls._header_field(c))
            if named >= 3 and field_row is None:
                field_row = idx
            joined = " ".join((c or "") for c in row).upper()
            if span_row is None and "START" in joined and "END" in joined:
                span_row = idx
        if field_row is None:
            return None, 0

        width = len(table[field_row])

        # Block boundaries from the span row: each non-empty cell opens a block
        # that runs until the next non-empty cell.
        blocks = []
        if span_row is not None:
            marks = [(i, (c or "").upper())
                     for i, c in enumerate(table[span_row]) if (c or "").strip()]
            for pos, (i, txt) in enumerate(marks):
                if "START" in txt:
                    block = "start"
                elif "END" in txt:
                    block = "end"
                else:
                    continue
                stop = marks[pos + 1][0] if pos + 1 < len(marks) else width
                blocks.append((i, stop, block))

        colmap = {}
        seen = set()
        for i, cell in enumerate(table[field_row]):
            field = cls._header_field(cell)
            if not field:
                continue
            block = None
            for lo, hi, name in blocks:
                if lo <= i < hi:
                    block = name
                    break
            if block is None:
                # No span row (or a column outside every span): the second time
                # a field name appears, the END block has started.
                block = "end" if field in seen else "start"
            seen.add(field)
            if block == "end" and field == "landlord_comments":
                field = "comments"
            if block == "start" and field == "comments":
                field = "landlord_comments"
            colmap[i] = (block, field)

        if not colmap:
            return None, 0
        return colmap, max(field_row, span_row if span_row is not None else 0) + 1

    # The two wide comment columns are labelled above the grid rather than in
    # it, and the NSW form SWAPS their order between entry and exit: an entry
    # report reads "Lessor/agent - Comments (if any)" then "Tenant/s - Comment
    # on lessor/agent report", an exit report the other way round. Reading them
    # by position files the agent's exit findings as the tenant's, which in a
    # bond dispute is precisely backwards.
    _COMMENT_OWNERS = (
        ("landlord_comments", ("lessor", "landlord", "agent")),
        ("tenant_comments", ("tenant",)),
    )

    @classmethod
    def _comment_owner_order(cls, page_text):
        """Owners of the two comment columns, left to right, from the page."""
        hits = []
        low = (page_text or "").lower()
        for field, needles in cls._COMMENT_OWNERS:
            pos = min((low.find(n) for n in needles if low.find(n) >= 0),
                      default=-1)
            if pos >= 0:
                hits.append((pos, field))
        hits.sort()
        order = [f for _, f in hits]
        # Fall back to the printed form's usual order rather than guessing.
        return order or ["landlord_comments", "tenant_comments"]

    _TICK_FIELDS = ("clean", "undamaged", "working", "tenant_agrees")

    def _rotated_column_map(self, page_tables, found_table, block, page_text):
        """Column map for a grid whose headings are rotated Clean / Undamaged /
        Working / Tenant Agrees labels, and whose two wide comment columns are
        named only in the page text.

        Agency systems that render the NSW form (PrinceXML and friends) put the
        rotated headings in their own one-row strip above the grid on the first
        page, then repeat them as the grid's own first row on continuation
        pages - and in both cases the text layer hands them over reversed
        ("naelC"). Neither shape gave a usable map, so the reader fell through
        to a positional guess that dropped every tick: 123 of them on one real
        report, with the unfilled rows promoted to areas in their place.

        Headings are matched to columns by x-position, which works whether they
        sit in the grid or in a strip above it.
        """
        bounds = self._column_bounds(found_table)
        signature = tuple(round(x0) for x0, _ in bounds)

        header_cells = []
        for other in page_tables:
            # The grid's own first row, or a heading strip sitting above it.
            if other.bbox != found_table.bbox and other.bbox[3] > found_table.bbox[1] + 2:
                continue
            rows = other.extract()
            if not rows:
                continue
            for row_idx, row in enumerate(rows[:1] if other.bbox == found_table.bbox
                                          else rows[:2]):
                for col_idx, cell in enumerate(row):
                    if self._header_field(cell) not in self._TICK_FIELDS:
                        continue
                    cell_box = self._cell_bounds(other, row_idx, col_idx)
                    if cell_box:
                        header_cells.append((cell_box, self._header_field(cell)))

        if len(header_cells) < 3:
            # Some continuation pages repeat neither. Reuse the map from a page
            # that did carry headings, but only for a grid ruled to the same
            # column positions, so it can never borrow a differently shaped
            # table's map.
            return self._detached_map_cache.get(signature)

        colmap, claimed = {}, set()
        for col_idx, (x0, x1) in enumerate(bounds):
            centre = (x0 + x1) / 2.0
            for (hx0, hx1), field in header_cells:
                if hx0 - 1 <= centre <= hx1 + 1 and field not in claimed:
                    colmap[col_idx] = (block, field)
                    claimed.add(field)
                    break
        if len(colmap) < 3:
            return None

        # Everything right of the last tick column is a comment column. Name
        # them from the page, in the order the page names them - see
        # _comment_owner_order for why position alone is not safe.
        last_tick = max(colmap)
        owners = self._comment_owner_order(page_text)
        for pos, col_idx in enumerate(i for i in range(len(bounds)) if i > last_tick):
            if pos >= len(owners):
                break
            field = owners[pos]
            if block == "end" and field == "landlord_comments":
                field = "comments"
            colmap[col_idx] = (block, field)
        self._detached_map_cache[signature] = colmap
        return colmap

    @staticmethod
    def _cell_bounds(table, row_idx, col_idx):
        """(x0, x1) of one extracted cell, or None."""
        try:
            row = table.rows[row_idx]
        except (AttributeError, IndexError):
            return None
        cells = [c for c in row.cells if c]
        if col_idx >= len(cells):
            return None
        return cells[col_idx][0], cells[col_idx][2]

    @staticmethod
    def _column_bounds(table):
        """(x0, x1) per column of a found table, left to right."""
        edges = sorted({round(c[0], 1) for row in table.rows
                        for c in row.cells if c})
        edges.append(table.bbox[2])
        return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]

    # An area heading in these grids is one cell merged across the whole table
    # width; an item row keeps its separate columns. That is structural, so it
    # needs no vocabulary of room names - which matters because an unfilled item
    # ("blinds/curtains" with no ticks) and an area heading ("Lounge room") are
    # otherwise the same shape: a name in column 0 and nothing else.
    _MERGED_HEADING_SHARE = 0.8

    @classmethod
    def _is_merged_heading(cls, table_row, table):
        cells = [c for c in table_row.cells if c]
        if len(cells) != 1:
            return False
        width = table.bbox[2] - table.bbox[0]
        return width > 0 and (cells[0][2] - cells[0][0]) / width >= cls._MERGED_HEADING_SHARE

    _REPORT_TITLE_BLOCK = (
        ("start", ("entry condition report", "entry inspection report",
                   "ingoing condition report", "condition report at the start")),
        ("end", ("exit condition report", "exit inspection report",
                 "outgoing condition report", "final inspection report",
                 "condition report at the end")),
    )

    def _document_block(self):
        """Whether this document records the START or the END of a tenancy.

        Agency systems title the page plainly - "Entry condition report: ..." /
        "Exit Condition Report: ..." - which beats keyword-counting the body,
        where the instruction pages discuss both ends at length. That is what
        made an entry report come out as a move-out, with every move-in
        observation filed under end-of-tenancy.
        """
        head = ""
        for page in self.fitz_doc[:3]:
            head += self._page_text(page)[:400].lower() + "\n"
        for block, titles in self._REPORT_TITLE_BLOCK:
            if any(t in head for t in titles):
                return block
        return "end" if self.detected_type == "move_out" else "start"

    _YN_FIELDS = ("clean", "undamaged", "working", "tenant_agrees")

    def _parse_row_by_header(self, item_name, row, colmap):
        """Fill an item from a row using the grid's own column headings."""
        item_data = self._build_empty_item(item_name)
        for i, cell in enumerate(row):
            spec = colmap.get(i)
            if not spec:
                continue
            block, field = spec
            text = str(cell).strip().replace("\n", " ") if cell else ""
            if not text:
                continue
            target = item_data["start_of_tenancy" if block == "start"
                               else "end_of_tenancy"]
            if field not in target:
                continue
            if field in self._YN_FIELDS:
                value = self._parse_yn(text)
                if value:
                    target[field] = value
            elif not target[field]:
                target[field] = text
        return item_data

    def _parse_table_row_for_item(self, item_name, row, colmap=None):
        if colmap:
            return self._parse_row_by_header(item_name, row, colmap)

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
                # These grids carry a Tenant comments column on the END side as
                # well as the START side. Without the key the value was read off
                # the row and then silently dropped on the way in.
                "comments": None, "tenant_agrees": None, "tenant_comments": None,
            },
        }

    def _build_empty_room(self, room_name, expected_items):
        return {
            "room_name": room_name,
            "page_number": None,
            "items": [self._build_empty_item(name) for name in expected_items],
        }

    @classmethod
    def _heading_above(cls, words, top):
        """The nearest text line sitting above a table.

        Some forms give each area its own table and put the area name on the
        line above it, leaving the table's own first column headed just "Item".
        Reading only inside the table names every area "Item", and the
        duplicate-area clean-up then merges them all into one - which is how a
        12-area report came out as a single area.
        """
        lines = {}
        for w in words:
            if w["bottom"] > top + 2:
                continue
            lines.setdefault(round(w["top"] / 3.0), []).append(w)

        best = None
        for group in lines.values():
            group.sort(key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in group).strip()
            if not text or len(text) > 60:
                continue
            if not re.search(r"[A-Za-z]{2,}", text):
                continue
            low = text.lower()
            # A legend ("Key: C = Clean...") or a column heading is not an area.
            if "=" in text or low.startswith(("key:", "note", "page ")):
                continue
            if cls._header_field(text):
                continue
            bottom = max(w["bottom"] for w in group)
            if best is None or bottom > best[0]:
                best = (bottom, text)
        return best[1] if best else None

    def _extract_rooms_generic(self):
        rooms = []
        current_room = None
        block = self._document_block()

        for i, page in enumerate(self.plumber_pdf.pages):
            found = page.find_tables()
            if not found:
                continue
            words = None

            page_text = None

            for found_table in found:
                table = found_table.extract()
                colmap, first_data = self._table_column_map(table)

                # A rotated-heading grid. Its own header row names only the four
                # tick columns, so a map from it is incomplete - the wider map
                # that also names the two comment columns wins.
                if page_text is None:
                    page_text = page.extract_text() or ""
                rotated = self._rotated_column_map(found, found_table, block, page_text)
                if rotated and len(rotated) > len(colmap or {}):
                    # Read structurally: a full-width merged cell opens an area,
                    # anything else is an item of the area above it.
                    for row_obj, row in zip(found_table.rows, table):
                        name = re.sub(r"\s+", " ", str(row[0] or "")).strip()
                        if not re.search(r"[A-Za-z]{2,}", name):
                            continue
                        # A rotated heading landing in column 0 ("naelC") is a
                        # label, not a component.
                        if self._header_field(name):
                            continue
                        if self._is_merged_heading(row_obj, found_table):
                            current_room = name
                            rooms.append({"room_name": name,
                                          "page_number": i + 1,
                                          "items": []})
                            continue
                        if not rooms:
                            continue
                        rooms[-1]["items"].append(
                            self._parse_row_by_header(name, row, rotated))
                    continue

                # An "Item"-headed grid describes ONE area, named above it.
                head = re.sub(r"\s+", " ", str(table[0][0] or "")).strip().lower()
                if colmap and head in _ITEM_COLUMN_HEADS:
                    if words is None:
                        words = page.extract_words()
                    heading = self._heading_above(words, found_table.bbox[1])
                    if heading:
                        rooms.append({
                            "room_name": heading,
                            "page_number": i + 1,
                            "items": [],
                        })
                        current_room = heading
                        for row in table[first_data:]:
                            if not row or not row[0]:
                                continue
                            name = str(row[0]).strip().replace("\n", " ")
                            if not re.search(r"[A-Za-z]{2,}", name) or len(name) > 100:
                                continue
                            rooms[-1]["items"].append(
                                self._parse_table_row_for_item(name, row, colmap))
                        continue

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

                    # An item / area name must contain a real word. A bare "Y" /
                    # "N", a stray tick or a lone number is a grid legend cell,
                    # not a row to turn into an item - reading those legend cells
                    # is exactly what produced components literally named "Y" (and
                    # bogus N/Y/N values) on blank forms. Reversed rotated labels
                    # (e.g. "MOOR EGNUOL") still contain a word and pass; they are
                    # normalised to reading order later.
                    if not re.search(r'[A-Za-z]{2,}', first_cell):
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
                                self._parse_table_row_for_item(
                                    first_cell, row, colmap))
                            continue
                        current_room = first_cell
                        rooms.append({
                            "room_name": current_room,
                            "page_number": i + 1,
                            "items": [],
                        })
                        continue

                    if has_yn:
                        item_data = self._parse_table_row_for_item(
                            first_cell, row, colmap)
                        if not rooms:
                            rooms.append({
                                "room_name": self._guess_room_name(
                                    self.fitz_doc[i].get_text(), i),
                                "page_number": i + 1,
                                "items": [],
                            })
                        rooms[-1]["items"].append(item_data)

        # A heading row that never gathered an item came from a table that is
        # not a condition grid - a key/value metadata block ("Tenancy start
        # date | 03/09/2026") or a signature block ("Party | ..."). Both used to
        # surface as empty areas. Only drop them when the document produced real
        # areas too, so a form we failed to read still returns what it found.
        if any(room["items"] for room in rooms):
            rooms = [room for room in rooms if room["items"]]

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
                # "removable lithium" also appears in the question ABOVE this
                # one ("...except for removable lithium batteries?"), so anchor
                # on the wording unique to this question.
                "lithium_batteries_replaced": self._find_yes_no(
                    text, "that have a removable lithium"),
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
            # Bounded to the label's own line: a blank meter box must stay null
            # rather than reaching down the page for the next digit it can find.
            "meter_reading_start": self._find_field(
                text, r"Water meter reading at START[^\n]*?(\d+)"),
            "meter_reading_start_date": self._find_date_after(
                text, "Water meter reading at START", window=40),
            "meter_reading_end": self._find_field(
                text, r"Water meter reading at END[^\n]*?(\d+)"),
            "meter_reading_end_date": self._find_date_after(
                text, "Water meter reading at END", window=40),
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

    # An image drawn on more than this many pages is a repeated header/footer
    # logo or watermark, not report content, so it is skipped.
    _LOGO_PAGE_LIMIT = 2
    # Content photos in these reports are large scans/photographs. Logos, icons
    # and scanned signatures are well under this in their smaller dimension.
    _MIN_PHOTO_DIM = 200
    # An image covering this much of the page is the page background / a full
    # page scan, not an inspection photo embedded on the page.
    _MAX_PHOTO_PAGE_FRAC = 0.9
    # For scanned reports the page scans ARE the images the user wants. We render
    # each scanned page to at most this many pixels on its long edge (keeps the
    # form legible while keeping the embedded JSON to a sensible size).
    _SCAN_MAX_DIM = 1400
    _SCAN_JPEG_QUALITY = 70
    # A real photo is roughly 4:3, 3:4 or up to ~16:9. Anything much wider or
    # taller is a full-width header banner, decorative wave or rule, not a photo.
    _MAX_PHOTO_ASPECT = 2.5
    # A photograph has thousands of distinct colours; a flat icon, clip-art or
    # line graphic (e.g. a paperclip "attachment" glyph) has only a handful.
    _MIN_PHOTO_COLORS = 200
    _PHOTO_JPEG_QUALITY = 78

    @staticmethod
    def _color_diversity(pix, cap=80):
        # Distinct-colour count on a small downsample - cheap and PIL-free.
        # High for photographs, very low for icons / clip-art / rules.
        try:
            probe = fitz.Pixmap(pix)
            if probe.n > 4:
                probe = fitz.Pixmap(fitz.csRGB, probe)
            while max(probe.width, probe.height) > cap:
                probe.shrink(1)
            samples = probe.samples
            n = probe.n
            colors = set()
            for i in range(0, len(samples), n):
                colors.add(samples[i:i + 3])
            return len(colors)
        except Exception:
            return 10 ** 6  # on any failure, do not filter it out

    def _parse_media_captions(self, page_text):
        # Software-generated exit reports (e.g. Inspection Manager) append a
        # "Media" gallery page where each photo carries a caption printed above
        # it, laid out as two lines:
        #     "Front Gardens : "
        #     "Photo Taken : 26/06/2023"   (or "Video Taken : ...")
        # Returns the captions in top-to-bottom reading order so they can be
        # zipped onto the photos, which we sort into the same reading order.
        lines = [ln.strip() for ln in page_text.split("\n")]
        low = [ln.lower() for ln in lines]
        if not any(ln == "media" or ln.startswith("view your photos") or
                   ln.startswith("view your photos/videos") for ln in low):
            return []
        caps = []
        for i in range(len(lines) - 1):
            label_m = re.match(r"^(.*\S)\s*:\s*$", lines[i])
            media_m = re.match(r"^(Photo|Video)\s+Taken\s*:\s*(.*)$",
                               lines[i + 1], re.IGNORECASE)
            if label_m and media_m:
                label = label_m.group(1).strip()
                mtype = media_m.group(1).lower()          # "photo" | "video"
                date = media_m.group(2).strip() or None
                caption = f"{label} - {media_m.group(1).title()} Taken: {date}" \
                    if date else label
                caps.append({
                    "label": label,
                    "media_type": mtype,
                    "date_taken": date,
                    "caption": caption,
                })
        return caps

    @staticmethod
    def _parse_media_links(page):
        # The "Media" gallery page hyperlinks each thumbnail to the real media
        # file held online: photos to an /image?...jpg URL and videos to a
        # /video?...mov URL, plus one /gallery/ link for the whole album. The
        # URL path is the authoritative photo-vs-video signal (a video's still
        # frame is otherwise indistinguishable from a photo), and it carries the
        # actual playable video link the caption text alone cannot give.
        #
        # Links are returned in top-to-bottom, left-to-right reading order so
        # they line up with the photos, which are sorted the same way.
        gallery_url = None
        media = []
        for link in page.get_links():
            uri = link.get("uri")
            if not uri:
                continue
            if "/gallery/" in uri:
                gallery_url = gallery_url or uri
                continue
            if "/video" in uri:
                mtype = "video"
            elif "/image" in uri:
                mtype = "photo"
            else:
                continue
            r = link["from"]
            media.append({
                "media_type": mtype,
                "url": uri,
                "cx": (r.x0 + r.x1) / 2,
                "cy": (r.y0 + r.y1) / 2,
            })
        media.sort(key=lambda m: (round(m["cy"] / 12), m["cx"]))
        return {"gallery_url": gallery_url, "media": media}

    # Column labels of the NSW "start of tenancy" tick grid, in order.
    _TICK_COLS = ("clean", "undamaged", "working", "tenant_agrees")
    _TICK_ITEMS = ("walls", "wall", "doors", "door", "windows", "window",
                   "ceiling", "blinds", "lights", "light", "skirting", "floor",
                   "cupboards", "bench", "sink", "stove", "oven", "exhaust",
                   "dishwasher", "washing", "dryer", "front", "hot", "range",
                   "shower", "bath", "toilet", "mirror", "heating", "hooks")

    @staticmethod
    def _tick_toks(s):
        """Significant word tokens of an item/component name (>=3 letters), used
        to match a noisy OCR'd tick-row name against a template component name."""
        return set(w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 3)

    def _apply_scanned_ticks(self, areas, ticks):
        """Write the scanned Y/N reads into each component's start_of_tenancy so
        they show directly in the converter's grid (not just the separate
        scanned_ticks section).

        The reader returns rows per scanned page in top-to-bottom order, but each
        grid page packs several rooms and the rows carry noisy OCR'd item names.
        We therefore:
          1. segment the rows into room-blocks (each NSW room grid restarts at a
             "walls ..." row);
          2. identify each block's area by the best in-order match of its item
             names against a template room's component names (a room's item
             sequence - e.g. sink/stove/oven/dishwasher - is distinctive), using
             a monotonic cursor so identical rooms (Bedroom 1/2/3) map in order;
          3. align the block's rows to that area's components by name and copy the
             Y/N values (with their per-cell confidence) in.
        Values only ever land where an item name matches a component in the
        correctly-identified room, so nothing is written blindly by position.
        Returns the number of components filled."""
        if not ticks:
            return 0
        toks = self._tick_toks

        area_ctoks = [[toks(c.get("component_name")) for c in a.get("components", [])]
                      for a in areas]

        # 1) Segment rows into room-blocks. A new block starts at a "walls" row
        #    (the leading item of almost every room) once the current block has
        #    real content; rows before the first room start (the example box) fall
        #    into a leading block that won't match any room and is dropped.
        blocks = []
        cur = []
        for t in ticks:
            it = toks(t.get("item"))
            if "walls" in it and any(toks(r.get("item")) for r in cur):
                blocks.append(cur)
                cur = [t]
            else:
                cur.append(t)
        if cur:
            blocks.append(cur)

        def block_area_score(block, ctoks):
            """In-order overlap of a block's item names against a room's
            components: how many rows match successive components."""
            ci = 0
            score = 0
            for r in block:
                rt = toks(r.get("item"))
                if not rt:
                    continue
                for k in range(ci, len(ctoks)):
                    if rt & ctoks[k]:
                        score += 1
                        ci = k + 1
                        break
            return score

        filled = 0
        used = set()
        for block in blocks:
            # 2) Best-matching still-unused area (physical page order does not
            #    follow the template's area order - a single grid page can pack,
            #    say, the Kitchen and the Laundry - so we match on the room's
            #    distinctive item signature rather than position). Each template
            #    area is claimed at most once; identical rooms (Bedroom 1/2/3)
            #    simply take the next free slot.
            best_i, best_score = None, 0
            for i in range(len(areas)):
                if i in used:
                    continue
                s = block_area_score(block, area_ctoks[i])
                if s > best_score:
                    best_score, best_i = s, i
            if best_i is None or best_score < 3:
                continue
            comps = areas[best_i].get("components", [])
            ctoks = area_ctoks[best_i]
            used.add(best_i)

            # 3) Align rows to this room's components by name, in order.
            ci = 0
            for r in block:
                rt = toks(r.get("item"))
                if not rt:
                    continue
                match_k, best_ov = None, 0
                for k in range(ci, min(len(comps), ci + 5)):
                    ov = len(rt & ctoks[k])
                    if ov > best_ov:
                        best_ov, match_k = ov, k
                if match_k is None:
                    continue
                ci = match_k + 1
                sot = comps[match_k].get("start_of_tenancy", {})
                conf = {}
                any_val = False
                for col in self._TICK_COLS:
                    v = r.get("start_of_tenancy", {}).get(col)
                    if v is not None and sot.get(col) is None:
                        sot[col] = v
                        conf[col] = r.get("confidence", {}).get(col)
                        any_val = True
                if any_val:
                    sot["condition_source"] = "ocr"
                    sot["condition_confidence"] = conf
                    filled += 1
        return filled

    def _extract_scanned_ticks(self):
        """Best-effort Y/N reader for scanned condition grids.

        Free-text OCR cannot see the small marks in the narrow tick columns, so
        we find the column grid-lines and the item rows and OCR each individual
        cell with a Y/N-restricted charset. Every value carries a confidence
        ('high' = read cleanly, 'low' = a mark was present but not read cleanly)
        so nothing uncertain is trusted silently. Returns a list of per-row
        dicts, or [] when nothing could be read."""
        try:
            import numpy as np
            from PIL import Image, ImageOps
        except Exception:
            return []
        if not self._ocr_enabled or not ocr_engine.is_available():
            return []
        DARK = 110

        def classify(pil, mask, x0, x1, y0, y1):
            sub = mask[y0:y1, x0:x1]
            if sub.size == 0 or sub.mean() < 0.03:
                return (None, None)
            crop = pil.crop((x0, y0, x1, y1))
            crop = ImageOps.autocontrast(crop.resize((crop.width * 5, crop.height * 5)))
            t = ocr_engine.ocr_char(crop, "YN").upper().replace("YY", "Y")
            if "N" in t and "Y" not in t:
                return ("N", "high")
            if "Y" in t:
                return ("Y", "high")
            return ("Y", "low")  # a mark is present but unread -> Y, low conf

        results = []
        for page in self.fitz_doc:
            if not self._is_scanned_page(page):
                continue
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), colorspace=fitz.csGRAY)
                pil = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                a = np.array(pil)
                mask = (a < DARK).astype(np.uint8)
                H, W = a.shape
            except Exception:
                continue
            # item-name anchors (reliable): known items at the left of the page.
            # Keep each word's line key so the full item name can be rebuilt.
            tsv_rows = ocr_engine.ocr_image_tsv(pil)
            lines = {}
            for r in tsv_rows:
                key = (r.get("block_num"), r.get("par_num"), r.get("line_num"))
                lines.setdefault(key, []).append(r)
            anc = []
            for r in tsv_rows:
                t = re.sub(r"[^a-z/ ]", "", (r.get("text") or "").strip().lower())
                if r["conf"] > 35 and r["left"] < W * 0.30 and \
                        t.startswith(self._TICK_ITEMS) and len(t) >= 4:
                    key = (r.get("block_num"), r.get("par_num"), r.get("line_num"))
                    anc.append((r["top"] + r["height"] // 2, r["left"] + r["width"], key))
            anc.sort()
            dedup = []
            for y, xr, key in anc:
                if not (dedup and y - dedup[-1][0] < 18):
                    dedup.append((y, xr, key))
            anc = dedup

            def item_name(line_key, x_limit):
                words = sorted((w for w in lines.get(line_key, []) if w["left"] < x_limit),
                               key=lambda w: w["left"])
                name = " ".join((w.get("text") or "").strip() for w in words).strip()
                name = re.sub(r"\s+", " ", name)
                return name or None
            if len(anc) < 3:
                continue
            ys = [y for y, _, _ in anc]
            y0, y1 = max(0, min(ys) - 20), min(H, max(ys) + 20)
            item_right = int(np.median([xr for _, xr, _ in anc]))
            # vertical grid lines that span the item rows = the tick columns
            n = y1 - y0
            col = mask[y0:y1].sum(axis=0)
            xs = [x for x in range(len(col)) if col[x] / n >= 0.45]
            groups, run = [], []
            for x in xs:
                if groups and x - groups[-1][-1] <= 3:
                    groups[-1].append(x)
                else:
                    groups.append([x])
            vl = [int(np.mean(g)) for g in groups]
            cand = [x for x in vl if item_right + 40 <= x <= W]
            for x in cand:
                if not run or 35 <= x - run[-1] <= 58:
                    run.append(x)
                elif len(run) >= 4:
                    break
                else:
                    run = [x]
            if len(run) < 4:
                continue
            run = run[:5]
            # rows aligned to the marks: ink peaks inside the tick columns
            prof = mask[:, run[0] + 3:run[-1] - 3].sum(axis=1)
            thr = max(4, 0.12 * (run[-1] - run[0]))
            peaks = []
            for yy in range(y0, min(y1, H - 1)):
                if prof[yy] >= thr and prof[yy] >= prof[yy - 1] and prof[yy] >= prof[yy + 1]:
                    if not peaks or yy - peaks[-1] > 22:
                        peaks.append(yy)
            for cy in peaks:
                vals, confs = {}, {}
                for i in range(min(len(run) - 1, len(self._TICK_COLS))):
                    v, cf = classify(pil, mask, run[i] + 2, run[i + 1] - 2, cy - 15, cy + 14)
                    vals[self._TICK_COLS[i]] = v
                    confs[self._TICK_COLS[i]] = cf
                if not any(vals.values()):
                    continue
                near = min(anc, key=lambda t: abs(t[0] - cy))
                item = item_name(near[2], run[0]) if abs(near[0] - cy) < 30 else None
                results.append({
                    "page": page.number + 1,
                    "item": item,
                    "start_of_tenancy": vals,
                    "confidence": confs,
                })
        return results

    def _make_scan_entry(self, page, page_idx, save_images, output_dir, embed_data):
        """Render one scanned page to a compressed image entry, so a scanned
        report still yields images (the page scans) rather than nothing."""
        try:
            long_edge = max(page.rect.width, page.rect.height) or 1
            zoom = min(self._SCAN_MAX_DIM / long_edge, 3.0)
            # These are black-on-white form scans, so greyscale keeps them fully
            # legible while roughly a third the size of an RGB render.
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                  colorspace=fitz.csGRAY)
        except Exception:
            return None
        entry = {
            "page": page_idx + 1,
            "index": 0,
            "width": pix.width,
            "height": pix.height,
            "position": {"x": 0, "y": 0},
            "label": "Page %d scan" % (page_idx + 1),
            "media_type": "scan",
            "date_taken": None,
            "caption": None,
            "media_url": None,
            "gallery_url": None,
            "format": None,
            "data_base64": None,
            "file_path": None,
        }
        if embed_data or (save_images and output_dir):
            try:
                jpg = pix.tobytes("jpg", jpg_quality=self._SCAN_JPEG_QUALITY)
                entry["format"] = "jpg"
            except Exception:
                jpg = pix.tobytes("png")
                entry["format"] = "png"
            if save_images and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                fn = "page%d_scan.%s" % (page_idx + 1, entry["format"])
                with open(os.path.join(output_dir, fn), "wb") as f:
                    f.write(jpg)
                entry["file_path"] = fn
            if embed_data:
                entry["data_base64"] = base64.b64encode(jpg).decode("utf-8")
        return entry

    def _extract_images(self, output_dir=None, save_images=True, embed_data=False):
        # We surface only genuine report content - the inspection photos - not
        # every raster in the file. Header/footer logos are referenced by every
        # page's resource dict, so we look at what is actually *drawn* on a page
        # (get_image_rects), drop images that repeat across many pages (logos)
        # and anything too small to be a photo (icons, scanned signatures).
        #
        # Where a "Media" gallery page prints a caption above each photo, the
        # captions are matched onto the photos in reading order (label, whether
        # it is a photo or video still, and the date the media was taken).
        #
        # Photos are encoded as JPEG - a 9-photo page is ~0.5 MB as JPEG versus
        # ~7 MB as PNG - so an embedded (base64) JSON stays light enough to copy
        # and for the converter to render inline. embed_data controls whether
        # the bytes are inlined; otherwise only lightweight metadata is kept.
        doc = self.fitz_doc

        # get_image_rects has to lay the page out to answer, which is by far the
        # most expensive call here - a real 79-page report with 456 photos spent
        # most of its time in it. The scan below asks the same question the main
        # loop asks again a moment later, so the answers are kept.
        rects_cache = {}

        def rects_for(page, xref):
            key = (page.number, xref)
            if key not in rects_cache:
                rects_cache[key] = page.get_image_rects(xref)
            return rects_cache[key]

        pages_drawn = {}
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if rects_for(page, xref):
                    pages_drawn.setdefault(xref, set()).add(page.number)

        images = []
        emitted = set()
        for page_idx, page in enumerate(doc):
            captions = self._parse_media_captions(page.get_text())
            media_links = self._parse_media_links(page)

            photos = []  # (xref, rect, pixmap)
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in emitted:
                    continue
                if len(pages_drawn.get(xref, ())) > self._LOGO_PAGE_LIMIT:
                    continue  # repeated header/footer logo or watermark
                # The dimensions are already in the resource entry, so the
                # too-small and wrong-shape rejections can be made before
                # decoding anything. Decoding first meant every icon and divider
                # in the file was fully decompressed only to be thrown away.
                raw_w, raw_h = img[2], img[3]
                if raw_w and raw_h:
                    if min(raw_w, raw_h) < self._MIN_PHOTO_DIM:
                        continue
                    if max(raw_w, raw_h) / max(1, min(raw_w, raw_h)) > self._MAX_PHOTO_ASPECT:
                        continue
                rects = rects_for(page, xref)
                if not rects:
                    continue
                # A near page-sized image is the page background or, in a scanned
                # report, the page scan itself - not an inspection photo. In a
                # scanned report the page scan IS the image the user wants, so
                # emit it as a page-scan image; in a digital report it is just a
                # background, so skip it.
                page_area = abs(page.rect.width * page.rect.height)
                if page_area and (abs(rects[0].width * rects[0].height) / page_area
                                  > self._MAX_PHOTO_PAGE_FRAC):
                    if self._is_scanned_page(page):
                        entry = self._make_scan_entry(
                            page, page_idx, save_images, output_dir, embed_data)
                        if entry:
                            emitted.add(xref)
                            images.append(entry)
                    continue
                try:
                    pix = fitz.Pixmap(doc, xref)
                except Exception:
                    continue
                if min(pix.width, pix.height) < self._MIN_PHOTO_DIM:
                    pix = None
                    continue  # icon or scanned signature, not a content photo
                aspect = max(pix.width, pix.height) / max(1, min(pix.width, pix.height))
                if aspect > self._MAX_PHOTO_ASPECT:
                    pix = None
                    continue  # header banner, wave or divider, not a photo
                if self._color_diversity(pix) < self._MIN_PHOTO_COLORS:
                    pix = None
                    continue  # flat icon / clip-art / line graphic, not a photo
                photos.append((xref, rects[0], pix))

            if not photos:
                continue

            # Sort into human reading order: rows top-to-bottom (bucketed so a
            # slightly uneven baseline still groups), then left-to-right.
            photos.sort(key=lambda t: (round(t[1].y0 / 12), t[1].x0))
            captions_match = len(captions) == len(photos)
            links = media_links["media"]
            links_match = len(links) == len(photos)
            gallery_url = media_links["gallery_url"]

            for i, (xref, rect, pix) in enumerate(photos):
                emitted.add(xref)
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                entry = {
                    "page": page_idx + 1,
                    "index": i,
                    "width": pix.width,
                    "height": pix.height,
                    "position": {"x": round(rect.x0, 1), "y": round(rect.y0, 1)},
                    "label": None,
                    "media_type": "photo",
                    "date_taken": None,
                    "caption": None,
                    "media_url": None,
                    "gallery_url": gallery_url,
                    "format": None,
                    "data_base64": None,
                    "file_path": None,
                }
                if captions_match:
                    cap = captions[i]
                    entry["label"] = cap["label"]
                    entry["media_type"] = cap["media_type"]
                    entry["date_taken"] = cap["date_taken"]
                    entry["caption"] = cap["caption"]
                # The hyperlink under each thumbnail carries the real media file
                # (the playable .mov for a video, or the full-resolution .jpg for
                # a photo) and its /video-vs-/image path is the authoritative
                # media type, so it wins over the caption's Photo/Video wording.
                if links_match:
                    ln = links[i]
                    entry["media_url"] = ln["url"]
                    entry["media_type"] = ln["media_type"]

                if embed_data or (save_images and output_dir):
                    try:
                        jpg = pix.tobytes("jpg", jpg_quality=self._PHOTO_JPEG_QUALITY)
                    except Exception:
                        jpg = pix.tobytes("png")
                        entry["format"] = "png"
                    else:
                        entry["format"] = "jpg"
                    if save_images and output_dir:
                        os.makedirs(output_dir, exist_ok=True)
                        ext = entry["format"]
                        fn = f"page{page_idx + 1}_photo{i + 1}.{ext}"
                        with open(os.path.join(output_dir, fn), "wb") as f:
                            f.write(jpg)
                        entry["file_path"] = fn
                    if embed_data:
                        entry["data_base64"] = base64.b64encode(jpg).decode("utf-8")

                images.append(entry)
                pix = None
        return images

    # The statutory questions are answered by TICKING one of two drawn boxes,
    # not by printing a word. Both "Yes" and "No" are printed against every
    # question regardless, so any text search finds one of them and reports it
    # as the answer - which is how an entry report whose Minimum Standards were
    # all ticked Yes came back as No on every line, and how an exit report with
    # all 61 boxes left empty came back as a confident "No" throughout.
    _BOX_MIN, _BOX_MAX = 8, 20     # the empty checkbox, ~14pt square
    _TICK_MIN, _TICK_MAX = 4, 14   # the check mark drawn inside it
    _ANSWER_MAX_DY = 25            # a box belongs to a question this close in y

    def _checkbox_rows(self, page_idx):
        """[{y, x0, answer}] for each Yes/No checkbox pair on a page.

        answer is "Yes", "No", or None when the question was left blank - which
        is a real and common state, and must stay null rather than becoming a
        default "No".
        """
        cache = self._checkbox_cache
        if page_idx in cache:
            return cache[page_idx]

        page = self.fitz_doc[page_idx]
        boxes, ticks = [], []
        for drawing in page.get_drawings():
            rect = drawing["rect"]
            ops = {item[0] for item in drawing["items"]}
            if ops == {"re"} and self._BOX_MIN <= rect.width <= self._BOX_MAX \
                    and self._BOX_MIN <= rect.height <= self._BOX_MAX:
                boxes.append(rect)
            elif "c" in ops and self._TICK_MIN <= rect.width <= self._TICK_MAX \
                    and self._TICK_MIN <= rect.height <= self._TICK_MAX:
                ticks.append(rect)

        def box_left_of(word):
            """The checkbox this Yes/No label belongs to - immediately left."""
            lx, ly = word[0], (word[1] + word[3]) / 2
            near = [b for b in boxes
                    if b.x1 <= lx + 2 and lx - b.x1 < 12
                    and abs((b.y0 + b.y1) / 2 - ly) < 8]
            return min(near, key=lambda b: lx - b.x1) if near else None

        def is_ticked(box):
            return any(box.x0 - 1 <= (t.x0 + t.x1) / 2 <= box.x1 + 1
                       and box.y0 - 1 <= (t.y0 + t.y1) / 2 <= box.y1 + 1
                       for t in ticks)

        pairs = {}
        for word in page.get_text("words"):
            if word[4] not in ("Yes", "No"):
                continue
            box = box_left_of(word)
            if box is None:
                continue
            # Group by line, and by column - these forms print two columns of
            # questions side by side on the same rows.
            key = (round((word[1] + word[3]) / 2 / 6), round(word[0] / 200))
            row = pairs.setdefault(key, {"y": (word[1] + word[3]) / 2,
                                         "x0": box.x0, "answer": None})
            row["x0"] = min(row["x0"], box.x0)
            if is_ticked(box):
                row["answer"] = word[4]

        rows = sorted(pairs.values(), key=lambda r: (r["x0"], r["y"]))
        cache[page_idx] = rows
        return rows

    def _lines_with_boxes(self, page_idx):
        """(single lines, wrapped runs) for a page, each as (text, rect).

        A wrapped run carries the UNION of the lines it spans, not the first
        line's box: using the first line's y made "plumbing and drainage" match
        a run beginning two questions earlier and answer from that row instead.
        """
        cache = self._line_cache
        if page_idx in cache:
            return cache[page_idx]
        lines = []
        for block in self.fitz_doc[page_idx].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"]).strip()
                if text:
                    lines.append((text, fitz.Rect(line["bbox"])))
        runs = []
        for i, (text, rect) in enumerate(lines):
            merged, box = text, fitz.Rect(rect)
            for nxt_text, nxt_rect in lines[i + 1:i + 3]:
                merged += " " + nxt_text
                box = box | nxt_rect
                runs.append((merged, fitz.Rect(box)))
        cache[page_idx] = (lines, runs)
        return cache[page_idx]

    def _answer_for_rect(self, rows, rect):
        """The checkbox answer belonging to a question at `rect`.

        The boxes sit to the right of their question, and these forms print two
        columns of questions side by side - so the NEAREST column to the right
        is taken first, and only then the nearest row within it. Choosing purely
        by vertical distance let a left-column question answer from the right
        column's boxes.
        """
        right = [r for r in rows if r["x0"] > rect.x1 - 2]
        if not right:
            return None, False
        band = min(r["x0"] for r in right)
        column = [r for r in right if r["x0"] - band < 60]
        mid = (rect.y0 + rect.y1) / 2
        best = min(column, key=lambda r: abs(r["y"] - mid))
        if abs(best["y"] - mid) <= self._ANSWER_MAX_DY:
            return best["answer"], True
        return None, False

    def _ticked_answer(self, pattern):
        """(answer, found) for a checkbox-answered question.

        found says a checkbox row for this question was located, so its answer
        stands even when it is None - the question was simply not answered, and
        falling back to a text search would invent one.
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return None, False
        pages = range(min(8, len(self.fitz_doc)))
        # Whole lines first. A wrapped run always contains its own first line,
        # so trying runs together with lines would let a longer, earlier-
        # starting run win over the line that actually asks the question.
        for which in (0, 1):
            for page_idx in pages:
                rows = self._checkbox_rows(page_idx)
                if not rows:
                    continue
                for text, rect in self._lines_with_boxes(page_idx)[which]:
                    if not rx.search(text):
                        continue
                    answer, found = self._answer_for_rect(rows, rect)
                    if found:
                        return answer, True
        return None, False

    def _find_yes_no(self, text, pattern):
        answer, found = self._ticked_answer(pattern)
        if found:
            return answer
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

    # A value belongs to its label only if it follows closely. An unbounded
    # ".*?" walks the rest of the document, so a BLANK box on the form was
    # filled from whatever came next - the empty water-meter boxes picked up
    # the "4" of "Page 4 of 79", and the empty date beside them took a date
    # printed further down the page.
    _FIELD_WINDOW = 60

    def _find_date_after(self, text, pattern, window=None):
        window = self._FIELD_WINDOW if window is None else window
        try:
            anchor = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if not anchor:
                return None
            tail = text[anchor.end():anchor.end() + window]
            match = re.search(r"(\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})", tail)
            if match:
                # These forms print a date one component per line.
                return re.sub(r"\s+", "", match.group(1))
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


def classify_pdf_format(pdf_path):
    """Classify a PDF as 'digital', 'scanned', 'mixed' or 'empty' WITHOUT running
    OCR.

    Used to validate uploads - the app accepts digital PDFs only. A page counts
    as a scanned form page when it has no text layer and is (almost) entirely
    covered by a single image; a digital page has selectable text. A photo page
    inside an otherwise-digital report is NOT a scanned page (its images don't
    cover the whole page), so digital reports with embedded photos stay 'digital'.
    Mirrors the extractor's own _is_scanned_page / _file_format logic.
    """
    doc = fitz.open(pdf_path)
    try:
        pages = list(doc)
        if not pages:
            return "empty"
        text_pages = 0
        scanned_pages = 0
        for page in pages:
            if page.get_text().strip():
                text_pages += 1
                continue
            page_area = abs(page.rect.width * page.rect.height)
            if not page_area:
                continue
            is_scan = False
            for im in page.get_images(full=True):
                for r in page.get_image_rects(im[0]):
                    if abs(r.width * r.height) / page_area >= 0.85:
                        is_scan = True
                        break
                if is_scan:
                    break
            if is_scan:
                scanned_pages += 1
        if scanned_pages == 0:
            return "digital"
        if text_pages == 0:
            return "scanned"
        return "mixed"
    finally:
        doc.close()


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
                save_images=True, embed_images=False, enable_ocr=True):
    extractor = ConditionReportExtractor(pdf_path, jurisdiction, report_type)
    return extractor.extract(output_dir=output_dir, save_images=save_images,
                             embed_images=embed_images, enable_ocr=enable_ocr)
