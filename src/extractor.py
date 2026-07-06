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


class ConditionReportExtractor:
    def __init__(self, pdf_path, jurisdiction="NSW", report_type="auto"):
        self.pdf_path = pdf_path
        self.jurisdiction = jurisdiction.upper()
        self.report_type = report_type
        self.detected_type = None
        self.fitz_doc = None
        self.plumber_pdf = None

    def extract(self, output_dir=None, save_images=True):
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
                area = {
                    "area_name": room["room_name"],
                    "page_number": room.get("page_number"),
                    "components": [],
                }
                for item in room.get("items", []):
                    component = {
                        "component_name": item["item_name"],
                        "start_of_tenancy": item.get("start_of_tenancy", {}),
                        "end_of_tenancy": item.get("end_of_tenancy", {}),
                    }
                    area["components"].append(component)
                areas.append(area)

            result = {
                "jurisdiction": self.jurisdiction,
                "document_type": self.detected_type,
                "detected_document_type": self.detected_type if self.report_type == "auto" else None,
                "report_metadata": self._build_metadata(),
                "areas": areas,
                "other_sections": {
                    "compliance": self._extract_compliance(full_text),
                    "utilities": self._extract_utilities(full_text),
                    "water_efficiency": self._extract_water_efficiency(full_text),
                    "additional_comments": self._extract_additional_comments(full_text),
                    "maintenance_dates": self._extract_maintenance_dates(full_text),
                    "landlord_promise": self._extract_landlord_promise(full_text),
                    "signatures": self._extract_signatures(full_text),
                },
                "images": self._extract_images(output_dir, save_images),
            }

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
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            match = re.search(r"Address of premises[:\s]*([^\n]+)", text, re.IGNORECASE)
            if match:
                addr = match.group(1).strip()
                if addr and not re.match(r'^[YN\s/|]+$', addr) and len(addr) > 3:
                    return addr
        return None

    def _extract_postcode(self):
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            match = re.search(r"[Pp]ostcode[:\s]*(\d{4})", text)
            if match:
                return match.group(1)
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

    def _extract_field_value(self, field_names):
        for page in self.fitz_doc[:5]:
            text = page.get_text()
            text_upper = text.upper()
            if "HOW TO COMPLETE" in text_upper or "EXAMPLE" in text_upper:
                continue
            for field in field_names:
                pattern = rf"(?:{re.escape(field)})\s*[:\s]+([^\n]+)"
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    val = match.group(1).strip()
                    if val and len(val) > 1 and not re.match(r'^[YN\s/|]+$', val):
                        if len(val) > 100:
                            continue
                        skip_words = ["must", "should", "indicate", "landlord or", "the tenant",
                                      "record contact", "before", "after", "sign", "agrees",
                                      "comments", "condition", "premises", "report",
                                      "/agent", "trading"]
                        if any(sw in val.lower() for sw in skip_words):
                            continue
                        return val
        return None

    def _extract_tenancy_date(self, which="start"):
        keywords = {
            "start": ["start of tenancy", "commencement", "lease start", "move in date", "ingoing date"],
            "end": ["end of tenancy", "termination", "lease end", "move out date", "vacating date"],
        }
        for page in self.fitz_doc[:3]:
            text = page.get_text()
            for kw in keywords.get(which, []):
                match = re.search(
                    rf"{re.escape(kw)}.*?(\d{{1,2}}\s*/\s*\d{{1,2}}\s*/\s*\d{{2,4}})",
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

    def _extract_rooms(self):
        room_config = ROOM_CONFIGS.get(self.jurisdiction, {})
        if not room_config:
            return self._extract_rooms_generic()

        return self._extract_rooms_structured(room_config)

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

        yn_values = [v for _, v in yn_cells if v is not None]
        if len(yn_values) >= 3:
            item_data["start_of_tenancy"]["clean"] = yn_values[0]
            item_data["start_of_tenancy"]["undamaged"] = yn_values[1]
            item_data["start_of_tenancy"]["working"] = yn_values[2]
        if len(yn_values) >= 4:
            item_data["start_of_tenancy"]["tenant_agrees"] = yn_values[3]
        if len(yn_values) >= 7:
            item_data["end_of_tenancy"]["clean"] = yn_values[4]
            item_data["end_of_tenancy"]["undamaged"] = yn_values[5]
            item_data["end_of_tenancy"]["working"] = yn_values[6]
        if len(yn_values) >= 8:
            item_data["end_of_tenancy"]["tenant_agrees"] = yn_values[7]

        if comment_cells:
            mid = len(cells) // 2
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
        for i, page in enumerate(self.plumber_pdf.pages):
            tables = page.extract_tables()
            if not tables:
                continue
            page_text = self.fitz_doc[i].get_text()
            room_name = self._guess_room_name(page_text, i)

            for table in tables:
                for row in table:
                    if not row or not row[0]:
                        continue
                    item_name = str(row[0]).strip().replace('\n', ' ')
                    if not item_name or len(item_name) > 100:
                        continue
                    if any(skip in item_name.upper() for skip in [
                        'CONDITION', 'ADDRESS', 'LANDLORD', 'TENANT', 'CLEAN',
                        'UNDAMAGED', 'WORKING', 'Y N',
                    ]):
                        continue

                    item_data = self._parse_table_row_for_item(item_name, row)
                    if not rooms or rooms[-1]["room_name"] != room_name:
                        rooms.append({
                            "room_name": room_name,
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

    def _extract_images(self, output_dir=None, save_images=True):
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
                    else:
                        img_data["data_base64"] = base64.b64encode(img_bytes).decode("utf-8")

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
        text_lower = text.lower()

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
            "NT": ["northern territory", "darwin nt", "nt – entry",
                    "nt – exit", "nt - entry", "nt - exit"],
        }

        scores = {}
        for jur, keywords in markers.items():
            score = sum(1 for kw in keywords if kw in text_lower)
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


def extract_pdf(pdf_path, jurisdiction="NSW", report_type="auto", output_dir=None, save_images=True):
    extractor = ConditionReportExtractor(pdf_path, jurisdiction, report_type)
    return extractor.extract(output_dir=output_dir, save_images=save_images)
