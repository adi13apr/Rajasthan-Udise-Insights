"""Run the full pipeline: raw zips -> bronze Parquet -> cleaned tables + data quality report.

Usage:  python run_pipeline.py                 (incremental: unchanged zips are skipped in bronze)
        python run_pipeline.py --full-refresh  (reload every zip into bronze)
"""
import argparse
import json
import time
from datetime import datetime, timezone

from pipeline import config as C
from pipeline.bronze import load_bronze, new_run_id, summarise_bronze
from pipeline.build import build_school_year, summarise
from pipeline.checks import run_checks
from pipeline.ingest import load_all


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full-refresh", action="store_true",
                        help="reload every raw zip into bronze, even if unchanged")
    args = parser.parse_args()

    run_id = new_run_id()
    manifest = {"run_id": run_id,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "stages": {}}

    # Stage 1: bronze (load raw as-is)
    t0 = time.perf_counter()
    bronze = load_bronze(run_id=run_id, full_refresh=args.full_refresh)
    if not bronze["files"]:
        raise SystemExit(f"No raw files found in {C.RAW_DIR}")
    manifest["stages"]["bronze"] = {"seconds": round(time.perf_counter() - t0, 2),
                                    **summarise_bronze(bronze),
                                    "files": bronze["files"]}

    # Stage 2: build (pandas; still reads the raw zips until silver exists, see docs/PLAN.md P3)
    t0 = time.perf_counter()
    tables, warnings = load_all()
    school = build_school_year(tables)
    district = summarise(school, ["district", "year"])
    district_mgmt = summarise(school, ["district", "year", "management_group"])

    C.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    C.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    school.to_csv(C.PROCESSED_DIR / "school_year.csv", index=False)
    district.to_csv(C.PROCESSED_DIR / "district_year.csv", index=False)
    district_mgmt.to_csv(C.PROCESSED_DIR / "district_year_by_management.csv", index=False)
    (C.REPORTS_DIR / "qa_report.md").write_text(run_checks(tables, school, warnings), encoding="utf-8")
    manifest["stages"]["build"] = {"seconds": round(time.perf_counter() - t0, 2),
                                   "rows_loaded": {k: len(v) for k, v in tables.items()},
                                   "school_year_rows": len(school),
                                   "district_year_rows": len(district)}

    warnings = bronze["warnings"] + warnings
    manifest["warnings"] = warnings
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    C.RUN_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    b = manifest["stages"]["bronze"]
    print(f"Run {run_id}")
    print(f"Bronze: {b['files_loaded']} files loaded, {b['files_skipped']} unchanged (skipped), "
          f"{b['bronze_bytes'] / 1e6:.1f} MB, {b['seconds']} s")
    print(f"Loaded: {', '.join(f'{k} ({len(v):,} rows)' for k, v in tables.items())}")
    print(f"School-year rows: {len(school):,} | districts: {school['district'].nunique()} | "
          f"years: {', '.join(sorted(school['year'].unique()))}")
    for w in warnings:
        print("WARNING:", w)
    print(f"Outputs written to {C.PROCESSED_DIR}, {C.REPORTS_DIR / 'qa_report.md'} "
          f"and {C.RUN_MANIFEST}")


if __name__ == "__main__":
    main()
