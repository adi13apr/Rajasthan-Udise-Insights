"""Benchmark: the same district-level query on raw CSV, bronze Parquet and the gold star schema.

    python scripts/benchmark.py            (after python run_pipeline.py)

Query: Kota enrolment by grade and gender, 2025-26 (all social categories, i.e.
Enrolment_1 item_group 1; a school is in Kota if its 2025-26 profile says so).
Approaches, all in DuckDB:
  1. raw CSV        the CSVs of 2025-26 extracted from the zips to data/tmp/ (gitignored),
                    read with read_csv_auto (types guessed on every read)
  2. bronze, no pruning   all bronze Parquet files, year filtered on a column inside the files
  3. bronze, pruned       bronze Parquet with the Hive partition filter year = '2025-26':
                          files of other years are never opened
  4. gold                 fact_enrolment joined to its dimensions in data/gold/udise.duckdb
Each approach runs COLD (a new DuckDB connection per run, so no DuckDB caches; the
operating system's file cache is not cleared, which cannot be done safely on Windows)
and WARM (repeated in one connection after one untimed run). Medians are reported.
All approaches must return the same rows, or the script stops.
Output: reports/benchmark.md and reports/benchmark.json.
"""
import json
import platform
import shutil
import statistics
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import config as C  # noqa: E402

YEAR, DISTRICT = "2025-26", "KOTA"
TMP = ROOT / "data" / "tmp" / "benchmark_csv"
COLD_RUNS, WARM_RUNS = 5, 10
SCAN_RUNS = 25  # the scans take ~10 ms, so more runs are needed to see past the noise
GRADE_GENDER = """upper(regexp_extract(col, '^c(pp|[0-9]+)_', 1)) as grade,
                  upper(right(col, 1)) as gender"""

QUERIES = {
    "1. raw CSV (read_csv_auto)": f"""
        with kota as (select distinct pseudocode from read_csv_auto('{{tmp}}/profile_*.csv')
                      where district = '{DISTRICT}'),
        enr as (select * from read_csv_auto('{{tmp}}/enrolment_*.csv', union_by_name = true)
                where item_group = 1 and pseudocode in (select pseudocode from kota)),
        long as (unpivot enr on columns('^c(pp|[0-9]+)_[bgt]$') into name col value students)
        select {GRADE_GENDER}, sum(students)::bigint as students
        from long group by all having sum(students) > 0""",
    "2. bronze Parquet, no partition pruning": f"""
        with kota as (select distinct pseudocode
                      from read_parquet('{{bronze}}/profile1/*/*.parquet', hive_partitioning = false,
                                        union_by_name = true)
                      where _year = '{YEAR}' and district = '{DISTRICT}'),
        enr as (select * exclude (_source_file, _source_row, _district_file, _year, _ingested_at, _run_id)
                from read_parquet('{{bronze}}/enrolment1/*/*.parquet', hive_partitioning = false,
                                  union_by_name = true)
                where _year = '{YEAR}' and item_group = '1' and pseudocode in (select pseudocode from kota)),
        long as (unpivot enr on columns('^c(pp|[0-9]+)_[bgt]$') into name col value students)
        select {GRADE_GENDER}, sum(students::bigint)::bigint as students
        from long group by all having sum(students::bigint) > 0""",
    "3. bronze Parquet, partition pruning (year=)": f"""
        with kota as (select distinct pseudocode
                      from read_parquet('{{bronze}}/profile1/*/*.parquet', hive_partitioning = true,
                                        union_by_name = true)
                      where year = '{YEAR}' and district = '{DISTRICT}'),
        enr as (select * exclude (_source_file, _source_row, _district_file, _year, _ingested_at, _run_id, year)
                from read_parquet('{{bronze}}/enrolment1/*/*.parquet', hive_partitioning = true,
                                  union_by_name = true)
                where year = '{YEAR}' and item_group = '1' and pseudocode in (select pseudocode from kota)),
        long as (unpivot enr on columns('^c(pp|[0-9]+)_[bgt]$') into name col value students)
        select {GRADE_GENDER}, sum(students::bigint)::bigint as students
        from long group by all having sum(students::bigint) > 0""",
    "4. gold star schema": f"""
        select g.grade_code as grade, ge.gender_code as gender, sum(f.students)::bigint as students
        from fact_enrolment f
        join dim_school s    using (school_key)
        join dim_district d  on d.district_key = s.district_key
        join dim_year y      using (year_key)
        join dim_grade g     using (grade_key)
        join dim_gender ge   using (gender_key)
        where d.district_name = '{DISTRICT}' and y.academic_year = '{YEAR}'
        group by all""",
}


# Isolating the scan: four ways to read only the 2025-26 bronze enrolment rows.
SCANS = {
    "only the year=2025-26 folder (path points at the partition)":
        "read_parquet('{e}/year=2025-26/*.parquet', union_by_name = true)",
    "all files, filter on the _year column inside the files":
        "read_parquet('{e}/*/*.parquet', union_by_name = true, hive_partitioning = false) where _year = '2025-26'",
    "all files, partition filter year = (no union_by_name: loses columns)":
        "read_parquet('{e}/*/*.parquet', hive_partitioning = true) where year = '2025-26'",
    "all files, partition filter year = + union_by_name":
        "read_parquet('{e}/*/*.parquet', hive_partitioning = true, union_by_name = true) where year = '2025-26'",
}


def scan_benchmark() -> dict:
    out = {}
    for name, src in SCANS.items():
        sql = "select count(*) from " + src.format(e=(C.BRONZE_DIR / "enrolment1").as_posix())
        runs = []
        for _ in range(SCAN_RUNS):
            con = duckdb.connect()
            seconds, rows = timed(con, sql)
            con.close()
            runs.append(seconds)
        q = statistics.quantiles(runs, n=4)
        out[name] = {"cold_median_ms": round(statistics.median(runs) * 1000, 1),
                     "p25_ms": round(q[0] * 1000, 1), "p75_ms": round(q[2] * 1000, 1), "rows": rows[0][0]}
    return out


def extract_csvs() -> int:
    """Copy the 2025-26 profile and enrolment CSVs out of the raw zips (raw zips untouched)."""
    if TMP.exists():
        shutil.rmtree(TMP)  # data/tmp is scratch space for this script
    TMP.mkdir(parents=True)
    total = 0
    for z in sorted((C.RAW_DIR / YEAR).glob("*.zip")):
        prefix = ("profile" if z.name.startswith("profile_data_1") else
                  "enrolment" if z.name.startswith("enrolment_data_1") else None)
        if not prefix:
            continue
        with zipfile.ZipFile(z) as zf:
            name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
            out = TMP / f"{prefix}_{z.stem}.csv"
            out.write_bytes(zf.read(name))
            total += out.stat().st_size
    return total


def connect(approach: str):
    if approach.startswith("4."):
        con = duckdb.connect(str(C.GOLD_DB), read_only=True)
    else:
        con = duckdb.connect()
    con.execute("set enable_progress_bar = false")
    return con


def sql_for(approach: str) -> str:
    return QUERIES[approach].format(tmp=TMP.as_posix(), bronze=C.BRONZE_DIR.as_posix())


def timed(con, sql: str) -> tuple[float, list]:
    t0 = time.perf_counter()
    rows = con.execute(sql).fetchall()
    return time.perf_counter() - t0, sorted(rows)


def machine() -> dict:
    cpu = platform.processor()
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except (ImportError, OSError):
        pass
    return {"cpu": cpu, "logical_cores": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "os": platform.platform(), "python": platform.python_version(), "duckdb": duckdb.__version__,
            "duckdb_threads": duckdb.sql("select current_setting('threads')").fetchone()[0]}


def sizes(csv_bytes: int) -> dict:
    def mb(*globs):
        return round(sum(p.stat().st_size for g in globs for p in g) / 1e6, 1)
    return {"1": f"{csv_bytes / 1e6:.1f} MB of CSV (2025-26 profile + enrolment)",
            "2": f"{mb((C.BRONZE_DIR / 'enrolment1').rglob('*.parquet'), (C.BRONZE_DIR / 'profile1').rglob('*.parquet'))} MB of Parquet (both years)",
            "3": f"{mb((C.BRONZE_DIR / 'enrolment1' / f'year={YEAR}').glob('*.parquet'), (C.BRONZE_DIR / 'profile1' / f'year={YEAR}').glob('*.parquet'))} MB of Parquet in the 2025-26 partition",
            "4": f"{C.GOLD_DB.stat().st_size / 1e6:.1f} MB DuckDB file (all gold tables)"}


def main():
    if not C.GOLD_DB.exists():
        raise SystemExit("Run python run_pipeline.py first (needs bronze and gold).")
    csv_bytes = extract_csvs()
    results, answers = {}, {}
    for approach in QUERIES:
        sql = sql_for(approach)
        cold = []
        for _ in range(COLD_RUNS):
            con = connect(approach)
            seconds, rows = timed(con, sql)
            con.close()
            cold.append(seconds)
        con = connect(approach)
        timed(con, sql)  # untimed warm-up
        warm = [timed(con, sql)[0] for _ in range(WARM_RUNS)]
        con.close()
        answers[approach] = rows
        results[approach] = {"cold_median_ms": round(statistics.median(cold) * 1000, 1),
                             "cold_min_ms": round(min(cold) * 1000, 1),
                             "warm_median_ms": round(statistics.median(warm) * 1000, 1),
                             "warm_min_ms": round(min(warm) * 1000, 1)}
        print(f"{approach}: cold {results[approach]['cold_median_ms']} ms, warm {results[approach]['warm_median_ms']} ms")

    first = next(iter(answers.values()))
    if any(a != first for a in answers.values()):
        raise SystemExit("Approaches returned different results; benchmark not written.")
    total = sum(r[2] for r in first)
    scans = scan_benchmark()

    info = machine()
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "machine": info,
           "query": f"{DISTRICT} enrolment by grade and gender, {YEAR}", "result_rows": len(first),
           "result_total_students": total, "cold_runs": COLD_RUNS, "warm_runs": WARM_RUNS,
           "results": results, "data_read": sizes(csv_bytes), "bronze_scans": scans}
    (C.REPORTS_DIR / "benchmark.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    (C.REPORTS_DIR / "benchmark.md").write_text(to_markdown(out), encoding="utf-8")
    print(f"All approaches returned the same {len(first)} rows ({total:,} students). "
          f"Written reports/benchmark.md")


def to_markdown(o: dict) -> str:
    m = o["machine"]
    base = o["results"]["1. raw CSV (read_csv_auto)"]
    lines = ["# Performance benchmark", "",
             f"Generated by `scripts/benchmark.py` on {o['generated_at'][:10]}. Real timings from one machine;",
             "they will differ on other hardware.", "",
             f"**Query:** {o['query']}. Every approach returned the same {o['result_rows']} rows "
             f"({o['result_total_students']:,} students), checked by the script.", "",
             f"**Machine:** {m['cpu']}, {m['logical_cores']} logical cores, {m['ram_gb']} GB RAM, "
             f"{m['os']}; Python {m['python']}, DuckDB {m['duckdb']} ({m['duckdb_threads']} threads).", "",
             f"**Method:** cold = new DuckDB connection for each of {o['cold_runs']} runs (DuckDB caches empty; "
             f"the operating system's file cache was not cleared). Warm = {o['warm_runs']} runs in one "
             "connection after one untimed run. Medians, with the fastest run in brackets.", "",
             "| Approach | Data it reads | Cold median (ms) | Warm median (ms) | Cold vs raw CSV |",
             "|---|---|---:|---:|---:|"]
    for i, (name, r) in enumerate(o["results"].items(), start=1):
        speed = base["cold_median_ms"] / r["cold_median_ms"]
        lines.append(f"| {name} | {o['data_read'][str(i)]} | {r['cold_median_ms']} ({r['cold_min_ms']}) | "
                     f"{r['warm_median_ms']} ({r['warm_min_ms']}) | {speed:.1f}x |")
    lines += ["", "## What makes the difference", "",
              "- **Column pruning (Parquet, DuckDB tables).** Columnar storage lets a query read only the "
              "columns it uses. A CSV has to be read and split line by line, every column.",
              "- **Types decided once.** `read_csv_auto` guesses column types on every read; bronze "
              "stores text (typed in the query), silver and gold store real integer types.",
              "- **Partition pruning.** Bronze is stored as `year=<YYYY-YY>/` folders, so a filter on the "
              "partition column lets DuckDB skip the other year's data. Its effect at this data size is "
              "measured separately below.",
              "- **Modelling.** Gold has already done the work: the unpivot to one row per grade and "
              "gender, typing, de-duplication (the Kota test copies), and the school's district for each "
              "year (SCD2). The query is a join of a narrow fact table to small dimensions.", "",
              "## Partition pruning, isolated", "",
              f"Counting the 2025-26 rows of bronze enrolment, four ways ({SCAN_RUNS} cold runs each; "
              "the middle half of the runs fell between p25 and p75):", "",
              "| How the files are read | Rows | Cold median (ms) | p25-p75 (ms) |", "|---|---:|---:|---:|"]
    lines += [f"| {k} | {v['rows']:,} | {v['cold_median_ms']} | {v['p25_ms']}-{v['p75_ms']} |"
              for k, v in o["bronze_scans"].items()]
    r2 = o["results"]["2. bronze Parquet, no partition pruning"]["cold_median_ms"]
    r3 = o["results"]["3. bronze Parquet, partition pruning (year=)"]["cold_median_ms"]
    medians = [v["cold_median_ms"] for v in o["bronze_scans"].values()]
    verdict = (f"In this run the full query with the partition filter (approach 3, {r3} ms) was "
               + ("faster" if r3 < r2 else "**not** faster") + f" than without it (approach 2, {r2} ms).")
    lines += ["",
              f"All four scans take {min(medians)}-{max(medians)} ms, and their order changes from one "
              f"run of this script to the next. {verdict} Conclusion: with about 11 MB per year, "
              "partition pruning makes no meaningful difference here; it pays off when partitions are "
              "large (many years, or many states). What does make a difference at this size is the "
              "storage format and the modelling (table above: up to "
              f"{base['cold_median_ms'] / o['results']['4. gold star schema']['cold_median_ms']:.0f}x).", "",
              "## A correctness trap found by this benchmark", "",
              "The first version of approach 3 left out `union_by_name = true`. DuckDB then takes the "
              "column list from the first file it finds, a 2024-25 file, which has no transgender "
              "columns (they start in 2025-26). The query ran without error but silently returned one "
              "student fewer (Kota's only transgender student in classes 1-12). The script compares the "
              "answers of all approaches before reporting any timing, which is how this was caught. "
              "The pipeline always reads multi-year Parquet with `union_by_name = true`.", "",
              "Raw CSVs were extracted from the zips into `data/tmp/` (gitignored) for approach 1; the "
              "zips in `data/raw/` were only read.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
