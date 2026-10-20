"""Gold layer: a DuckDB star schema built from silver by the SQL files in sql/.

    data/gold/udise.duckdb      dimensions + facts (+ marts from P5)
    data/gold/parquet/*.parquet the same tables exported as Parquet

All SQL lives in sql/ as numbered files, run in name order. They read silver
through temporary views (src_schools, src_enrolment, ...) and reference data
through temporary tables (ref_districts, ref_district_aliases, ref_management),
so the saved database holds only gold tables.

sql/tests/*.sql are data tests: each returns (check_name, failures) rows, and
every failures value must be 0 (referential integrity, grain, SCD2, totals).
The database is built in a temporary file and only replaces the previous one
when every test passes.
"""
import os
import time
from pathlib import Path

import duckdb
import pandas as pd

from . import config as C
from .profiling import read_sql
from .silver import TABLES as SILVER_TABLES

SQL_DIR = C.ROOT / "sql"


def attach_sources(con, silver_dir: Path = C.SILVER_DIR) -> None:
    """Temporary views over silver and temporary reference tables (not saved in the database)."""
    for table in SILVER_TABLES:
        con.execute(f"create or replace temp view src_{table} as "
                    f"select * from {read_sql((silver_dir / table).as_posix())}")
    districts = pd.DataFrame({"district_name": C.DISTRICTS})
    aliases = pd.DataFrame(list(C.DISTRICT_ALIASES.items()), columns=["alias", "district_name"])
    mgmt = C.load_reference("management_codes.json")["codes"]
    management = pd.DataFrame([{"code": int(k), "management_group": v["group"],
                                "is_government": v["is_government"]} for k, v in mgmt.items()])
    for name, df in [("ref_districts", districts), ("ref_district_aliases", aliases),
                     ("ref_management", management)]:
        con.register(f"_{name}", df)
        con.execute(f"create or replace temp table {name} as select * from _{name}")
        con.unregister(f"_{name}")


def run_sql_files(con, folder: Path) -> dict[str, float]:
    """Run every *.sql file in the folder in name order. Returns seconds per file."""
    timings = {}
    for path in sorted(folder.glob("*.sql")):
        t0 = time.perf_counter()
        con.execute(path.read_text(encoding="utf-8"))
        timings[path.name] = round(time.perf_counter() - t0, 3)
    return timings


def run_sql_tests(con, folder: Path = SQL_DIR / "tests") -> list[dict]:
    results = []
    for path in sorted(folder.glob("*.sql")):
        for check, failures in con.execute(path.read_text(encoding="utf-8")).fetchall():
            results.append({"file": path.name, "check": check, "failures": int(failures)})
    return results


def table_names(con) -> list[str]:
    return [r[0] for r in con.execute(
        "select table_name from information_schema.tables "
        "where table_schema = 'main' and table_type = 'BASE TABLE' order by table_name").fetchall()]


def build_gold(silver_dir: Path = C.SILVER_DIR, gold_path: Path = C.GOLD_DB,
               parquet_dir: Path = C.GOLD_PARQUET_DIR, sql_dirs: tuple[Path, ...] = (SQL_DIR,)) -> dict:
    """Build the star schema (and anything else in sql_dirs), test it, export it."""
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = gold_path.with_name(gold_path.stem + ".building.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        attach_sources(con, silver_dir)
        timings = {}
        for folder in sql_dirs:
            timings.update(run_sql_files(con, folder))
        tests = run_sql_tests(con)
        failed = [t for t in tests if t["failures"]]
        if failed:
            raise RuntimeError("gold data tests failed: " + "; ".join(
                f"{t['check']} ({t['failures']:,})" for t in failed))
        tables = table_names(con)
        rows = {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
        parquet_dir.mkdir(parents=True, exist_ok=True)
        for t in tables:
            con.execute(f"copy {t} to '{(parquet_dir / f'{t}.parquet').as_posix()}' "
                        f"(format parquet, compression zstd)")
        scd2 = scd2_summary(con)
    finally:
        con.close()
    os.replace(tmp, gold_path)  # only a fully built and tested database replaces the old one
    return {"tables": rows, "sql_seconds": timings, "tests": tests,
            "tests_passed": len(tests), "tests_failed": 0, "dim_school": scd2}


def scd2_summary(con) -> dict:
    """How many schools have more than one version, and which district moves caused them."""
    multi = con.execute("select count(*) from (select pseudocode from dim_school "
                        "group by pseudocode having count(*) > 1)").fetchone()[0]
    moves = con.execute("""
        select a.district_name || ' -> ' || b.district_name as move, count(*) as schools
        from dim_school v1
        join dim_school v2 on v2.pseudocode = v1.pseudocode and v2.version = v1.version + 1
        join dim_district a on a.district_key = v1.district_key
        join dim_district b on b.district_key = v2.district_key
        where v1.district_key <> v2.district_key
        group by all order by schools desc, move""").fetchall()
    return {"schools_with_more_than_one_version": multi,
            "schools_changing_district": sum(n for _, n in moves),
            "district_moves": dict(moves)}
