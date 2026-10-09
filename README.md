# TMP pipeline

Extraction, chunking and retrieval for New Zealand Temporary Traffic Management Plans. The full design is in
`TMP Combined Plan.html`; the build is split into 8 phases (reading, handlers, diagrams and images, facts,
chunking and index, retrieval L0+1, planner, operations). This repo is at **Phase 1: Reading**.

```
uv sync
uv run pytest -q

# PDF -> element store, zones, router log (out/<doc_id>/)
uv run python -m tmp_pipeline.run "TMP V1 (...).pdf" --out out/ [--force] [--skip-render] [--template file.toml]

# Document Intelligence track (separate; default only evaluates when DI is needed)
uv run python -m tmp_pipeline.di.run "TMP V1 (...).pdf" --doc-dir out/sha256-... [--cache-only | --live]
```

`--live` needs `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` in the environment (the key is never logged).
Layout cues (header band, rating colours, thresholds) live in `src/tmp_pipeline/templates/default.toml`.
