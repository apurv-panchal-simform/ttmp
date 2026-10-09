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
