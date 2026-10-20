# Rajasthan School Education Insights (UDISE+)

District-level analysis of school infrastructure, teachers and enrolment in
Rajasthan, built from UDISE+ school-level microdata.

**Core question:** Which districts are falling behind on school infrastructure and
teachers, and is that linked to girls staying in secondary school?

> Status: pipeline built and tested on Kota (2024-25, 2025-26). All-district
> data, analysis, dashboard and brief in progress.

## Data
- **Source:** UDISE+ Data Sharing Portal, school-level CSVs (profile, facility,
  enrolment, teachers, safety) for 2024-25 and 2025-26.
- **Why these years:** UDISE+ moved to student-wise data collection from 2022-23,
  so older years are not directly comparable.
- Raw files are not included in this repo. Download them from the portal and
  place them in `data/raw/<year>/` with their original file names
  (e.g. `data/raw/2024-25/facility_data_KOTA_2024-25.zip`). Do not unzip them.

## How to run
```powershell
pip install -r requirements.txt
python run_pipeline.py                 # incremental: unchanged zips are skipped
python run_pipeline.py --full-refresh  # reload every zip into bronze
python -m pytest                       # tests (synthetic data only)
```
Outputs:
- `data/bronze/<dataset>/year=<YYYY-YY>/*.parquet`: faithful Parquet copy of each raw
  zip (values as text, plus load metadata `_source_file`, `_source_row`,
  `_district_file`, `_year`, `_ingested_at`, `_run_id`). Not committed.
- `reports/profiling/`: per-column profile of every table (`<layer>__<table>.csv`) and a
  `README.md` listing only the columns with problems (invalid codes, schema differences, ...)
- `reports/run_manifest.json`: what the last run did (files loaded/skipped with
  SHA-256 checksums, rows per dataset and year, timings per stage)
- `data/processed/school_year.csv`: one row per school per year
- `data/processed/district_year.csv`: district summaries
- `data/processed/district_year_by_management.csv`: district summaries by management type
- `reports/qa_report.md`: automated data quality checks

New districts or years need no code changes: drop the zips into `data/raw/<year>/` and re-run.

## Repository structure
```
pipeline/
  config.py   paths, code mappings, indicator definitions
  ingest.py   finds raw zips, reads them in place, stacks years/districts
  bronze.py   raw zips -> bronze Parquet (as-is, incremental by checksum)
  profiling.py  column profiles and invalid-code checks for each layer
  build.py    school-year table + district summaries
  checks.py   automated data quality report
reference/            lookup data as JSON: districts, management codes, official totals,
                      schema columns and codes (from the PDFs via scripts/extract_schema.py)
scripts/
run_pipeline.py
tests/                pytest, on synthetic data
DATA_QUALITY_LOG.md   issues found and decisions taken
docs/                 UDISE+ schema PDFs
```

## Data ethics and limitations
- Data is at school level with pseudonymised school codes; it contains no
  personal information about students or teachers.
- UDISE+ is self-reported by schools and verified by block/district officials;
  figures may contain reporting errors (see `DATA_QUALITY_LOG.md`).
- Relationships found in this analysis are correlations, not causal effects.
