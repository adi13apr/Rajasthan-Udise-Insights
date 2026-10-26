"""Parity check: PySpark silver/gold against the DuckDB pipeline's silver/gold.

    python -m spark.parity                 local Spark on data/bronze vs data/gold/udise.duckdb
    python -m spark.parity --sample        the same on the synthetic sample (run_pipeline.py --sample first)
    python -m spark.parity --from FILE     use the fingerprint written by the Databricks notebook
    python -m spark.parity --time-duckdb   also time the DuckDB silver + gold build (into data/tmp)

Both engines run the same FINGERPRINT queries (plain SQL that DuckDB and Spark both accept)
over the same table names (gold tables, silver_<table>, quarantine_<table>), and the
results are compared cell by cell:
    - whole numbers (row counts, sums, keys) and text/booleans must be equal exactly;
    - ratio columns (listed per query) may differ by at most RATIO_TOLERANCE (0.001): the
      marts round to 3 decimals and the engines can round a .xxx5 value differently.
Every mismatch is listed with its keys, column and both values, and the exit code is 1.
Also checked for both engines: every data test returns 0 failures and every official
2024-25 total matches exactly. Output: reports/spark_parity.md and .json (district-level
aggregates only; no school-level data).
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

RATIO_TOLERANCE = 0.001
SILVER = ["schools", "enrolment", "enrolment_other", "facilities", "teachers"]
GOLD = ["dim_year", "dim_district", "dim_management", "dim_grade", "dim_gender", "dim_social_category",
        "dim_school", "fact_enrolment", "fact_teachers", "fact_school_facilities", "int_school_year",
        "mart_district_year", "mart_district_year_management"]
HAS_FLAGS = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_boys_toilet", "has_handwash",
             "has_library", "has_playground", "has_ramp", "has_internet", "has_computer"]
ENR_COLS = ["enr_total", "enr_girls", "enr_pre_primary", "enr_primary", "enr_primary_b", "enr_primary_g",
            "enr_secondary_plus", "enr_secondary_plus_b", "enr_secondary_plus_g", "enr_sc", "enr_st"]
MART_WHOLE = {"schools", "enrolment", "teachers", "is_government"}


@dataclass
class Query:
    name: str
    sql: str
    keys: list[str]
    ratios: set[str] = field(default_factory=set)  # compared with RATIO_TOLERANCE; the rest exactly


def flag_columns(table: str) -> list[str]:
    from pipeline.rules import WARNING, rules_for
    return [f"flag_{r.name}" for r in rules_for(table, WARNING)]


def union(parts: list[str]) -> str:
    return "\nunion all\n".join(parts)


def fingerprint_queries(validation_sql: str) -> list[Query]:
    by_district_year = ("join dim_school s on s.school_key = f.school_key "
                        "join dim_district d on d.district_key = s.district_key "
                        "join dim_year y on y.year_key = f.year_key")
    facility_measures = ",\n".join(
        f"sum(case when f.{c} = 1 then 1 else 0 end) as {c}_yes, count(f.{c}) as {c}_reported" for c in HAS_FLAGS)
    queries = [
        Query("table_rows", union(
            [f"select '{t}' as table_name, count(*) as n from {t}" for t in GOLD]
            + [f"select 'silver_{t}' as table_name, count(*) as n from silver_{t}" for t in SILVER]
            + [f"select 'quarantine_{t}' as table_name, count(*) as n from quarantine_{t}" for t in SILVER]),
            ["table_name"]),
        Query("quarantine_by_rule", union(
            [f"select '{t}' as table_name, _rule as rule, count(*) as n from quarantine_{t} group by _rule"
             for t in SILVER]), ["table_name", "rule"]),
        Query("silver_rows_and_flags_by_year", union(
            [f"select '{t}' as table_name, year, 'rows' as measure, count(*) as n from silver_{t} group by year"
             for t in SILVER]
            + [f"select '{t}' as table_name, year, '{c}' as measure, cast(sum({c}) as bigint) as n "
               f"from silver_{t} group by year" for t in SILVER for c in flag_columns(t)]),
            ["table_name", "year", "measure"]),
        Query("silver_schools_by_district_year", """
            select district, year, count(*) as schools, cast(sum(is_govt) as bigint) as govt,
                   sum(case when block_is_admin_region then 1 else 0 end) as admin_region_blocks,
                   count(block_clean) as with_block_clean, count(location) as with_location,
                   count(district_raw) as with_district_raw
            from silver_schools group by district, year""", ["district", "year"]),
        Query("facilities_by_district_year", f"""
            select d.district_name as district, y.academic_year as year, count(*) as school_years,
                   sum(case when m.is_government then 1 else 0 end) as govt_schools,
                   sum(case when s.location = 'Rural' then 1 else 0 end) as rural_schools,
                   {facility_measures},
                   cast(sum(f.classrooms) as bigint) as classrooms,
                   cast(sum(f.classrooms_needs_major_repair) as bigint) as classrooms_major_repair,
                   cast(sum(f.lowclass) as bigint) as sum_lowclass,
                   cast(sum(f.highclass) as bigint) as sum_highclass,
                   cast(sum(f.flag_facility_invalid_code) as bigint) as flag_facility_invalid_code,
                   cast(sum(f.flag_functional_toilets_exceed_total) as bigint) as flag_toilets,
                   cast(sum(f.flag_classroom_total_mismatch) as bigint) as flag_classrooms,
                   avg(f.pct_classrooms_major_repair) as avg_pct_classrooms_major_repair
            from fact_school_facilities f {by_district_year}
            join dim_management m on m.management_key = s.management_key
            group by d.district_name, y.academic_year""", ["district", "year"],
              {"avg_pct_classrooms_major_repair"}),
        Query("enrolment_by_district_year_stage_gender_category", """
            select d.district_name as district, y.academic_year as year, g.stage, ge.gender_code as gender,
                   sc.social_category, cast(sum(f.students) as bigint) as students, count(*) as fact_rows
            from fact_enrolment f
            join dim_school s on s.school_key = f.school_key
            join dim_district d on d.district_key = s.district_key
            join dim_year y on y.year_key = f.year_key
            join dim_grade g on g.grade_key = f.grade_key
            join dim_gender ge on ge.gender_key = f.gender_key
            join dim_social_category sc on sc.social_category_key = f.social_category_key
            group by d.district_name, y.academic_year, g.stage, ge.gender_code, sc.social_category""",
              ["district", "year", "stage", "gender", "social_category"]),
        Query("teachers_by_district_year", f"""
            select d.district_name as district, y.academic_year as year, count(*) as rows,
                   cast(sum(f.total_tch) as bigint) as teachers,
                   sum(case when f.total_tch = 1 then 1 else 0 end) as single_teacher_schools,
                   cast(sum(f.flag_no_teachers_with_students) as bigint) as flag_no_teachers,
                   cast(sum(f.flag_high_ptr) as bigint) as flag_high_ptr
            from fact_teachers f {by_district_year}
            group by d.district_name, y.academic_year""", ["district", "year"]),
        Query("dim_school_by_district", """
            select d.district_name as district, count(*) as versions,
                   sum(case when s.is_current then 1 else 0 end) as current_versions,
                   cast(sum(s.school_key) as bigint) as sum_school_key,
                   cast(sum(s.version) as bigint) as sum_version,
                   cast(sum(s.valid_from_year) as bigint) as sum_valid_from_year,
                   cast(sum(s.valid_to_year) as bigint) as sum_valid_to_year,
                   cast(sum(s.management_key) as bigint) as sum_management_key,
                   count(s.block_clean) as with_block_clean, count(s.location) as with_location
            from dim_school s join dim_district d on d.district_key = s.district_key
            group by d.district_name""", ["district"]),
        Query("scd2_district_moves", """
            select a.district_name || ' -> ' || b.district_name as move, count(*) as schools
            from dim_school v1
            join dim_school v2 on v2.pseudocode = v1.pseudocode and v2.version = v1.version + 1
            join dim_district a on a.district_key = v1.district_key
            join dim_district b on b.district_key = v2.district_key
            where v1.district_key <> v2.district_key
            group by a.district_name || ' -> ' || b.district_name""", ["move"]),
        Query("int_school_year_by_district_year", f"""
            select district_name as district, academic_year as year, count(*) as school_years,
                   {', '.join(f'cast(sum({c}) as bigint) as {c}' for c in ENR_COLS)},
                   cast(sum(total_tch) as bigint) as teachers,
                   cast(sum(single_teacher) as bigint) as single_teacher,
                   cast(sum(infra_index) * 10 as bigint) as infra_index_sum_x10,
                   count(infra_index) as infra_index_reported,
                   sum(case when is_government then 1 else 0 end) as govt
            from int_school_year group by district_name, academic_year""", ["district", "year"]),
        Query("mart_district_year", "select * from mart_district_year", ["district", "year"], {"*"}),
        Query("mart_district_year_management", "select * from mart_district_year_management",
              ["district", "year", "management_group"], {"*"}),
        Query("official_indicators", validation_sql, ["year", "indicator"]),
    ]
    return queries


def ratio_columns(q: Query, columns: list[str]) -> set[str]:
    if "*" in q.ratios:  # marts: every column except the keys and the whole numbers
        return {c for c in columns if c not in q.keys and c not in MART_WHOLE}
    return q.ratios


# --- Running the queries --------------------------------------------------------

def validation_sql() -> str:
    from pipeline import config as C
    return (C.ROOT / "sql" / "validation" / "01_official_indicators.sql").read_text(encoding="utf-8") \
        .strip().rstrip(";")


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Decimals -> float, keys and text as str; same shape from both engines."""
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object and out[c].map(lambda v: isinstance(v, Decimal)).any():
            out[c] = out[c].map(lambda v: None if v is None else float(v)).astype(float)
    return out


def run_queries(execute, queries: list[Query]) -> dict[str, pd.DataFrame]:
    """execute(sql) -> pandas DataFrame, for one engine."""
    return {q.name: normalise(execute(q.sql)) for q in queries}


def duckdb_fingerprint(queries: list[Query], gold_db=None, silver_dir=None, quarantine_dir=None
                       ) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """Fingerprint and data tests of the DuckDB pipeline's outputs (read only)."""
    import duckdb

    from pipeline import config as C
    from pipeline.gold import attach_sources, run_sql_tests
    from pipeline.profiling import read_sql

    gold_db, silver_dir = gold_db or C.GOLD_DB, silver_dir or C.SILVER_DIR
    quarantine_dir = quarantine_dir or C.QUARANTINE_DIR
    con = duckdb.connect()
    try:
        con.execute(f"attach '{gold_db.as_posix()}' as gold (read_only)")
        con.execute("use gold")
        for t in SILVER:
            con.execute(f"create temp view silver_{t} as select * from {read_sql((silver_dir / t).as_posix())}")
            con.execute(f"create temp view quarantine_{t} as select * from "
                        f"read_parquet('{(quarantine_dir / f'{t}.parquet').as_posix()}')")
        result = run_queries(lambda sql: con.execute(sql).df(), queries)
        attach_sources(con, silver_dir)
        tests = run_sql_tests(con)
    finally:
        con.close()
    return result, tests


def spark_fingerprint(spark, silver: dict, quarantine: dict, gold: dict, queries: list[Query]
                      ) -> dict[str, pd.DataFrame]:
    for t, df in silver.items():
        df.createOrReplaceTempView(f"silver_{t}")
    for t, df in quarantine.items():
        df.createOrReplaceTempView(f"quarantine_{t}")
    for t, df in gold.items():
        df.createOrReplaceTempView(t)
    return run_queries(lambda sql: spark.sql(sql).toPandas(), queries)


def to_json(fingerprint: dict[str, pd.DataFrame]) -> dict:
    def value(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if hasattr(v, "item"):  # numpy scalar
            return v.item()
        return float(v) if isinstance(v, Decimal) else v
    return {name: [{k: value(v) for k, v in row.items()} for row in df.to_dict("records")]
            for name, df in fingerprint.items()}


def from_json(data: dict) -> dict[str, pd.DataFrame]:
    return {name: pd.DataFrame(rows) for name, rows in data.items()}


# --- Comparing ---------------------------------------------------------------------

def same(a, b) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    if isinstance(a, (bool,)) or isinstance(b, (bool,)):
        return bool(a) == bool(b)
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def compare_query(q: Query, duck: pd.DataFrame, spark: pd.DataFrame) -> dict:
    """Cell-by-cell comparison of one query's results. Returns counts, max ratio diff, mismatches."""
    mismatches = []
    if set(duck.columns) != set(spark.columns):
        mismatches.append({"query": q.name, "problem": "columns differ",
                           "only_duckdb": sorted(set(duck.columns) - set(spark.columns)),
                           "only_spark": sorted(set(spark.columns) - set(duck.columns))})
        return {"rows": 0, "cells": 0, "max_ratio_diff": None, "mismatches": mismatches}
    for df in (duck, spark):
        for k in q.keys:
            df[k] = df[k].astype(str)
    merged = duck.merge(spark, on=q.keys, how="outer", suffixes=("_duckdb", "_spark"), indicator=True)
    for _, row in merged[merged["_merge"] != "both"].iterrows():
        mismatches.append({"query": q.name, "key": {k: row[k] for k in q.keys},
                           "problem": f"row only in {'DuckDB' if row['_merge'] == 'left_only' else 'Spark'}"})
    both = merged[merged["_merge"] == "both"]
    ratios = ratio_columns(q, list(duck.columns))
    max_diff, cells = None, 0
    for c in [c for c in duck.columns if c not in q.keys]:
        for _, row in both.iterrows():
            a, b = row[f"{c}_duckdb"], row[f"{c}_spark"]
            cells += 1
            if c in ratios and not (pd.isna(a) or pd.isna(b)):
                diff = abs(float(a) - float(b))
                max_diff = diff if max_diff is None else max(max_diff, diff)
                ok = diff <= RATIO_TOLERANCE + 1e-12
            else:
                ok = same(a, b)
            if not ok:
                mismatches.append({"query": q.name, "key": {k: row[k] for k in q.keys}, "column": c,
                                   "duckdb": None if pd.isna(a) else a, "spark": None if pd.isna(b) else b,
                                   "kind": "ratio" if c in ratios else "exact"})
    return {"rows": len(both), "cells": cells, "max_ratio_diff": max_diff, "mismatches": mismatches}


def compare(queries: list[Query], duck: dict[str, pd.DataFrame], spark: dict[str, pd.DataFrame]) -> dict:
    results = {}
    for q in queries:
        if q.name not in spark:
            results[q.name] = {"rows": 0, "cells": 0, "max_ratio_diff": None,
                               "mismatches": [{"query": q.name, "problem": "missing from the Spark fingerprint"}]}
            continue
        results[q.name] = compare_query(q, duck[q.name].copy(), spark[q.name].copy())
    return results


# --- Report --------------------------------------------------------------------------

def fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.6g}" if v != int(v) else f"{int(v):,}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def write_report(out_dir, results: dict, tests: dict, official: dict, timings: dict, source: str) -> dict:
    from pipeline import config as C

    mismatches = [m for r in results.values() for m in r["mismatches"]]
    ratio_diffs = [r["max_ratio_diff"] for r in results.values() if r["max_ratio_diff"] is not None]
    failed_tests = {e: [t for t in ts if t["failures"]] for e, ts in tests.items()}
    official_bad = {e: [o for o in os_ if not o["match"]] for e, os_ in official.items()}
    names = {e: [t["check"] for t in ts] for e, ts in tests.items()}
    same_tests = len(set(map(tuple, names.values()))) == 1 if names else True
    passed = (not mismatches and not any(failed_tests.values()) and not any(official_bad.values())
              and same_tests)
    summary = {
        "passed": passed,
        "spark_source": source,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ratio_tolerance": RATIO_TOLERANCE,
        "queries": {n: {k: v for k, v in r.items() if k != "mismatches"} | {"mismatches": len(r["mismatches"])}
                    for n, r in results.items()},
        "cells_compared": sum(r["cells"] for r in results.values()),
        "mismatches": len(mismatches),
        "max_ratio_diff": max(ratio_diffs) if ratio_diffs else None,
        "data_tests": {e: {"checks": len(ts), "failed": len(failed_tests[e])} for e, ts in tests.items()},
        "same_data_test_names": same_tests,
        "official": official,
        "timings_seconds": timings,
        "mismatch_details": mismatches[:500],
    }
    lines = ["# Parity: PySpark vs DuckDB", "",
             "Generated by `python -m spark.parity`. Compares the PySpark silver/gold tables (`spark/`) "
             "with the DuckDB pipeline's (`pipeline/`, `sql/`), using the same SQL queries on both engines.", "",
             f"Spark results: {source}", "",
             f"**Result: {'PASS' if passed else 'FAIL'}.** {summary['cells_compared']:,} cells compared, "
             f"{len(mismatches)} mismatches. Whole numbers must match exactly; ratio columns may differ by at "
             f"most {RATIO_TOLERANCE}. Largest ratio difference found: "
             f"{fmt(summary['max_ratio_diff'])}.", "",
             "| Query | Rows matched | Cells compared | Mismatches | Largest ratio difference |",
             "|---|---:|---:|---:|---:|"]
    for n, r in results.items():
        lines.append(f"| {n} | {r['rows']:,} | {r['cells']:,} | {len(r['mismatches'])} | "
                     f"{fmt(r['max_ratio_diff']) if r['max_ratio_diff'] is not None else '-'} |")
    lines += ["", "## Data tests (sql/tests and their Spark port)", "",
              "| Engine | Checks | Failed |", "|---|---:|---:|"]
    lines += [f"| {e} | {len(ts)} | {len(failed_tests[e])} |" for e, ts in tests.items()]
    if not same_tests:
        lines.append("\nThe two engines do not run the same list of checks.")
    lines += ["", "## Official 2024-25 totals", ""]
    if not official:
        lines.append("Not checked: synthetic sample data (the official indicators are still compared "
                     "between the two engines above).")
    else:
        lines += ["| Indicator | Official | DuckDB | Spark |", "|---|---:|---:|---:|"]
    engines = list(official)
    for i, o in enumerate(official[engines[0]] if engines else []):
        vals = [official[e][i]["ours"] for e in engines]
        lines.append(f"| {o['year']} {o['indicator']} | {o['official']:,} | "
                     + " | ".join(fmt(v) + ("" if official[e][i]["match"] else " (differs)")
                                  for e, v in zip(engines, vals)) + " |")
    if timings:
        lines += ["", "## Timings", "", f"Where the Spark steps ran: {source}.", "", "| Step | Seconds |",
                  "|---|---:|"]
        lines += [f"| {k} | {v} |" for k, v in timings.items()]
    if mismatches:
        lines += ["", "## Mismatches", "", "| Query | Key | Column | DuckDB | Spark | Problem |",
                  "|---|---|---|---|---|---|"]
        for m in mismatches[:200]:
            lines.append(f"| {m['query']} | {m.get('key', '')} | {m.get('column', '')} | "
                         f"{fmt(m.get('duckdb'))} | {fmt(m.get('spark'))} | {m.get('problem', m.get('kind', ''))} |")
    lines += ["", f"{C.SOURCE_NOTE}. District-level aggregates only."]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "spark_parity.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "spark_parity.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


# --- Local Spark run ------------------------------------------------------------------

def run_local_spark(queries: list[Query]) -> tuple[dict, list[dict], pd.DataFrame, dict]:
    """Build silver and gold with local Spark from bronze (read only: nothing is written),
    then compute the fingerprint, data tests and official indicators."""
    from pipeline import config as C

    from .checks import official_indicators, run_data_tests
    from .gold import build_gold
    from .session import cache_and_count as cache
    from .session import local_session
    from .silver import build_silver

    timings = {}
    t0 = time.perf_counter()
    spark = local_session("udise-parity")
    spark.sparkContext.setLogLevel("ERROR")
    timings["spark: start session"] = round(time.perf_counter() - t0, 1)

    t0 = time.perf_counter()
    s = build_silver(spark, C.BRONZE_DIR.as_posix(), persist=cache)
    timings["spark: bronze -> silver"] = round(time.perf_counter() - t0, 1)
    t0 = time.perf_counter()
    gold = build_gold(spark, s["silver"], persist=cache)
    timings["spark: silver -> gold"] = round(time.perf_counter() - t0, 1)
    t0 = time.perf_counter()
    tests = run_data_tests(spark, s["silver"], gold)
    indicators = official_indicators(spark, gold)
    fingerprint = spark_fingerprint(spark, s["silver"], s["quarantine"], gold, queries)
    timings["spark: data tests + fingerprint"] = round(time.perf_counter() - t0, 1)
    spark.stop()
    return fingerprint, tests, indicators, timings


def time_duckdb() -> dict:
    """Rebuild DuckDB silver + gold from the same bronze into data/tmp (the real outputs are untouched)."""
    import shutil

    from pipeline import config as C
    from pipeline.gold import build_gold
    from pipeline.silver import build_silver

    tmp = C.DATA_DIR / "tmp" / "parity_duckdb"
    if tmp.exists():
        shutil.rmtree(tmp)
    t0 = time.perf_counter()
    build_silver(C.BRONZE_DIR, tmp / "silver", tmp / "quarantine", run_id="parity")
    t1 = time.perf_counter()
    build_gold(tmp / "silver", tmp / "gold" / "udise.duckdb", tmp / "gold" / "parquet", tmp / "gold" / "exports")
    t2 = time.perf_counter()
    shutil.rmtree(tmp)
    return {"duckdb: bronze -> silver": round(t1 - t0, 1), "duckdb: silver -> gold": round(t2 - t1, 1)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PySpark vs DuckDB parity check")
    parser.add_argument("--sample", action="store_true", help="synthetic sample outputs (data/sample/)")
    parser.add_argument("--from", dest="from_file", help="Spark fingerprint JSON from the Databricks notebook")
    parser.add_argument("--time-duckdb", action="store_true", help="also time the DuckDB silver + gold build")
    args = parser.parse_args(argv)
    if args.sample:
        os.environ["UDISE_SAMPLE"] = "1"  # before pipeline.config is imported
    from pipeline import config as C

    from .checks import check_official

    queries = fingerprint_queries(validation_sql())
    duck, duck_tests = duckdb_fingerprint(queries)
    timings: dict = {}
    if args.from_file:
        data = json.loads(open(args.from_file, encoding="utf-8").read())
        spark_fp = from_json(data["queries"])
        spark_tests = data["data_tests"]
        spark_indicators = spark_fp["official_indicators"]
        timings = data.get("timings_seconds", {})
        source = f"Databricks notebook run {data.get('run_id', '')} ({args.from_file})"
    else:
        spark_fp, spark_tests, spark_indicators, timings = run_local_spark(queries)
        source = "local Spark run on " + C.display_path(C.BRONZE_DIR)
    if args.time_duckdb:
        timings.update(time_duckdb())
    results = compare(queries, duck, spark_fp)
    # Synthetic data cannot match the published totals; there the official-indicator query is
    # still compared between the engines (official_indicators above), just not with the publication.
    official = {} if C.SAMPLE else {"DuckDB": check_official(duck["official_indicators"]),
                                    "Spark": check_official(spark_indicators)}
    summary = write_report(C.REPORTS_DIR, results, {"DuckDB": duck_tests, "Spark": spark_tests},
                           official, timings, source)
    print(f"Parity {'PASS' if summary['passed'] else 'FAIL'}: {summary['cells_compared']:,} cells, "
          f"{summary['mismatches']} mismatches, largest ratio difference {fmt(summary['max_ratio_diff'])}, "
          f"data tests DuckDB {summary['data_tests']['DuckDB']}, Spark {summary['data_tests']['Spark']} "
          f"-> {C.display_path(C.REPORTS_DIR / 'spark_parity.md')}")
    for k, v in timings.items():
        print(f"  {k}: {v} s")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
