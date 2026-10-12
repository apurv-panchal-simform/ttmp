# TMP pipeline

Extraction, chunking and retrieval for New Zealand Temporary Traffic Management Plans. The design is in
`TMP Combined Plan.html` (not committed; it lives next to the repo). The build has 8 phases: reading, handlers,
diagrams and images, facts, chunking and index, retrieval L0+1, planner, operations. This repo is at
**Phase 3: Diagrams and images** (vector overlay, legend, symbols, regions, image lineage; X7-X10 prompts and checks),
built on the Phase 2 handlers (text, tables, checkboxes, risk matrix and register, numerics) and the Phase 1 reading
pipeline. Model calls (X2-X10) are wired as prompts, validators and a client interface only; no live call has been made.

```
uv sync
uv run pytest -q                      # unit tests; the sample-PDF tests skip without tests/data/sample.pdf
TMP_REQUIRE_SAMPLE=1 uv run pytest    # CI: a missing sample fails instead of skipping
uv run ruff check src tests && uv run mypy

# PDF -> element store, zones, routes, records (out/<doc_id>/)
uv run tmp-pipeline "TMP V1 (...).pdf" --out out/ [--force] [--skip-render] [--template file.toml] \
    [--gold tests/data/gold/sample.gold.json] [--model off|replay --model-fixtures DIR]

# score an existing run against the gold set
uv run tmp-pipeline-gold out/sha256-... tests/data/gold/sample.gold.json

# Document Intelligence track (separate; default only evaluates when DI is needed)
uv run tmp-pipeline-di "TMP V1 (...).pdf" --doc-dir out/sha256-... [--cache-only | --live]
```

`--live` needs `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` in the environment (the key is never logged); install the
`azure` extra to authenticate with `DefaultAzureCredential` instead. Exit codes: 0 ok, 1 handler failures or
incomplete coverage, 2 bad input, config or schema (message on stderr).

## Output layout (`out/<doc_id>/`)

| File | Content |
| --- | --- |
| `elements.json` | The element store: every item with page, boxes, zone, type, flags, route and parent. `schema_version` 1 |
| `records.json` | Records produced by handlers: typed values with status, evidence element ids and a version stamp |
| `sheets.json` | The diagram sheets as read by the diagram stage: symbols, legend, notes, regions, images, title block |
| `router.log` | One JSON line per element: route, rule, triage, outcome, claimed_by, error |
| `run.log` | One JSON line per event, with `run_id`, `doc_id`, `level`; stages log started/finished/failed |
| `report.md` | Per-page zones, routes, flags, handler failures, LEFTOVER list; records by kind, unverified values, rating mismatches, refers-elsewhere, struck records, checkbox answers to confirm, gold score |
| `master/` | 200 DPI page renders (`bbox_px` values refer to these); `pageNN_overlay.webp` is a diagram sheet without its photo |
| `.done` / `FAILED` | Success marker (a rerun on the same hash is skipped) / failure marker with stage and traceback |

A failed run also writes `elements.partial.json`. Re-running a failed document works; the marker is cleared.

## How the pieces fit

```
intake -> layers -> elements -> zones -> furniture -> masters -> diagram stage -> router -> handlers -> records
 (hash)   (reader)  (coarse)   (per region) (header/footer) (200 DPI) (symbols, legend)  (decide, apply, run)
```

- `config.py` + `templates/default.toml`: typed, validated config. Layout cues (header band, rating colours,
  form names) and tunable thresholds live in the template, so a new company's layout is a new file, not a code
  change. Unknown keys, wrong types and bad regexes fail at load with the key path.
- `models.py` / `vocab.py`: `Element`, `ElementStore`, `PageInfo`; closed vocabularies (`ElementType`, `Zone`,
  `Flag`, `Route`, `Outcome`) are validated on every assignment.
- `geometry.py`: the one place for box maths and point/pixel conversion.
- `router.py`: `decide()` is pure (one `Decision` per element, from ordered named rules); `apply_decisions()`
  writes them back; `route()` runs handlers with per-element failure isolation.
- `contracts.py`, `records.py`: the handler contract and the records model (below).
- `model/`: model client interface, caching, recording and replay. No live client yet.
- `handlers/`: `text.py` (§2.1), `tables.py` + `grid.py` (§2.2), `risk.py` (§2.3), `checkboxes.py` (§2.4); `numerics.py`
  (§2.5); `diagram.py` + `diagram_model.py` (§2.6), `image.py` + `imageread.py` + `imageio.py` (§2.7). `build_registry()`
  wires them. The counting stubs (`handlers/stubs.py`) remain for tests of the router alone.
- `diagrams/`: the pre-router diagram stage (§2.6). `overlay.py` re-reads a diagram page with pypdfium2 in drawing order
  (colours, dashes, text, form transforms composed); `textlines.py` rebuilds rotated text from glyph order;
  `symbols.py` groups touching objects into signs and classifies them; `legend.py` decodes swatches; `regions.py`
  finds notes, title block, insets and figures; `assoc.py` links RP labels, TEMPORARY plates and roads;
  `lineage.py` links reused pictures by perceptual hash; `stage.py` adds `vector_symbol`, `legend`, `notes` and `region`
  elements under each sheet and retypes the drawing frame the table finder saw to a `region`. Handlers never add
  elements; the stage does, and the diagram handler reads its `SheetStore`.
- `catalog.py` + `templates/catalog.yaml`: the minimal schema catalog (entities, fields, synonyms, units, critical,
  section headings). Synonyms drive code; descriptions drive prompts; adding a field never edits a prompt.
- `model/`: `prompts.py` (X2-X10 text from the plan, versioned), `validators.py` (quote verbatim, value in quote, unit
  matches, X2/X4/X6/X3 output checks; for images, cited words must exist, hold the value and sit together, X8 claims
  may only cite existing symbols and numbers on the sheet), `client.py` (interface, cache, record/replay). Without a model, unknown
  headings stay unnamed, X5-only fields are absent, and critical facts nobody judged are stored `unverified`.
- `gold/`: the scorer for the extraction gold set (§7.1), including sheet facts (legend, notes, roads exact; symbol
  recall and precision). `tests/data/gold/sample.gold.json` is a **draft** keyed by
  the same person who wrote the extractor; it needs an independent second keying before its numbers mean accuracy.
- `di/`: Document Intelligence track. The core package never imports it (a test enforces this).

## Routes and outcomes

Routes: `TEXT`, `TABLE`, `RISK_TABLE`, `CHECKBOX`, `FIGURE`, `DIAGRAM`, `METADATA`, `STRUCTURE`, `DECORATIVE`,
`LEFTOVER`. Outcomes: `sent` (given to the handler), `claimed` (owned by a parent table, figure or sheet),
`skipped` (decorative), `kept` (structure), `metadata`, `leftover` (flagged), `failed` (the handler raised).
A checkbox is always owned by the CHECKBOX handler, even inside a table (the table is context only; table
handlers skip checkbox children). On a diagram sheet or inside a figure it belongs to that sheet or figure.

## Adding a handler

1. Subclass `BaseHandler`: set `route` and `version`, implement `handle(ctx, element) -> HandlerResult`.
   `ctx` has the element store, record store, config, version stamp and optional model client.
2. Return records, element flags/metadata and, rarely, an outcome override. Never mutate the store directly.
   Use `begin` / `finish` for anything that needs more than one element (stitching, pairing).
3. Register it: `registry.replace(MyHandler())`. A route with `sent` elements and no handler raises
   `MissingHandlerError` before anything runs; an exception in `handle` fails only that element.
4. Every value is a `FieldValue` (typed value, raw text, unit, status) with `Evidence` (element id, fingerprint,
   box); stamp the record with `Stamp(handler, version, config_hash, ...)`.

## Adding a template

Copy only the keys that differ into a new `.toml` and pass `--template`. Nested tables merge; lists replace.
The template name and config hash are stamped on every run and record.

## What the handlers guarantee

- Text is stored as read. The only rewriting is the removal of a line break after a hyphen; "du e" stays "du e".
- A value that cannot be typed is `unverified` with the reason, never guessed. Ratings that disagree with the matrix
  are flagged `rating_mismatch` with both values and never corrected. "See Above" is `refers_elsewhere`.
- Every value carries its raw text, status, method and the elements it came from (id, fingerprint, box).
- Handlers are isolated: one failing element or `finish` fails only itself and appears in `report.md`.

## Diagram sheets: what is read from vectors, what from a model

Everything below is code, from the drawing's own objects, and stored `verified` with method `vector`: title block (sheet,
title, rev, date), legend (swatch colour -> meaning), notes, roads, signs with their exact text and class, RP labels,
distance labels, cone runs, areas, controllers, which TEMPORARY plate belongs to which speed disc, nearest RP and road
for each sign. A pictogram with no text is `needs_caption`. Text in a broken font encoding is flagged
`low_text_quality` and never repaired. A model adds, only through `ask` and only after the code checks:

| Prompt | Used for | Checked against |
| --- | --- | --- |
| X10 | caption of a pictogram | unclear and low confidence are not captions; digits must come from the symbol |
| X8 | spatial summary of a sheet (signs in order, layout, pedestrians) | every cited symbol id exists, every number is on the sheet |
| X7 | pictures beside the plan and informative figures (page 1 site plan) | cited OCR/text words exist, hold the number, sit together; failed values get a second read |
| X9 | a sheet with no vector overlay (flattened) | as X7, plus a second read on a tighter crop without the word list; only agreed and grounded values are verified |

Without a model the sheet keeps all its vector facts, the summary is empty and the symbols and figures wait for captions.
The direction of travel on an approach is not recoverable from vectors: signs are numbered along the road with the rule
"RP rising" written on each (`direction_of_travel: undecided`) and X8 is the cross-check.

## Known gaps (Phase 3)

- No live model call has run. X2-X10 are exercised against fakes and recorded fixtures only.
- Sheet gold (`sheets` in `sample.gold.json`) is a draft keyed from the vectors and the page renders by the extractor's
  author; its 100% is a regression guard. The legend swatch for the Pram Ramp reads as `grey hatched` from the vectors
  (the plan says yellow hatched; the render shows yellow): a second keyer should confirm.
- Sign order is along the road axis, not in travel direction (above). Dimension lines without a label and leader arrows are
  counted but not listed as symbols.
- The page 1 site plan's labels ("RHB 0.3M O.S", "Main=100PE") are not in the text layer: until Document Intelligence has
  run, X7 values read from pixels only are `unverified`.
- A flattened sheet is recognised only when its title block is text (`SHEET NO` in the text layer); a fully scanned
  sheet needs Document Intelligence first.
- Cross-sheet checks (TMD id conflicts between stage lists and sheet titles, X11) are Phase 4.

## Known gaps (Phase 2)

- Cross-record merging, conflicts (TMD ids), approval status and "See Above" resolution are Phase 4.
- `ElementStore.metadata` is still an untyped dict (keys are read by string); `_repeating_images` is quadratic.
- The sample PDF is a client document: `tests/data/sample.pdf` is a symlink and must not be committed.

## Tests

`tests/helpers.py` has shared builders (`grid`, `header_footer`, `prepare`, and `sheet_page`, `speed_disc`, `plate`, `label`
for synthetic diagram sheets); fixtures are in `conftest.py`.
`tests/test_golden_sample.py` pins zones, element counts, routes and flags of the sample run: a refactor must
not change them, and a deliberate change regenerates `tests/data/golden_sample_snapshot.json`.
