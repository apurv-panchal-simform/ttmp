"""Prompts X2-X6 (plan part 2), text copied from the plan. Each builder returns a ``ModelRequest`` whose key
covers the prompt version, so editing a prompt here (and bumping its version) invalidates exactly its cache."""
from __future__ import annotations

import json

from .client import ModelRequest

PROMPT_VERSIONS = {"X2": "1.0", "X3": "1.0", "X4": "1.0", "X5": "1.0", "X6": "1.0"}

# Deployments named in the plan; change them in one place when the model choice is settled (open decision).
MODELS = {"X2": "gpt-5.4", "X3": "gpt-6-sol", "X4": "gpt-6-luna", "X5": "gpt-5.4", "X6": "gpt-6-sol"}

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


def _req(pid: str, system: str, user: str, images: tuple[bytes, ...] = ()) -> ModelRequest:
    return ModelRequest(pid, PROMPT_VERSIONS[pid], MODELS[pid], user, system, images, {"temperature": 0})


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
