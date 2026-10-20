"""Run the full pipeline: raw zips -> bronze -> silver (+ quarantine) -> profiling -> summaries + QA report.

Usage:  python run_pipeline.py                 (incremental: unchanged zips are skipped in bronze)
        python run_pipeline.py --full-refresh  (reload every zip into bronze)
"""
import argparse
import json
import time
from datetime import datetime, timezone

from pipeline import config as C
from pipeline.bronze import load_bronze, new_run_id, summarise_bronze
from pipeline.gold import build_gold
from pipeline.build import build_school_year, summarise
from pipeline.checks import run_checks
from pipeline.profiling import bronze_tables, profile_layer, write_readme
from pipeline.silver import TABLES as SILVER_TABLES, build_silver, silver_tables, tables_for_build


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

    # Stage 2: silver (typed, cleaned, rule-checked; failures -> data/quarantine)
    t0 = time.perf_counter()
    silver = build_silver(run_id=run_id)
    manifest["stages"]["silver"] = {"seconds": round(time.perf_counter() - t0, 2), **silver}
    warnings = list(bronze["warnings"])
    for table, s in silver["tables"].items():
        rules = {r: n for r, n in s["quarantined_by_rule"].items() if n}
        if rules:
            warnings.append(f"{table}: {s['rows_quarantined']:,} rows quarantined ("
                            + ", ".join(f"{r}: {n:,}" for r, n in rules.items())
                            + f") -> data/quarantine/{table}.parquet")

    # Stage 3: gold (DuckDB star schema from sql/, tested by sql/tests/)
    t0 = time.perf_counter()
    gold = build_gold()
    manifest["stages"]["gold"] = {"seconds": round(time.perf_counter() - t0, 2), **gold}

    # Stage 4: profiling of bronze and silver (per-column statistics and invalid codes)
    t0 = time.perf_counter()
    profiles = {"bronze": profile_layer("bronze", bronze_tables()),
                "silver": profile_layer("silver", silver_tables(),
                                        datasets={t: d for t, (d, _) in SILVER_TABLES.items()})}
    problems = write_readme(profiles)
    manifest["stages"]["profile"] = {
        "seconds": round(time.perf_counter() - t0, 2),
        "columns_with_problems": problems,
        "duplicate_rows_on_natural_key": {
            f"{layer}__{table}": r["checks"]["duplicate_rows"]
            for layer, res in profiles.items() for table, r in res.items() if "duplicate_rows" in r["checks"]},
    }

    # Stage 5: build (pandas, from silver): school-year table, district summaries, QA report
    t0 = time.perf_counter()
    tables = tables_for_build()
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

    manifest["warnings"] = warnings
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    C.RUN_MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    b = manifest["stages"]["bronze"]
    print(f"Run {run_id}")
    print(f"Bronze: {b['files_loaded']} files loaded, {b['files_skipped']} unchanged (skipped), "
          f"{b['bronze_bytes'] / 1e6:.1f} MB, {b['seconds']} s")
    sv = manifest["stages"]["silver"]
    print(f"Silver: {sum(s['rows_silver'] for s in sv['tables'].values()):,} rows in "
          f"{len(sv['tables'])} tables, {sum(s['rows_quarantined'] for s in sv['tables'].values()):,} "
          f"quarantined, bronze = silver + quarantine for every dataset, {sv['seconds']} s")
    g = manifest["stages"]["gold"]
    print(f"Gold: {len(g['tables'])} tables in {C.GOLD_DB.name}, {g['tests_passed']} data tests passed, "
          f"{g['dim_school']['schools_changing_district']} schools changed district (SCD2), {g['seconds']} s")
    pr = manifest["stages"]["profile"]
    print(f"Profiling: {len(pr['columns_with_problems'])} tables, "
          f"{sum(pr['columns_with_problems'].values())} column-years with problems, {pr['seconds']} s "
          f"-> {C.PROFILING_DIR / 'README.md'}")
    print(f"Loaded: {', '.join(f'{k} ({len(v):,} rows)' for k, v in tables.items())}")
    print(f"School-year rows: {len(school):,} | districts: {school['district'].nunique()} | "
          f"years: {', '.join(sorted(school['year'].unique()))}")
    for w in warnings:
        print("WARNING:", w)
    print(f"Outputs written to {C.PROCESSED_DIR}, {C.REPORTS_DIR / 'qa_report.md'} "
          f"and {C.RUN_MANIFEST}")


if __name__ == "__main__":
    main()
