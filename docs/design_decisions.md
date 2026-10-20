# Design decisions

Why the pipeline is built the way it is. Each section: the decision, the reason, and
what was given up. Numbers are from the full run on all 41 districts, 2024-25 and 2025-26.

## 1. ELT, not ETL: load first, transform later

**Decision.** Raw zips are copied into bronze unchanged (every value kept as text, plus
where it came from: `_source_file`, `_source_row`, `_run_id`, `_ingested_at`). All cleaning
happens afterwards, in silver.

**Why.** If a cleaning rule turns out to be wrong, silver can be rebuilt from bronze without
going back to the source. Text-only bronze never guesses a type, so nothing can be lost in
loading: 72,159,372 cells were compared with the raw CSVs, 0 differences. Every quarantined
row points back to the exact line of the source file.

**Given up.** Bronze takes disk space (31.7 MB, about the same as the zips) and every run
does the typing work again in silver. At this size that costs a few seconds.

**Incremental loading.** The SHA-256 checksum of each zip is stored next to bronze; a rerun
skips unchanged files (94 files in 0.14 s instead of 17 s). Bronze files are written to a
temporary name and renamed, so a crash never leaves a half-written file.

## 2. Quality rules as data, with quarantine

**Decision.** `pipeline/rules.py` lists every rule with a name, tables, severity, description
and check. **Errors** (missing key, not a number, negative count, unknown district, duplicate,
orphan row) move the row to `data/quarantine/` with the rule and a reason. **Warnings**
(undocumented code, more functional toilets than toilets, PTR above 60, ...) keep the row and
add a `flag_<rule>` column. `docs/quality_rules.md` is generated from the same list.

**Why.** Nothing is dropped silently, and every number can be traced. The split matters: a
duplicate row would double count, so it must go; an odd code in the solar-panel column does
not make the school less real, and dropping the school would break the official school count.
Each run checks that bronze rows = silver rows + quarantined rows.

**Example.** 4,520 Kota schools were in both the Kota test download and the state-wide file.
The copies were checked to be identical (0 differing rows), the state-wide copy is kept, and
the 4,520 x 3 extra rows are in quarantine with the reason "extra copy; kept the row from
profile_data_1_RAJASTHAN_2024-25.zip".

## 3. Profiling before (and after) cleaning

Every column of every bronze and silver table is profiled each run (nulls, distinct values,
min/max, top values, codes outside the documented set, duplicates on the key). The allowed
codes come from the schema PDFs, parsed once into `reference/schema_columns.json`. This found
five issues that were not known before (DATA_QUALITY_LOG #23-27), e.g. a code `3` in the
Yes/No columns `solar_panel` and `rain_water_harvesting`. School codes are never written to
the reports.

## 4. A star schema in DuckDB

**Decision.** Gold is a star schema: facts (`fact_enrolment`, `fact_teachers`,
`fact_school_facilities`) at a stated grain, pointing through integer surrogate keys to small
dimensions (year, district, management, school, grade, gender, social category). All SQL is
in numbered files in `sql/`, run in order.

**Why a star schema.** Every question in this project is "a measure, sliced by some
attributes" (enrolment by district, grade and gender; teachers by management). A fact table at
one clear grain plus dimensions answers all of them with the same joins, keeps each attribute
in one place, and is what Tableau and Power BI expect. `fact_enrolment` (school x year x grade
x gender x social category) has 6,403,516 rows; zero cells are left out and that is stated
in the table's comment.

**Why DuckDB.** It is an analytical (columnar) database that runs inside Python with no
server, reads Parquet directly, and is fast at exactly this kind of work (the gold build takes
about 5 seconds). The whole warehouse is one file. A server database (Postgres) would add
setup without adding anything at 30 MB; the SQL would move to a cloud warehouse with few
changes.

**Surrogate keys** are `row_number()` over the natural key, so the same data always gives the
same keys (tested). They are not stable when new rows arrive in between; a key-map table would
be the next step if the warehouse were loaded incrementally.

**Data tests.** `sql/tests/` holds 33 checks that must all return 0: referential integrity
(an anti-join from every fact key to its dimension), grain, SCD2 consistency, totals preserved
from silver to gold, and the marts adding up. The database is built in a temporary file and
replaces the old one only if every test passes.

## 5. District boundary changes: SCD Type 2 school dimension

**Problem.** 189 schools changed district between the two years; 180 of them moved from ALWAR
to the new district KOTPUTLI-BEHROR. If a school had only one district, either 2024-25
enrolment would be counted in the wrong district, or the district's trend would show fake
growth or loss.

**Decision.** `dim_school` is a Type 2 slowly changing dimension. A new version of a school
starts whenever district, block, management, rural/urban, category or type changes; each
version has `valid_from_year`, `valid_to_year` and `is_current`. Facts join to the version
valid in their year. The full run gives 1,708 schools with two versions (1,245 because the
school category changed, e.g. a primary school upgraded; 189 because the district changed).

**Given up.** Queries must join on the year range, and "a school" now means `pseudocode`,
while `school_key` means one version of it.

## 6. Parquet, partitioned by year

**Decision.** Bronze and silver are Parquet files in `year=<YYYY-YY>/` folders (Hive-style).

**Why.** Parquet is columnar and compressed: a query reads only the columns it needs, and
types are stored with the data. Year folders let a query skip years it does not need and make
a new year a new folder. Measured (`reports/benchmark.md`): the same Kota query took about
0.9 s on raw CSV, about 0.12 s on bronze Parquet and about 0.01-0.03 s on gold.

**Honest result.** At about 11 MB per year, partition pruning itself made no meaningful
difference (scans within a few milliseconds of each other, order changing between runs).
It pays off with many years or states. The benchmark also showed a trap: multi-year Parquet
must be read with `union_by_name = true`, otherwise DuckDB uses the first file's columns and
the 2025-26-only transgender columns silently disappear.

## 7. Marts in SQL, checked against the old pandas output

The district marts (`mart_district_year`, `mart_district_year_management`) come from one
`GROUPING SETS` query, so each indicator has exactly one definition. They replaced the earlier
pandas summary only after reproducing it: 9,882 of 9,888 cells identical, 6 differing by 0.001
because SQL rounds an exact half up and pandas rounds it to even. The old output is kept in
`tests/regression/` and checked by pytest. The infrastructure index is stored as an exact
decimal, because averaging floating-point numbers in parallel made one rounded value flip
between runs.

## 8. Validation against official figures

The 8 indicators UDISE+ publishes for 2024-25 (schools, teachers, enrolment, zero-enrolment
and single-teacher schools, government schools/enrolment/teachers) are computed from gold in
SQL (`sql/validation/`) and compared with `reference/official_totals.json`. All 8 match
exactly. The same check also runs in pandas from silver (`reports/qa_report.md`); two
independent routes agreeing is the strongest evidence that nothing was lost or double counted.

## 9. How the management codes were validated

The schema says management codes are "as per UDISE DCF" without listing them. The data has 13
codes. Grouping 1, 2, 3, 6, 90 (state), 92, 93 (central) and 94, 95, 101 (other government) as
government makes government schools, enrolment and teachers match the official figures
exactly (4,45,073 teachers); codes 94, 95 and 101 hold exactly the 7 schools and 170 teachers
that were otherwise missing. 97 and 98 are not government. The grouping lives in
`reference/management_codes.json`; the exact names of 94, 95, 101, 97, 98 are still to be
confirmed with the DCF.

## 10. Synthetic data in tests and CI

The portal's terms forbid redistributing the data, so it can never be in the repository or on
a CI server. `scripts/make_sample_data.py` generates synthetic zips with the real file names,
columns and quirks (state-wide and district files, duplicate Kota copies, old spellings,
transgender columns only in 2025-26, schools moving district), with one planted problem per
error rule. The same seed always gives byte-identical zips, so incremental loading works on them.
`python run_pipeline.py --sample` runs every stage on them, and the end-to-end test checks
that each planted problem lands in quarantine under the right rule. CI runs pytest and the
sample pipeline on every push.

## 11. Reproducible outputs

Re-running on the same data gives the same files: deterministic surrogate keys, top values
ordered by count then value, exact decimals where averages are rounded. Committed reports
therefore change only when the data or the code changes.
