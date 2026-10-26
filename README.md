# TMP pipeline

Extraction, chunking and retrieval for New Zealand Temporary Traffic Management Plans. The design is in
`TMP Combined Plan.html` (not committed; it lives next to the repo). The code is two folders:

- `src/tmp_pipeline/ingestion/`: a PDF becomes chunks and embeddings (`extract/` reads the PDF, `facts/`, `chunking/`,
  `embedding/` with the Azure AI Search, Cosmos DB and PostgreSQL stores).
- `src/tmp_pipeline/retrieval/`: a question becomes a checked answer (plain RAG, below), plus the Streamlit UI (`ui/`),
  the question test set (`eval.py`) and the synthetic test TMPs (`synthetic.py`).

Shared code is plain files at the package root: `config.py`, `llm.py` (model clients and cost), `logging_.py`,
`tracing.py`, `cache.py`, `transport.py`, `records.py`, `errors.py`, `constants.py`; `data/` and `templates/` hold
package data. A live Azure OpenAI client exists (`--model live`); everything is also tested with fakes, replay fixtures
and fake transports.

```
uv sync
uv run pytest -q                      # unit tests; the sample-PDF tests skip without tests/data/sample.pdf
TMP_REQUIRE_SAMPLE=1 uv run pytest    # CI: a missing sample fails instead of skipping
uv run ruff check src tests && uv run mypy

# PDF -> element store, zones, routes, records (out/<doc_id>/)
uv run tmp-pipeline "TMP V1 (...).pdf" --out out/ [--force] [--skip-render] [--template file.toml] \
    [--gold tests/data/gold/sample.gold.json] [--model off|replay --model-fixtures DIR]

# score an existing run against the gold set (records, sheets and, when facts.json exists, facts)
uv run tmp-pipeline-gold out/sha256-... tests/data/gold/sample.gold.json

# Phase 4 on a finished run, without reading the PDF again
uv run tmp-pipeline-facts out/sha256-...                      # rebuild facts.json + review.json (catalog/check change)
uv run tmp-pipeline-review apply out/sha256-... corrections.json   # reviewer corrections -> "reviewed"

# Phase 5 on a finished run: rebuild chunks.jsonl + lineage.json (chunker change or reviewer correction)
uv run tmp-pipeline-chunks out/sha256-...                     # prints added / removed / changed / corrected chunks

# Phase 6: ask questions about the processed TMPs (out/), and run the question test set
uv run tmp-pipeline-ask --out out --tmp "KTS 26-1581" -v "How long is the 30 km/h zone?"
uv run tmp-pipeline-ask --out out --role site            # interactive session; empty line or "exit" ends it
uv run tmp-pipeline-qa tests/data/gold/sample.questions.json --out out [--json results.json]
#   --model off (default: code-built safe answers) | replay --model-fixtures DIR | live [--model-fixtures DIR]
#   live needs AZURE_OPENAI_ENDPOINT/KEY (+ AZURE_OPENAI_CHAT_DEPLOYMENTS="gpt-6-sol=<deployment>,..." when the
#   deployment names differ) and records every answer to --model-fixtures so replay repeats the run offline

# Azure AI Search (optional: AZURE_SEARCH_ENDPOINT/KEY; --embed-live also AZURE_OPENAI_ENDPOINT/KEY)
uv run tmp-pipeline-index create
uv run tmp-pipeline-index push out/sha256-... [--embed-live]

# PostgreSQL facts store (optional: uv sync --extra postgres; DSN in TMP_PG_DSN)
uv run tmp-pipeline-db migrate
uv run tmp-pipeline-db write out/sha256-... [--access-group designers]

# Document Intelligence track (separate; default only evaluates when DI is needed)
uv run tmp-pipeline-di "TMP V1 (...).pdf" --doc-dir out/sha256-... [--cache-only | --live]

# Streamlit UI: browse every output, ask questions with the full trace, run the pipeline on a PDF
uv sync --extra ui --extra cosmos --extra postgres --extra tracing   # uv sync removes extras it is not given
uv run tmp-pipeline-ui --out out_live            # extra arguments go to streamlit, e.g. --server.port 8600
```

The UI pages are: overview (metadata, stage timings, report), pages and elements (boxes on the page renders),
router and records (with evidence crops), diagrams, facts and checks, chunks, ask, QA results, and run pipeline.
It reads the same run folders as the CLIs. Live mode needs the same environment variables, exported in the shell
that starts the UI.

`--live` needs `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` in the environment (the key is never logged); install the
`azure` extra to authenticate with `DefaultAzureCredential` instead. Exit codes: 0 ok, 1 handler failures or
incomplete coverage, 2 bad input, config or schema (message on stderr).

## Output layout (`out/<doc_id>/`)

| File | Content |
| --- | --- |
| `elements.json` | The element store: every item with page, boxes, zone, type, flags, route and parent. `schema_version` 1 |
| `records.json` | Records produced by handlers: typed values with status, evidence element ids and a version stamp |
| `sheets.json` | The diagram sheets as read by the diagram stage: symbols, legend, notes, regions, images, title block |
| `facts.json` | The canonical TMP record (Phase 4): merged facts with sources, diagram register and mentions, sign-offs, approval, conflicts, check results, coverage, review list, revision |
| `review.json` | The review list alone: unverified values, conflicts, rating mismatches, leftovers, unmatched mentions, unsigned blocks |
| `corrections.jsonl` | One line per applied corrections file (reviewer, items, affected paths, revision) |
| `chunks.jsonl` | Phase 5: a header line, then one chunk per line (id, type, header, content, summary, embedding text, metadata, field paths, records, elements, tokens, content hash, vector key). The index is rebuilt from it |
| `lineage.json` | The lineage check: complete or not, counts, exempt records (no text), struck facts, gaps |
| `vectors/` | Embedding vectors by model, dimensions and text hash (only with an embedding client); unchanged chunks reuse theirs |
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

records -> map_fields -> run_checks -> write_facts -> facts.json, review.json  (-> PostgreSQL, optional)
          (merge, ids,   (10 rules,     (snapshot)
           pointers,      approval,
           sign-offs)     coverage)

facts + records + elements -> build_chunks -> summarise_and_embed -> chunks.jsonl, lineage.json  (-> AI Search, optional)
                              (22 types,      (X12 + number guard,
                               sizes, meta,    embedding text,
                               lineage)        vectors)
```

Module paths below are inside `src/tmp_pipeline/ingestion/` (`extract/` holds the intake, layers, handlers, diagrams, `di/`,
router, zones and furniture modules) unless a folder is named.

- `config.py` + `templates/default.toml`: typed, validated config. Layout cues (header band, rating colours,
  form names) and tunable thresholds live in the template, so a new company's layout is a new file, not a code
  change. Unknown keys, wrong types and bad regexes fail at load with the key path.
- `models.py` / `vocab.py`: `Element`, `ElementStore`, `PageInfo`; closed vocabularies (`ElementType`, `Zone`,
  `Flag`, `Route`, `Outcome`) are validated on every assignment.
- `geometry.py`: the one place for box maths and point/pixel conversion.
- `router.py`: `decide()` is pure (one `Decision` per element, from ordered named rules); `apply_decisions()`
  writes them back; `route()` runs handlers with per-element failure isolation.
- `contracts.py`, `records.py`: the handler contract and the records model (below).
- `llm.py` (package root): the model client interface, caching, recording and replay, the live Azure OpenAI client and the call cost.
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
- `prompts.py` (X2-X13 text from the plan, versioned), `validators.py` (quote verbatim, value in quote, unit
  matches, X2/X4/X6 output checks; for images, cited words must exist, hold the value and sit together, X8 claims
  may only cite existing symbols and numbers on the sheet), `llm.py` (interface, cache, record/replay). Without a model, unknown
  headings stay unnamed, X5-only fields are absent, and critical facts nobody judged are stored `unverified`.
- `score.py`: the scorer for the extraction gold set (§7.1), including sheet facts (legend, notes, roads exact; symbol
  recall and precision). `tests/data/gold/sample.gold.json` is a **draft** keyed by
  the same person who wrote the extractor; it needs an independent second keying before its numbers mean accuracy.
- `facts/` (Phase 4, plan part 3): records stay as the handlers wrote them; facts are built beside them.
  `mentions.py` finds every diagram reference ("TMD4: Parking Management"); `tmd.py` maps it to a sheet from the title
  blocks (title wins over number, the original kept as an alias, `id_conflict`; number-only by number; no match -> X11,
  without a model unmatched and unverified); `parse.py` reads TMP references, the order reference, speed-limit rows,
  stages and contacts; `merge.py` merges by `match_key` (names normalised, "Ave" -> "avenue"), agreeing values keep
  every source and clashing ones become `conflict`; `pointers.py` stores "See Above" as a link; `links.py` links facts to
  sheets and sections; `signoff.py` reads sign-off grids by label and computes approval; `checks.py` runs the ten
  rules of plan 3.3 (ids C-001... in rule order); `review.py` builds the review list and applies corrections.
- `embedding/db/`: the PostgreSQL facts store (`schema.sql`, writer, CLI), an optional extra. Like `di/`, the core never
  imports it (a test enforces this). Until phase 8 `facts.json` is canonical and the database is loaded from it.
- `chunking/` (Phase 5, plan part 4): `build.py` has one builder per chunk type (doc card, stakeholders, activity, road,
  schedule, speed limit, stage, form fields, yes/no questions, vulnerable users, risk matrix, register rows,
  narrative sections and table rows, procedures by operation x install/removal, sheet overview / sign schedule / notes,
  figure captions, sign-offs, standard rules, other); `text.py` writes values verbatim with "(unconfirmed)" and conflicts
  in words; `size.py` merges small siblings and splits over 600 tokens between sentences with overlap; `meta.py` adds
  the plan 4.4 metadata; `lineage.py` checks every field, section, image and leftover is in a chunk; `summarise.py`
  runs X12 and builds the embedding text; `tokens.py` counts with `cl100k_base`, bundled in `chunking/data/` (no
  download). `embedding/embeddings.py` is the embedding interface with fake, cache and record/replay clients.
- `embedding/search/`: Azure OpenAI embeddings and the Azure AI Search index (schema, delete-upload-count push), an optional
  package over the injectable HTTP transport. The core never imports it (a test enforces this).
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

## Facts: what is checked, and how statuses work

Every value is `verified`, `unverified` (a check failed or nothing confirmed it; all candidates kept), `conflict` (the
document contradicts itself; every version kept, no value chosen), `not_applicable` (struck) or `reviewed` (a person
confirmed or corrected it; the original is kept under `review`). Checks never block and never correct.

| Rule | Check | On the sample |
| --- | --- | --- |
| 1 | TMP reference identical everywhere (headers, text, title blocks) | C-001: KTS 26-1581 vs KTS 26-1580 (p1) |
| 2 | Every diagram mention's number agrees with its title | C-002: pages 3, 5, 10 |
| 3 | Every diagram appears in each stage list | C-003: Stop/Go missing on pages 3 and 10 |
| 4 | Traffic-flow statements fit the diagrams (cues in `[facts.flow_statements]`/`diagram_modes`) | C-004: p12 vs TMD-04 |
| 5 | Speed-limit extent inside the works extent | pass |
| 6 | Speed-limit hours match the shift windows by day type | pass |
| 7 | Work dates agree everywhere | pass |
| 8 | Merged records have no clashing values | pass |
| 9 | Risk rating agrees with its matrix cell (from the risk handler) | 7 rows flagged |
| 10 | Matrix cell colour matches a key rating | pass |

Approval: a block is signed only when its signature cell has content (a date alone is not a signature); struck blocks
are not applicable; Form 3 is required only when a temporary speed limit is requested. The sample is
`partially_signed` (designer and checker signed; activity manager, RCA primary and Form 3 not).

## Retrieval: how a question is answered

Chunks first, then code tools the answer model may call. `retrieval/` has no pattern rules and no query rewriting: the
model decides when a tool is needed. `Retriever.ask(question, session, user)`:

1. **TMP**: the user picks the TMP from the list they may read (`store.docs(user)`; auth decides the list). The UI shows a
   select box, the CLI takes `--tmp`. One readable TMP is used automatically; several and none chosen gives the ask-back
   reply; a TMP the user may not read is "not available to you". (`session.py`)
2. **Retrieve** (`search.py`): one hybrid search in that TMP's chunks: BM25 plus optional vectors, reciprocal rank
   fusion, at most two general-standard chunks, boilerplate collapsed, then expansion (parent table, linked sheet,
   neighbouring parts, form section, other side of a conflict). The query is the question plus the previous one.
   Facts are not read at question time: they were used to build the chunks. The `doc_card` chunk (TMP reference and
   spellings, dates, contacts, roads, approval) is an ordinary chunk: it is found by search like any other.
3. **Context** (`context.py`): the TMP line and the chunks, best first; a token budget cuts chunks from the bottom.
4. **Answer** (`answer.py`): Q3 (gpt-6-luna), with today's NZ date and time, the last turns and the role style from
   `[retrieval.role_styles]`. Most questions are answered from the chunks in one call. When the question needs more, Q3
   calls code tools (`tools.py`) and goes on with their results, at most `max_tool_rounds` (2) rounds and
   `max_tool_calls` (6) calls: `calculate` (arithmetic over stored values, never a number of its own, shown with its
   working), `time_check` (allowed or in force at a NZ date and time, from the shift windows, speed-limit hours, works
   dates and the Auckland school-term and public-holiday calendar), `find_tmps`, `compare_versions`, `get_facts` and
   `search_chunks` (another query, or another TMP). The results join the context (`TOOL RESULTS`), so the check below
   accepts the numbers they computed. Without a model the code-built safe answer is used.
   Worked figures that people ask for most (the speed-limit length from its RPs, the works extent on each road, the works
   duration) are also computed in code when the chunks are built (`[chunks.derived]`), so those need no tool.
   With several TMPs readable and none chosen, the model gets the list and the tools and either answers across them
   or asks which TMP is meant.
5. **Check** (`check.py`): numbers, times, dates, RPs and phones must be in the context; cited pages in the context;
   generic or struck text labelled. Unconfirmed values and conflicting versions are written in the chunk text and the
   prompt tells the model to say so; the check does not enforce it.
   A number nothing in the context gives sends the question back to Q3 once, told to use `calculate` or `time_check`;
   then one Q4 repair (gpt-6-sol), then the safe answer.

```bash
uv run tmp-pipeline-ask --out out --tmp "KTS 26-1581" --model live -v "Is the speed limit in force at 7am on a Saturday?"
uv run tmp-pipeline-qa tests/data/gold/sample.questions.json --out out --model live
uv run tmp-pipeline-ui --out out                       # pick a TMP in the Ask page
```

`-v` shows the chunks used, the context size, model calls, timing and the check. Backends: `LocalStore` and
`LocalSearch` by default; `ingestion/embedding/db/reader.py: PgFactsStore` reads the same facts from PostgreSQL and
`ingestion/embedding/search/query.py: AzureSearch` and `.../cosmos/store.py: CosmosSearch` run the candidate step in
Azure AI Search or Cosmos DB (all optional).

Question test set (`tests/data/gold/sample.questions.json`, draft, 41 questions): offline the deterministic measures
(context holds the needed strings and pages, no number missing from the context) must be 100% and are. Questions
whose wording is not in the chunk ("school day hours" for the shift times) have no offline context check: keyword
search alone misses them and the vector backend finds them. Answer measures need `--model live` or `replay`.

## Azure resources and tracing

`deploy/azure/provision.sh` sets up resource group **AI-CoE-rg** (Simform-Dev, Australia East): a Foundry (AIServices)
resource `ai-coe-tmp-aue` with project `tmp-pipeline` and the model deployments in `deploy/azure/models.tsv`. Steps:
`check` (read-only, lists the region's model catalogue), `create`, `deploy`, `env` (writes `~/.config/tmp-pipeline/.env.azure`, outside the repo, mode 600,
git-ignored, keeps any `LANGFUSE_*` lines). All steps are idempotent. On this network Python needs
`SSL_CERT_FILE=~/.azure-docker-ca/combined-ca.crt` (TLS inspection).

Tracing goes to **Langfuse Cloud** over OpenTelemetry (OTLP/HTTP, `<LANGFUSE_HOST>/api/public/otel`): `uv sync --extra
tracing`, put `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` in `~/.config/tmp-pipeline/.env.azure`, then
`set -a; . ~/.config/tmp-pipeline/.env.azure; set +a`. Each question is a trace `tmp.ask` (session per conversation, tags = answer kind and
question types, metadata = answer method, TMP, role) with `tool <name>` children (search is a retriever) and
`model <prompt>` spans whose live calls are generations (`<prompt> <model>`, e.g. `Q3 gpt-6-sol`: model, input messages, output, token usage and
cost). Cost comes from `src/tmp_pipeline/data/model_prices.toml` (USD per 1M tokens, Azure retail prices; another file
via `TMP_MODEL_PRICES`). Langfuse sums it per trace, and `tmp.ask` also carries the question's total
(`tmp.cost_usd`, tokens, model calls); `tmp-pipeline-qa` prints the run's total and per-question cost.
Extraction stages are `stage <name>` spans. Prompt, context and answer text is sent only with `TMP_TRACE_CONTENT=1`
(set in the env file by choice: it includes names and phone numbers from the TMPs); keys never.

### Image descriptions (live run)

`tmp-pipeline tests/data/sample.pdf --out out_live --model live --model-fixtures out_live/_fixtures` reads the images
with gpt-6-luna (X7, X8, X9, X10; second reads stay on gpt-5.5; the plan names gpt-6-sol, changed by decision).
Where it lands: `out_live/<doc>/records.json` (every reading with status, second read and model), `facts.json`, and
`chunks.jsonl`: `figure_caption` chunks (the forms photo once, plus each picture beside a diagram, tied to its TMD),
and the TMD `sheet_overview` chunks (layout, approaches, pedestrians, symbol captions; a claim that failed a check is
marked "(unconfirmed)"). Every model answer is recorded in `out_live/_fixtures` (replay: `--model replay`, free) and
cached in `out_live/_model_cache`, so a re-run only pays for calls that changed. Sample cost: about $0.34 (260 calls).
Cosmos is only updated by `tmp-pipeline-cosmos upload out_live/<doc>`.

### Image questions (any question about a diagram or picture)

Every informative image is read completely at extraction time and stored as text, so image questions are answered
from chunks, with no image at answer time.
- Code facts, `diagrams/describe.py`, no model: every item with a plain label and a 3x3 position, the plan's items
  from left to right, every distance with the items at the two ends of its dimension line (from the drawing's
  geometry), and all text as printed (broken-font lines flagged). Settings are in `[diagrams.describe]`.
- Model read X13, gpt-6-luna, `handlers/image_full.py`: the whole sheet, then tiles, and each picture. It gives the
  description, type, purpose, appearance and road side per item, what each distance measures, relations, pedestrian
  and vehicle routes, readings of broken-font text, what is unreadable, and what is inferred. `check_x13` checks
  that every ID exists and every number is on the sheet, and that a broken-font reading keeps the printed letters.
  Failures are kept and marked "(unconfirmed)".
- Chunks per sheet: `sheet_text`, `sheet_layout`, `sheet_distances`, `sheet_details` (title block, legend) and
  `sheet_image` (description), next to the shorter `sheet_overview`, the sign schedules and notes. Figure chunks gain
  the full picture read.
- Retrieval: the diagram rule covers image words. A named sheet sends its whole chunk set only for broad questions
  (`[retrieval] sheet_whole`: describe, list all, extract, JSON...). Otherwise it sends the sheet overview, every
  chunk of the types the question's words point to (`[retrieval.sheet_topics]`), and the best 3 other chunks of that
  sheet (`k.diagram_sheet`, search filtered with `only_tmd`). Expansion adds only the neighbouring parts of a split
  chunk. Q3 rule 12 answers from the image facts. On 2026-10-15 the 70 image questions averaged about 4,800 context
  tokens, down from about 7,700. Live: 67/70 correct, p95 9.3 s (was 65/70, 14 s). The context budget is 10000 tokens.
- Test set `tests/data/gold/sample.image_questions.json` (draft): 35 questions keyed to TMD-02, plus 35 generated for
  every sheet from the code facts. Live, 2026-10-14: offline gates 70/70 and answers 61/70 (user's 35: 34/35); after
  the conflict-check fix, 7 of the 9 misses pass. About $0.027 per question. p95 is about 40 s with repairs (large
  contexts; streaming, Phase 8).

### Answer model

Answers (Q3) are written by gpt-6-luna without thinking (`prompts.REASONING`); an answer that fails the code check
is repaired (Q4) by gpt-6-sol. Measured 2026-10-14 through Cosmos:
- Text set: 44/46, against 43/46 with gpt-6-sol. About $0.002 per question including repairs; p95 6.7 s.
- Image set: 65/70, against about 68/70 with gpt-6-sol. About $0.004 per question; p95 14 s.

### Speed

Measured 2026-10-13 from this machine to Australia East, through the Zscaler proxy (gpt-6-sol, Cosmos backend):

| | before | now |
|---|---|---|
| a question in a running session | 6-16 s | 2-4.5 s (search about 1 s, Q3 1.5-3.5 s) |
| one-shot command (start-up included) | 10-16 s | about 9 s: 1 s opening Cosmos, about 3 s loading chunks the first time, about 1 s sending traces at exit |

What changed: HTTPS connections to Azure OpenAI are kept open and shared (`transport.KeepAliveTransport`; a new TLS
handshake through the proxy costs about 1.5 s per call), the first one is opened in the background at start-up;
`reasoning_effort` defaults to `low` (`AZURE_OPENAI_REASONING_EFFORT`: none | low | medium | high, empty for the
model's default: medium spent about 500 hidden reasoning tokens and 3-4 s more on Q3 with the same answers); the
Cosmos keyword query runs while the question is embedded; chunks are read back without their embedding text.
For demos ask in one session (`tmp-pipeline-cosmos ask --out out --tmp "KTS 26-1581" --model live`, no question:
type questions at the `>` prompt) so start-up is paid once. Streaming the answer (Phase 8) brings first words under
the 3 s target.

## Where chunks and embeddings live: Cosmos DB (free tier)

Instead of Azure AI Search (Free tier already used in the subscription; Basic ~US$97/month), chunks and their
text-embedding-3-large vectors are stored in **Azure Cosmos DB for NoSQL, free tier** (`ai-coe-tmp-cosmos`, Australia
East; database `tmp`, container `chunks`, partition `/doc_id`, 1000 RU/s = the free allowance, DiskANN vector index on
`/vector` and full-text index on `/text`). Nothing is billed while usage stays within 1000 RU/s and 25 GB; add no
other throughput to the account. The sample takes 2.4 MB.

```
uv sync --extra cosmos --extra tracing
set -a; . ~/.config/tmp-pipeline/.env.azure; set +a   # AZURE_COSMOS_* written by deploy/azure/provision.sh env
uv run tmp-pipeline-cosmos upload out/sha256-...          # embeds only new/changed chunks; deletes stale ones
uv run tmp-pipeline-cosmos ask --out out --tmp "KTS 26-1581" -v "What happens if there's a spill?"
uv run tmp-pipeline-cosmos qa tests/data/gold/sample.questions.json --out out
```

Search runs a BM25 query (`FullTextContainsAny` + `ORDER BY RANK FullTextScore`; without the filter Cosmos scores every
chunk alike) and a vector query (`VectorDistance`), and fuses the two rankings with RRF in code; ranking rules and
expansion are shared with the local backend. Facts are still read from `out/<doc>/facts.json` (PostgreSQL per plan
3.4 when hosted). The `cosmos` package is optional; core never imports it.

The synthetic set (`uv run tmp-pipeline-synthetic out/sha256-... out_synth/`: V1, a V2 and another TMP, made by text
replacement in a real run folder; each copy has a `SYNTHETIC` file) tests retrieval across several TMPs, not
extraction; keep it in `out_synth/`, never in `out/`.

## Known gaps (Phase 6)

- The plan 7.2 answer targets (answer correct >= 95%, page cited >= 95%, first words p95 < 3 s) are measured live only
  with `tmp-pipeline-qa --model live` and credentials; the offline gate covers the context and the number check.
- The question set is a draft by the extractor's author; it needs a second keying, the client's 20-30 real questions
  and 2-3 more TMPs (ask-back on real data needs two TMPs on one road).
- Q3 is not streamed; streaming belongs to the chat API (phase 8).
- Contacts are not access-controlled separately (access-group naming is open, plan 8.2); row-level security policies
  are a phase 8 deployment step (the Postgres reader filters by access groups in every query).
- Standards checks ("is a 30 m taper enough?") get the out-of-scope response with what the TMP says.

## Known gaps (Phase 5)

- No live embedding, X12 or AI Search call has run (fakes, replay fixtures and a fake transport only). A push needs
  the Azure endpoints and keys; a schema change that builds `tmp-chunks-v2` and switches over is phase 8.
- Without a model, sheet overviews have no spatial summary (X8), pictograms no captions (X10) and figures no
  description (X7): those chunks state this in words and are filled when the models run.
- Procedure splitting relies on the upper-case operation headings in `[chunks.procedure_operations]`; a new layout's
  headings are a template change.
- A few narrative chunks are under the 60-token minimum (a short section with no sibling to merge with).
- Boilerplate is detected only as identical chunk text on three or more pages.
- Access groups default to none; group naming is an open decision (plan 8.2).

## Known gaps (Phase 4)

- X11 has run only against fakes; no live model call. Without a model a title that matches no sheet stays unmatched.
- Rule 4 knows only the phrasings in `[facts.flow_statements]`; a new phrasing is a template change, and an unmatched
  statement is not raised.
- A drawn (image) signature is not detected: an empty signature cell reads as unsigned and goes on the review list.
- Mention titles are cut at punctuation, "and" or four words, so "TMD 02 – Lane Diversion The footpath" keeps two
  extra words (matching is unaffected).
- The facts gold (`facts` in `sample.gold.json`) is the extractor author's draft, like the rest of the gold set.
- No reviewer editing in the UI yet: `tmp-pipeline-ui` shows the review queue, but corrections are still a JSON file; rebuilding affected chunks after a correction is Phase 5
  (`corrections.jsonl` records the affected paths). Blob containers and row-level security policies are phases 8 and 6.

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

## Known gaps (Phase 2)

- `ElementStore.metadata` is still an untyped dict (keys are read by string); `_repeating_images` is quadratic.
- The sample PDF is a client document: `tests/data/sample.pdf` is a symlink and must not be committed.

## Tests

Tests live in `tests/ingestion/`, `tests/retrieval/` and (shared code) `tests/`; pytest puts the three on its path.
`tests/helpers.py` has shared builders (`grid`, `header_footer`, `prepare`, and `sheet_page`, `speed_disc`, `plate`, `label`
for synthetic diagram sheets); fixtures are in `conftest.py`.
`tests/ingestion/test_golden_sample.py` pins zones, element counts, routes and flags of the sample run: a refactor must
not change them, and a deliberate change regenerates `tests/data/golden_sample_snapshot.json`.
