"""Run the full pipeline: raw zips -> cleaned tables + data quality report.

Usage:  python run_pipeline.py
"""
from pipeline import config as C
from pipeline.build import build_school_year, summarise
from pipeline.checks import run_checks
from pipeline.ingest import load_all


def main():
    tables, warnings = load_all()
    if not tables:
        raise SystemExit(f"No raw files found in {C.RAW_DIR}")

    school = build_school_year(tables)
    district = summarise(school, ["district", "year"])
    district_mgmt = summarise(school, ["district", "year", "management_group"])

    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    C.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    school.to_csv(C.PROCESSED_DIR / "school_year.csv", index=False)
    district.to_csv(C.PROCESSED_DIR / "district_year.csv", index=False)
    district_mgmt.to_csv(C.PROCESSED_DIR / "district_year_by_management.csv", index=False)
    (C.REPORTS_DIR / "qa_report.md").write_text(run_checks(tables, school, warnings), encoding="utf-8")

    print(f"Loaded: {', '.join(f'{k} ({len(v):,} rows)' for k, v in tables.items())}")
    print(f"School-year rows: {len(school):,} | districts: {school['district'].nunique()} | "
          f"years: {', '.join(sorted(school['year'].unique()))}")
    for w in warnings:
        print("WARNING:", w)
    print(f"Outputs written to {C.PROCESSED_DIR} and {C.REPORTS_DIR / 'qa_report.md'}")


if __name__ == "__main__":
    main()
