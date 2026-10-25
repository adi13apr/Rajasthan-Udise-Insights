# Resume bullets

Every number below is from a real run of this project (`reports/run_manifest.json`,
`reports/validation.md`, `reports/benchmark.md`, `reports/analysis/numbers.json`, pytest).
Pick 3-4 per application; each one can be defended line by line in an interview.

**Project line:** Rajasthan School Education Insights: ELT pipeline, DuckDB star schema and policy
analysis on UDISE+ school-level data (41 districts, 2 years, 1.06 lakh schools, 1.5 crore students).
Python, SQL, DuckDB, Parquet, pandas, statsmodels, pytest, GitHub Actions.

## Combined: Sattva + Data Engineering + Mico (Data Science)

From the statistical module run of 25 Oct 2026 (`reports/analysis/stats_module_results.json`) and the
pipeline runs above.

- Built a tested ELT pipeline in Python, SQL and DuckDB that ingests 94 UDISE+ files (1.72 million
  rows) into a star schema with a 6.4-million-row fact table and an SCD Type 2 school dimension; 15
  data-quality rules quarantine bad rows with reasons, and 33 SQL data tests plus 52 pytest tests run in
  GitHub Actions CI. Queries run 33x faster than on raw CSV (885 ms to 27 ms).
- Analysed 1.06 lakh schools across all 41 districts of Rajasthan after matching all 8 official 2024-25
  UDISE+ totals exactly; found three distinct causes of the secondary-school gender gap and wrote a
  two-page policy brief for funders and NGOs, with a Tableau dashboard (say "published" once it is on
  Tableau Public).
- Ran EDA, missing-value treatment, IQR/z-score outlier detection and feature engineering (14 ratio and
  per-student features) on 105,801 schools, then correlation analysis (Pearson/Spearman, chi-square with
  Cramer's V) and 5 hypothesis tests with p-values and effect sizes: girls are 52.7% of class 9-12
  students in government vs 40.3% in non-government co-ed schools (Welch t-test, p < 0.001, Cohen's d = 0.95).
- Modelled pupil-teacher ratio with multiple linear regression and district fixed effects (test R² = 0.756,
  Adjusted R² = 0.755, RMSE = 0.373 on log scale, 5-fold CV R² = 0.760; all VIF < 5) and compared logistic
  regression, a decision tree and a random forest for flagging single-teacher schools under 6.7% class
  imbalance (random forest ROC-AUC 0.927, PR-AUC 0.616 vs 0.067 baseline, recall 92%), with train-test
  gaps of 0.015 or less (0.76 for an unconstrained tree).

## Sattva Consulting: Data Intern (India Data Insights)

- Analysed UDISE+ microdata for all 41 districts of Rajasthan (1.06 lakh schools, 1.5 crore
  students), after validating the pipeline against the 8 officially published 2024-25 indicators
  (schools, teachers, enrolment, government totals); all 8 matched exactly.
- Showed that the district gender gap in secondary school has three distinct causes (girls leaving in
  Jaisalmer and Barmer, boys migrating into coaching hubs like Sikar, boys leaving the tribal south), so
  a single "gender gap" indicator would misdirect girls' retention funding.
- Estimated a school-level regression (29,479 co-ed schools, district fixed effects, clustered errors):
  a functional girls' toilet is associated with a 1.7-point higher girls' share in classes 9-12
  (95% CI 1.2-2.3) and private unaided schools with a 13-point lower share; quantified the gap as
  2,420 secondary schools (1,08,636 girls) without a functional girls' toilet.
- Compared the same 1,05,418 schools across two years (correcting for a district boundary change):
  single-teacher schools rose 4.4 points in Jaisalmer and Barmer against 1.0 state-wide.
- Wrote a two-page policy brief for funders and NGOs (three findings, each with a chart and a "so
  what"), and built Tableau-ready district datasets and an Excel workbook of pivot summaries.

## Data Engineering Intern (Data & AI)

- Built an ELT pipeline (raw zips -> bronze Parquet -> silver -> DuckDB gold) that ingests 94 source
  files (1.72 million rows) in one command, incrementally by SHA-256 checksum (a rerun with no changes
  takes 0.14 s instead of 17 s).
- Declared 15 data quality rules as code: rows failing an error rule are quarantined with a reason
  (13,560 rows, including 4,520 duplicate schools across files x 3 datasets), warnings become flag
  columns, and every run checks that bronze rows = silver + quarantine.
- Profiled every column against codes parsed from the official schema PDFs, finding 5 undocumented-code
  and schema issues; designed a star schema (7 dimensions, 3 facts, 6.4 million fact rows) with an
  SCD Type 2 school dimension that handles 189 schools changing district.
- Wrote 33 SQL data tests (referential integrity, grain, SCD2, totals) that gate each gold build, and
  replaced a pandas summary with SQL marts reproducing it cell for cell (9,882 of 9,888 identical,
  6 differing only in rounding).
- Benchmarked raw CSV vs Parquet vs the star schema (885 ms -> 27 ms for the same query); added 52
  pytest tests and GitHub Actions CI on synthetic data (the source data cannot be redistributed),
  which caught a dependency break (pandas 3) that was then fixed by pinning versions.
