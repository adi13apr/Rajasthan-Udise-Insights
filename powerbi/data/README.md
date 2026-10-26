# Power BI data

Written by `python scripts/export_powerbi.py` from the gold database; do not edit by hand.
How to load it: [../BUILD_GUIDE.md](../BUILD_GUIDE.md).

Committed (no school-level data):

- `official_totals.csv`: published UDISE+ figures per year and indicator (from `reference/official_totals.json`),
  empty where nothing is published.
- `norms.csv`: teacher norms per stage (from `reference/norms.json`).
- `export_manifest.json`: rows, columns and size of every file of the last export.

Not committed (school-level, UDISE+ data-sharing terms): the dimension and fact CSVs and
`agg_school_year.csv`. Regenerate them with the script.

Source: UDISE+, Ministry of Education, Government of India.
