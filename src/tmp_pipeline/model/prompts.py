"""Prompts X2-X11 (plan parts 2 and 3), text copied from the plan. Each builder returns a ``ModelRequest`` whose key
covers the prompt version, so editing a prompt here (and bumping its version) invalidates exactly its cache."""
from __future__ import annotations

import json

from .client import ModelRequest

PROMPT_VERSIONS = {"X2": "1.0", "X3": "1.0", "X4": "1.0", "X5": "1.0", "X6": "1.0",
                   "X7": "1.0", "X8": "1.0", "X9": "1.0", "X10": "1.0", "X11": "1.0"}

# Deployments named in the plan; change them in one place when the model choice is settled (open decision).
MODELS = {"X2": "gpt-5.4", "X3": "gpt-6-sol", "X4": "gpt-6-luna", "X5": "gpt-5.4", "X6": "gpt-6-sol",
          "X7": "gpt-6-sol", "X8": "gpt-6-sol", "X9": "gpt-6-sol", "X10": "gpt-6-sol",
          "X11": "gpt-5.4"}
# The independent second read of an image (failed checks, critical fields): a different model generation (plan 2.6, 2.7).
READ2_MODEL = "gpt-5.5"

_X2_SYSTEM = """You match the names used in a table from a New Zealand Traffic Management Plan
to fields in our schema. Tables come from many companies, so the same thing can
be named in many ways. Match on meaning, not exact wording.
You never invent, change or copy values."""

_X2_USER = """Schema catalog (entity → fields with descriptions):
{schema_catalog}
Where the table sits:
  Section heading: {section_heading}
  Group label:     {group_label}          (for example a rotated side heading)
  Nearby text:     {nearby_text}
Table shape: {shape}                      (records_table → names are column headers;
                                             label_value_grid → names are labels)
Names:          {names}
Sample values:  {sample_values}
Task:
1. Decide which entity this table describes, or "unknown".
2. Map each name to one field from the catalog. Use units and sample values as clues.
3. If one name holds two things (for example "Start RP / House #"), map it to the
   main field and explain in "note".
4. If no field fits, use OTHER and suggest a short snake_case name.
5. Do not guess. If you are not confident, set confidence to "low".
Reply JSON:
{{"entity": "<entity or unknown>", "entity_confidence": "high|medium|low",
 "mapping": [{{"name": "<name>", "field": "<field or OTHER>",
              "confidence": "high|medium|low", "note": "<short or null>"}}],
 "unmapped": [{{"name": "<name>", "suggested_field": "<snake_case>"}}]}}"""

_X3_SYSTEM = """You read exactly what is visible in an image cropped from one table cell.
Never guess. If something is unclear, say so."""

_X3_USER = """[image: crop of the cell area]
Column header:              {column_header}
Row(s) this cell covers:    {row_labels}
Expected kind of value:     {expected_type}
What value does this image show for this column?
- If it is a sign or symbol, give the text or number on it and describe the sign.
- If text or numbers are unreadable, set "readable" to false.
Reply JSON: {{"value": "<text exactly as shown>", "visual_type": "<sign, symbol, stamp, other>",
             "description": "<short>", "readable": true/false}}"""

_X4_SYSTEM = """You identify what a section of a New Zealand Traffic Management Plan is about.
Headings vary between companies; match on meaning, not wording."""

_X4_USER = """Section types (from the catalog, with descriptions):
{section_catalog}
Heading path:  {heading_path}
Start of text: {first_400_characters}
Which section type is this? If none fits, answer "other" and suggest a name.
Reply JSON: {{"section_type": "<type or other>", "suggested_name": "<or null>",
             "confidence": "high|medium|low"}}"""

_X5_SYSTEM = """You extract facts from one section of a New Zealand Traffic Management Plan.
Use only what the text says. Never add, infer or round anything."""

_X5_USER = """Section type: {section_type}
Fields to look for (from the catalog, with descriptions and units):
{fields_for_this_section_type}
Section text:
{section_text}
Rules:
- For every fact, copy the exact sentence or phrase it comes from into "quote",
  character for character.
- If a field is not stated, leave it out. Do not guess.
- Anything important that fits no field goes in "other_facts", also with a quote.
Reply JSON:
{{"facts": [{{"field": "<field>", "value": <value>, "unit": "<or null>", "quote": "<exact text>"}}],
 "other_facts": [{{"description": "<short>", "quote": "<exact text>"}}]}}"""

_X6_SYSTEM = """You check whether a short quote from a New Zealand Traffic Management Plan
supports one specific claim. Judge only from the quote. Be strict: if the
quote could support a different meaning, answer false."""

_X6_USER = """Quote:  {quote}
Claim:  {field} = {value} {unit}
Meaning of this field (from the catalog): {field_description}
Does the quote state exactly this claim, with this meaning?
Reply JSON: {{"supported": true/false, "reason": "<one short sentence>"}}"""


_X7_SYSTEM = """You read an image from a New Zealand Traffic Management Plan.
Report only what is visible. Never guess, complete or correct text."""

_X7_USER = """[image: the crop]
Context: section "{section_heading}", nearby text "{nearby_text}", drawn title "{title}"
OCR words found inside this image (id: text):
{ocr_words}
Tasks:
1. Say what kind of image this is (site plan, aerial photo, map, sign, figure, chart, other).
2. List every label or note visible, exactly as written, and what it points to.
3. List every number with its unit, with the OCR word id(s) it comes from.
   If a number is visible but not in the OCR list, set ocr_ids to [] and say so.
4. If colours or symbols carry meaning (legend, key, coloured lines), list them.
   If no key is shown, list the colours used without interpreting them.
5. Write a 2-4 sentence plain description of what the image shows.
Reply JSON:
{{"kind": "...",
 "labels": [{{"text": "...", "points_to": "...", "ocr_ids": [...]}}],
 "numbers": [{{"value": ..., "unit": "...", "meaning": "...", "ocr_ids": [...]}}],
 "legend": [{{"colour_or_symbol": "...", "meaning": "..."}}],
 "description": "..."}}"""

_X8_SYSTEM = """You read a temporary traffic management diagram (TMD) from a New Zealand TMP.
The facts have already been extracted from the drawing's vectors and are listed
below with IDs. Use them; do not invent signs, numbers or RPs."""

_X8_USER = """[image 1: vector overlay only (design without the photo)]
[image 2: master render (design over the aerial photo)]
Sheet: {sheet_no} \u00b7 {title}
Legend: {legend}
Extracted symbols (id, kind, text/value, RP, road, box): {symbol_list}
Notes panel: {notes}
Describe:
1. For each approach (direction of travel), the signs a driver passes, in order, by symbol ID.
2. Where the work area, safety zones and traffic controllers are, relative to roads and house numbers.
3. How pedestrians are managed.
Every statement must list the symbol IDs it relies on. If something can't be
determined from the image, say so.
Reply JSON:
{{"approaches": [{{"direction": "...", "signs_in_order": ["<symbol_id>"], "notes": "..."}}],
 "layout": [{{"statement": "...", "symbol_ids": ["..."]}}],
 "pedestrians": [{{"statement": "...", "symbol_ids": ["..."]}}],
 "unclear": ["..."]}}"""

_X9_SYSTEM = """You read one region of a temporary traffic management diagram (TMD) from a
New Zealand TMP. Report only what is visible. Never guess."""

_X9_USER = """[image: the region crop]
Region type: {region_type} (plan tile, notes, legend, title block)
Legend for this sheet: {legend}
OCR words in this region (id: text): {ocr_words}
Extract:
- signs: type, text on the sign, which approach it faces, order along that approach
- distances and lengths with units (taper, safety zones, work area, sign spacing)
- cone spacing, RPs, controller positions, pedestrian routes, bus stop changes
- every number must list the OCR word id(s) it comes from
- anything that fits nowhere goes in "other", exactly as written
Reply JSON: {tmd_region_schema}"""

TMD_REGION_SCHEMA = (
    '{"signs": [{"type": "...", "text": "...", "approach": "...", "order": 1, "ocr_ids": []}],'
    ' "distances": [{"value": 0, "unit": "m", "meaning": "...", "ocr_ids": []}],'
    ' "rps": [{"value": 0.0, "ocr_ids": []}],'
    ' "cones": [{"spacing": 0, "unit": "m", "where": "...", "ocr_ids": []}],'
    ' "controllers": [{"position": "...", "ocr_ids": []}],'
    ' "pedestrians": [{"statement": "...", "ocr_ids": []}],'
    ' "other": [{"text": "...", "ocr_ids": []}]}')

_X10_USER = """[image: crop of one symbol from a traffic management diagram]
Legend for this sheet: {legend}
Colours in the symbol: {colours}
In a few words, what is this symbol? (for example "keep-left arrow sign",
"cone bar", "bus stop sign"). If unsure, say "unclear".
Reply JSON: {{"caption": "...", "confidence": "high|medium|low"}}"""

_X11_SYSTEM = """You match a reference to a traffic management diagram (TMD) to one diagram
sheet from the same document. Match on meaning, not wording. Never guess."""

_X11_USER = """Diagram sheets in this document (from their title blocks):
{sheet_list}
Mention: "{mention_text}"
Surrounding sentence: "{context}"
Which sheet does the mention refer to? If none clearly fits, answer "none".
Reply JSON: {{"tmd_id": "<id or none>", "confidence": "high|medium|low", "reason": "<short>"}}"""


def _req(pid: str, system: str, user: str, images: tuple[bytes, ...] = (), model: str | None = None) -> ModelRequest:
    return ModelRequest(pid, PROMPT_VERSIONS[pid], model or MODELS[pid], user, system, images, {"temperature": 0})


def _words(words: list[tuple[str, str]] | None) -> str:
    return "\n".join(f"{i}: {t}" for i, t in words) if words else "(none)"


def catalog_text(entities: dict) -> str:
    """The schema catalog as the prompts show it: one entity per block, one field per line with its description."""
    out = []
    for name, ent in entities.items():
        out.append(f"{name}: {ent.description}")
        out += [f"  {f.name}: {f.desc}" + (f" ({f.unit})" if f.unit else "") for f in ent.fields.values()]
    return "\n".join(out)


def x2_match_names(schema_catalog: str, section_heading: str, group_label: str, nearby_text: str, shape: str,
                   names: list[str], sample_values: list[str]) -> ModelRequest:
    return _req("X2", _X2_SYSTEM, _X2_USER.format(
        schema_catalog=schema_catalog, section_heading=section_heading, group_label=group_label,
        nearby_text=nearby_text, shape=shape, names=json.dumps(names, ensure_ascii=False),
        sample_values=json.dumps(sample_values, ensure_ascii=False)))


def x3_read_cell(image_png: bytes, column_header: str, row_labels: str, expected_type: str) -> ModelRequest:
    return _req("X3", _X3_SYSTEM, _X3_USER.format(column_header=column_header, row_labels=row_labels,
                                                  expected_type=expected_type), (image_png,))


def x4_match_heading(section_catalog: str, heading_path: str, start_of_text: str) -> ModelRequest:
    return _req("X4", _X4_SYSTEM, _X4_USER.format(section_catalog=section_catalog, heading_path=heading_path,
                                                  first_400_characters=start_of_text[:400]))


def x5_extract_facts(section_type: str, fields_text: str, section_text: str) -> ModelRequest:
    return _req("X5", _X5_SYSTEM, _X5_USER.format(section_type=section_type, fields_for_this_section_type=fields_text,
                                                  section_text=section_text))


def x6_judge(quote: str, field: str, value: object, unit: str | None, field_description: str) -> ModelRequest:
    return _req("X6", _X6_SYSTEM, _X6_USER.format(quote=quote, field=field, value=value, unit=unit or "",
                                                  field_description=field_description))


def x7_read_image(image_png: bytes, section_heading: str, nearby_text: str, title: str,
                  ocr_words: list[tuple[str, str]], second_read: bool = False) -> ModelRequest:
    return _req("X7", _X7_SYSTEM, _X7_USER.format(section_heading=section_heading, nearby_text=nearby_text, title=title,
                                                  ocr_words=_words(ocr_words)), (image_png,),
                READ2_MODEL if second_read else None)


def x8_sheet_summary(overlay_png: bytes, master_png: bytes, sheet_no: str, title: str, legend: str, symbol_list: str,
                     notes: str) -> ModelRequest:
    return _req("X8", _X8_SYSTEM, _X8_USER.format(sheet_no=sheet_no, title=title, legend=legend, symbol_list=symbol_list,
                                                  notes=notes), (overlay_png, master_png))


def x9_read_region(crop_png: bytes, region_type: str, legend: str, ocr_words: list[tuple[str, str]] | None,
                   second_read: bool = False) -> ModelRequest:
    """The no-vector fallback. The second read (gpt-5.5, tighter crop) is given no OCR word list, so it is independent."""
    words = None if second_read else ocr_words
    return _req("X9", _X9_SYSTEM, _X9_USER.format(region_type=region_type, legend=legend, ocr_words=_words(words),
                                                  tmd_region_schema=TMD_REGION_SCHEMA), (crop_png,),
                READ2_MODEL if second_read else None)


def x10_caption_symbol(crop_png: bytes, legend: str, colours: str) -> ModelRequest:
    return _req("X10", "", _X10_USER.format(legend=legend, colours=colours), (crop_png,))


def x11_match_mention(sheet_list: list[tuple[str, str]], mention_text: str, context: str) -> ModelRequest:
    """``sheet_list`` is (tmd_id, title) per sheet, in sheet order."""
    sheets = "\n".join(f"{tid}: {title}" for tid, title in sheet_list) or "(none)"
    return _req("X11", _X11_SYSTEM, _X11_USER.format(sheet_list=sheets, mention_text=mention_text, context=context))
