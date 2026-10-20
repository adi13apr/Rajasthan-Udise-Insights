# Work plan

Two tracks in one project. Data engineering (P0-P10) first, then the Sattva
analysis (A1-A6), which reads from the gold marts built in P5.

After **each phase**: run the tests and the pipeline, update docs if affected,
explain the phase to Aditya in simple words, then `git commit` and `git push`.

Target architecture:

```
data/raw (zips, untouched)
  -> data/bronze   Parquet copies of each source file, as-is + load metadata, partitioned by year
  -> data/silver   cleaned, typed, standardised, rule-checked (failures -> data/quarantine)
  -> data/gold     DuckDB star schema + data marts (also exported as Parquet)
  -> reports/      profiling, QA report, validation, benchmark, run manifest
```

Sources (the JD asks for multiple sources, structured and semi-structured):
1. UDISE+ school-level CSVs inside zips (structured).
2. Reference data as **JSON**: management codes, district aliases and canonical
   district list, official published totals (semi-structured). Move these out of
   `config.py` into `reference/*.json`, loaded by the pipeline.
3. Run manifest written as JSON (row counts, rule failures, timings per stage).

---

## P0. Baseline (5 min)
- Confirm the current pipeline runs on the full data and commit.
- Add dependencies: `duckdb`, `pyarrow`, `pytest` (later `statsmodels`, `matplotlib`).

## P1. Bronze layer (ELT: load first, transform later)
- New `pipeline/bronze.py`, reusing `ingest.discover()` and `read_zip_csv()`.
- For each raw zip, write a Parquet file **as-is** (all original columns, no
  cleaning) plus metadata columns: `_source_file`, `_district_file`, `_year`,
  `_ingested_at`, `_run_id`.
- Layout: `data/bronze/<dataset>/year=<YYYY-YY>/<source_file_stem>.parquet`
  (Hive-style partitions, so DuckDB can prune by year).
- **Incremental:** keep a checksum per source zip in the manifest and skip files
  that haven't changed since the last run.
- Bronze keeps duplicates (Kota): it is a faithful copy of the sources.

## P2. Profiling
- `pipeline/profiling.py`: per column, for every bronze and silver table: dtype,
  null %, distinct count, min/max (numeric), top values, and **invalid codes**
  (values outside the allowed set from the reference JSON / schema, e.g. yes/no not
  in {1,2,9}). Also duplicate count on the table's natural key.
- Output: `reports/profiling/<layer>__<table>.csv` plus a short summary
  `reports/profiling/README.md` that lists only the columns with problems.

## P3. Silver layer + explicit quality rules + quarantine
- Move the cleaning logic that already exists in `build.py` (district aliasing,
  yes/no mapping, classroom rebuild, management grouping) into silver transforms.
  Reuse functions; don't rewrite them.
- `pipeline/rules.py`: rules declared as data (name, table, description, severity,
  check). Two severities:
  - **error** -> row moves to `data/quarantine/<table>.parquet` with `_rule`,
    `_reason`, `_run_id`. Never silently dropped.
  - **warning** -> row stays in silver with a `flag_<rule>` column (e.g. functional
    toilets > total toilets, PTR > 60, students but no teachers).
- Minimum rules: required fields present (pseudocode, year, district); unique
  `(year, pseudocode)` (extra copies, e.g. Kota, quarantined as
  `duplicate_school_year`); counts non-negative; district valid after aliasing;
  management code known; yes/no codes in {1,2,9}; rural_urban in {1,2};
  enrolment rows must belong to a school in the profile (orphans quarantined).
- Silver tables (Parquet, partitioned by year): `schools`, `facilities`,
  `teachers`, `enrolment` (wide, item_group 1 for totals; keep other item_groups
  in a separate table if cheap).
- Keep `reports/qa_report.md` (now read from silver) and keep
  `DATA_QUALITY_LOG.md` updated.

## P4. Gold star schema in DuckDB
- `data/gold/udise.duckdb`, all SQL in `sql/` (numbered files, run in order).
- Dimensions:
  - `dim_year` (year_key, academic_year, start_year).
  - `dim_district` (district_key, district_name, aliases); 41 rows.
  - `dim_management` (management_key, code, group, is_government); from JSON.
  - `dim_school` **SCD Type 2**: one row per version of a school's attributes
    (district, block, management, rural/urban, school category, type), with
    `valid_from_year`, `valid_to_year`, `is_current`. This is how boundary changes
    are handled: the 180 Alwar -> Kotputli-Behror schools get two versions, and each
    year's facts point to the version valid that year.
  - Small lookups as needed: `dim_grade` (PP, 1-12, stage), `dim_gender` (B/G/T),
    `dim_social_category` (General/SC/ST/OBC).
- Facts (state the grain in comments):
  - `fact_enrolment`: school x year x grade x gender x social category (unpivot
    item_group 1). Measure: students. Zero rows may be omitted; document it.
  - `fact_teachers`: school x year (the source is already aggregated per school).
    Measures: total, female, regular, contract, trained_cwsn, etc.
  - `fact_school_facilities`: school x year snapshot of facility flags and
    classroom counts (needed for the infrastructure index).
- **Surrogate keys**: integers, deterministic (row_number over the natural key),
  so reruns give the same keys.
- **Referential integrity tests**: every fact foreign key must match a dimension
  row (anti-join count = 0). Put the checks in `sql/tests/` and run them from pytest.
- Also export gold tables to Parquet under `data/gold/parquet/`.

## P5. Data marts (gold, SQL only, built from the star schema)
- `mart_district_year`: schools, enrolment, teachers, PTR, % single-teacher schools,
  infrastructure index (same 8 facilities as now, playground excluded), each
  facility %, girls' share (primary, secondary+), girls' retention gap,
  boys'/girls' progression ratios, SC/ST shares. Zero-enrolment schools excluded,
  as now.
- `mart_district_year_management`: same by government / private / other.
- **Regression test:** the mart must reproduce the current pandas
  `data/processed/district_year.csv` (within rounding). Once it does, the pandas
  summary can be retired, with a note in the docs.
- Export marts as CSV to `data/gold/exports/` for Tableau and Excel (district-level
  aggregates are fine to publish).

## P6. Validation against official figures
- Official totals in `reference/official_totals.json`.
- SQL in `sql/validation/` computes the same indicators from gold. Output
  `reports/validation.md` and `.csv` (official, ours, difference, % difference).
- Record the results in `DATA_QUALITY_LOG.md`.

## P7. Performance benchmark (real timings)
- `scripts/benchmark.py`: the same district-level query (e.g. Kota enrolment by
  grade and gender, 2025-26) run three ways in DuckDB:
  1. raw CSVs (extracted to `data/tmp/`, which is gitignored), read with
     `read_csv_auto`;
  2. bronze Parquet with a year partition filter (partition pruning);
  3. the gold star schema.
- Run each several times (cold + warm), report the median, and record machine info
  (CPU, RAM, Python and DuckDB versions). Output `reports/benchmark.md`.
- Also note any optimisation applied (column pruning, dtypes, partition pruning).

## P8. Tests, CI and one-command run
- `scripts/make_sample_data.py`: generates **synthetic** zips with the same schema
  (a few fake districts and schools, including deliberately bad rows: a duplicate,
  a negative count, an unknown district, an orphan enrolment row, a 9 code) into
  `tests/fixtures/raw/`. No real UDISE+ data in the repo.
- pytest (`tests/`): unit tests (district normalisation, yes/no mapping, classroom
  rebuild), rule tests (each bad row lands in quarantine with the right reason),
  transform tests (totals preserved bronze -> silver -> gold), join tests
  (referential integrity), and an end-to-end run on the sample.
- `run_pipeline.py --sample` runs everything on the fixtures.
- `.github/workflows/ci.yml`: on every push, install, run pytest, run the pipeline
  on the sample.

## P9. Documentation
- README: what and why, Mermaid architecture diagram (raw -> bronze -> silver ->
  gold -> marts/reports), how to run, layout, data-sharing terms and source
  acknowledgement.
- `docs/data_dictionary.md`: every gold table and column (generate the column
  list from DuckDB and add descriptions).
- `docs/quality_rules.md`: generated from `rules.py`.
- `docs/design_decisions.md`: why ELT, why a star schema, why Parquet partitioned
  by year, why DuckDB, how district boundary changes are handled (SCD2), why
  synthetic data in CI, how management codes were validated.

## P10. Optional, only if time is left
- Store silver and gold as Delta tables (`deltalake` library) with the same layout.

## End of data-engineering track: report REAL numbers
From `reports/run_manifest.json` and the test run: rows read per source, rows per
layer, rows quarantined by rule, number of gold tables, tests passed/failed,
benchmark timings, validation differences.

---

## Sattva analysis track (after P5 exists)

Reads from the gold marts and silver; lives in `notebooks/analysis.ipynb` (plus any
helper module). Findings must be written as correlations, not causes.

- **A1. District rankings:** infrastructure index, % single-teacher schools, PTR;
  bottom-10 districts; maps or bar charts.
- **A2. Girls' secondary story:** retention gap and boys'/girls' progression
  ratios, by district and by government vs private. Distinguish three patterns
  seen so far: girls dropping out (e.g. Jaisalmer, Barmer), boys moving in to
  coaching hubs (Sikar, Kota), boys leaving in the tribal south (Pratapgarh,
  Salumbar, Banswara, Dungarpur).
- **A3. School-level regression:** girls' share at secondary vs girls' toilets,
  teachers, infrastructure, controlling for district (fixed effects), rural/urban
  and management (statsmodels). Report coefficients with confidence intervals and
  clear caveats.
- **A4. Year-on-year changes using matched schools only:** e.g. the rise in
  single-teacher schools in the western desert (Jaisalmer, Barmer, Phalodi,
  Balotra). Handle the Alwar/Kotputli-Behror boundary change.
- **A5. Outputs:** charts for the brief, CSV for the Tableau Public dashboard (built
  by Aditya), an Excel workbook with pivot-table summaries (`excel/`).
- **A6. Two-page policy brief** (three findings, each with a chart and a
  "so what" for funders and NGOs) and resume bullets for both applications.
