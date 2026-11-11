# Handoff

Status as of 2026-08-26. This turns scanned sales-order info sheets into a
hybrid-search reference tool: an employee drafting a new order can describe a
custom instruction vaguely (not exact wording) and find similar past orders,
instead of manually keyword-searching SAP/Excel.

## What's here

- **Pipeline** (`scripts/pdf_to_md.py` -> `scripts/md_to_json.py` ->
  `scripts/ingest_info_sheets.py`, orchestrated by `scripts/run_pipeline.py` /
  `scripts/batch_*.py`): OCR -> LLM extraction -> Postgres + ChromaDB. All of
  `scripts/` is a Python package -- run its modules as `python -m scripts.X`,
  not `python scripts/X.py` (they import each other). See `CLAUDE.md` for full
  architecture (gitignored -- ask for it if you don't have a copy).
- **`data/<doc>/`**: every processed document's `.pdf`/`.md`/`.json` together in
  one folder, tracked via **Git LFS**. Every folder here has a real source PDF
  behind it -- a batch of json-only entries with no traceable source document
  was found and removed in a 2026-08-26 cleanup. Raw not-yet-processed PDFs
  still live in `OneDrive_*/` folders (not tracked -- proprietary).
- **`search/`**: the retrieval mechanism -- hybrid semantic (ChromaDB +
  `gte-large-en-v1.5`) + lexical (BM25) search, fused via Reciprocal Rank
  Fusion, grouped by source info sheet, with a relevance floor so irrelevant
  queries correctly return nothing. Entry point:
  `search/info_sheet_search.py:search_info_sheets()`.
- **`backend/`**: FastAPI wrapper around `search_info_sheets()`. See below for
  endpoints.
- **`frontend/`**: Next.js UI with three modes -- search by instruction, SO
  lookup, and draft-info-sheet search.
- **Fixed 2026-08-22**: a critical bug where the embedding model's rotary
  buffers were silently corrupted (no crash, just wrong embeddings) -- see
  `git log` on `scripts/ingest_info_sheets.py` if you hit weird search results
  after a `sentence-transformers`/model upgrade.
- **Fixed 2026-08-26**: `ingest_info_sheets.py` was looking up existing rows by
  the wrong column after a schema change (primary key had moved from
  `source_file` to `internal_so_no`, but the dedup lookup wasn't updated) --
  this meant re-ingesting an already-ingested order, or ingesting a newer
  amendment of one, would crash instead of updating. Also added: when a newer
  amendment supersedes an existing row, its predecessor's now-stale Chroma
  vectors are purged so superseded instructions stop surfacing in search.

## Getting in sync (Postgres + ChromaDB)

Postgres and ChromaDB are **not** shared directly (`chroma_data/` is
gitignored, nobody's pushing a Postgres dump). Instead, `data/` is the shared
source of truth (via LFS) and everyone regenerates their own local Postgres +
ChromaDB from it.

```bash
git lfs install                          # once per machine
git pull                                 # pulls data/ through LFS too
git lfs pull                             # ensures all LFS objects are downloaded
pip install -r requirements.txt          # covers the pipeline, search/, and backend/
cp .env.example .env                     # set POSTGRES_PASSWORD

# One-command sync & seed: validates data/, starts Postgres if down, wipes local
# DBs cleanly, and re-seeds fresh from data/:
python -m scripts.sync_and_seed
```

Or step-by-step manually:
```bash
docker compose up -d postgres
python -m scripts.sync_state_from_data   # updates local checkpoint DB from data/
python -m scripts.batch_ingest --dir data  # populates Postgres + ChromaDB from data/
```

## Starting the backend + frontend

```bash
python -m uvicorn backend.main:app --reload --port 8001
```

Or via Docker Compose, which starts Postgres + backend + frontend together:

```bash
docker compose up -d --build
```

Ports (defined once in `docker-compose.yml` -- if you change one there, update
`.env.example`/this file/README.md/`scripts/ingest_info_sheets.py`'s docstring
to match):

| Service    | Host port |
|------------|-----------|
| Postgres   | 5433      |
| Backend    | 8001      |
| Frontend   | 3000      |

Endpoints:

- `GET /health` -> `{"status": "ok"}`
- `GET /materials` -> `{"materials": ["LT-40 2000 LM - FANUC", ...],
  "material_ids": [{"id": "40004", "description": "LT-20 C CLASSIC - FANUC"}, ...]}`
  -- the distinct `material_description` and `material_id` values in Postgres,
  alphabetical. Feeds the two material filter dropdowns in the UI.
- `POST /search` -> body `{"query": "...", "n_sheets": 5, "pool": 30}`
  (`n_sheets` 1-50, `pool` 1-200, both optional) -> `{"results": [...]}`,
  each result a sheet with its header fields (customer, machine, dates, etc.),
  `total_instructions`, and the `matched` instructions with their individual
  `semantic_distance`/`lexical_score`/`fused_score` -- show these, don't hide
  them, so the user can judge confidence themselves.
- `GET /so/{customer_so_no}` -> the full stored info sheet for that SO number.
- `POST /search/document` -> body `{"instructions": ["...", "..."], "n_sheets": 5, "pool": 30}`
  -- runs a search per instruction line (e.g. from a draft info sheet) and
  aggregates hits by summed best-fused-score per past sheet.

  Both `POST` endpoints also accept four optional, independent filters that
  combine with AND: `material` (matched case-insensitively as a *substring* of
  `material_description`, so `"SLC-16"` and `"SLC-16 LM G - FANUC"` both work),
  `material_id` (matched *exactly*, trimmed and case-insensitive -- it is an
  identity code, so substring matching would let `"400"` sweep in most of the
  catalogue), `date_from`, and `date_to` (inclusive `YYYY-MM-DD` bounds on
  `required_delivery_date`; either may be omitted). `date_from` after
  `date_to` is a 422.

  A fifth flag, `exact_match` (default `false`), changes what *matches* rather
  than which sheets survive: it keeps only instructions that literally contain
  every content word of the query -- whole words, order-independent, stopwords
  ignored, sharing `lexical_search.py`'s tokeniser so `STP-80` stays one term
  and `gun` never matches `gunmetal`. It is tested against each hit's own text,
  not its BM25 `matched_terms`, so a hit found only by the semantic leg is
  still judged on what it says. Note it also *bypasses* the usual relevance
  floor (`_is_relevant`): literal containment is the relevance test in this
  mode, and leaving the floor on would make any one-word query return nothing,
  since a single term can never reach `MIN_LEXICAL_MATCHED_TERMS` (2).

  Filters are applied *before* the `n_sheets` cut, so a
  filtered search for 5 sheets returns the 5 best matching sheets rather than
  whatever survives of the unfiltered top 5 -- which is why
  `search_info_sheets()` now fetches headers for every candidate rather than
  only the final few.

  Caveat worth knowing: a date filter silently excludes any sheet whose
  `required_delivery_date` is NULL in Postgres, and that is far more common
  than the source PDFs suggest -- `ingest_info_sheets.parse_date()` only
  accepts ISO dates, so the ~57% of sheets whose PDF prints `30.04.2026`
  store NULL. Widening that parser (plus a re-ingest) is the fix; until then
  a date range searches roughly the 43% of the corpus that has a parsed date.

  Full response shapes: `backend/schemas/search.py`, `frontend/src/lib/search-api.ts`.

CORS defaults to `http://localhost:3000` (Next.js dev default); override via
`CORS_ORIGINS` in `.env` (comma-separated) if needed.

`/openapi.json` and `/docs` (Swagger UI) are auto-generated once the server's
running -- useful for exploring the contract without reading the code.

## Known limitations (don't be surprised by these)

- Only ~360 of ~1,800+ real documents are processed so far -- this is a
  validated mechanism, not a fully-populated corpus yet.
- Search quality has been validated against constructed queries, not real
  employee usage patterns yet.
- BM25's stopword handling has a couple of patched-but-not-fully-general edge
  cases (see `search/lexical_search.py`'s `STOPWORDS` comment) -- expect to
  occasionally find another one as real queries come in.
- Ollama-based extraction can hit a rare repetition bug in
  `scripts/md_to_json.py` on certain documents -- bounded by `num_predict` so
  it fails cleanly instead of hanging, but the failing document needs manual
  JSON reconstruction from its `.md` when it happens.
- Amendment history isn't independently queryable: Postgres keeps only the
  latest amendment per `internal_so_no` (older ones are overwritten on
  ingest, and their Chroma vectors purged). If full amendment history ever
  needs to be browsable, that's a schema change, not a quick patch.
