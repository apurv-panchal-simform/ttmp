# TMP Pipeline: how it works, step by step

This document follows a Temporary Traffic Management Plan (TMP) PDF through every stage of the pipeline. A TMP goes in as a PDF; the pipeline extracts its contents, turns them into a checked set of facts, cuts them into searchable chunks, and finally answers questions about them. For each step it names the file and function that does the work, what goes in, what comes out, and the settings that control it.

All links are relative to this file (`docs/`). Line numbers refer to the working tree as of 2026-10-15, on top of commit `6cc89b0` (Phase 6), and include the uncommitted retrieval changes (`only_tmd`, sheet splitting).

---

## Contents

1. [The big picture](#1-the-big-picture)
2. [Commands and output files](#2-commands-and-output-files)
3. [The main run, step by step (Phase 1 reading)](#3-the-main-run-step-by-step)
4. [The model layer and the catalog](#4-the-model-layer-and-the-catalog)
5. [Phases 2-3: diagram stage, router and handlers](#5-phases-2-3-diagram-stage-router-and-handlers)
6. [Phase 4: facts](#6-phase-4-facts)
7. [Phase 5: chunks](#7-phase-5-chunks)
8. [Storage back ends: AI Search, Cosmos DB, PostgreSQL](#8-storage-back-ends)
9. [Phase 6: answering questions](#9-phase-6-answering-questions)
10. [The Document Intelligence track (separate)](#10-the-document-intelligence-track-separate)
11. [Evaluation: gold set and question set](#11-evaluation-gold-set-and-question-set)
12. [Environment, deployment and tests](#12-environment-deployment-and-tests)
13. [Known gaps and things to watch](#13-known-gaps-and-things-to-watch)
14. [Appendix: file-to-step index](#14-appendix-file-to-step-index)

---

## 1. The big picture

The design has 8 phases:
1. reading
2. handlers
3. diagrams and images
4. facts
5. chunking and index
6. retrieval (plain RAG: choose the TMP, hybrid search, one answer call, code check)
7. Streamlit UI (the model-driven agent was removed)
8. operations

**Phases 1-7 are built. Phase 8 is not.** The rule-based recipes of phase 6 and the model-driven agent and tools of phase 7 have been removed; retrieval is now a plain RAG (section 9).

```
                         ┌───────────────────── tmp-pipeline (run.py) ─────────────────────┐
 PDF ─► intake ─► layers ─► elements ─► zones ─► furniture ─► render ─► diagram stage ─► router ─► handlers
        (hash)   (pdfplumber,  (tables,    (page &     (header/    (200 DPI  (symbols,     (decide   (text, table,
                  pypdf,        text        element     footer)     masters)  legend,       a route   risk, checkbox,
                  pypdfium2)    blocks,     zones)                            regions)      per       diagram, figure)
                                ticks,                                                      element)      │
                                images)                                                                   ▼
                                                                                                    records.json
        ┌─────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▼
  map_fields ─► run_checks ─► facts.json / review.json ─► build_chunks ─► summarise_and_embed ─► chunks.jsonl
  (mentions, TMD ids,  (10 rules,                         (27 chunk types,  (X12 summary,          lineage.json
   merge, pointers,     approval,                          size policy,      embedding text,
   sign-offs, links)    coverage, review)                  metadata,         vectors/)
                                                           lineage)
        └────────────────────────────────────────── report.md, .done ───────────────────────────────────┘

 Optional, run afterwards on out/<doc>/:
   tmp-pipeline-facts / -review    rebuild facts, apply reviewer corrections
   tmp-pipeline-chunks             rebuild chunks (prints the diff)
   tmp-pipeline-index / -cosmos    push chunks to Azure AI Search / Cosmos DB
   tmp-pipeline-db                 load facts into PostgreSQL
   tmp-pipeline-di                 Azure Document Intelligence for scanned or unclear areas

 Questions:
   tmp-pipeline-ask / -qa ─► choose TMP ─► hybrid chunk search ─► context ─► Q3 answer
                              ─► code check ─► Q4 repair (once) ─► safe answer built in code if still failing
```

### Design principles that recur throughout

- **Code first, model second.** Everything that code can read exactly is read by code: vectors, text layers, regexes, the catalog. A model is used only where code cannot read something, and its output is always checked by code ([model/validators.py](../src/tmp_pipeline/ingestion/validators.py)).
- **Never invent, never correct.** A value that fails a check is kept and marked `unverified`, with the reason. Values that disagree become a `conflict` that keeps every version. Struck-through text is flagged `not_applicable`, not deleted.
- **Runs without a model.** `--model off` is the default, and every stage has a fallback written in code. `replay` serves recorded answers; `live` calls Azure OpenAI.
- **Every element is accounted for.** Each element gets exactly one route and outcome. Leftovers are flagged, not dropped. Coverage and lineage checks show any gaps.
- **Layout lives in config.** Company-specific layout cues are regexes and thresholds in `templates/default.toml`. Field names and synonyms are in `templates/catalog.yaml`.
- **Optional packages stay apart.** The core never imports `di/`, `db/`, `search/` or `cosmos/`, and a test enforces this.


---

## 2. Commands and output files

### 2.1 Commands (from `pyproject.toml`)

| Command | Entry point | What it does |
|---|---|---|
| `tmp-pipeline` | [run.py](../src/tmp_pipeline/ingestion/run.py) `main` | Runs a PDF through every stage up to `chunks.jsonl` |
| `tmp-pipeline-facts` | [facts/cli.py](../src/tmp_pipeline/ingestion/facts/cli.py) `main_facts` | Rebuilds `facts.json` and `review.json` from a finished run, without reading the PDF again |
| `tmp-pipeline-review` | [facts/cli.py](../src/tmp_pipeline/ingestion/facts/cli.py) `main_review` | Applies a reviewer's corrections file |
| `tmp-pipeline-chunks` | [chunks/cli.py](../src/tmp_pipeline/ingestion/chunking/cli.py) `main` | Rebuilds `chunks.jsonl` and `lineage.json`, and prints what was added, removed, changed or corrected |
| `tmp-pipeline-index` | [search/run.py](../src/tmp_pipeline/ingestion/embedding/search/run.py) `main` | Creates or pushes the Azure AI Search index |
| `tmp-pipeline-cosmos` | [cosmos/cli.py](../src/tmp_pipeline/ingestion/embedding/cosmos/cli.py) `main` | Uploads chunks to Cosmos DB, and runs `ask` or `qa` against Cosmos |
| `tmp-pipeline-db` | [db/run.py](../src/tmp_pipeline/ingestion/embedding/db/run.py) `main` | Migrates the PostgreSQL facts store or writes a document to it |
| `tmp-pipeline-di` | [di/run.py](../src/tmp_pipeline/ingestion/extract/di/run.py) `main` | Runs the Document Intelligence track: works out where DI is needed and, with a flag, calls DI or reads its cache |
| `tmp-pipeline-ask` | [retrieval/cli.py](../src/tmp_pipeline/retrieval/cli.py) `main` | Answers a question, or starts an interactive session |
| `tmp-pipeline-qa` | [gold/questions.py](../src/tmp_pipeline/retrieval/eval.py) `main` | Runs the question test set and scores it |
| `tmp-pipeline-gold` | [gold/score.py](../src/tmp_pipeline/ingestion/score.py) `main` | Scores a finished run against the extraction gold set |
| `tmp-pipeline-synthetic` | [gold/synthetic.py](../src/tmp_pipeline/retrieval/synthetic.py) `main` | Makes a synthetic V2 and a second TMP from one run folder, for testing multi-TMP behaviour |
| `tmp-pipeline-ui` | [ui/launch.py](../src/tmp_pipeline/retrieval/ui/launch.py) `main` | Starts the Streamlit UI ([ui/app.py](../src/tmp_pipeline/retrieval/ui/app.py); needs the `ui` extra). It browses the run folders, asks questions through `Retriever.ask` and shows the full `AnswerResult`, and runs `run()` on an uploaded PDF |

### 2.2 The output folder `out/<doc_id>/`

The folder is named after the document's id: `sha256:<hex>` with the `:` replaced by `-`. The id is the hash of the PDF's bytes, so the same PDF always lands in the same folder.

| File | Written by (stage) | Content |
|---|---|---|
| `run.log` | every stage ([logging_.py](../src/tmp_pipeline/logging_.py)) | One JSON line per event, with `run_id`, `doc_id` and `stage` |
| `master/pageNN_master.webp` | render | 200 DPI page image. Every `bbox_px` value refers to it |
| `master/pageNN_overlay.webp` | diagrams | A diagram sheet redrawn without its aerial photo |
| `router.log` | router | One JSON line per element: route, rule, triage, outcome, claimed_by, error |
| `elements.json` | outputs | The element store (`schema_version` 1) |
| `records.json` | outputs | Records from the handlers: typed values with status, evidence and a version stamp |
| `sheets.json` | outputs | The diagram sheets: symbols, legend, notes, regions, images, title block |
| `facts.json` | write_facts | The canonical TMP record (see section 6.4) |
| `review.json` | write_facts | The review list on its own |
| `corrections.jsonl` | `tmp-pipeline-review` | One line per corrections file applied |
| `chunks.jsonl` | summarise_and_embed | A header line, then one chunk per line |
| `lineage.json` | summarise_and_embed | The lineage check |
| `vectors/` | summarise_and_embed (only with an embedder) | Cached embedding vectors |
| `report.md` | outputs | A readable report covering every stage |
| `.done` / `FAILED` / `elements.partial.json` | run | The success marker / the failure marker with stage and traceback / the elements built before a crash |
| `di_triggers.json`, `di_elements.json`, `di/`, `di.log` | `tmp-pipeline-di` | The DI track's output |
| `facts.log`, `chunks.log` | rebuild commands | Logs from the rebuild commands |

`out/_model_cache/` sits at the root of `--out`, not inside a document folder. It holds the cache of live model answers.

---

## 3. The main run, step by step

The command `tmp-pipeline` ([run.py](../src/tmp_pipeline/ingestion/run.py)) runs every stage from reading the PDF through to building chunks for one PDF. `main()` ([run.py:168](../src/tmp_pipeline/ingestion/run.py#L168)) parses the arguments and builds the clients, then calls `run()` ([run.py:69](../src/tmp_pipeline/ingestion/run.py#L69)).

### 3.1 What `main()` does

1. **Parses the arguments.** The flags are listed in section 3.4.
2. **Calls `tracing.configure()`** ([tracing.py:64](../src/tmp_pipeline/tracing.py#L64)). This turns on Langfuse export only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both set. Without them it does nothing.
3. **Checks the replay flags.** `--model replay` needs `--model-fixtures`, and `--embed replay` needs `--embed-fixtures`. If either is missing, it exits with code 2.
4. **Builds the embedder.** With `--embed replay` it is a `ReplayEmbeddings`; otherwise it is `None`.
5. **Builds the model client** with `model_client(mode, fixtures, cache_dir)` ([model/live.py:12](../src/tmp_pipeline/llm.py#L12)):
   - `off` gives `None`. Every stage then falls back to rules written in code.
   - `replay` gives a `ReplayClient`, which reads recorded answers.
   - `live` gives `AzureChat`. When fixtures are given, it is wrapped in a `RecordingClient`. It is then always wrapped in `CachedClient(FileCache(<out>/_model_cache))`.
6. **Calls `run(...)`** and turns the result into an exit code (see section 3.4).

### 3.2 What `run()` does, stage by stage

Each stage runs inside `lg.stage(name)` ([logging_.py:118](../src/tmp_pipeline/logging_.py#L118)), which:
- writes `stage_started`, then `stage_finished` (with `duration_ms`) or `stage_failed` (with the traceback) to `run.log`;
- opens a Langfuse span named `stage <name>`.

The variable `current` always holds the name of the running stage. A crash uses it to record which stage failed.

| # | Stage | Function (file) | What it produces | Written to `out/<doc>/` |
|---|---|---|---|---|
| 0 | setup | `load_config()` [config.py:429](../src/tmp_pipeline/config.py#L429), `compute_doc_id()` [intake.py:54](../src/tmp_pipeline/ingestion/extract/intake.py#L54), `doc_dir()` [intake.py:36](../src/tmp_pipeline/ingestion/extract/intake.py#L36), `lg.start_run()` | `Config`, `doc_id = "sha256:<hex>"`, output dir `out/sha256-<hex>` | opens `run.log` (append) |
| 1 | `intake` | `intake()` [intake.py:74](../src/tmp_pipeline/ingestion/extract/intake.py#L74) | `IntakeResult` (pages, version, scanned, already_processed) | stops here if `.done` exists and `--force` is not set |
| 2 | `layers` | `read_layers()` [layers/reader.py:194](../src/tmp_pipeline/ingestion/extract/layers/reader.py#L194) | `list[PageLayers]`: chars, shapes, images, drawing-order objects, form fields | – |
| 3 | `elements` | `build_elements()` [layers/to_elements.py:23](../src/tmp_pipeline/ingestion/extract/layers/to_elements.py#L23) | `ElementStore` holding `table`, `text_block`, `checkbox` and `image` elements | – |
| 4 | `zones` | `assign_zones()` [zones.py:86](../src/tmp_pipeline/ingestion/extract/zones.py#L86) | a zone for every page and element; risk `region` and `diagram_sheet` elements | – |
| 5 | `furniture` | `apply_furniture()` [furniture.py:40](../src/tmp_pipeline/ingestion/extract/furniture.py#L40) | header and footer text retyped to `page_furniture`; TMP reference, form names, printed page numbers | – |
| 6 | `render` (skipped with `--skip-render`) | `Renderer.save_master()` [layers/render.py:17](../src/tmp_pipeline/ingestion/extract/layers/render.py#L17) | `PageInfo.master` | `master/pageNN_master.webp` (200 DPI) |
| 7 | `diagrams` | `read_diagrams()` [diagrams/stage.py:131](../src/tmp_pipeline/ingestion/extract/diagrams/stage.py#L131) | `SheetStore`, plus `vector_symbol` / `legend` / `notes` / `region` elements | `master/pageNN_overlay.webp` |
| 8 | `router` | `route()` [router.py:260](../src/tmp_pipeline/ingestion/extract/router.py#L260) with `build_registry()` [handlers/\_\_init\_\_.py:15](../src/tmp_pipeline/ingestion/extract/handlers/__init__.py#L15) | `RouterResult`; handlers fill a `RecordStore` | `router.log` |
| 9 | `map_fields` | `map_fields()` [facts/stage.py:77](../src/tmp_pipeline/ingestion/facts/stage.py#L77) | `Facts` | – |
| 10 | `run_checks` | `run_checks()` [facts/stage.py:149](../src/tmp_pipeline/ingestion/facts/stage.py#L149) | check results, approval, coverage and the review list, all on `Facts` | – |
| 11 | outputs | `store.dump`, `records.dump`, `sheets.dump` | – | `elements.json`, `records.json`, `sheets.json` |
| 12 | `write_facts` | `facts.dump`, `review_json()` | – | `facts.json`, `review.json` |
| 13 | `build_chunks` | `build_chunks()` [chunks/stage.py:25](../src/tmp_pipeline/ingestion/chunking/stage.py#L25) | `ChunkSet`, lineage dict | – |
| 14 | `summarise_and_embed` | `summarise_and_embed()` [chunks/stage.py:40](../src/tmp_pipeline/ingestion/chunking/stage.py#L40) | summaries, embedding text, vectors | `chunks.jsonl`, `lineage.json`, `vectors/` |
| 15 | outputs | `score_gold()` (optional), `build_report()` [report.py:212](../src/tmp_pipeline/ingestion/report.py#L212) | – | `report.md` |
| 16 | done | `mark_done()` [intake.py:40](../src/tmp_pipeline/ingestion/extract/intake.py#L40) | – | `.done` |

**Failure handling** ([run.py:60](../src/tmp_pipeline/ingestion/run.py#L60)):
- Any exception, including Ctrl-C, makes `_write_failure()` write a `FAILED` JSON file holding `{stage, error, run_id, trace}`.
- If the element store already exists, it also writes `elements.partial.json`.
- The exception is then raised again.
- `router.log` is written in a `finally` block, so it survives a crash inside the router.
- A later real run deletes `FAILED` after intake.
- A config error is raised before the `try` block, so it never leaves a `FAILED` marker.

### 3.3 The Phase 1 modules in detail

#### Intake ([intake.py](../src/tmp_pipeline/ingestion/extract/intake.py))
- **`doc_id`.** This is `sha256:` followed by the sha256 hash of the file's bytes. The same file always maps to the same `out/sha256-…` folder.
- **`open_pdf()`** ([intake.py:59](../src/tmp_pipeline/ingestion/extract/intake.py#L59)) turns pypdf failures into typed errors from [errors.py](../src/tmp_pipeline/errors.py): `EncryptedPdfError`, `EmptyPdfError` and `CorruptPdfError`. All of these exit with code 2.
- **Text per page.** pypdf extracts each page's text. A page with fewer than `intake.min_text_chars` (20) characters counts as "without text". The document is marked `scanned` when more than `intake.scanned_share` (0.5) of its pages have no text. **`scanned` is only recorded.** Pages without text are handled by the separate DI track (section 10).
- **Version.** It comes from the filename (`V1`) if possible, otherwise from the first two pages (`Version: N`).

#### Reading the PDF layers ([layers/reader.py](../src/tmp_pipeline/ingestion/extract/layers/reader.py))
Every coordinate is converted to **PDF points with the origin at the bottom-left**. Three libraries each read a different part:
- **pdfplumber** reads chars (font, size, bold, colour, rotation), rects, lines, curves and images.
  - A char counts as bold when its font name contains bold, black, heavy, semibold or demi.
  - Colours are converted to RGB.
  - Rotation is snapped to 0, 90, 180 or 270 when it is within 5°.
- **pypdf** reads AcroForm widgets (`_form_fields`).
  - A `/Btn` field is a checkbox or a radio button, and is ticked when `/AS` is not `/Off`.
  - `/Tx` fields are text and `/Ch` fields are choices.
- **pypdfium2** reads page objects in drawing order (`_page_objects`, [reader.py:149](../src/tmp_pipeline/ingestion/extract/layers/reader.py#L149)). It goes up to 4 levels into form XObjects and composes their matrices. The diagram stage needs this list later.

#### Finding tables ([layers/gridfind.py](../src/tmp_pipeline/ingestion/extract/layers/gridfind.py))
`detect_tables()` ([gridfind.py:108](../src/tmp_pipeline/ingestion/extract/layers/gridfind.py#L108)) is a rough first pass. It finds where the grids are; the table handler reads cells and rows later.
1. **Collect candidate parts** (`_parts`). These are lines, thin rects at least 6 pt long, and filled or stroked boxes. Rects covering more than 80 % of the page are skipped as background.
2. **Join touching parts.** Union-find links parts whose boxes touch within `join_tol_pt` (1.0).
3. **Keep only real grids.** A group is kept when it has at least 4 parts, at least 2 distinct x-edges and 2 distinct y-edges, and at least 2 cells.
4. **Merge rules.** Collinear rule pieces are joined (`_merge_rules`).
5. **Count fills.** Each filled box is assigned to the smallest region that contains its centre. A fill whose colour matches a `zones.rating_colours` entry adds to `rating_cells`; this is how risk matrices are found.
6. **Drop nested regions.** Only the outermost grid is kept (`_drop_nested`).

#### Building text blocks ([layers/textblocks.py](../src/tmp_pipeline/ingestion/extract/layers/textblocks.py))
Chars are grouped into words, then lines, then segments, then blocks. Text at each rotation is read along its own axis.
- **Lines and words.** Chars belong to the same line when they are within `line_group_em` (0.6 × size) of each other. A gap of more than `word_gap_em` (0.25 em) starts a new word.
- **Segments.** A gap of more than max(2.5 em, 15 pt) starts a new segment. A vertical rule between two words also starts one, so that table columns stay apart.
- **Bands.** Text with no rotation is placed in a band: **header** (top 62 pt), **footer** (bottom 55 pt) or **body**. Rotated text has no band and is treated as body.
- **Blocks.** `build_text_blocks()` ([textblocks.py:208](../src/tmp_pipeline/ingestion/extract/layers/textblocks.py#L208)) joins one segment to the next when all of these hold:
  - they have the same rotation, band and containing table;
  - the second follows directly below the first;
  - their columns overlap by at least 50 %, or their left edges are aligned within 4 pt.
- **Struck lines.** A line is marked `struck` when a thin horizontal rule crosses the middle 25-75 % of its height and covers at least 60 % of its width (`_is_struck`, [textblocks.py:192](../src/tmp_pipeline/ingestion/extract/layers/textblocks.py#L192)). Struck values are kept but flagged throughout the rest of the pipeline.
- **Reading order** (`_reading_order`). Header blocks come first, then body, then footer. Within a band, blocks are read top-down, and blocks that start at the same height (within 3 pt) are read left to right.

#### Detecting checkboxes ([layers/ticks.py](../src/tmp_pipeline/ingestion/extract/layers/ticks.py))
`detect_ticks()` ([ticks.py:22](../src/tmp_pipeline/ingestion/extract/layers/ticks.py#L22)) tries three methods on each page. It uses the first one that finds anything:
1. **AcroForm checkboxes** from the form fields.
2. **Tick glyphs**: ☒☑✓✔✗✘ count as ticked, ☐ as unticked.
3. **Vector squares**: stroked squares of 6-16 pt. A square counts as ticked when a line or curve is drawn inside it.

#### Scoring text quality ([layers/quality.py](../src/tmp_pipeline/ingestion/extract/layers/quality.py))
`assess()` ([quality.py:46](../src/tmp_pipeline/ingestion/extract/layers/quality.py#L46)) computes the share of words that appear in the bundled `data/words.txt`, and counts `(cid:N)` glyphs that could not be mapped to text. A block gets the `low_text_quality` flag when:
- its score is below `quality.low_threshold` (0.6) and it has at least 4 words; or
- it contains cid glyphs.

#### Creating elements ([layers/to_elements.py](../src/tmp_pipeline/ingestion/extract/layers/to_elements.py))
`build_elements()` creates the first elements on each page. All of them start in zone `unclassified` and get a box in pixels at 200 DPI.

| Element | Created from | Main fields |
|---|---|---|
| `table` | each grid region | `metadata` = n_xs, n_ys, fills, rating_cells, h_rules, v_rules |
| `text_block` | each text block | `parent_id` = the table it sits in; `representations` = `{text, lines[]}` (each line has struck and word details); `metadata` = rotation, band, size, bold, quality |
| `checkbox` | each detected tick | `parent_id` = the table it sits in; `metadata.state` = ticked or unticked; the detection method |
| `image` | each image | width_px, height_px, `area_share` (share of the page area) |

It also fills `PageInfo` for each page: size, mean text quality, `n_chars` and `n_paths`.

#### Assigning zones ([zones.py](../src/tmp_pipeline/ingestion/extract/zones.py))
**Page signals.** `page_signals()` ([zones.py:34](../src/tmp_pipeline/ingestion/extract/zones.py#L34)) records for each page:
- whether there is a header band matching `TMP\s*Reference`;
- the form name from the footer;
- how much of the page images cover;
- the number of paths;
- `SHEET NO` title blocks and generic codes `FH.M\d+`;
- whether the page has body content;
- the share of the page taken by risk tables.

**Page zone.** `page_default()` ([zones.py:66](../src/tmp_pipeline/ingestion/extract/zones.py#L66)) applies these rules in order and uses the first that matches:

| Order | Zone | When |
|---|---|---|
| 1 | **diagram** | images cover more than 50 % of the page, there is no header band, and the page has at least 15 paths or a SHEET NO block |
| 2 | **blank** | there is a header band or form name but no body content |
| 3 | **generic_standard** | there is no header band and the page has a generic code |
| 4 | **unclassified** | there is no header band and no generic code |
| 5 | **risk** | risk tables cover at least 50 % of the body |
| 6 | **form kind** | `zones.form_kind[kind]` maps the form name: planning → forms, job pack → narrative, regulatory authorisation → forms |
| 7 | **unclassified** | nothing above matched |

**Element zones.** `assign_zones()` then:
- adds a risk `region` element for each risk table found on a page that is not itself a risk page;
- adds a full-page `diagram_sheet` element to each diagram page;
- sets the zone of every element. Children inherit their table's zone, and an element inside a risk region becomes `risk`.

#### Finding page furniture ([furniture.py](../src/tmp_pipeline/ingestion/extract/furniture.py))
`apply_furniture()` looks at header and footer text. Before comparing, it normalises the text: lower case, collapsed spaces, and digits replaced by `#`. A block becomes `page_furniture` when one of these holds:
- its text repeats on more than half of the pages;
- at least half of its lines repeat;
- it matches the form-name or page-number pattern.

A table in a band whose children are all furniture becomes furniture too. `store.retype()` changes the element's id and updates every reference to it.

While doing this, it also collects:
- the **TMP reference** (`[A-Z]{2,5}\s?\d{2}-\d{4}`, e.g. `KTS 26-1581`). The most common value is stored in `store.metadata.tmp_reference`;
- the form name of each page;
- the printed page number (`Page N`), which is copied onto every element.

#### Rendering pages ([layers/render.py](../src/tmp_pipeline/ingestion/extract/layers/render.py))
- `Renderer` uses pypdfium2 to render each page at 200 DPI ([constants.py](../src/tmp_pipeline/constants.py) `MASTER_DPI`) and saves it as WEBP. Every `bbox_px` value refers to these master images.
- `perceptual_hash()` is a 64-bit DCT pHash. The diagram stage uses it to link pictures that are reused.

### 3.4 Command-line flags, exit codes and re-runs

| Flag | Meaning |
|---|---|
| `pdf` | The input PDF |
| `--out` (default `out`) | The output root folder |
| `--template file.toml` | A layout template merged over `templates/default.toml` |
| `--force` | Run again even if `.done` exists |
| `--skip-render` | Do not create master renders or overlays |
| `--model off\|replay\|live` and `--model-fixtures DIR` | The model mode (section 4) |
| `--embed off\|replay` and `--embed-fixtures DIR` | The embedding mode |
| `--gold file.json` | Score the run against a gold set and add the score to `report.md` |

**Exit codes:**
- **0**: success with 100 % coverage and no handler failures, or the document was skipped because it was already done.
- **1**: coverage was incomplete or a handler failed.
- **2**: bad input, config or schema. The message goes to stderr.

### 3.5 Shared infrastructure

| File | Role |
|---|---|
| [config.py](../src/tmp_pipeline/config.py) + [templates/default.toml](../src/tmp_pipeline/templates/default.toml) | **Config.** Typed, frozen dataclasses, one section per area: page, intake, text, quality, tables, ticks, checkboxes, risk, furniture, zones, figures, diagrams, facts, chunks, retrieval and others. `load_config()` deep-merges the template over the defaults, then `_build()` checks types strictly: an unknown key, a missing key or a bad regex raises `ConfigError` naming the dotted path. `_validate()` checks ranges and cross-references. `config_hash()` (sha256 of the merged config) is logged with every run. **A new company's layout is a new TOML file, not a code change.** |
| [models.py](../src/tmp_pipeline/ingestion/models.py) | **Data model.** `Element`, `PageInfo` and `ElementStore`. Element ids have the form `p{page}:{type}:{n}`. `retype()` changes an element's type and rewrites every reference to it. `dump()` / `load()` write and read `elements.json` (`schema_version` 1), with atomic writes. |
| [vocab.py](../src/tmp_pipeline/ingestion/vocab.py) | **Closed vocabularies** (see the table below). They are checked on every assignment, so a misspelled zone raises immediately. |
| [geometry.py](../src/tmp_pipeline/ingestion/geometry.py) | **Box maths**: centre, containment, IoU, and conversion between points and pixels with the y-axis flipped. This is the only place it lives. |
| [logging_.py](../src/tmp_pipeline/logging_.py) | **`run.log`**: one JSON line per event, with `run_id` and `doc_id`. Secret-looking keys are redacted. The current run is held in a ContextVar. |
| [tracing.py](../src/tmp_pipeline/tracing.py) | **Langfuse tracing** over OpenTelemetry. Each stage gets a chain span, and model and embedding calls get generation spans with tokens and cost. Prompt and answer text is sent only when `TMP_TRACE_CONTENT=1`. |
| [cache.py](../src/tmp_pipeline/cache.py) | **`FileCache`**: one JSON file per key, written atomically. A corrupt entry counts as a miss. `stable_key()` builds keys. It is used for the model cache, fixtures, `vectors/` and the DI cache. |
| [transport.py](../src/tmp_pipeline/transport.py) | **HTTP for every Azure client.** `urllib_transport` and `KeepAliveTransport` send requests. `with_retry` retries 408, 429 and 5xx up to 3 times with exponential backoff and honours `Retry-After`. |
| [versions.py](../src/tmp_pipeline/ingestion/versions.py) | **Version stamp** for the extractor, router, catalog, prompts, model, chunker, facts and retrieval. It is copied onto every record. |
| [report.py](../src/tmp_pipeline/ingestion/report.py) | **`report.md`**, with these sections: pages and zones, routes, failures, LEFTOVER, records, diagrams, facts and checks, chunks and lineage, gold score. |

**Vocabularies** ([vocab.py](../src/tmp_pipeline/ingestion/vocab.py)):

| Name | Values |
|---|---|
| `ElementType` | heading, paragraph, list_item, table, table_row, kv_field, checkbox, numeric, identifier, image, figure, photo, diagram_sheet, vector_symbol, legend, notes, region, text_block, page_furniture, ocr_word |
| `Zone` | forms, risk, narrative, diagram, generic_standard, blank, unclassified |
| `Flag` | struck, duplicate, boilerplate, generic, contact_info, id_conflict, rating_mismatch, needs_caption, low_text_quality, leftover, decorative, refers_elsewhere, merged, unverified, conflict |
| `Route` | TEXT, TABLE, RISK_TABLE, CHECKBOX, FIGURE, DIAGRAM, METADATA, STRUCTURE, DECORATIVE, LEFTOVER |
| `Outcome` | sent, claimed, skipped, kept, metadata, leftover, failed |

---

## 4. The model layer and the catalog

Section 5 onwards refers to these pieces, so they are described first.

### 4.1 Model clients ([model/client.py](../src/tmp_pipeline/llm.py))

**`ModelRequest`** holds:
- `prompt_id` and `prompt_version`
- `model`
- `system` and `user` text
- `images` (PNG bytes)
- `params`

Its `key()` is a stable hash of all of these, with images reduced to their sha256. Changing the prompt version, the model, the text, an image or any parameter therefore misses the cache.

The clients:

| Client | Purpose |
|---|---|
| `DisabledClient` | Always raises `ModelUnavailableError` |
| `FakeClient` | Returns canned answers. Used in tests |
| `CachedClient` | Puts a `FileCache` in front of another client |
| `RecordingClient` | Saves each live answer to a fixtures folder |
| `ReplayClient` | Serves fixtures only. A missing key raises `ModelUnavailableError` |
| `AzureChat` ([model/azure_chat.py](../src/tmp_pipeline/llm.py)) | Azure OpenAI chat completions (API `2025-04-01-preview`). Asks for JSON output, sends images as base64 parts, and sets `reasoning_effort`. If a deployment rejects `temperature` or `reasoning_effort`, it drops that setting and remembers this. Any failure becomes `ModelUnavailableError` |

**`ask(model, request)`** ([client.py:125](../src/tmp_pipeline/llm.py#L125)) is the only way stages call a model. It returns `None` when there is no model or the model is unavailable. **Every caller treats `None` as "use the code fallback"**, which usually means storing the value as `unverified`.

**Building a client.** `model_client(mode, fixtures, cache_dir)` ([model/live.py:12](../src/tmp_pipeline/llm.py#L12)) builds one of:
- `off`: `None`
- `replay`: `ReplayClient`
- `live`: `CachedClient(RecordingClient(AzureChat))`

The cache is the outermost wrapper, so in live mode a cache hit is not written to the fixtures.

**Cost tracking.** [model/pricing.py](../src/tmp_pipeline/llm.py) reads `data/model_prices.toml`, or the file named by `TMP_MODEL_PRICES`. Prices are USD per 1M tokens. Cost is attached to Langfuse spans and added up per question in retrieval.

**Embeddings.** [model/embeddings.py](../src/tmp_pipeline/ingestion/embedding/embeddings.py) provides:
- `FakeEmbeddings`: deterministic, 16 dimensions
- `CachedEmbeddings`: key is `stable_key(model, dims, sha256(text))`
- `RecordingEmbeddings` and `ReplayEmbeddings`: default `text-embedding-3-large`, 3072 dimensions

The live embedding client is `AzureEmbeddings`, in the optional [search/azure_openai.py](../src/tmp_pipeline/ingestion/embedding/search/azure_openai.py).

### 4.2 Prompts ([model/prompts.py](../src/tmp_pipeline/ingestion/prompts.py))

**How prompts are set up:**
- Prompt versions are in `PROMPT_VERSIONS`. All are 1.0 except Q3, which is 1.1.
- Each prompt's model is in `MODELS`.
- Every request has `temperature: 0`.
- Second, independent reads use `READ2_MODEL = gpt-5.5`.
- `catalog_text()` renders the catalog's entities and fields into prompts. **Adding a field never requires editing a prompt.**

| ID | Model | Purpose | Called from | Validator |
|---|---|---|---|---|
| X2 | gpt-5.4 | Map table column or label names to a catalog entity and its fields | [handlers/tables.py](../src/tmp_pipeline/ingestion/extract/handlers/tables.py) (only when the catalog synonyms fail) | `check_x2` |
| X4 | gpt-6-luna | Classify a section heading as a catalog section type | [handlers/text.py](../src/tmp_pipeline/ingestion/extract/handlers/text.py) | `check_x4` |
| X5 | gpt-5.4 | Extract catalog fields from section text, with exact quotes | handlers/text.py | `check_fact` |
| X6 | gpt-6-sol | Strict judge: does the quote support `field = value unit`? Used for critical fields | handlers/text.py | `check_x6` |
| X7 | gpt-6-luna (2nd read gpt-5.5) | Read an image: kind, labels, numbers with OCR word ids, description | [handlers/imageread.py](../src/tmp_pipeline/ingestion/extract/handlers/imageread.py) | `check_x7` |
| X8 | gpt-6-luna | Spatial summary of a TMD sheet, from the overlay, the master and the symbol list | [handlers/diagram_model.py](../src/tmp_pipeline/ingestion/extract/handlers/diagram_model.py) | `check_x8` |
| X9 | gpt-6-luna (2nd read gpt-5.5) | Read a flattened (non-vector) sheet in tiles | handlers/diagram_model.py | `check_x9`, `agree_x9` |
| X10 | gpt-6-luna | Caption one pictogram symbol | handlers/diagram_model.py | `check_x10` |
| X11 | gpt-5.4 | Match a TMD mention to one sheet | [facts/tmd.py](../src/tmp_pipeline/ingestion/facts/tmd.py) | `check_x11` |
| X12 | gpt-6-luna | One- or two-sentence note on what a chunk can answer | [chunks/summarise.py](../src/tmp_pipeline/ingestion/chunking/summarise.py) | `check_x12` (number guard) |
| X13 | gpt-6-luna | Full image or diagram read: items, distances, relations, garbled text, unreadable parts, inferences | [handlers/image_full.py](../src/tmp_pipeline/ingestion/extract/handlers/image_full.py) | `check_x13` |
| Q3 | gpt-6-luna, reasoning `none` | Answer from the context only (13 rules) | [retrieval/answer.py](../src/tmp_pipeline/retrieval/answer.py) | [retrieval/check.py](../src/tmp_pipeline/retrieval/check.py) |
| Q4 | gpt-6-sol | Rewrite an answer that broke the rules | retrieval/answer.py | retrieval/check.py |

X1 (image triage) and X3 (cell read) are not implemented. The router only records `needs_x1`. Q1 (question reading) and Q2 (the agent) existed in an earlier design and have been removed.

### 4.3 Validators ([model/validators.py](../src/tmp_pipeline/ingestion/validators.py))

The validators are plain code. **They never change a value.** They return a list of problems, and the caller stores the value as `unverified` with those problems as notes.

| Validator | What it checks |
|---|---|
| `check_quote`, `value_in_quote`, `unit_matches` (combined as `check_fact`) | The quote appears verbatim in the source; the value is inside the quote; the unit matches the catalog |
| `check_x6` | The judge returned `supported: true` |
| `check_x2`, `check_x4` | Only catalog entities, fields and section types are allowed, each used once; low confidence becomes OTHER |
| `check_x7`, `check_item` | Cited word ids exist, lie within 80 pt of each other, and hold the value; numbers appear in the PDF text inside the crop |
| `check_x8` | Every claim cites real symbol ids; every number is one of the sheet's own numbers |
| `check_x9`, `agree_x9` | Numbers cite real words; an item is kept only if both independent reads report the same (kind, value) |
| `check_x10` | Not "unclear"; confidence high or medium; any digits come from the symbol |
| `check_x11` | The id is a sheet in this document; low confidence leaves the mention unmatched |
| `check_x12` | **Number guard**: every number in the summary appears in the chunk |
| `check_x13` | Item, distance and relation ids exist; numbers are on the sheet; a garbled-text reading keeps at least 70 % of the printed letters, in order |

### 4.4 The catalog ([catalog.py](../src/tmp_pipeline/ingestion/catalog.py) + [templates/catalog.yaml](../src/tmp_pipeline/templates/catalog.yaml))

`load_catalog()` ([catalog.py:179](../src/tmp_pipeline/ingestion/catalog.py#L179)) loads the catalog and validates it strictly:
- unknown keys and bad field types are rejected;
- every `match_key` must name an existing field;
- duplicate synonyms and duplicate headings are rejected.

The catalog `version` (currently 0.2.0) is stamped on facts.

| Part | Content | Used by |
|---|---|---|
| `entities` | `road`, `work_period`, `shift_window`, `road_geometry`, `vulnerable_user`, `road_aspect`, `risk`. Each field has type, desc, unit, `critical` and synonyms | Table and checkbox name matching (`match_entity`), merge keys, X2/X5 prompts |
| `sections` | About 22 section types (contingency.\*, monitoring, staging, notification, …), with `heading_synonyms` and optional typed fields with `pattern` | Text handler: `section_type()`, falling back to X4 if that fails; pattern fields are read by code, the rest by X5 |
| `facts_entities` | `tmp_document`, `stage`, `speed_limit`, `signoff`, `contact`, `authorisation` | Phase 4 only |
| `record_kinds` | `risk_cell→risk_matrix_cell`, `figure→figure`, `yes_no→form_answer`, `section→section` | `merge.copy_records` |
| `diagram_title_synonyms` | e.g. `stop go`, `no parking plan` | `tmd.match_title` |
| `required_signoffs` | `ttm_designer`, `design_checker`, `activity_manager`, `rca_primary`, and `rca_authorisation_form3` if `tsl_requested` | `signoff.approval` |

**How the parts divide the work:**
- **`critical`** fields need the X6 judge (or a second image read). Without one they are stored `unverified`.
- **Synonyms** drive code.
- **Descriptions** drive prompts.

---

## 5. Phases 2-3: diagram stage, router and handlers

### 5.1 Diagram stage ([diagrams/stage.py](../src/tmp_pipeline/ingestion/extract/diagrams/stage.py))

The diagram stage runs **before the router**. It is the only stage after Phase 1 that **adds elements**; handlers never add elements. It reads each diagram sheet (TMD) from the vector drawing and passes a `SheetStore` to the diagram handler.

**Entry point:** `read_diagrams(store, pdf, cfg, out_dir, render)` ([stage.py:131](../src/tmp_pipeline/ingestion/extract/diagrams/stage.py#L131)).

1. **Check the switch.** If `cfg.diagrams.enabled` is false, it returns an empty store.
2. **Hash the pictures (`tag_picture_hashes`).** Each large image on a non-diagram page gets a perceptual hash of its own source pixels in `metadata.phash`. The figure handler uses this later to reuse readings.
3. **Find the sheets.** These are the `diagram_sheet` elements that zones created.
4. **Read the overlays.** `read_overlays()` ([diagrams/overlay.py:143](../src/tmp_pipeline/ingestion/extract/diagrams/overlay.py#L143)) makes a second pass with pypdfium2 in **drawing order**, giving one `OverlayObject` per object: kind, bbox, fill and stroke colour, dashed, text, size, matrix and phash.
5. **Read each sheet.** `_read_sheet()` ([stage.py:43](../src/tmp_pipeline/ingestion/extract/diagrams/stage.py#L43)) runs these steps on each sheet page:

   | Step | Module | Algorithm |
   |---|---|---|
   | Text lines | [textlines.py](../src/tmp_pipeline/ingestion/extract/diagrams/textlines.py) `build_lines` | Joins glyphs into a line while the gap is at most 2.5 em and the direction stays within 20° of the baseline. This rebuilds rotated street labels. Lines with odd glyphs or poor quality are marked `low_quality` |
   | Legend | [legend.py](../src/tmp_pipeline/ingestion/extract/diagrams/legend.py) `read_legend` | Finds the `LEGEND` title and its box. The paths drawn between one label and the next form that label's swatch. Each entry gets an HSV colour word plus "dashed" or "hatched" |
   | Notes | [regions.py](../src/tmp_pipeline/ingestion/extract/diagrams/regions.py) `find_notes` | Finds the box around the `NOTES` title; lines are split into notes by bullet pattern |
   | Title block | regions.py `find_title_block` / `read_title_block` | Anchored on `PREPARED FOR`. Reads the labelled cells: sheet, rev, date, scale, tmp_number, drawn_by, project |
   | Images | regions.py `find_images` | Gives each image a role: `decorative` (in the title block), `aerial` (≥ 50 % of the page), `inset` (inside a form XObject) or `figure`. `grow_figure_over_title` extends a figure's box to include its caption |
   | Symbols | [symbols.py](../src/tmp_pipeline/ingestion/extract/diagrams/symbols.py) `read_symbols` | See below |
   | Associations | [assoc.py](../src/tmp_pipeline/ingestion/extract/diagrams/assoc.py) `link_all` | See below |

   **How symbols are read** (`read_symbols`):
   - **Grouping (`group_objects`).** It walks the objects in drawing order. A path joins a recent group (one of the last 6) when they overlap by at least 50 %. A text line joins a group when its centre falls inside the group. Some paths always form their own group: large filled areas, legend-matched areas and cones. Small fragments within 3 pt are merged (`merge_fragments`).
   - **Classifying (`_classify`).** The first match wins. With text:
     - `^RP(\d+\.\d+)$` → `rp_label`
     - `^\d+M$` → `dimension_label`
     - `NAME-L\d` → `road_label`
     - `STOPGO` → `traffic_controller`
     - long lowercase text → `callout`
     - a `sign_text` regex → speed-ahead, school, advance, end-of-works, TEMPORARY plate, bus stop or warning sign
     - a 2-3 digit number with paths → `speed_limit`
     - an orange plate → `warning_sign`
     - anything else → `place_label`

     Without text:
     - black or stroke-only paths → `line`, with the legend meaning
     - anything else → **`pictogram` with `needs_caption`**
   - **Afterwards.** `merge_cone_runs` joins consecutive cones into runs, and `absorb_controller_parts` folds nearby fragments into the traffic controller.

   **How associations are made** (`link_all`):
   - **`link_temporary_plates`.** A TEMPORARY plate claims the nearest speed disc within 40 pt, which then becomes `temporary_speed_limit`.
   - **`link_rp_labels`.** Each RP label goes to the nearest sign within 45 pt. Signs stacked against that sign inherit the RP.
   - **`link_roads`.** Each sign is linked to the nearest road label.
   - **`order_along_roads`.** Signs on each road are projected onto the road's principal axis (PCA) and ordered so that RP values rise. They get `order` and `approach`.

6. **Link reused pictures.** `link_reuse()` ([lineage.py:11](../src/tmp_pipeline/ingestion/extract/diagrams/lineage.py#L11)) marks pictures whose pHash is within 6 bits as reused across sheets.
7. **Write the elements.** Each sheet gets:
   - `region` elements (legend, notes, title_block, inset, figure);
   - a `legend` element and a `notes` element;
   - one `vector_symbol` per symbol, flagged `needs_caption` or `low_text_quality` where that applies.

   In addition:
   - A Phase 1 `table` covering at least 60 % of the page is really the drawing frame. It is retyped to `region` (kind `frame`).
   - Provisional ids are swapped for real element ids.
   - `save_overlay()` ([diagrams/render.py](../src/tmp_pipeline/ingestion/extract/diagrams/render.py)) renders the page with every bitmap blanked, giving "the drawing without the photo", which the X8 and X13 prompts use.

**Output.** The `SheetStore` ([diagrams/models.py](../src/tmp_pipeline/ingestion/extract/diagrams/models.py)) holds one `SheetModel` per page: sheet, title, rev, title_block, legend, notes, callouts, regions, symbols, images, roads and overlay paths. It is saved as `sheets.json`.

[diagrams/describe.py](../src/tmp_pipeline/ingestion/extract/diagrams/describe.py) uses no model. The diagram handler calls it to compute "code facts" for image questions:
- where each item is (a 3×3 grid; plan, inset or figure);
- left-to-right order;
- what each dimension line runs between;
- every printed text, with garbled fonts detected.

### 5.2 Router ([router.py](../src/tmp_pipeline/ingestion/extract/router.py))

The router is plain code and makes no model calls. It works in three steps.

#### `decide(store, cfg)` ([router.py:203](../src/tmp_pipeline/ingestion/extract/router.py#L203))

`decide` is pure: it builds one `Decision` per element from named rules applied in order. Before the rules run, it works out:
- which page belongs to which sheet;
- which images repeat on most pages (logos);
- the **triage** of each image:
  - `decorative` (repeating, or small with no text);
  - `informative` (has text, or is large);
  - `needs_x1` (size unclear; treated as informative).

The rules run in phases. The first rule that returns a decision wins:

| # | Rule | Matches | Route → Outcome |
|---|---|---|---|
| 1 | `rule_furniture` | `page_furniture` | METADATA → metadata |
| 2 | `rule_structure` | `diagram_sheet` | DIAGRAM → **sent** |
| 2 | `rule_structure` | `region` | STRUCTURE → kept |
| 3 | `rule_image` | decorative image | DECORATIVE → skipped |
| 3 | `rule_image` | image on a sheet page | DIAGRAM → claimed |
| 3 | `rule_image` | any other image | FIGURE → sent |
| 4 | `rule_table` | table on a sheet page | DIAGRAM → claimed |
| 4 | `rule_table` | table in the risk zone | RISK_TABLE → sent |
| 4 | `rule_table` | any other table | TABLE → sent |
| 5 | `rule_checkbox` | checkbox on a sheet | DIAGRAM → claimed |
| 5 | `rule_checkbox` | checkbox inside a figure | FIGURE → claimed |
| 5 | `rule_checkbox` | any other checkbox | CHECKBOX → sent (the parent table is kept as context) |
| 6 | `rule_claimed_by_container` | parent routed TABLE, RISK_TABLE or DIAGRAM | same route → claimed |
| 7 | `rule_on_sheet` | anything else on a sheet page (symbols, legend, notes, text) | DIAGRAM → claimed |
| 8 | `rule_inside_figure` | centre inside a figure | FIGURE → claimed |
| 9 | `rule_text` | `text_block` with a known zone | TEXT → sent |
| 10 | `rule_leftover` | everything else | LEFTOVER → leftover, flagged `leftover` |

After the rules, elements in the `generic_standard` zone get the `generic` flag.

#### `apply_decisions()` ([router.py:223](../src/tmp_pipeline/ingestion/extract/router.py#L223))

This writes `route`, `route_rule`, `outcome`, `triage` and `claimed_by` onto each element. It also sets the parent if the element has none, and adds any flags.

#### `route(...)` ([router.py:260](../src/tmp_pipeline/ingestion/extract/router.py#L260))

1. **Check the handlers.** It checks that every route with **sent** elements has a handler. If one is missing, it raises `MissingHandlerError` before anything runs.
2. **Start the handlers.** It builds a `HandlerContext` and calls `begin()` on each handler in use.
3. **Hand over the elements.** For each sent element it calls `handler.handle(ctx, e)`.
   - **If a handler raises:** that element gets outcome `failed`, the error is added to `failures`, and the run continues.
4. **Finish the handlers.** It calls `handler.finish(ctx)` on each one. Most handlers build their records here. A failure in `finish` is caught as well.
5. **Apply the results.** The router applies each `HandlerResult`: records, flags and metadata updates. **Handlers never change the store directly.**
6. **Write `router.log`.** It is written in a `finally` block. `RouterResult.coverage()` reports any elements that were missing, duplicated or failed.

**Outcome values:**

| Outcome | Meaning |
|---|---|
| `sent` | Given to a handler |
| `claimed` | Owned by a container |
| `skipped` | Decorative |
| `kept` | Structure, kept as it is |
| `metadata` | Used as page furniture or document metadata |
| `leftover` | Not claimed by anything |
| `failed` | The handler raised an error |

### 5.3 Handler contract and records

[contracts.py](../src/tmp_pipeline/ingestion/contracts.py) defines the contract:
- **`Handler` protocol:** `route`, `version`, `begin`, `handle`, `finish`.
- **`HandlerContext`:** store, records, cfg, versions, model, catalog, sheets, out_dir, plus a `stamp()` helper.
- **`HandlerResult`:** records, `element_updates`, outcome, note.
- **`HandlerRegistry`.**

`build_registry()` ([handlers/\_\_init\_\_.py:15](../src/tmp_pipeline/ingestion/extract/handlers/__init__.py#L15)) replaces the counting stubs ([handlers/stubs.py](../src/tmp_pipeline/ingestion/extract/handlers/stubs.py)) with the real handlers.

[records.py](../src/tmp_pipeline/records.py) defines the records model:

| Type | Fields |
|---|---|
| `Record` | `kind`, `page`, `entity`, `fields`, `evidence`, `flags`, `status`, `applicability`, `data`, `produced_by: Stamp`, `record_id` (`r{page}:{kind}:{n}`) |
| `FieldValue` | `value` (typed), `raw` (exactly as read), `unit`, `status`, `method` (code / pattern / catalog / vector / X2–X7), `quote`, `notes` |
| `Status` | `verified` (default), `unverified`, `not_applicable`, `conflict`, `reviewed` |
| `Evidence` | `element_id`, a fingerprint that does not depend on detection order, `bbox_pt`, `quote` |
| `Stamp` | handler and its version, `config_hash`, prompt versions, model, `catalog_version` |

[numerics.py](../src/tmp_pipeline/ingestion/numerics.py) does typed extraction in code, with no model.
- **`extract(text, kinds)`** runs regex rules in priority order: date, time, rp, speed, percent, flow, distance, weight, duration, tmd, tmp_reference, phone. It normalises what it finds: NZ d/m/y dates to ISO, `HH:MM`, RP in km, `TMD-NN`, and so on.
- **`parse_value(text, type, unit)`** parses one whole cell and returns `(value, problem)`.

### 5.4 The handlers

| Handler | Route | File | Reads | Produces |
|---|---|---|---|---|
| **Text** | TEXT | [handlers/text.py](../src/tmp_pipeline/ingestion/extract/handlers/text.py) | Text lines | `section` records |
| **Table** | TABLE | [handlers/tables.py](../src/tmp_pipeline/ingestion/extract/handlers/tables.py) + [grid.py](../src/tmp_pipeline/ingestion/extract/handlers/grid.py) | Tables | entity rows / `table_row`, `table` |
| **Risk table** | RISK_TABLE | [handlers/risk.py](../src/tmp_pipeline/ingestion/extract/handlers/risk.py) | Risk matrices and registers | the table kinds + `risk_matrix`, `risk_cell`, `risk_impact_key` |
| **Checkbox** | CHECKBOX | [handlers/checkboxes.py](../src/tmp_pipeline/ingestion/extract/handlers/checkboxes.py) | Checkboxes | `checkbox`, `yes_no` |
| **Diagram** | DIAGRAM | [handlers/diagram.py](../src/tmp_pipeline/ingestion/extract/handlers/diagram.py) + [diagram_model.py](../src/tmp_pipeline/ingestion/extract/handlers/diagram_model.py) | The sheets | `diagram_sheet` |
| **Figure** | FIGURE | [handlers/image.py](../src/tmp_pipeline/ingestion/extract/handlers/image.py) + [imageread.py](../src/tmp_pipeline/ingestion/extract/handlers/imageread.py) + [image_full.py](../src/tmp_pipeline/ingestion/extract/handlers/image_full.py) | Images | `figure` |

#### Text handler
1. **Find the body size.** The body font size is the size used for the most characters.
2. **Decide heading levels.**
   - Rotated text is level 3.
   - Bold text at least `heading_min_delta` larger than the body is level 1.
   - Bold text at body size is level 2.
   - A run of level-2 lines inside one block is reset to body text.
3. **Build sections.** A heading stack builds the sections, each with a heading path. Lines are joined into paragraphs with `textutil.join_lines` ([textutil.py](../src/tmp_pipeline/ingestion/extract/handlers/textutil.py)).
4. **Set the section type.** It comes from the catalog's `section_type(heading)`. If that fails, the **X4** prompt is used; if that fails too, the type is unknown.
5. **Read the fields.**
   - Fields with a `pattern` are read by `numerics.extract`.
   - Other fields are read by **X5**, which must give an exact quote, and are then checked with `check_fact`.
   - **Critical** fields also need the **X6** judge. Without a judge, they are stored `unverified` ("critical field was not judged").
6. **Handle struck text.** A section whose heading or whole body is struck is stored `not_applicable`.

#### Table handler
1. **Build the grid.** `build_grid()` ([grid.py:140](../src/tmp_pipeline/ingestion/extract/handlers/grid.py#L140)) clusters the rules into column and row edges. It merges slots wherever a shared edge has no rule (merged cells), assigns fills, and puts each text *line* into the cell that holds its centre.
2. **Find header rows.** Header rows are those whose cells all have a header fill.
3. **Classify the shape.** The table is `records_table`, `checkbox_row`, `header_block`, `label_value_grid` or `unknown`.
4. **Stitch across pages.** A header-less table on the next page with the same column positions continues the previous one.
5. **Map the headers.** The headers are matched to a catalog entity with `match_entity`. If that fails, the **X2** prompt maps the names only; **the model never supplies values**.
6. **Make one record per data row.**
   - Values are typed with `parse_value`, and the raw text is always kept.
   - "See above" sets `refers_elsewhere`.
   - N/A becomes `None` with a note.
   - Merged cells get the `merged` flag.
   - Code checks flag problems but never fix them: `start_rp < end_rp`, percentages between 0 and 100, and speeds that are multiples of 10.
   - Phone numbers and emails set `contact_info`.
   - Struck rows are stored `not_applicable`.
7. **Add a table record.** Each table also gets one `table` record holding its shape, headers, pairs and cells.

#### Risk table handler
This is a subclass of the table handler.

**Reading the matrix:**
- Likelihood labels (`1 - very unlikely` …) and impact labels are found.
- Score cells (1–25) are placed at the nearest likelihood row and impact column.
- The key is read from coloured swatches, and each cell's rating comes from the nearest key colour.

**Records it adds:**
- **`risk_matrix`**: verified only when all 25 cells are found.
- **`risk_cell`**: `unverified` when the score is not likelihood × impact, or when its colour matches no key entry.
- **`risk_impact_key`**: one per key entry.

**Register rows.** Each row's stated initial and residual ratings are looked up in the matrix. A mismatch adds `rating_mismatch` with a note and is **never corrected**. `affected_tmds` are converted to `TMD-NN` links.

#### Checkbox handler
**Finding each box's label.** It tries these in order:
1. a line containing tick glyphs, split at the glyphs;
2. the nearest label to the left on the same row;
3. a note to the right;
4. all the text on the row.

If none of these finds text, the layout is `unlabelled`.

**Yes/No pairs.** Boxes on the same row labelled yes and no form a `yes_no` record. It is `unverified` when both or neither are ticked.

**Other boxes.** Every other box becomes a `checkbox` record. Its field name comes from the catalog (`vulnerable_user`, `road_aspect`), or otherwise from the label in snake_case.

This handler uses no model.

#### Diagram handler
It makes one `diagram_sheet` record per sheet from the `SheetModel`.

**Fields** (method `vector`): `tmd_id` (from SHEET NO), sheet, title, rev, date, tmp_number, drawn_by, scale.

**`data`:**
- aliases (`TMD 04: TITLE`, `TMD4`, `TMD-04`)
- roads, legend, symbols, notes, callouts, regions and title block
- `describe.facts` (the code facts)
- `images` (master, overlay, placed)

**With a model:**
- **Vector sheets:**
  - **X10** captions each pictogram from a crop of the master, the legend and its colours. The caption is stored in symbol metadata.
  - **X8** summarises the sheet from the overlay, the master, the symbol table and the notes. Every claim must cite symbol ids.
- **Flattened sheets:** **X9** reads 2×2 tiles twice: the first read gets the word list, the second (gpt-5.5) does not. Only items **both reads agree on** are kept.
- **Figures beside the plan:** **X7** and **X13** read each distinct picture once; a repeat is reused by phash.
- **Whole sheet:** **X13** reads the sheet, plus tiles when `describe.read_tiles` is set. `_merge` keeps checked statements over unchecked ones.

**Status.** The record is `unverified` if any field has a problem, the sheet has no vectors, or the summary has unverified claims.

#### Figure handler
For each image sent to it:
1. **Reuse a reading if possible.** If an earlier figure's phash is within 6 bits, its reading is reused (`reused_from`).
2. **Otherwise read the image:**
   - `read_image` (**X7**) gets the crop, the heading, nearby text and the citable word ids from `imageio.words_in`. It is checked with `check_x7`, and a failed check triggers a second read with gpt-5.5.
   - `read_picture` (**X13**) does a full read.
3. **Make the `figure` record.** Its fields are `image_kind` and `description`. It is verified only when every item is verified and the check passed.
4. **Without a model,** the description is `None`, the status is `unverified`, and the image is flagged `needs_caption`.

**Shared rule for every image prompt.** Model output is never corrected by code. A value is either `verified`, meaning it passed the code checks and, where required, an independent second read agreed, or it is kept as `unverified` with its problem list. [handlers/imageio.py](../src/tmp_pipeline/ingestion/extract/handlers/imageio.py) provides the crops (from the 200 DPI master) and the word ids. It uses DI `ocr_word` elements if any exist, otherwise the PDF text lines.

---

## 6. Phase 4: facts

**What this phase does:** records stay as the handlers wrote them, and **facts are built beside them**. The facts form one canonical TMP record. Regexes and cues for this phase live in `templates/default.toml` under `[facts]`.

**Where it runs:**
- in `run()`, after the router;
- or standalone, from a finished run's `elements.json` and `records.json`, with `tmp-pipeline-facts <doc_dir>` (`rebuild`, [facts/cli.py:29](../src/tmp_pipeline/ingestion/facts/cli.py#L29)).

### 6.1 `map_fields()` ([facts/stage.py:77](../src/tmp_pipeline/ingestion/facts/stage.py#L77))

The steps run in this order:

1. **Create `Facts`.** It holds the doc_id, version stamp, config hash and catalog version.
2. **Build the diagram register.** `sheet_register()` ([tmd.py:20](../src/tmp_pipeline/ingestion/facts/tmd.py#L20)) makes one `TmdSheet` per `diagram_sheet` record: id, title, sheet number, rev and aliases.
3. **Find the diagram mentions.** `find_mentions()` ([mentions.py:38](../src/tmp_pipeline/ingestion/facts/mentions.py#L38)) finds every "TMD4", "TMD-04" or "TMD Sheet 04" outside the diagram pages. It also takes the title that follows, up to `title_max_words` and stopping at `title_stop`. Duplicates are collapsed. Mention ids look like `m<page>:<i>`.
4. **Map the mentions to sheets.** `map_mentions()` ([tmd.py:61](../src/tmp_pipeline/ingestion/facts/tmd.py#L61)) works like this:
   - A mention with only a number maps by sheet number.
   - A mention with a title maps by `match_title`, which counts shared content words and adds a bonus for title synonyms. A tie means no match.
   - If there is no match and a model is available, the **X11** prompt is asked.
   - Without a model, the mention stays unmatched and `unverified`.
   - **The title wins.** If the number points to a different sheet than the title, the mention gets `id_conflict`, and its text is added to the sheet's aliases.
5. **Merge records into entities.** `merge_entities()` ([merge.py:76](../src/tmp_pipeline/ingestion/facts/merge.py#L76)) groups records by the catalog `match_key`:
   - Names are normalised first: "Ave" becomes "avenue", and day types are made canonical.
   - Entities marked `one_per_document` form a single group.
   - `merge_values` decides each field:
     - values that agree keep **every source**;
     - values that clash become `status=conflict`, keeping every version together with its pages;
     - a missing value never counts as a clash.
6. **Copy records one to one.** `copy_records()` copies records according to `record_kinds`, e.g. sections and yes/no answers become `form_answer`.
7. **Build the TMP document.**
   - `tmp_references()` ([parse.py:27](../src/tmp_pipeline/ingestion/facts/parse.py#L27)) collects the TMP reference from headers, text and sheet title blocks. If there is one distinct value it is verified; if there are several, they become a conflict.
   - `order_reference()` finds the order reference.
8. **Read the stages.** `stage_lists()` and `stage_entries()` find the lists of stages and their hours (e.g. "24x7 hrs"), merged per `tmd_id`.
9. **Read the speed limits.** `tsl_rows()` ([parse.py:82](../src/tmp_pipeline/ingestion/facts/parse.py#L82)) reads them from table rows matching `tsl_row`. It collects:
   - `speed_kmh`, `length_m` and `rp_from`/`rp_to` (`unverified` if fewer than 2 RPs are found);
   - the road;
   - `hours_by_day_type` and the dates;
   - the `tmd_ids` mentioned.
10. **Read the sign-offs.** `read_signoffs()` ([signoff.py:111](../src/tmp_pipeline/ingestion/facts/signoff.py#L111)):
    - Any table with a "Signature" label is a sign-off block.
    - The role comes from the `signoff_roles` regexes, applied to the heading above the table.
    - The status is one of:
      - `not_applicable`: the block is struck through;
      - `signed`: the signature cell has letters beyond a date;
      - `unsigned`: anything else, with a note.
11. **Collect the contacts.** Contacts come from the sign-offs plus `contacts_from_pairs()`. They are merged by name, and the roles are joined.
12. **Read the authorisation.** For a Form 3 table, the numbered items are read and the yes/no answers on that page are attached.
13. **Resolve pointers.** `resolve_pointers()` ([pointers.py:20](../src/tmp_pipeline/ingestion/facts/pointers.py#L20)) handles values like "See Above". Using `[facts.pointer_targets]` (e.g. `work_period.start_time → shift_window.first_sign_in`), it finds the nearest earlier fact on the same form. It stores this as a **link**, never as a copy.
14. **Add links.** `link_tmds()` and `link_sections()` ([links.py](../src/tmp_pipeline/ingestion/facts/links.py)) set `fact.links["tmd"]` and `fact.links["section"]`.
15. **Collect what is left over.** Records that nothing used go to `facts.other`.

### 6.2 `run_checks()` ([facts/stage.py:149](../src/tmp_pipeline/ingestion/facts/stage.py#L149))

`run_checks` does four things in order: it runs the rules, works out approval, checks coverage, and builds the review list.

#### Rules

`run_rules()` ([checks.py:238](../src/tmp_pipeline/ingestion/facts/checks.py#L238)) runs the ten rules of plan 3.3 in the order listed below.
- Conflicts get sequential ids, **`C-001`, `C-002`, …**, in rule order, so a rerun gives the same ids.
- Every value a conflict affects is set to `status=conflict`.

| # | Rule | What it checks |
|---|---|---|
| 1 | `tmp_reference` | The TMP reference is the same everywhere |
| 2 | `tmd_number_title` | A mention's number and title name the same sheet |
| 3 | `stage_lists` | Every sheet appears in each stage list |
| 4 | `traffic_flow` | A text statement (e.g. "two-way traffic maintained") does not contradict a sheet's mode (e.g. stop/go) |
| 5 | `tsl_extent` | The speed-limit RP range lies within the road's `start_rp..end_rp` |
| 6 | `tsl_hours` | The speed-limit hours for each day type match the shift window `first_sign_in..site_cleared` |
| 7 | `dates` | All the start dates agree, and all the end dates agree |
| 8 | `merged_values` | Every merge clash becomes a conflict |
| 9 | `risk_rating` | Risk rows with `rating_mismatch` are **flagged only** |
| 10 | `cell_colour` | Matrix cells with no verified rating are **flagged only** |

#### Approval

`approval()` ([signoff.py:156](../src/tmp_pipeline/ingestion/facts/signoff.py#L156)) walks `catalog.required_signoffs`.
- Form 3 is required only when a TSL is requested.
- The result is `signed`, `partially_signed` or `unsigned`, and is written to `tmp_document.approval_status`.

#### Coverage

`checks.coverage` lists two kinds of gap:
- elements with no outcome, or with outcome LEFTOVER;
- records that no fact, sheet, sign-off or `other` entry accounts for.

#### Review list

`build_review()` ([review.py:33](../src/tmp_pipeline/ingestion/facts/review.py#L33)) builds the review list, with these item kinds:
- `unverified:<path>`
- `conflict:<C-id>`
- `rating_mismatch:<fact>`
- `leftover:<element>`
- `unmatched_mention:<id>`
- `unsigned:<role>`

### 6.3 Reviewer corrections (`tmp-pipeline-review apply <doc_dir> corrections.json`)

The input file looks like this:

```json
{"reviewer": "A. Person", "items": [{"ref": "C-002", "action": "correct", "value": "...", "note": "..."},
                                    {"ref": "road:1.start_rp", "action": "confirm"}]}
```

`apply_corrections()` ([review.py:75](../src/tmp_pipeline/ingestion/facts/review.py#L75)) works out what each `ref` points to, trying these in order:
1. a conflict id;
2. a mention id;
3. a fact path.

It then:
- sets the matching values to `reviewed`, with a `review` stamp of who did it, when, the action, the note and the **original** value;
- closes the matching review items;
- increases `revision` by one;
- rewrites `facts.json` and `review.json`;
- appends a line to `corrections.jsonl`.

The paths it touched are returned, so `tmp-pipeline-chunks` can report which chunks were **corrected**.

### 6.4 `facts.json`

The file is defined in [facts/model.py](../src/tmp_pipeline/ingestion/facts/model.py). It holds:
- `doc_id`, `versions`, `config_hash`, `catalog_version`, `prompt_versions`, `revision`;
- `facts[]`;
- `tmd_sheets[]`, `mentions[]`, `signoffs[]`, `approval`, `conflicts[]`, `checks[]`, `coverage`, `other[]`, `review[]`.

The main types inside it:
- **`Fact`**: `fact_id` (`<entity>:<n>`), `entity`, `fields{name: FactValue}`, `sources[]`, `links{tmd, section}`, `applicability`, `flags`.
- **`FactValue`**: `value`, `raw`, `unit`, `status`, `method`, `sources[]` (record_id, page, element_ids, raw), `notes`, `versions` (only for a conflict), `link` (only for a pointer), `review`.
- A **fact path** is written `"<fact_id>.<field>"`, e.g. `road:1.start_rp`.

---

## 7. Phase 5: chunks

This phase builds the chunks from the saved `facts.json`, `records.json` and `elements.json`. **It never reads the PDF.** It runs inside `run()`, or on its own with `tmp-pipeline-chunks <doc_dir>`.

### 7.1 `build_chunks()` ([chunks/stage.py:25](../src/tmp_pipeline/ingestion/chunking/stage.py#L25))

1. **Build the document context.** `build_context()` ([chunks/context.py:49](../src/tmp_pipeline/ingestion/chunking/context.py#L49)) collects:
   - **TMP reference:** if it is in conflict, the first version is used and the rest become aliases. `slug` is made from it.
   - **Site address:** matched by `chunks.site_pattern`, e.g. "Worksite location: …".
   - **Roads and suburb.**
   - **Activity:** from the section named by `activity_context`.
   - **Parties:** client, contractor, rca and designer, taken from contact and sign-off roles.
   - **Work dates and approval status.**
   - **Per page:** form name, zone, printed page number.
   - **Context line:** a one-line sentence, "Temporary traffic management for {activity}, {site}".
2. **Run the builders.** `Builder(...).build()` ([chunks/build.py](../src/tmp_pipeline/ingestion/chunking/build.py)) runs 20 builder methods in a fixed order, then a `merge_small` pass (see 7.3). **Each record is rendered only by the first builder that claims it** (`self.used`). Every chunk is created through `emit()` ([build.py:128](../src/tmp_pipeline/ingestion/chunking/build.py#L128)), which:
   - collects the record ids, element ids and `field_paths` from the facts' sources. **Page furniture** (the TMP number printed in every page header, a source with no record) never decides a chunk's pages;
   - sets `chunk_id` to `"<SLUG>:<type>:<key>"`;
   - splits content that is too large into `:part-i` pieces that list each other as `siblings`.
3. **Add metadata.** `add_metadata()` ([chunks/meta.py:47](../src/tmp_pipeline/ingestion/chunking/meta.py#L47)) adds the plan 4.4 metadata, recounts tokens and recomputes the content hash.
4. **Check lineage.** `lineage.check()` ([chunks/lineage.py:28](../src/tmp_pipeline/ingestion/chunking/lineage.py#L28)) runs the lineage check.
5. **Return the result.** It returns `ChunkSet(doc_id, versions, facts_revision, chunks)` and the lineage dict.

### 7.2 Chunk types

The builders run in this order:

doc_card → stakeholders → activity → roads → schedules → speed_limits → stages → signoffs → vulnerable_users → yes_no_questions → form_answers_summary → risk_matrix → register_rows → register_summary → procedures → sheets → figures → standard_rules → narrative_sections → form_fields → narrative_table_rows → other_sections

| Chunk type | Built from | Content |
|---|---|---|
| `doc_card` | context, `tmp_document`, roads, sheets, approval, conflicts | TMP reference (and other spellings), order reference, site, activity, roads with RPs, parties, work dates, list of diagrams, approval and who has and has not signed, all conflicts. Never split. Its `doc_fields` are copied onto every indexed document |
| `stakeholders` | `contact` facts | One line per contact: "name — org — phone — email (roles)" |
| `activity` | activity section records | Heading and text |
| `road` | one per `road` fact | Field lines, e.g. "Posted speed (km/h): 50" |
| `schedule` | one per `shift_window` | First sign in, traffic control in, commence pick up, site cleared |
| `speed_limit` | one per `speed_limit` | Limit, road, RP extent, length, hours for each day type, dates, diagrams, "As written" |
| `stage` | one per `stage` | TMD and title, hours in place, the section text, where it is listed |
| `signoff` | one per sign-off | Role, status, name, organisation, signature and date (written "blank" when empty) |
| `vulnerable_user` | all tick boxes of the section | One chunk (or a few whole-block packs): "label: ticked / not ticked", note and details for each user |
| `form_fields` | road aspects, unclaimed checkboxes, unclaimed tables, road geometry, work period, the document identity, contingency and monitoring facts | Pairs or cells as text. Small record-backed chunks on one page under a shared heading are merged into `form_fields:page-N` |
| `yes_no_questions` | `form_answer` and `authorisation` facts, per page | "question answer", the items requested |
| `form_answers_summary` | `form_answer`, `vulnerable_user` and `road_aspect` facts | **Column view**: answers grouped by value with counts written out: "Answered yes (3): …", "Vulnerable road users not ticked (1): …". Not ticked is never written as "no" |
| `risk_matrix` | matrix cells, impact key, likelihood criteria | One line per likelihood; never split |
| `register_row` | one per `risk` | Hazard, groups, initial risk, controls, residual risk, rating-mismatch note, affected TMDs |
| `register_summary` | all `risk` facts and the matrix cells | **Column views** of the register, so "which risks are Very High?" meets one chunk instead of 15 rows. Counts are counted in code and written out, because the answer must not count. `:by-rating` holds initial and residual risk, initial → residual pairs and rating mismatches. `:by-likelihood` holds likelihood, consequence and likelihood x consequence for each stage. `:matrix-cells` holds the matrix cells grouped by rating. `:table` (and `:table-2` …) holds one line per risk with every rated column, packed whole with the column header in each pack; the packs are siblings |
| `procedure` | procedure tables | Split by operation (stop/go, lane diversion, shoulder closure, …) and by installation or removal. No parent id |
| `sheet_overview` | one per TMD sheet | Title, aliases, roads, legend, speed limits, RPs, callouts, places, drawn items |
| `sheet_sign_schedule` | sheet symbols with an `order` | "Signs on <approach>, numbered along the road": up to 15 per chunk |
| `sheet_notes` | the sheet's notes | Notes |
| `sheet_text` / `sheet_layout` / `sheet_distances` / `sheet_details` / `sheet_image` | `describe` facts plus the X13 read | Printed texts with where they are; left-to-right layout and relations; what each dimension runs between; title block and legend; the model's description, marked "(unconfirmed)" unless checked |
| `figure_caption` | sheet figures and standalone `figure` facts (grouped by phash) | Kind, pages, labels, numbers, description, plus the X13 full read |
| `standard_rule` | records on `generic_standard` pages | Text, `scope="generic"` |
| `narrative_section` | remaining sections, grouped by top-level heading | Whole sections packed up to `target_max`. A section in several chunks is numbered `:part-1 … :part-n` with "(part k of n)" in the header, and the parts are siblings |
| `narrative_table_row` | remaining `table_row` records | "header: cell" for each column. **Rows of one table are one chunk** (split into packs only above `target_max`) |
| `other_section` | LEFTOVER elements and all remaining records | Packed into pieces, with overlap |

### 7.3 Text, size and metadata rules

**Text** ([chunks/text.py](../src/tmp_pipeline/ingestion/chunking/text.py)):
- **Header.** Each chunk's header is a breadcrumb, `tmp_ref › form › path… — pages`, followed by the context line on a second line.
- **Values are written verbatim**, with these additions:
  - `" (unconfirmed)"` for an unverified value;
  - `" (confirmed by a reviewer)"` for a reviewed one;
  - `"conflict: v1 (pages) / v2 (pages)"` for a conflict;
  - `"<raw> (refers to: …)"` for a pointer.
- **Field labels** are made readable: `posted_speed_kmh` becomes "Posted speed (km/h)".

**Size** ([chunks/size.py](../src/tmp_pipeline/ingestion/chunking/size.py), `[chunks]` in the TOML):
- **Token targets:** `target_min` 200, `target_max` 450, `hard_max` 600.
- **Overlap:** `split_overlap_sentences` 2 when splitting; `other_overlap` 0.12 for `other_section`.
- **Token counting** ([chunks/tokens.py](../src/tmp_pipeline/ingestion/chunking/tokens.py)) uses `cl100k_base`, which is bundled in `chunks/data/` and checked by hash, so nothing is downloaded.
- **How content is broken up.** `pieces()` splits it at blank lines, then bullets, then sentences. `pack()` fills each piece greedily up to the budget, with the overlap. `split()` is used only for chunks over budget, which is `hard_max` minus the header.
- **Never split:** doc_card, risk_matrix, register_row, sheet_overview, sheet_sign_schedule, figure_caption (sheet figures) and other_section.

**Metadata** (`add_metadata`) adds these fields to each chunk:

| Field | Value |
|---|---|
| `doc_id` | The document id |
| `zone` | The zone |
| `page`, `printed_page`, `pages`, `section_path` | Where the chunk comes from |
| `tmd_ids` | From the builder plus every `TMD-NN` in the content |
| `linked_sheets` | The linked sheets |
| `rp_from`, `rp_to`, `rp_values` | The RPs |
| `scope` | `site_specific` or `generic` |
| `applicability` | `active` |
| `status` | The worst status of its values: conflict > unverified > not_applicable > verified |
| `has_conflict`, `conflict_ids` | The conflicts it touches |
| `is_boilerplate` | True if the same content is in at least 2 chunks spread over at least 3 pages (one chunk spanning pages is not repetition) |
| `boilerplate_hash` | The hash of that content |
| `access_groups` | The access groups |
| `codes` | Exact-match codes: `TMD-04`, `RP 0.465`, phone numbers, the TMP reference |

Finally `tokens` is set, and `content_hash` is the first 16 hex characters of the sha256 of the header plus the content.

**Merging small chunks** (`Builder.merge_small`, which finally uses `chunks.merge_below`): chunks that only clutter a page are merged so a question meets one coherent chunk instead of fragments that each cost a header.
- `form_fields` chunks under `merge_below` tokens, with no fact paths, on the same page and under a shared heading → `form_fields:page-N`.
- `narrative_table_row` chunks of the same table → one chunk per table.
- Provenance is unioned (pages, records, elements), so lineage still holds. Fact-backed and entity chunks (`road`, `schedule`, `signoff`, `register_row`, …) are never merged.

**Keywords** ([chunks/keywords.py](../src/tmp_pipeline/ingestion/chunking/keywords.py), no model): each chunk carries the catalog names for what it states (e.g. "street" for the road name, "speed limit" for the posted speed, "single lane alternating" for the Stop Go sheet), leaving out any word the chunk already says. They are indexed in local BM25, Cosmos `text` and the Azure `keywords` field, and are part of the embedding text.

### 7.4 Lineage check ([chunks/lineage.py](../src/tmp_pipeline/ingestion/chunking/lineage.py))

The check reports gaps; it never fixes them. A gap is any of the following:

| Gap kind | What must be true |
|---|---|
| `field` | Every field of every applicable fact appears in some chunk's `field_paths`. Struck facts are listed, not checked |
| `section_text` | Every sentence of every section that is not struck appears verbatim in the combined chunk text |
| `image` | Every `figure` and `diagram_sheet` record is carried by an image or sheet chunk |
| `leftover` | Every LEFTOVER element is in an `other_section` chunk |
| `record` | Every record is carried, either by id or because all its elements are |

Struck records and records with no text are **exempt** rather than counted as gaps.

`lineage.json` holds `{complete, fields, sections, images, leftovers, records, exempt, struck_facts, gaps[]}`.

### 7.5 `summarise_and_embed()` ([chunks/stage.py:40](../src/tmp_pipeline/ingestion/chunking/stage.py#L40))

1. **Write the X12 summaries.** `summarise()` ([chunks/summarise.py:23](../src/tmp_pipeline/ingestion/chunking/summarise.py#L23)) handles each chunk as follows:
   - If the previous `chunks.jsonl` has a chunk with the same `content_hash`, its summary is reused and no model call is made.
   - Without a model, the chunk is `skipped`.
   - Otherwise it asks **X12** up to twice. The second try is a separate cache key.
   - `check_x12` applies the **number guard**: every number in the summary must appear in the chunk. After two failures the chunk has no summary, `summary_notes` records why, and the chunk is counted as `rejected`.
2. **Embed the chunks.** `embed()` ([summarise.py:69](../src/tmp_pipeline/ingestion/chunking/summarise.py#L69)) builds the `embedding_text` from:
   - the breadcrumb line and the content (the document context line, identical in every chunk, is left out);
   - the summary;
   - the keywords;
   - glossary lines (`"STMS: site traffic management supervisor"` and so on) for each abbreviation found in the text, as a whole word (`chunks/text.py` `glossary_matches`, the one rule used everywhere).

   With an embedder it also embeds the text, through `CachedEmbeddings` over `vectors/`, and sets `vector_key`. `embedding_text` is **not** saved to `chunks.jsonl`. `fill_embedding_text()` rebuilds it before an upload.

**The core run does not embed live.** `run.py` and `tmp-pipeline-chunks` accept only `--embed off|replay`. Live vectors are made by `tmp-pipeline-index push --embed-live` and `tmp-pipeline-cosmos upload`.

### 7.6 Files and the rebuild command

**`chunks.jsonl`** ([chunks/model.py](../src/tmp_pipeline/ingestion/chunking/model.py)):
- Line 1 is a header: `{schema_version, doc_id, versions, facts_revision, chunks}`.
- Each following line is one chunk: `chunk_id`, `chunk_type`, `header`, `content`, `metadata`, `field_paths`, `record_ids`, `element_ids`, `parent_id`, `siblings`, `summary`, `summary_notes`, `tokens`, `content_hash`, `vector_key`.

**`tmp-pipeline-chunks <doc_dir>`** (`rebuild`, [chunks/cli.py:42](../src/tmp_pipeline/ingestion/chunking/cli.py#L42)) rebuilds the chunks and compares them with the previous file. It prints:

```
N chunks, lineage complete|with gaps; added a, removed r, changed c, corrected k
```

"corrected" counts chunks whose fields or conflicts are named in `corrections.jsonl`. The command exits with 0 when lineage is complete and 1 when there are gaps.

---

## 8. Storage back ends

All three back ends are **optional extras that the core never imports**. Each one loads from the files in `out/<doc>/`. The local files stay the source of truth, so a back end can always be rebuilt from them.

### 8.1 Azure AI Search ([search/](../src/tmp_pipeline/ingestion/embedding/search/))

**Commands:**
- `tmp-pipeline-index create`
- `tmp-pipeline-index push out/sha256-… [--embed-live]`

**Index `tmp-chunks-v1`** ([search/index.py](../src/tmp_pipeline/ingestion/embedding/search/index.py)), API `2024-07-01`:

| Field group | Fields |
|---|---|
| Key | `id` (the `chunk_id` with `:` replaced by `__`) |
| Searchable | `header`, `content`, `summary`, `glossary_terms`, `codes` (`keyword` analyzer, for exact matches) |
| Filterable | doc_id, tmp_ref, the doc-card fields (site, suburb, roads, parties, dates), chunk_type, zone, pages, tmd_ids, rp_from, rp_to, scope, applicability, status, has_conflict, conflict_ids, access_groups, … |
| Vector | `content_vector`: 3072 dims, HNSW with cosine distance, int8 scalar quantisation with rerank |
| Semantic | config `default` over `content` and `summary` |

**What `push()` does:**
1. Deletes the document's existing entries.
2. Uploads the new ones.
3. Polls until the count matches. If it never does, it raises `IndexCountError`.

`--embed-live` creates any missing vectors with `AzureEmbeddings` before uploading. A chunk without a vector is still uploaded and can still be found by keyword search.

**Querying** ([search/query.py](../src/tmp_pipeline/ingestion/embedding/search/query.py)):
- `filter_for()` builds the OData filter from doc, applicability, chunk types, scope, `only_tmd`, and **security trimming** on `access_groups`.
- `AzureSearch.search()` runs a hybrid query: BM25 plus `vectorQueries`, merged by the service with RRF. It then passes the results to the shared `rank_and_expand`.

> No command line currently uses `AzureSearch` for asking questions. It is a library back end; `tmp-pipeline-index` only pushes.

### 8.2 Cosmos DB for NoSQL ([cosmos/](../src/tmp_pipeline/ingestion/embedding/cosmos/))

**Commands:**
- `tmp-pipeline-cosmos upload <doc_dir> [--no-embed]`
- `tmp-pipeline-cosmos ask …`
- `tmp-pipeline-cosmos qa …`

**Storage.** Data goes in database `tmp`, container `chunks`, partitioned on `/doc_id`. Each chunk becomes one item ([cosmos/store.py](../src/tmp_pipeline/ingestion/embedding/cosmos/store.py) `to_item`). An item holds:
- the chunk fields;
- a `text` field for the full-text index (breadcrumb, content, summary, glossary expansions, codes);
- a `vector` (3072 dims, DiskANN);
- a `vector_key` (plain sha256 of the embedding text).

**Upload.** `upload()` re-embeds only the chunks whose `vector_key` changed and reuses the stored vector for the rest. It then upserts the items and deletes stale ones.

**Search.** `CosmosSearch.search()`:
1. Runs two queries **in parallel**:
   - a keyword query: `FullTextContainsAny` + `ORDER BY RANK FullTextScore` (BM25);
   - a vector query: `ORDER BY VectorDistance`.
2. Merges them **in Python** with RRF (k = 60). Cosmos's own RRF cannot filter the keyword side separately.
3. Passes the result to `rank_and_expand`.

`where()` applies the doc, access groups, applicability, types, scope and `only_tmd`.

**`ask` / `qa` with Cosmos.** Facts still come from the local `out/` folders; chunks and search come from Cosmos.

### 8.3 PostgreSQL facts store ([db/](../src/tmp_pipeline/ingestion/embedding/db/))

**Install:** `uv sync --extra postgres`. The DSN is read from `TMP_PG_DSN` and is never logged.

**Commands:**
- `tmp-pipeline-db migrate`: runs [db/schema.sql](../src/tmp_pipeline/ingestion/embedding/db/schema.sql), which is safe to run repeatedly.
- `tmp-pipeline-db write <doc_dir> [--access-group G …]`:
  1. `build_rows()` turns the facts into rows, in plain code.
  2. `write()` deletes the document's existing rows and inserts the new ones, all in **one transaction**. Writing the same document twice gives the same result.

**Schema:**
- `tmp_document` is the parent table. Every other table has `doc_id` (cascading delete) and `access_groups text[]`.
- `fact` holds every value; `field_evidence` holds the sources.
- **Typed tables:**
  - `road`, `shift_window`, `work_stage`, `speed_limit` (+ `speed_limit_window`), `authorisation`, `contact`, `risk` (+ `risk_tmd`), `risk_matrix_cell`, `section`, `figure`;
  - `tmd_sheet`, `tmd_mention`, `tmd_symbol`, `checkbox`;
  - `signoff`, `conflict`, `check_result`, `review_item`, `coverage`.
- **Conflicts.** A value in conflict is NULL in the typed tables; its versions live in `conflict`.

**Reading.** [db/reader.py](../src/tmp_pipeline/ingestion/embedding/db/reader.py) `PgFactsStore` implements the retrieval `FactsStore` interface. Every query filters on `cardinality(access_groups)=0 OR access_groups && user.groups`. Chunks are not stored in Postgres.

**Phase 8 work.** Row-level security is deferred to Phase 8. Until then `facts.json` stays canonical.

---

## 9. Phase 6: answering questions

Retrieval is chunks first, then code tools. Code finds the TMP, runs one hybrid search over its chunks, builds the context and checks the answer. Most questions are answered by one model call (Q3) from those chunks. When a question needs a computed figure, a date check, another TMP or another version, **Q3 may call read-only code tools** and write the answer from their results (section 9.6a). The tools are plain code over the facts and the search; the model never calculates. Worked figures that are asked for most are computed when the chunks are built, so they need no tool. Q3 may also call tools when it is given no TMP at all (library mode, 9.6a).

### Examples

```
uv run tmp-pipeline-ask --out out --tmp "KTS 26-1581" -v "How long is the 30 km/h zone?"
uv run tmp-pipeline-ask --out out --role site        # interactive; an empty line or "exit" ends it
```

### What `main()` sets up

`main()` ([retrieval/cli.py](../src/tmp_pipeline/retrieval/cli.py)) prepares the following, in order:
1. Tracing.
2. Config.
3. `LocalStore(out)`.
4. `LocalSearch`, or an injected back end such as Cosmos.
5. `Retriever(store, search, cfg, model)`.
6. `User(groups, role)` and a `Session`.

If `--tmp` is given, `find_tmp()` pins the session to that TMP.

With `-v`, the output also shows kind, method, TMP, latency, context tokens, model calls, each chunk used (with how it was found and its score) and any check problems.

### 9.1 The steps for one question: `Retriever._ask()` ([retrieval/pipeline.py](../src/tmp_pipeline/retrieval/pipeline.py))

`Retriever.ask()` wraps each question in a Langfuse span `tmp.ask` and a cost scope. Each step's latency is recorded.

```
question
  │ 1 choose TMP  choose_tmp(): the session's TMP, else the only TMP the user may read
  │               none chosen and several readable ──► ask_back; none readable ──► no_access (fixed reply, no search, no model)
  │ 2 retrieve    search_chunks(last question + this question)
  │ 3 context     assemble() → Context (≤ 10 000 tokens)
  │ 4 answer      Q3 write(): answers, or calls tools (≤ 2 rounds, ≤ 6 calls) → results join the context (TOOL RESULTS)
  │               (no model → safe_answer)
  │ 5 check       check_answer() → a number nothing gave? Q3 once more with a tool hint
  │               → problems? Q4 repair() once → still failing? safe_answer
  ▼ 6 remember    session.remember(), log "ask/answered"
AnswerResult{answer, kind, method, context, problems, first_problems, repaired, latency_ms, cost_usd, tokens, tool_calls}
```

The search query is the question joined to the **previous question** of the conversation, so a follow-up such as "and the other one?" still carries its subject. The question itself is not rewritten.

**`AnswerResult.kind`** is one of `answer`, `ask_back` or `no_access`.

**`method`** is one of `Q3`, `Q4`, `safe (no model)`, `safe (check failed)` or `fixed`.

### 9.2 Step 1: choose the TMP ([retrieval/session.py](../src/tmp_pipeline/retrieval/session.py))

No model and no pattern rules are involved.

**`Session`** holds the chosen TMP (`doc_id`, `tmp_ref`), the last 6 turns (`Q: …` / `A: …`, the answer cut to 300 characters) and a `session_id` that groups a conversation in traces. `use()` clears the turns when the TMP changes.

**`choose_tmp(store, user, session)`**:

| Situation | Result |
|---|---|
| The session has a TMP the user may still read | that TMP (`via` = session) |
| The session's TMP is no longer readable | `no_access` |
| No TMP chosen, exactly one newest version readable | that TMP (`via` = only TMP) |
| No TMP chosen, several readable | `several`: the fixed `ask_back` reply lists them |
| Nothing readable | `no_access` |

The user picks the TMP from the list they may read (the UI, or `--tmp`). `latest()` keeps only the newest version of each reference.

**`find_tmp(store, user, text)`** is the plain string matcher behind `--tmp` and the UI picker. It tries, in order: the exact key (`KTS 26-1581 V2`), the reference or an alias, then a part of the address, road or suburb. One match gives the TMP; several give `several`; none give `no_match`.

### 9.3 Step 2: facts and the document card

Retrieval does not read `facts.json` for the answer. Facts were used only in the ingestion, to build the chunks; their statuses and conflict versions are written into the chunk text in words ("(unconfirmed)", "conflict: both versions"), and each chunk carries `status` and `conflict_ids` in its metadata.

The `doc_card` chunk (TMP reference and other spellings, site, activity, roads with RPs, parties, dates, approval, conflicts, about 530 tokens) is **not** added to every question. It competes in the search like any other chunk. Questions about the TMP's identity or approval find it by their words; if the search does miss it, the model can call `get_facts` or `search_chunks` (9.6a).

### 9.4 Step 2: `search_chunks` ([retrieval/search.py](../src/tmp_pipeline/retrieval/search.py))

This is the only retrieval function. It has three back ends behind the `ChunkSearch` protocol; all three end in the same `rank_and_expand`.

| Back end | Where candidates come from |
|---|---|
| `LocalSearch` | In memory over `chunks.jsonl`: BM25, plus cosine over `vectors/` when an embedder is given |
| `AzureSearch` ([search/query.py](../src/tmp_pipeline/ingestion/embedding/search/query.py)) | One hybrid query to Azure AI Search (keyword and `vectorQueries`, fused by the service with RRF) |
| `CosmosSearch` ([cosmos/store.py](../src/tmp_pipeline/ingestion/embedding/cosmos/store.py)) | A keyword query and a vector query against Cosmos DB, fused in code |

**`SearchRequest`** holds `doc_id`, `query`, `k` (default 6) and `include_not_applicable`. **`Hit`** holds the chunk, its score and `via` (`match`, `parent`, `sibling`, `form_section`, `linked_sheet` or `conflict`).

**Filtering comes first.** The chunk must belong to the document, be `applicability = active` (unless asked otherwise) and be readable by the user's access groups. In Azure the group filter is part of the query (`access_groups/any()`), so a prompt can never widen access.

#### Keyword candidates: BM25 (K1 = 1.2, B = 0.75)

- **What is indexed:** the breadcrumb, content and summary; the glossary expansions of any abbreviation written in the content; the chunk's catalog `keywords`; and `metadata.codes`, each counted ×3.
- **Tokens** (`tokens()`): letters and digits are split apart, stopwords are dropped, a light stemmer joins "exceeding", "exceeds" and "exceed", and unit words are mapped (minutes becomes min, metres becomes m). Numbers and codes are not stemmed.
- **Codes** (`code_key()`): one spelling per code, so "TMD 4", "TMD-04" and "TMD sheet 4" all become `tmd04`, "RP 0.41" becomes `rp0.410`, and a phone number becomes its digits. `query_codes()` pulls the diagram numbers, RPs and phone numbers out of the question and adds them to the query, so an exact code beats ordinary words.
- It keeps the top `retrieval.candidates` (20). **Weak matches are cut:** a candidate scoring below `retrieval.min_score_ratio` (0.4) of the best one is dropped, so a chunk that shares one common word with the question does not take a slot. The cut applies to the local keyword list only; Cosmos and AI Search use their service scores.

#### Vector candidates: cosine

The question is embedded and compared with each chunk's `embedding_text` vector. Vectors are cached under `<doc>/vectors/`. This runs only when `LocalSearch` is given an embedder. **`tmp-pipeline-ask` and `-qa` build `LocalSearch` without one, so local search is BM25 only.** If the embedding call fails, a warning is logged and keyword search carries on.

#### Fusing, ranking and expanding

The two candidate lists are fused with reciprocal rank fusion, `1/(60 + rank)` per list. **`rank_and_expand()`** then:
1. **Drops generic chunks** (the contractor's general standard) when any site-specific chunk matched.
2. **Collapses boilerplate** that repeats (same `boilerplate_hash`).
3. **Keeps the top k.**
4. **Puts column views in.** When 3 or more of the k hits are rows of a table that has column views (`register_row` → `register_summary`), up to 3 views, best fused score first, take the place of the lowest-ranked rows, so the hits stay k. Keyword ranking (Cosmos BM25 above all) ranks the short rows above the longer views, and k rows can show only part of the table.
5. **Expands.** Each added hit is labelled with how it got there. At most k extra hits are added.

| `via` | What is added |
|---|---|
| `parent` | The parent table, when at least 3 hits are rows (`narrative_table_row`, `register_row`) of the same table |
| `sibling` | The **adjacent** `:part-N` pieces of a split chunk (`_neighbours`); they overlap, so a list stays whole. A split chunk without part numbers brings all its siblings |
| `form_section` | Every chunk of the same form section, for form chunks (`form_fields`, `yes_no_questions`, `vulnerable_user`) |
| `linked_sheet` | The `sheet_overview` of the TMD a top-3 hit links to (at most 2 linked sheets), up to `retrieval.max_linked_sheets` (1) per search: an overview is about 450 tokens |
| `conflict` | For the top 2 hits: the cheapest chunk (fewest tokens) that shows the other side of a conflict the hit carries |

### 9.5 Step 3: the context ([retrieval/context.py](../src/tmp_pipeline/retrieval/context.py))

`assemble()` writes the context as plain text, in this order:
1. **The TMP line:** reference, site, version, approval.
2. **CHUNKS:** best first. Each is `[n] breadcrumb [status · scope · struck · conflicts]` followed by its content. Generic chunks are labelled "the contractor's general standard", and struck chunks are marked.

**Budget.** `retrieval.context_budget` is 10 000 tokens. When the text is over budget, chunks are removed from the bottom of the ranking one at a time. The chunk ids that were cut are kept in `Context.dropped`.

**The `Context` object** carries the text, its token count, the question, the set of pages that can be cited (from the chunks' pages and the pages the text names), the chunks that fitted, and the generic and struck texts.

### 9.6 Steps 4-5: answer, check, repair ([retrieval/answer.py](../src/tmp_pipeline/retrieval/answer.py), [retrieval/check.py](../src/tmp_pipeline/retrieval/check.py))

#### 9.6a The code tools ([retrieval/tools.py](../src/tmp_pipeline/retrieval/tools.py), [calc.py](../src/tmp_pipeline/calc.py), [retrieval/timecheck.py](../src/tmp_pipeline/retrieval/timecheck.py))

All tools are read-only, run as the signed-in user (a TMP the user may not read is never reached) and return JSON with each value's status, unit and citation (form, section, page). A bad argument returns `{"error": …}`; a tool never raises. Every tool takes an optional `tmp` (a TMP key such as "KTS 26-1581 V2"; default: the current TMP).

| Tool | Does |
|---|---|
| `get_facts(entity, fields?, filter?)` | Stored values of one entity (road, shift_window, speed_limit, work_period, stage, risk, contact …) |
| `calculate(expression, entity? / values?, filter?, result_unit?)` | + − × ÷, min, max, abs, round, one comparison, km↔m, min↔h, date differences in days. Names only: values come from the entity's own fields or from `{name: {fact, tmp}}`; a number written in the expression is rejected. Returns result, unit, working, sources and a cross-check against a stated value |
| `time_check(what, date?, time?, day_type?)` | Whether work, the temporary speed limit or each stage is allowed at a NZ date and time: the date becomes a day type through `data/nz_calendar.toml` (Auckland 2026–27; a day it cannot decide gives "not known" and every window), then the time is compared with the shift window or speed-limit hours; dates outside the works dates are not allowed |
| `find_tmps(road?, suburb?, organisation?, active_from?, active_to?, approval_status?, has_conflict?)` | The readable TMPs (latest version of each) that match |
| `compare_versions(tmp?, from_version?, to_version?)` | Changed fields (from → to), sections added or removed, approval of each; default the two newest versions |
| `search_chunks(query, tmp?)` | The best chunks for another query, or of another TMP |

**The loop** (`answer.write`): Q3 gets the context, the tool definitions and a catalogue of the stored entities and fields of the TMP. If it asks for tools, the calls of one round run side by side (`llm.map_parallel`), their results go back to it as tool messages, and it goes on. After `max_tool_rounds` rounds or `max_tool_calls` calls it is called once more without tools and must answer. Afterwards the calls are written into `Context.text` after the chunks as `TOOL RESULTS` (never cut by the budget), so the answer check accepts the numbers and pages they gave. `retrieval.tools = false` turns all of this off.

**Library mode.** With several TMPs readable and none chosen, Q3 gets a list of the TMPs and the tools, with no chunks. It answers across them (`find_tmps`, then `get_facts` or `search_chunks` with a TMP), or replies `{"ask_back": true}` for a question about one unnamed TMP, which becomes the usual ask-back reply.

**Derived figures.** `[chunks.derived]` lists worked figures written into the chunks at build time with their working, e.g. "Length from the RPs: 0.615 km − 0.465 km = 0.150 km = 150 m (matches the stated 150 m)" in the speed-limit chunk (and on the document card), "Works extent on this road: 0.68 km − 0.41 km = 0.27 km = 270 m" in each road chunk, and "Works duration: 2026-10-28 − 2026-09-28 = 30 days" on the card. A value in conflict gives no line; an unconfirmed input marks the line "(unconfirmed)".

#### Writing the answer (Q3)

`write()` calls **Q3** (gpt-6-luna, no reasoning, prompt version 3.0) with the question, the context, today's date in NZ time, the last 4 turns and the role's style. Q3 follows 13 rules:
1. Copy numbers, times, dates, RPs, phone numbers and names exactly.
2. Never invent a number. Never calculate or work out dates yourself: call `calculate` or `time_check` and quote the result with its working and sources (a figure already written in the context needs no tool). With tools off: comparing a time or date with the context is fine, naming the entry used.
3. Cite (form, section, page).
4. Say when a value is unconfirmed.
5. Give every version of a conflicting value.
6. Say when a field is blank.
7. "Not ticked" does not mean "no".
8. Keep the general standard separate from this site's facts.
9. Describe diagrams in words, with the TMD and page.
10. Say what is missing.
11. Answer first, then give the detail, in the role's style.
12. Answer image questions from the image facts only.
13. Put JSON or a list inside the answer text when asked.

The reply is JSON, `{"answer": "…"}`. **Arithmetic is code's job:** the model calls `calculate` and `time_check` (9.6a) and quotes the result; a worked figure that is already in a chunk (the derived lines) needs no tool.

**Roles** (`retrieval.role_styles`):

| Role | Style |
|---|---|
| `site` | Short: one or two lines, then the source |
| `designer` (default) | Full: answer, working, sources, conflicts, statuses, sign-off detail |
| `office` | Plain language, with contacts and dates first |

#### Checking the answer

`check_answer()` is plain code. It scans the answer with the helpers in [retrieval/words.py](../src/tmp_pipeline/retrieval/words.py) (hand-written loops, no regular expressions) and reports a problem when:
1. **Numbers:** a number, time, date, RP or phone number in the answer is not in the context or the question. Equivalent spellings are accepted (15:00 and 1500, 0.150 and 0.15, 1,000 and 1000). The numbers of a numbered list are ignored.
2. **Pages:** a cited page is not in `Context.pages`.
3. **Generic or struck text:** a sentence shares a 6-word run with generic or struck-through text, none with the site's own text, and the answer never says "general standard" or "does not apply".

Unconfirmed values and conflicting versions are written in the chunk text, and Q3 rules 4 and 5 tell the model to say so; the check does not enforce them. The check never decides what is fetched.

#### Repairing the answer (Q4)

If the check finds problems, **Q4** `repair()` (gpt-6-sol, prompt version 1.1) is given the problems, the answer and the same context, and rewrites the answer **once**. The new answer is checked again. If it still fails, or Q4 returns nothing, the safe answer is used instead.

#### The safe answer

`safe_answer()` builds an answer in code from the same context: the best 4 passages, each clipped to about 420 characters from the line that shares most words with the question, labelled general standard or struck where that applies, with a breadcrumb.

With nothing at all, it returns the `not_in_tmp` text. It is built to pass the check, but it is still checked.

#### Fixed responses

`fixed()` fills the texts in `[retrieval.responses]`:

| Key | When it is used |
|---|---|
| `ask_back` | No TMP chosen and several readable: "I found {n} TMPs: {options}. Which one?" |
| `no_access` | The chosen TMP is not readable, or none is: "That TMP isn't available to you." |
| `not_in_tmp` | Nothing in the TMP answers the question: "The TMP doesn't say {what}." |

### 9.7 Data the retrieval reads ([retrieval/store.py](../src/tmp_pipeline/retrieval/store.py))

**`LocalStore(out)`** reads each document folder under `out/`. A single document folder also works. From each folder it reads:
- `facts.json`;
- `chunks.jsonl`, or chunks from Cosmos;
- `elements.json`, for the version only.

**`FactsStore`** is the protocol (`docs`, `facts`, `chunks`) that `LocalStore`, `PgFactsStore` ([db/reader.py](../src/tmp_pipeline/ingestion/embedding/db/reader.py)) and the Cosmos store all implement.

**`DocInfo`** summarises each document: reference and aliases, version, site address, roads, suburbs, organisations, work dates, approval status, whether it has conflicts, and access groups. `latest()` keeps only the newest version of each reference. `label()` is how a TMP is named in the ask-back list.

**Access.** `User.may_read` is true when the document has no access groups or the user's groups overlap them. Each chunk's own `access_groups` is filtered as well.

### 9.8 Model modes for retrieval

| Mode | Behaviour |
|---|---|
| `--model off` (default) | Every answer is a `safe (no model)` answer. TMP choice, search, context and the check run exactly as in the other modes |
| `--model replay --model-fixtures DIR` | Repeats a recorded live run offline |
| `--model live [--model-fixtures DIR]` | Calls Azure OpenAI. With a fixtures folder, it records every answer so a later replay can repeat the run |

Q3 and Q4 are the only retrieval prompts, so a replayed run needs only their fixtures.

---

## 10. The Document Intelligence track (separate)

**Command:** `tmp-pipeline-di "file.pdf" --doc-dir out/sha256-… [--cache-only | --live]`. The code is in [di/run.py](../src/tmp_pipeline/ingestion/extract/di/run.py).

This track runs **after** the main run and reads its `elements.json`. It is kept separate on purpose:
- Azure Document Intelligence is a paid, optional service.
- It must not change the results of the PDF-layer pipeline.
- An offline run must never reach the network by accident.

A test, `tests/ingestion/test_di.py::test_core_package_does_not_import_the_optional_packages`, checks that no core module imports `di`, `db`, `search` or `cosmos`.

### 10.1 Steps (`run()`, [di/run.py:24](../src/tmp_pipeline/ingestion/extract/di/run.py#L24))

1. **Load the elements.** It reads `elements.json` into an `ElementStore` and opens `di.log`.
2. **Decide where DI is needed.** `di_triggers()` ([di/triggers.py:27](../src/tmp_pipeline/ingestion/extract/di/triggers.py#L27)) is a pure function over Phase 1 output, with these triggers:

   | Trigger | When | Area |
   |---|---|---|
   | `scanned_page` | the page has fewer than `intake.min_text_chars` (20) characters | the whole page |
   | `low_text_quality` | a `text_block` has the `low_text_quality` flag | the block's box |
   | `text_in_picture` | an `image` routed FIGURE has no text block inside it | the image box |
   | `ambiguous_table` | a table routed TABLE or RISK_TABLE has no child text | the table box (a second opinion) |

   The triggers are always written to `di_triggers.json`.
3. **Stop here by default.** Without a flag, nothing is called and nothing is spent.
4. **Get the DI result** (only with `--cache-only` or `--live`). `analyze()` ([di/client.py:48](../src/tmp_pipeline/ingestion/extract/di/client.py#L48)) works like this:
   1. It looks in the cache first. The cache lives under `di/` ([di/cache.py](../src/tmp_pipeline/ingestion/extract/di/cache.py)), keyed on the file hash, model, API version, features and pages.
   2. On a cache miss with `--cache-only`, it raises `CacheMiss` and the command exits with code 2.
   3. On a cache miss with `--live`, it POSTs the PDF to `prebuilt-layout` (API `2024-11-30`, features `ocrHighResolution` and `styleFont`, output `figures`). It then polls `operation-location` every 2 seconds, up to 150 times.
   4. Authentication uses `AZURE_DI_ENDPOINT` together with either `AZURE_DI_KEY` or `DefaultAzureCredential` (the `azure` extra). The key is never logged.
5. **Convert the result to elements.** `di_to_elements()` ([di/to_elements.py:16](../src/tmp_pipeline/ingestion/extract/di/to_elements.py#L16)) does the following:
   - **Geometry.** DI uses inches with a top-left origin; elements use points with a bottom-left origin. [di/geometry.py](../src/tmp_pipeline/ingestion/extract/di/geometry.py) converts between them. A page is rejected if its size or angle does not match the PDF.
   - **Figures.** Each DI figure becomes a `figure` element.
   - **Words.** Each DI word becomes an `ocr_word` element with a stable id `p{page}:ocr_word:{di_index}` and a confidence value. Its parent is the figure that contains it, if any.
   - **Region filter.** A word is kept only if it lies inside a figure or a trigger area. A scanned page has no area, so every word on it is kept.
   - **Output.** The elements are written to **`di_elements.json`**, which uses the same schema as `elements.json`.

> **Current state:** no code merges or compares `di_elements.json` with the core elements yet. It is a parallel output, waiting for later phases to use it.

`deploy/azure/provision.sh` does not provision Document Intelligence, so `AZURE_DI_*` must be set by hand.

---

## 11. Evaluation: gold set and question set

### 11.1 Extraction gold set

**Command:** `tmp-pipeline-gold <doc_dir> <gold.json>`. The code is in [gold/score.py](../src/tmp_pipeline/ingestion/score.py). You can also pass `--gold` to `tmp-pipeline`, which adds the score to `report.md`.

| Measure | Target |
|---|---|
| Field accuracy (typed equality) | ≥ 98 % |
| Wrong values marked verified, including critical ones | 0 |
| Wrong values flagged unverified | 100 % |
| Expected unverified values and flags present | 100 % |
| Invented fields (every word of `raw` must appear in the cited elements) | 0 |
| Record coverage | 100 % |
| Sheet legend, notes and roads match exactly | 100 % |
| Symbol recall and precision | ≥ 95 % |
| Conflict recall and precision | 100 % |
| Facts exact: approval, sign-offs, mention mapping, pointers, merges, mismatches | 100 % |
| Lineage complete, chunk types, chunk size limits, key chunk lines | 100 % |

`tests/data/gold/sample.gold.json` is a **draft**. It was keyed by the same person who wrote the extractor, so its numbers do not measure accuracy until someone independent keys it a second time.

### 11.2 Question test set

**Command:** `tmp-pipeline-qa tests/data/gold/sample.questions.json --out out [--model …] [--json results.json]`. The code is in [gold/questions.py](../src/tmp_pipeline/retrieval/eval.py).

**File format.** The top level has:
- `document`;
- `today` (pins the clock, so relative dates give the same result every run);
- `questions[]`.

Each question has:
- `id`, `row`, `question`;
- `types`, and `rules` (true if the rules alone must find the types);
- `context`, `answer` and `pages`: strings and pages that must appear;
- `flags`: `conflict`, `unconfirmed`, `not_in_tmp`, `ask_back`, `no_match`, `no_tmp` or `json`;
- `must_not`;
- `turns` (earlier questions in the same session), `session`.

**Running.** `run_questions()` ([questions.py:176](../src/tmp_pipeline/retrieval/eval.py#L176)) gives each question a new session, replays its `turns`, asks it, and scores it with `score_one()`.

**What is scored:**
- **The offline gate:** `rules_type`, `context`, `pages_in_context`, `no_wrong_number`, `fixed_response`. All of these must be 100 % for exit code 0, and none of them needs a model.
- **The answer measures:** `type`, `answer`, `page_cited`, `conflict`, `unconfirmed`, `not_in_tmp`, `no_invented`, `json`.

The report also gives the repair count, p95 latency (target: first words in under 3 s) and model cost.

---

## 12. Environment, deployment and tests

### 12.1 Environment variables

| Variable | Used by |
|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY` | Live chat (`--model live`) and embeddings |
| `AZURE_OPENAI_CHAT_DEPLOYMENTS` | `"gpt-6-sol=<deployment>,…"`, needed when deployment names differ from model names |
| `AZURE_OPENAI_ROUTES`, `AZURE_OPENAI_<NAME>_ENDPOINT/KEY` | Sending a model to another Azure resource |
| `AZURE_OPENAI_REASONING_EFFORT` | Default `low` |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Default `text-embedding-3-large` |
| `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_KEY` | `tmp-pipeline-index` |
| `AZURE_COSMOS_ENDPOINT`, `AZURE_COSMOS_KEY`, `AZURE_COSMOS_DATABASE`, `AZURE_COSMOS_CONTAINER` | `tmp-pipeline-cosmos` |
| `AZURE_DI_ENDPOINT`, `AZURE_DI_KEY` | `tmp-pipeline-di --live` (or `DefaultAzureCredential` with the `azure` extra) |
| `TMP_PG_DSN` | `tmp-pipeline-db` |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `LANGFUSE_TRACING_ENVIRONMENT` | Tracing (`tracing` extra) |
| `TMP_TRACE_CONTENT=1` | Send prompt and answer text to Langfuse |
| `TMP_MODEL_PRICES` | Use a different price file |
| `TMP_REQUIRE_SAMPLE=1` | In tests, a missing `tests/data/sample.pdf` fails instead of skipping |

### 12.2 Deployment ([deploy/azure/](../deploy/azure/))

`provision.sh` is an idempotent `az` script. Its defaults are subscription `Simform-Dev`, resource group `AI-CoE-rg` and region `australiaeast`.

| Subcommand | What it does |
|---|---|
| `check` | Shows what exists |
| `create` | Creates the AI Foundry resource `ai-coe-tmp-aue` and the project `tmp-pipeline` |
| `deploy` | Deploys the models from `models.tsv`: gpt-6-sol, gpt-6-luna, gpt-5.4, gpt-5.5, gpt-5.4-mini, text-embedding-3-large |
| `search` | Creates AI Search (free tier) |
| `cosmos` | Creates Cosmos DB (free tier): container `chunks`, partition `/doc_id`, DiskANN index on `/vector` (3072 dims), full-text index on `/text` |
| `env` | Writes `~/.config/tmp-pipeline/.env.azure` |

It does **not** create Document Intelligence. Set `AZURE_DI_*` yourself.

### 12.3 Tests (`tests/`)

**Commands:**
- `uv run pytest -q` runs the tests.
- `uv run ruff check src tests && uv run mypy` runs the linter and type checker.

**Shared files:**
- `conftest.py` builds small PDFs with reportlab.
- `data/` holds `sample.pdf`, `golden_sample_snapshot.json` and `gold/`.

| Area | Test files |
|---|---|
| Phase 1 | `test_intake`, `test_config`, `test_models`, `test_geometry`, `test_layers_reader`, `test_textblocks`, `test_to_elements`, `test_zones`, `test_quality`, `test_furniture`, `test_gridfind`, `test_layer_enrichment`, `test_ticks`, `test_render` |
| Router and handlers | `test_router`, `test_text_handler`, `test_tables`, `test_risk`, `test_checkboxes`, `test_numerics`, `test_records`, `test_catalog` |
| Diagrams and images | `test_diagram_sheet`, `test_diagram_sample`, `test_diagram_overlay`, `test_diagram_model`, `test_diagram_validators`, `test_image_facts` |
| Model | `test_model_client`, `test_model_validators` |
| Facts, chunks, back ends | `test_facts`, `test_chunks`, `test_search`, `test_cosmos`, `test_db` (writes only when `TMP_PG_DSN` is set) |
| Retrieval | `test_retrieval`, `test_retrieval_sample` |
| Infrastructure | `test_tracing`, `test_cache_transport`, `test_logging`, `test_versions` |
| End to end | `test_run_lifecycle`, `test_run_sample`, `test_golden_sample` (snapshot regression), `test_gold` |
| DI | `test_di`, including the check that the core does not import the optional packages |

---

## 13. Known gaps and things to watch

These came up while tracing the code. None of them breaks a run, but each one may surprise someone reading or extending it.

**Not built yet:**
- **Operations.** Phase 8 (row-level security, database as source of truth) is not built. The plain RAG has not been measured live on the question sets, and the offline tests are stale after the retrieval rewrite.
- **DI output is unused.** Nothing merges `di_elements.json` into the core elements.
- **No live embedding in the core run.** `tmp-pipeline` and `tmp-pipeline-chunks` accept only `--embed off|replay`.
- **Local search is keyword only** (with a relative score cutoff, 9.4). `tmp-pipeline-ask` and `-qa` build `LocalSearch` without an embedder, so local search is BM25. Vectors are used only through Cosmos (or AI Search used as a library).
- **`AzureSearch` is not wired to a command.** It exists for asking questions, but no command uses it.
- **Scanned documents are only noted.** `scanned` is recorded at intake but changes nothing; the DI track handles scans.

**Defined but unused:**
- **Prompts.** X1 does not exist (the router only records `needs_x1`).
- **Code paths.** `read_image(critical=True)` is never called, so a second X7 read happens only after a failed check. `layers/render.py` `Renderer.crop` is unused (`handlers/imageio.py` crops the master).

**Inconsistencies:**

**Surprising behaviour:**
- **Rotated text has no band.** It is always treated as body, so it can never be page furniture.
- **Live cache hits are not recorded.** In live mode the model cache is outermost, so an answer served from the cache is not written to the fixtures.

---

## 14. Appendix: file-to-step index

All paths are under `src/tmp_pipeline/`. The section numbers refer to this document.

| File | Step | Section |
|---|---|---|
| `run.py` | Main command; runs every stage in order | 3 |
| `intake.py` | Hashes the file, opens it, checks for text, reads the version, `.done` | 3.3 |
| `layers/reader.py` | Reads chars, shapes, images, drawing order and form fields | 3.3 |
| `layers/gridfind.py` | Finds table grids | 3.3 |
| `layers/textblocks.py` | Builds words, lines, segments and blocks; struck text; reading order | 3.3 |
| `layers/ticks.py` | Finds checkboxes | 3.3 |
| `layers/quality.py` | Scores text quality | 3.3 |
| `layers/to_elements.py` | Creates the first elements | 3.3 |
| `layers/render.py` | 200 DPI masters, pHash | 3.3 |
| `zones.py` | Page and element zones | 3.3 |
| `furniture.py` | Headers and footers, TMP reference, form names | 3.3 |
| `models.py`, `vocab.py`, `geometry.py` | Data model, vocabularies, box maths | 3.5 |
| `config.py`, `templates/default.toml` | Config | 3.5 |
| `logging_.py`, `tracing.py`, `cache.py`, `transport.py`, `versions.py`, `errors.py`, `constants.py` | Shared infrastructure | 3.5 |
| `report.py` | `report.md` | 3.5 |
| `model/client.py`, `model/live.py`, `model/azure_chat.py` | Model clients and modes | 4.1 |
| `model/embeddings.py`, `model/pricing.py` | Embeddings, cost | 4.1 |
| `model/prompts.py` | X2–X13, Q3, Q4 | 4.2 |
| `model/validators.py` | Checks on model output | 4.3 |
| `catalog.py`, `templates/catalog.yaml` | Schema catalog | 4.4 |
| `diagrams/stage.py` | Diagram stage entry point | 5.1 |
| `diagrams/overlay.py`, `textlines.py`, `legend.py`, `regions.py`, `symbols.py`, `assoc.py`, `lineage.py`, `render.py`, `describe.py`, `models.py` | Diagram reading | 5.1 |
| `router.py` | Decides, applies and routes | 5.2 |
| `contracts.py`, `records.py`, `numerics.py` | Handler contract, records, typed values | 5.3 |
| `handlers/__init__.py`, `handlers/stubs.py` | Registry | 5.3 |
| `handlers/text.py`, `handlers/textutil.py` | TEXT | 5.4 |
| `handlers/tables.py`, `handlers/grid.py` | TABLE | 5.4 |
| `handlers/risk.py` | RISK_TABLE | 5.4 |
| `handlers/checkboxes.py` | CHECKBOX | 5.4 |
| `handlers/diagram.py`, `handlers/diagram_model.py` | DIAGRAM (X8, X9, X10) | 5.4 |
| `handlers/image.py`, `imageread.py`, `image_full.py`, `imageio.py` | FIGURE (X7, X13) | 5.4 |
| `facts/stage.py` | `map_fields`, `run_checks` | 6.1–6.2 |
| `facts/mentions.py`, `facts/tmd.py` | Diagram mentions, mapping them to ids (X11) | 6.1 |
| `facts/merge.py`, `facts/parse.py`, `facts/pointers.py`, `facts/links.py`, `facts/signoff.py`, `facts/common.py` | Building facts | 6.1 |
| `facts/checks.py` | The ten rules, coverage | 6.2 |
| `facts/review.py`, `facts/cli.py` | Review list, corrections, rebuild | 6.2–6.3 |
| `facts/model.py` | `facts.json` | 6.4 |
| `chunks/stage.py`, `context.py`, `build.py`, `text.py`, `size.py`, `tokens.py`, `meta.py`, `keywords.py`, `lineage.py`, `summarise.py`, `model.py`, `cli.py` | Chunks | 7 |
| `search/index.py`, `search/query.py`, `search/run.py`, `search/azure_openai.py` | Azure AI Search, embeddings | 8.1 |
| `cosmos/store.py`, `cosmos/cli.py` | Cosmos DB | 8.2 |
| `db/schema.sql`, `db/writer.py`, `db/reader.py`, `db/run.py` | PostgreSQL | 8.3 |
| `retrieval/cli.py`, `retrieval/pipeline.py` | Question command, orchestration | 9.1 |
| `retrieval/session.py` | `Session`, choosing the TMP, `find_tmp` | 9.2 |
| `retrieval/search.py` | BM25, vectors, RRF, ranking and expansion | 9.4 |
| `retrieval/context.py` | Context | 9.5 |
| `retrieval/answer.py`, `retrieval/check.py` | Q3, check, Q4, safe answer, fixed responses | 9.6 |
| `retrieval/store.py` | `LocalStore`, `DocInfo`, access | 9.7 |
| `retrieval/words.py` | Plain-text helpers (no regular expressions) | 9.4, 9.6 |
| `di/run.py`, `di/triggers.py`, `di/client.py`, `di/cache.py`, `di/geometry.py`, `di/to_elements.py` | Document Intelligence track | 10 |
| `gold/score.py`, `gold/questions.py` | Gold set and question set | 11 |

---


> **Chunker 0.2.0 (2026-10-16).** Page furniture no longer decides a chunk's pages; the boilerplate rule needs repetition across chunks; `other_section` parts carry only their own provenance; tiny chunks are merged (`merge_small`, one chunk per table, one for the vulnerable-user tick list); split sections have real `:part-N` ids and siblings; no chunk has a made-up parent id; chunks carry catalog keywords; the embedding text drops the repeated context line; one glossary rule is used by every back end; the conflict expansion adds the cheapest chunk that shows the other side.

> **Retrieval, 2026-10-16 (since replaced by the plain RAG of section 9).** Local search drops keyword candidates below 0.4 of the best score at most one linked-sheet overview is added per search, and the conflict expansion adds the cheapest chunk. On the question set the offline gate stays 100%; context tokens per question fell from 1,164 to 1,038 against the same chunks.

> **Cleanup, 2026-10-16.** Removed code nothing used: the X3 cell-read prompt and its validator, the `struck` fixed response and `diagrams.dimension_assoc_max_pt` config keys, five unreferenced helpers (`region_centre`, `sign_centre`, `sources_of`, `received_counts`, `tmd_numbers`), the `MASTER_DPI_KEY` constant, and two dead parameters (`find_images(other_pages)`, `_tag_image(sheet_id)`). A custom template that still sets either removed config key now fails to load, naming the key.
