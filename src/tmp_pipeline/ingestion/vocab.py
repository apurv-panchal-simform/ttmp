"""Closed vocabularies. StrEnum members compare, hash and serialise as their plain string values, so stored
JSON and ``zone == "risk"`` style comparisons keep working while a misspelling now raises at the point of use."""
from __future__ import annotations

from enum import StrEnum


class ElementType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    TABLE_ROW = "table_row"
    KV_FIELD = "kv_field"
    CHECKBOX = "checkbox"
    NUMERIC = "numeric"
    IDENTIFIER = "identifier"
    IMAGE = "image"
    FIGURE = "figure"
    PHOTO = "photo"
    DIAGRAM_SHEET = "diagram_sheet"
    VECTOR_SYMBOL = "vector_symbol"
    LEGEND = "legend"
    NOTES = "notes"
    REGION = "region"
    # Phase 1 coarse types
    TEXT_BLOCK = "text_block"
    PAGE_FURNITURE = "page_furniture"
    OCR_WORD = "ocr_word"


class Zone(StrEnum):
    FORMS = "forms"
    RISK = "risk"
    NARRATIVE = "narrative"
    DIAGRAM = "diagram"
    GENERIC_STANDARD = "generic_standard"
    BLANK = "blank"
    UNCLASSIFIED = "unclassified"


class Flag(StrEnum):
    STRUCK = "struck"
    DUPLICATE = "duplicate"
    BOILERPLATE = "boilerplate"
    GENERIC = "generic"
    CONTACT_INFO = "contact_info"
    ID_CONFLICT = "id_conflict"
    RATING_MISMATCH = "rating_mismatch"
    NEEDS_CAPTION = "needs_caption"
    LOW_TEXT_QUALITY = "low_text_quality"
    LEFTOVER = "leftover"
    DECORATIVE = "decorative"
    # Phase 2
    REFERS_ELSEWHERE = "refers_elsewhere"
    MERGED = "merged"
    UNVERIFIED = "unverified"
    # Phase 4
    CONFLICT = "conflict"


class Route(StrEnum):
    TEXT = "TEXT"
    TABLE = "TABLE"
    RISK_TABLE = "RISK_TABLE"
    CHECKBOX = "CHECKBOX"
    FIGURE = "FIGURE"
    DIAGRAM = "DIAGRAM"
    METADATA = "METADATA"
    STRUCTURE = "STRUCTURE"
    DECORATIVE = "DECORATIVE"
    LEFTOVER = "LEFTOVER"


class Outcome(StrEnum):
    SENT = "sent"              # handed to the route's handler
    CLAIMED = "claimed"        # owned by a parent table, figure or sheet
    SKIPPED = "skipped"        # decorative, logged and not read
    KEPT = "kept"              # structure kept as is
    METADATA = "metadata"      # promoted to document metadata
    LEFTOVER = "leftover"      # unclaimed, flagged
    FAILED = "failed"          # the handler raised; the run continued


class SymbolKind(StrEnum):
    """What a drawn symbol on a diagram sheet is (plan section 2.6, symbol classes)."""
    SPEED_AHEAD_SIGN = "speed_ahead_sign"
    TEMPORARY_SPEED_LIMIT = "temporary_speed_limit"
    SPEED_LIMIT = "speed_limit"
    WARNING_SIGN = "warning_sign"
    ADVANCE_SIGN = "advance_sign"
    SCHOOL_ZONE = "school_zone"
    END_OF_WORKS_SIGN = "end_of_works_sign"
    SUPPLEMENTARY_PLATE = "supplementary_plate"
    BUS_STOP_SIGN = "bus_stop_sign"
    TRAFFIC_CONTROLLER = "traffic_controller"
    AREA = "area"
    LINE = "line"
    CONES = "cones"
    RP_LABEL = "rp_label"
    DIMENSION_LABEL = "dimension_label"
    ROAD_LABEL = "road_label"
    PLACE_LABEL = "place_label"
    HOUSE_LABEL = "house_label"
    CALLOUT = "callout"
    PICTOGRAM = "pictogram"      # a sign with no text; needs a caption (X10)
