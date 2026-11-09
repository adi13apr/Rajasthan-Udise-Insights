# rag/: questions answered from UDISE+ documents and the gold tables

The RAG layer will let a user ask a question and get an answer from two kinds of evidence:
official UDISE+ documents (schemas, reports, guidelines) and SQL over the gold tables. It is
built in five phases. **Phase 1 ingests documents**: it reads them, cuts them into chunks,
turns every chunk into a vector with a local model, and stores the vectors in a ChromaDB index
on disk. **Phase 3** (below) is a safe, read-only SQL tool over the gold tables and the schema
card the agent will read. There are no LLM calls and no paid APIs yet.

```
docs_corpus/manifest.csv ──> extract.py ──> chunking.py ──> embedder.py ──> store.py (ChromaDB)
  (which documents)          (pages, headings,  (text + table   (bge-small,      (+ state file,
                              tables)            chunks, ids)    local)           report)
```

## Running it

```powershell
# once: a separate environment, so the pipeline's own packages are not touched
python -m venv D:\dev\.venv-rag
D:\dev\.venv-rag\Scripts\python.exe -m pip install -r requirements-rag.txt

D:\dev\.venv-rag\Scripts\python.exe -m rag.ingest                 # incremental
D:\dev\.venv-rag\Scripts\python.exe -m rag.ingest --full-refresh  # rebuild the index
```

It prints, per file, the status (new, changed, unchanged, reindexed), pages, sections,
tables and chunks; the totals; the files removed from the manifest; and the pages that had no
text. The same numbers go to `reports/rag_ingest.json`. The first run downloads the embedding
model (about 130 MB) into the Hugging Face cache; later runs use the cached copy.

| Setting | Default | Change with |
|---|---|---|
| Corpus folder (documents not in the repo) | `E:\udise-docs` | `UDISE_DOCS_DIR` or `--docs-dir` |
| Index folder | `data/rag/chroma/` (gitignored) | `UDISE_RAG_DIR` or `--index-dir` |
| Manifest | `docs_corpus/manifest.csv` | `--manifest` |

Several worktrees (or later phases) can share one index by pointing `UDISE_RAG_DIR` at the
same folder. Do not run two ingestions on the same index at the same time.

## The manifest

`docs_corpus/manifest.csv` is the only part of the corpus kept in the repo. Columns:
`file, title, publisher, year, doc_type, source_url`.

- `file` is either a path inside the repo (`docs/DSP_Schema_2024-25.pdf`) or a file name
  inside the corpus folder (`UDISE+_Report_2024-25.pdf`). To add an official report, put the
  PDF in `E:\udise-docs\` and add a row: no code change. A name that exists in both places is
  an error, so it is always clear which file was read.
- `title` is taken from the document itself, not from the file name.
- `year` is `2026` or `2024-25`; `doc_type` is a lower-case word (`schema`, `project_doc`,
  `official_report`, ...); `source_url` may be blank (the project's own documents).
- PDF (`.pdf`) and Markdown (`.md`) files are supported.

The manifest is checked before anything is loaded, and all problems are listed at once
(missing file, empty title, bad year, file listed twice...).

## Design choices, in plain words

### Chunk size: about 450 tokens, never more than 512
A chunk is the piece of text the search returns. If chunks are too small, an answer is spread
over many pieces and each one lacks context. If they are too big, one chunk covers several
topics and its vector becomes a blurry average of all of them, so searches match it poorly.
A few hundred tokens (roughly one or two paragraphs) is the usual middle ground.

The hard limit comes from the model: **bge-small-en-v1.5 reads at most 512 tokens and silently
ignores the rest**. A 600-token chunk would be stored in full but searched only by its first
512 tokens, so the end of it could never be found. So the target is 450 and no chunk passes
512. Tokens are counted with the model's own tokenizer, special tokens included, so "512"
means exactly what the model reads. The counter is a parameter: tests pass a word-count
estimate instead, so they need no model.

### Overlap: about 15% (≈ 68 tokens)
When a chunk ends, the next one starts with the last few sentences of it. Without overlap, a
fact that sits on a boundary ("... 6,117 schools have one teacher. Most of them are in ...")
is cut in two and neither half answers the question. Repeating ~15% makes sure every
sentence is complete in at least one chunk, at the cost of storing ~15% more text. Overlap is
whole sentences when they fit, otherwise the last words of the previous chunk.

### Heading-aware
A chunk never crosses a heading. Text under "Silver layer and quarantine" and text under
"Validation" are about different things; mixing them in one chunk blurs both. Every chunk
also starts with its section path (e.g. `Data Quality Log > Issues in the data itself`), so
a chunk read on its own still says what it is about, and the search can match on it.

In PDFs, headings are found from the typography: a short line that is all bold, or clearly
bigger than the body text, or a bold title row inside a table. Headings that come straight
after each other stack up (`Data Sharing Portal - Schema > School Profile 1`). In Markdown
the `#` headings are used, and **the section heading takes the place of a page number**
(Markdown has no pages): `page` is 0 and `location` is the section.

### Tables are separate chunks
A table only makes sense with its header. If tables were treated like text, a chunk boundary
could fall in the middle of a row (half the numbers in one chunk, half in the next), or rows
would end up in a chunk without the header that says what the numbers mean ("| 6117 | 215 |"
answers nothing). So:

- every table becomes its own chunk(s), written as a Markdown pipe table under its heading;
- table cells are cut out of the page text, so they never also appear as loose text;
- a long table is split **between rows only**, and every part starts with the header row
  again, so each part can be read alone;
- a table that runs over a page break (no header at the top of the next page) is joined to
  the table above it; a header repeated on each page is not repeated as a data row;
- a row with an empty first cell is the wrapped rest of the row above and is joined to it
  (the 2025-26 schema PDF also repeats some column names on a separate line: that text is
  not added twice);
- a single row is never cut. If one row alone were longer than 512 tokens it would be kept
  whole and marked `oversized` (the report counts them).

### Ids that do not change
Each chunk id is the first 16 hex characters of `sha256(file | page | offset)`: the
document's manifest key, the page (Markdown: the section), and the character offset where the
chunk starts (tables: table number and first row). Running the ingestion twice on the same
file gives the same ids, and an edit late in a document does not change the ids of chunks
before it. Later phases can cite a chunk by id.

### Metadata on every chunk
`file, title, publisher, year, doc_type, source_url, kind, file_sha256, page, page_end,
section, location ("p. 3", "pp. 3-4" or the section), chunk_type (text/table), chunk_index,
n_tokens, oversized`, plus `table_index, row_start, row_end` for tables. That is enough to
cite a source ("Data Sharing Portal - Schema, 2024-25, p. 3") and to filter (only schemas,
only 2025-26).

### Re-runs are safe (idempotent)
A file is skipped when nothing that affects its chunks has changed: its bytes (SHA-256), its
manifest row, the chunker version and sizes, the token counter and the embedder. Otherwise
all of its old chunks are deleted and the new ones added, so no stale chunk is left behind.
A file removed from the manifest has its chunks deleted. The state (one hash per file) is
kept next to the index (`rag_ingest_state.json`), so it moves with the index; if the index
lost a file's chunks, the file is rebuilt even when the state says it is done.

### Embedder behind an interface
`Embedder` has `name`, `dim`, `embed_documents(texts)` and `embed_query(text)`. The default is
`BAAI/bge-small-en-v1.5` (384 dimensions, ~130 MB, runs on a laptop CPU, good English
retrieval for its size), with vectors normalised to length 1 so cosine similarity is a dot
product. Documents and queries are separate methods because bge is trained that way: **only
queries** get the instruction `Represent this sentence for searching relevant passages: `.
Another model can be swapped in by writing a class with the same four members; the index
records which embedder built it and refuses to mix two models (use `--full-refresh`).

### ChromaDB
A persistent local index in a folder: no server, no account. Chroma is only storage and
search here. The collection has no embedding function of its own (so it never downloads its
default model), telemetry is off, and the distance is cosine.

### Pages with no text
Some PDF pages are scans or blank. This phase does no OCR: such pages produce no chunk and
are listed in the CLI output and in `rag_ingest.json` (`empty_pages`), so they are visible
instead of silently missing.

## Tests

`tests/test_rag_chunking.py`, `tests/test_rag_pdf.py` and `tests/test_rag_ingest.py` use a
tiny fake embedder (hash -> 16 numbers), the word-count token estimate and synthetic
documents (Markdown strings, PDFs drawn with reportlab), so CI never downloads a model or
reads a real document. They check overlap, the size limits, headings, tables split only
between rows with the header repeated, tables over a page break, stable ids, re-runs
(unchanged skipped, changed replaced, removed deleted), metadata on every chunk, empty pages,
the manifest checks and that only queries get the bge prefix. CI job `rag` installs only
`chromadb` and `reportlab` from `requirements-rag.txt`.

## Limits (known, for later phases)

- Heading detection in PDFs is a heuristic (bold or bigger text). A PDF without such
  typography gives fewer sections; its chunks are still correct, just less labelled.
- Tables are found from ruling lines (pdfplumber's default). A table drawn without lines is
  read as text.
- No OCR: scanned pages are reported, not read.
- Oversized rows: in both schema PDFs the `Item_group` row (Enrolment_1) lists every
  item_group/item_id code in one cell, 522 tokens (2024-25) and 525 (2025-26) with the
  header. It is kept whole and marked `oversized`: the full text is stored and returned, but
  the embedding only sees the first 512 tokens (the last few codes: repeater, EWS).

## Phase 3: a safe SQL tool over the gold tables

The second kind of evidence is SQL over the gold star schema. `rag/sql_tool.py` is the only way
later phases will touch the database, so it has to be safe even when the SQL is written by an
LLM. Still no LLM in this phase.

```powershell
D:\dev\.venv-rag\Scripts\python.exe -m pip install -r requirements-rag.txt   # adds sqlglot
D:\dev\.venv-rag\Scripts\python.exe -m rag.schema_card                       # rewrite rag/schema_card.md
D:\dev\.venv-rag\Scripts\python.exe -m pytest tests/test_rag_sql.py -v -rs
```

```python
from rag.sql_tool import SqlTool
with SqlTool() as tool:                  # UDISE_GOLD_DB, else the main checkout's gold database
    r = tool.run("select district, ptr from mart_district_year where year = '2025-26' order by ptr desc")
    r.columns, r.rows, r.row_count, r.sql_executed, r.truncated
```

| Setting | Default | Change with |
|---|---|---|
| Gold database | this checkout's `data/gold/udise.duckdb`, else the main checkout's (worktrees have none: it is gitignored) | `UDISE_GOLD_DB` or `SqlTool(path)` |
| Row cap | 200 | `SqlTool(max_rows=...)` |
| Timeout | 10 s | `SqlTool(timeout_s=...)` |

### Two layers of protection
Each layer would stop most attacks on its own; together a gap in one is caught by the other.

1. **The SQL is checked before it runs** (`check_sql`, parsed by sqlglot in the DuckDB dialect).
   Exactly one statement. The top level must be a query (SELECT, UNION / INTERSECT / EXCEPT;
   CTEs and subqueries are fine). No DDL, DML or command anywhere in the tree (INSERT, DELETE
   inside a CTE, ATTACH, COPY, PRAGMA, SET, INSTALL, LOAD, CALL, EXPLAIN, ...). Every table is
   a bare name from the allowlist (the 13 gold tables) or a CTE of the same query: no
   `main.x`, no `FROM 'file.csv'`, no table functions (`read_csv(...)`, `range(...)`). No
   file-reading or system function anywhere (`read_*`, `glob`, `query`, `duckdb_*`,
   `current_setting`, ...). Why parse rather than search for words: a word search is fooled by
   comments, quoting and spacing, and rejects harmless queries that mention "delete" in a string.
2. **The database refuses anything but reading.** It is opened `read_only=True` with
   `enable_external_access = false`: DuckDB then cannot touch the file system at all (no
   reading files, no COPY TO, no ATTACH, no extensions), and it does not allow switching that
   back on while the database is open. `lock_configuration = true` then stops any `SET`. The
   tests send dangerous statements straight to the connection, past layer 1, and check that
   DuckDB refuses each one.

**What runs is what was checked.** The tool runs the SQL that sqlglot writes back out of the
checked tree, not the original text, and returns it as `sql_executed`. So nothing can hide in
a comment or an odd quote, and the caller sees exactly what ran, including the LIMIT.

**LIMIT 200.** A query without a LIMIT gets `LIMIT 200`. A larger LIMIT (or a percentage,
an expression, `FETCH FIRST`) is lowered to 200, and an OFFSET is kept. `truncated` says that
the cap was reached, so the caller knows more rows may exist and should aggregate instead.

**Timeout.** A timer calls DuckDB's `interrupt()` after 10 s. The query stops with
`SqlTimeout`, and the tool stays usable for the next query. Memory is capped at 2 GB and
threads at 4. If the SQL passed the checks but DuckDB still fails (wrong column, for example),
the tool raises `SqlExecutionError` with DuckDB's message, so an agent can correct the query.

**The school code is hidden.** The UDISE+ terms forbid re-identifying schools. `pseudocode`
is rejected by layer 1, and temporary views with the same names as `dim_school` and
`int_school_year` leave it out (`select * exclude (pseudocode)`), so even `select *` never
returns it. `school_key` (a row number given by the pipeline) is still there for joins.

### The schema card
`rag/schema_card.md` is what the agent will read before writing SQL. It is generated
(`python -m rag.schema_card`) from the gold database: names, types and row counts come from
DuckDB, column meanings are reused from `scripts/make_data_dictionary.py`, and code lists come
from the dimension tables. That way it cannot drift from the real schema, and a test fails if
the committed card is stale. It is compact on purpose (about 2,400 words): the 37 teacher count
columns are grouped, the facility flags are listed once, and the management mart refers to the
district mart. It covers how to query (joins through the SCD2 `school_key`, official vs
classes 1-12 enrolment, NULL is not "no", the playground reporting change), the codes
(including how management codes 94, 95 and 101 were resolved as government), and 5 example
question -> SQL pairs. The tests run every example.

### Tests (`tests/test_rag_sql.py`)
- **Rejected:** 64 unsafe statements, each must raise `SqlRejected`. They cover a second
  statement hidden after a comment, DELETE inside a CTE, every command above, file functions
  in FROM, in a subquery, in a CTE and as a scalar, file paths as tables, qualified names, and
  the school code. There are also 14 safe queries that must pass and 11 LIMIT rewrites.
- **On the synthetic sample gold** (built by the test, as in CI): results and `sql_executed`,
  the row cap, the hidden school code, the database's own lock-down, the timeout, DuckDB
  errors, the examples, and both validation queries (`sql/validation/01_official_indicators.sql`
  through `int_school_year`, `sql/powerbi/check_validation_measures.sql` straight from the
  facts), which must give exactly what `pipeline.validation` gives.
- **On the full gold database** (skipped when it is absent, e.g. in CI): both validation
  queries run through the tool reproduce the 8 official 2024-25 figures exactly, the first
  example gives the official schools / students / teachers, and the committed card is up to
  date.

### Limits (known)
- With the school code hidden, the two versions of a school that changed district cannot be
  linked (so "which schools moved?" cannot be answered row by row). The count of moves is in the
  card as a fact.
- A query can still return up to 200 school-level rows (without codes). The card tells the
  agent to aggregate; a later phase can add a check that answers only show aggregates.
- DuckDB allows only one configuration per database file per process: while a `SqlTool` is
  open, other code in the same process cannot open the same file read-only with different
  settings. Open the tool last, or close it first.
