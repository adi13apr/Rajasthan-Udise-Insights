"""Export the gold star schema to CSV for Power BI: powerbi/data/*.csv

    python scripts/export_powerbi.py
    python scripts/export_powerbi.py --gold-db E:/rajasthan-udise-insights/data/gold/udise.duckdb

Reads the gold DuckDB file read-only (it never changes it) and writes:

    dim_year, dim_district, dim_management, dim_grade, dim_gender, dim_social_category,
    dim_school               the dimensions (dim_school without the school code)
    fact_enrolment, fact_teachers, fact_school_facilities
                             the facts, as in gold
    agg_school_year          helper table for the resource-gap measures (sql/powerbi/)
    official_totals.csv      published figures to validate against (reference/official_totals.json)
    norms.csv                teacher norms for the resource-gap measures (reference/norms.json)
    export_manifest.json     rows, columns and size of every file, and how long it took

Every CSV is written to a temporary name, its rows are counted back from the file and
compared with the source table, and only then renamed into place.

The facts and dim_school are school-level: under the UDISE+ data-sharing terms they are
not committed (powerbi/data/ is gitignored except the reference files and the manifest).
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import config as C  # noqa: E402

OUT_DIR = C.ROOT / "powerbi" / "data"
AGG_SQL = C.ROOT / "sql" / "powerbi" / "agg_school_year.sql"

# Power BI table name -> SELECT on the gold database.
TABLES = {
    "dim_year": "select * from dim_year",
    # aliases is a list in DuckDB; Power BI reads it as one text value
    "dim_district": "select district_key, district_name, array_to_string(aliases, '; ') as aliases "
                    "from dim_district",
    "dim_management": "select * from dim_management",
    "dim_grade": "select * from dim_grade",
    "dim_gender": "select * from dim_gender",
    "dim_social_category": "select * from dim_social_category",
    # pseudocode (the school code) is left out: Power BI joins on school_key and never needs it
    "dim_school": "select * exclude (pseudocode) from dim_school",
    "fact_enrolment": "select * from fact_enrolment",
    "fact_teachers": "select * from fact_teachers",
    "fact_school_facilities": "select * from fact_school_facilities",
    "agg_school_year": None,  # read from AGG_SQL
}

INDICATOR_LABELS = {
    "schools": "Schools",
    "enrolment": "Enrolment incl. pre-primary",
    "enrolment_classes_1_12": "Enrolment, classes 1-12",
    "teachers": "Teachers",
    "single_teacher_schools": "Single-teacher schools",
    "zero_enrolment_schools": "Zero-enrolment schools",
    "govt_schools": "Government schools",
    "govt_enrolment": "Government enrolment incl. pre-primary",
    "govt_teachers": "Government teachers",
}


def official_totals(years: list[str]) -> pd.DataFrame:
    """Every year x indicator; official_value is empty where nothing has been published."""
    ref = C.load_reference("official_totals.json")
    rows = [{"year": y, "indicator": k, "indicator_label": label,
             "official_value": ref["totals"].get(y, {}).get(k),
             "official_source": ref["source"] if k in ref["totals"].get(y, {}) else ""}
            for y in years for k, label in INDICATOR_LABELS.items()]
    out = pd.DataFrame(rows)
    out["official_value"] = out["official_value"].astype("Int64")
    return out


def norms() -> pd.DataFrame:
    return pd.DataFrame(C.load_reference("norms.json")["teachers"])


def export_table(con, name: str, query: str, out_dir: Path) -> dict:
    t0 = time.perf_counter()
    final, tmp = out_dir / f"{name}.csv", out_dir / f"{name}.csv.tmp"
    con.execute(f"copy ({query}) to '{tmp.as_posix()}' (header, delimiter ',')")
    expected = con.execute(f"select count(*) from ({query})").fetchone()[0]
    written = con.execute(f"select count(*) from read_csv('{tmp.as_posix()}', header = true)").fetchone()[0]
    if written != expected:
        tmp.unlink()
        raise RuntimeError(f"{name}: {written} rows in the CSV, {expected} in gold")
    tmp.replace(final)
    columns = con.execute(f"select * from ({query}) limit 0").description
    return {"rows": int(written), "columns": len(columns), "bytes": final.stat().st_size,
            "seconds": round(time.perf_counter() - t0, 2)}


def export(gold_db: Path = C.GOLD_DB, out_dir: Path = OUT_DIR) -> dict:
    if not gold_db.exists():
        raise FileNotFoundError(f"{gold_db} not found: run python run_pipeline.py first, or pass --gold-db")
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    con = duckdb.connect(str(gold_db), read_only=True)
    try:
        tables = {}
        for name, query in TABLES.items():
            query = query or AGG_SQL.read_text(encoding="utf-8")
            tables[name] = export_table(con, name, query, out_dir)
            print(f"  {name}: {tables[name]['rows']:,} rows, {tables[name]['seconds']} s")
        years = [r[0] for r in con.execute("select academic_year from dim_year order by start_year").fetchall()]
    finally:
        con.close()
    for name, df in [("official_totals", official_totals(years)), ("norms", norms())]:
        df.to_csv(out_dir / f"{name}.csv", index=False)
        tables[name] = {"rows": len(df), "columns": df.shape[1], "bytes": (out_dir / f"{name}.csv").stat().st_size}
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gold_db": C.display_path(gold_db),
        "seconds": round(time.perf_counter() - t0, 1),
        "tables": tables,
        "note": "School-level files (facts, dim_school, agg_school_year) stay local: UDISE+ data-sharing terms. "
                + C.SOURCE_NOTE,
    }
    (out_dir / "export_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gold-db", type=Path, default=C.GOLD_DB, help="gold DuckDB file (read-only)")
    p.add_argument("--out", type=Path, default=OUT_DIR, help="output folder")
    a = p.parse_args()
    m = export(a.gold_db, a.out)
    print(f"Exported {len(m['tables'])} files to {C.display_path(a.out)} in {m['seconds']} s")


if __name__ == "__main__":
    main()
