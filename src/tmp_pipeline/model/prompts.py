"""Prompts X2-X12 and Q1, Q3, Q4 (plan parts 2-5), text copied from the plan. Each builder returns a ``ModelRequest`` whose key
covers the prompt version, so editing a prompt here (and bumping its version) invalidates exactly its cache."""
from __future__ import annotations

import json

from .client import ModelRequest

PROMPT_VERSIONS = {"X2": "1.0", "X4": "1.0", "X5": "1.0", "X6": "1.0",
                   "X7": "1.0", "X8": "1.0", "X9": "1.0", "X10": "1.0", "X11": "1.0", "X12": "1.0",
                   "X13": "1.0", "Q1": "1.0", "Q3": "1.1", "Q4": "1.0"}

# Deployments named in the plan; change them in one place when the model choice is settled (open decision).
# Image reads (X7, X8, X9, X10) run on gpt-6-luna by decision (plan 2.2 names gpt-6-sol); the second read stays gpt-5.5.
MODELS = {"X2": "gpt-5.4", "X4": "gpt-6-luna", "X5": "gpt-5.4", "X6": "gpt-6-sol",
          "X7": "gpt-6-luna", "X8": "gpt-6-luna", "X9": "gpt-6-luna", "X10": "gpt-6-luna",
          "X11": "gpt-5.4", "X12": "gpt-6-luna", "X13": "gpt-6-luna", "Q1": "gpt-6-luna",
          "Q3": "gpt-6-luna", "Q4": "gpt-6-sol"}
# Answers (Q3) are written by gpt-6-luna without thinking by decision (2026-10-14: same text-set score as gpt-6-sol at
# a twentieth of the cost); a failed answer is repaired (Q4) by gpt-6-sol. Per-prompt thinking overrides the default.
REASONING = {"Q3": "none"}
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

_X13_SYSTEM = """You look at an image from a New Zealand Traffic Management Plan (TMP) and say
what it shows, so that later questions about the image can be answered without it.
What the drawing's vectors and text layer already give is listed below with IDs
(items, distances, text): use those IDs and never invent signs, numbers, text or
distances. Report only what is visible; anything you work out rather than see goes
in "inferred". If you cannot read something, list it in "unreadable"."""

_X13_USER = """[{images}]
What this is: {scope}
Diagram: {title}
Legend: {legend}
Notes panel: {notes}
Items (id | what | where): {items}
Distances (id | value | the dimension line runs from ... to ..., from the geometry): {distances}
Text printed with a broken font (id | as printed): {garbled}
Tell:
1. description: 3-5 plain sentences on what the image shows{whole}.
2. drawing_type and purpose (one sentence each){whole}.
3. items: for each item ID you can see, what it looks like (shape, colour, text on
   it, arrows) and which side of the road it is on (north/south/east/west of the
   road, or "unclear"); give a sign's meaning only if it is clear from the image.
   Things with no ID that matter go in with "item_id": "new" and where they are.
4. distances: for each distance ID, what it measures, in words, from the image.
5. relations between items ("before", "after", "opposite", "next to", "inside").
6. pedestrians and vehicles: how each is routed past the works.
7. garbled_text: what each broken-font line actually says, letter for letter
   where you can see it in the image.
8. unreadable: anything too small, cut off or blurred to read.
9. inferred: statements you made that you did not read directly from the image.
Reply JSON:
{{"description": "...", "drawing_type": "...", "purpose": "...",
 "items": [{{"item_id": "...", "looks_like": "...", "road_side": "...", "meaning": "...", "where": "..."}}],
 "distances": [{{"item_id": "...", "measures": "..."}}],
 "relations": [{{"a": "<item id>", "relation": "...", "b": "<item id>"}}],
 "pedestrians": "...", "vehicles": "...",
 "garbled_text": [{{"id": "...", "reads_as": "..."}}],
 "unreadable": [{{"where": "...", "what": "..."}}],
 "inferred": ["..."]}}"""

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

_X12_SYSTEM = """You write a one or two sentence note saying which questions a chunk from a
New Zealand Traffic Management Plan can answer. Do not add any fact, number,
name or date that is not in the chunk."""

_X12_USER = """Chunk type: {chunk_type}
Chunk:
{content}
Reply JSON: {{"summary": "<1–2 sentences>"}}"""


def _req(pid: str, system: str, user: str, images: tuple[bytes, ...] = (), model: str | None = None) -> ModelRequest:
    params: dict = {"temperature": 0}
    if pid in REASONING:
        params["reasoning_effort"] = REASONING[pid]
    return ModelRequest(pid, PROMPT_VERSIONS[pid], model or MODELS[pid], user, system, images, params)


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


def x13_read_image(images: tuple[bytes, ...], image_names: str, scope: str, title: str, legend: str, notes: str,
                   items: str, distances: str, garbled: str, whole: bool = True) -> ModelRequest:
    """``whole`` False for a tile of a larger sheet: the description and purpose are left to the whole-sheet read."""
    tail = "" if whole else " (this is one part of a larger sheet: describe only this part)"
    return _req("X13", _X13_SYSTEM, _X13_USER.format(images=image_names, scope=scope, title=title, legend=legend,
                                                    notes=notes, items=items or "(none)", distances=distances or "(none)",
                                                    garbled=garbled or "(none)", whole=tail), images)


def x10_caption_symbol(crop_png: bytes, legend: str, colours: str) -> ModelRequest:
    return _req("X10", "", _X10_USER.format(legend=legend, colours=colours), (crop_png,))


def x11_match_mention(sheet_list: list[tuple[str, str]], mention_text: str, context: str) -> ModelRequest:
    """``sheet_list`` is (tmd_id, title) per sheet, in sheet order."""
    sheets = "\n".join(f"{tid}: {title}" for tid, title in sheet_list) or "(none)"
    return _req("X11", _X11_SYSTEM, _X11_USER.format(sheet_list=sheets, mention_text=mention_text, context=context))


def x12_chunk_summary(chunk_type: str, content: str, attempt: int = 1) -> ModelRequest:
    """``attempt`` 2 is the one retry after a failed number guard; it is a separate request (and cache entry)."""
    req = _req("X12", _X12_SYSTEM, _X12_USER.format(chunk_type=chunk_type, content=content))
    return req if attempt == 1 else ModelRequest(req.prompt_id, req.prompt_version, req.model, req.user, req.system,
                                                 req.images, {**req.params, "attempt": attempt})


# --- retrieval (plan part 5) -------------------------------------------------------------------------------------
# Q3 and Q4 answer in plain text; the text travels inside {"answer": ...} so every prompt has the same
# structured-output shape.

QUESTION_TYPES = ("fact", "procedure", "diagram", "status", "calculation", "time_check", "across_tmps", "summary",
                  "general_rule", "version_change", "out_of_scope")
Q1_DETAILS = ("tmp_ref", "address", "road", "suburb", "tmd", "rp", "date_text", "date_iso", "time", "day_type",
              "organisation", "entity", "field")

_Q1_SYSTEM = """You classify questions about New Zealand Temporary Traffic Management Plans (TMPs)
so the right tools can be called. You never answer the question.
Question types (a question can have several):
- fact:          a stored value about this TMP (dates, roads, RPs, speeds, AADT,
                 parties, contacts, ticked boxes, risk ratings, stages, diagram titles)
- procedure:     what to do, how something is done, what happens if… (contingency,
                 monitoring, installation/removal, notification)
- diagram:       anything shown on a traffic management diagram (signs, order of signs,
                 cones, taper, safety zones, controllers, pedestrian routes, notes)
- status:        approval, who has or hasn't signed, conflicts or errors in the document
- calculation:   needs arithmetic on stored values (lengths, differences, durations,
                 comparisons)
- time_check:    whether something is allowed or in force at a date or time
- across_tmps:   about several or all TMPs, or finding TMPs by road, date, organisation
- summary:       an overview of this TMP or of one section
- general_rule:  a general traffic management rule, not specific to this site
- version_change: what changed between versions of this TMP
- out_of_scope:  not answerable from TMPs (for example checks against official standards
                 that are not loaded, or unrelated topics)"""

_Q1_USER = """Today (NZ time): {now_nz}
Current TMP in this session: {current_tmp}
Conversation so far (last 3 turns): {recent_turns}
Question: {question}
Do this:
1. Rewrite the question so it stands alone: replace "it", "that road", "the other one",
   "same for weekends" with what they refer to. Keep the user's wording otherwise.
2. List every question type it contains.
3. Pull out details exactly as written. Dates and days: also give an ISO date if the
   question names a day relative to today ("Wednesday", "tomorrow").
4. If you are unsure of a type, set confidence to "low". Do not guess.
Reply JSON:
{{"standalone_question": "...",
 "types": ["..."],
 "confidence": "high|medium|low",
 "details": {{"tmp_ref": null, "address": null, "road": null, "suburb": null,
             "tmd": null, "rp": null, "date_text": null, "date_iso": null,
             "time": null, "day_type": null, "organisation": null,
             "entity": null, "field": null}}}}
Examples:
Q: "how long is the 30 zone"
A: {{"standalone_question": "How long is the 30 km/h temporary speed limit zone?",
    "types": ["fact", "calculation"], "confidence": "high",
    "details": {{"entity": "speed_limit", "field": "length"}}}}
Q: "is this one approved?"
A: {{"standalone_question": "Is this TMP approved?", "types": ["status"], "confidence": "high"}}
Q: "can the crew still be on site at 4pm on a school day"
A: {{"standalone_question": "Is work allowed at 16:00 on a school day?",
    "types": ["time_check"], "confidence": "high",
    "details": {{"time": "16:00", "day_type": "school_day"}}}}
Q: (previous turn asked about school-day hours) "and weekends?"
A: {{"standalone_question": "What are the weekend work hours for this TMP?",
    "types": ["fact"], "confidence": "high", "details": {{"entity": "shift_window", "day_type": "weekend"}}}}
Q: "what signs does a westbound driver pass on the stop go plan"
A: {{"standalone_question": "Which signs does a westbound driver pass, in order, on the Stop/Go diagram?",
    "types": ["diagram"], "confidence": "high", "details": {{"tmd": "Stop Go"}}}}
Q: "what happens if there's a spill"
A: {{"standalone_question": "What must be done if there is a spill?", "types": ["procedure"], "confidence": "high"}}
Q: "which TMD is TMD4"
A: {{"standalone_question": "Which diagram does 'TMD4' refer to?", "types": ["fact", "status"],
    "confidence": "high", "details": {{"tmd": "TMD4"}}}}
Q: "is a 30m taper ok for a 50 road"
A: {{"standalone_question": "Is a 30 m taper sufficient on a 50 km/h road?",
    "types": ["out_of_scope", "diagram"], "confidence": "medium"}}
Q: "any TMPs on sunrise ave next week by fulton hogan"
A: {{"standalone_question": "Which TMPs on Sunrise Avenue by Fulton Hogan are active next week?",
    "types": ["across_tmps"], "confidence": "high",
    "details": {{"road": "Sunrise Avenue", "organisation": "Fulton Hogan", "date_text": "next week"}}}}"""

_Q3_SYSTEM = """You answer questions about New Zealand Traffic Management Plans using ONLY the
context below. Answer in plain text.
Rules:
1. Copy every number, time, date, RP, phone number and name exactly as it appears
   in the context.
2. Never do arithmetic. If a calculation is in the context, use its result and
   show its working.
3. Cite every fact as (form, section, page).
4. If a value's status is "unverified", say it is unconfirmed.
5. If a value has a conflict, give every version and where it appears. Do not choose.
6. If something is blank in the document, say it is blank. Never fill it in.
7. "Not ticked" means not ticked on the form; don't turn it into "no" unless the
   form is a Yes/No question.
8. Keep general standards separate from this site's facts, and say which is which.
9. Describe diagrams in words: signs in order, positions, distances, with the TMD
   and page.
10. If the context doesn't answer the question, say so and say what is missing.
11. Answer the question first, then the detail. Style for this user: {role_style}
12. Questions about an image or drawing are answered from the image facts in the
   context (text as printed, positions, order, distances and what each dimension
   line runs between, appearance). Give positions as written there. Say what a
   distance measures only as stated; separate what the image shows from what is
   inferred, and list unreadable or missing items instead of guessing. If several
   diagrams match, answer for each one and name its TMD and page.
13. If the question asks for JSON or a list, put it inside the answer text."""

_Q3_USER = """QUESTION: {standalone_question}
{context}
Reply JSON: {{"answer": "<the answer, plain text>"}}"""

_Q4_SYSTEM = """Your previous answer broke the rules. Rewrite it using ONLY the context."""

_Q4_USER = """Problems found by the checker:
{problems}
Previous answer: {answer}
QUESTION: {standalone_question}
{context}
Rewrite the answer so every problem is fixed. Same rules as before.
Reply JSON: {{"answer": "<the answer, plain text>"}}"""


def q1_classify(question: str, now_nz: str, current_tmp: str | None, recent_turns: list[str]) -> ModelRequest:
    turns = "\n".join(recent_turns[-3:]) or "(none)"
    return _req("Q1", _Q1_SYSTEM, _Q1_USER.format(now_nz=now_nz, current_tmp=current_tmp or "none",
                                                  recent_turns=turns, question=question))


def q3_answer(standalone_question: str, context: str, role_style: str) -> ModelRequest:
    return _req("Q3", _Q3_SYSTEM.format(role_style=role_style),
                _Q3_USER.format(standalone_question=standalone_question, context=context))


def q4_repair(problems: list[str], answer: str, standalone_question: str, context: str) -> ModelRequest:
    return _req("Q4", _Q4_SYSTEM, _Q4_USER.format(problems="\n".join(f"- {p}" for p in problems), answer=answer,
                                                  standalone_question=standalone_question, context=context))
