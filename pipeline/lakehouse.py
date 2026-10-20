"""Lakehouse copy (P10): silver and gold as Delta Lake tables.

    data/delta/silver/<table>/   partitioned by year (same layout as data/silver/)
    data/delta/gold/<table>/

Delta Lake = Parquet files + a transaction log (_delta_log/). Each pipeline run commits
a new version of every table (tagged with the run id), so:
  - a reader never sees a half-written table (atomic commits),
  - earlier versions stay readable ("time travel": DeltaTable(path, version=n)),
  - the schema is recorded and enforced on every write.
Spark, Databricks, DuckDB (delta extension) and Polars all read these tables.
The pipeline itself keeps using data/silver/ and data/gold/udise.duckdb; this stage
writes the Delta copy and checks every table's row count after writing.
"""
from pathlib import Path

import duckdb
from deltalake import CommitProperties, DeltaTable, write_deltalake

from . import config as C
from .profiling import read_sql
from .silver import TABLES as SILVER_TABLES

DELTA_DIR = C.DATA_DIR / "delta"
RETENTION_HOURS = 168  # vacuum: files no version has needed for 7 days are deleted


def _write(table, path: Path, run_id: str, partition_by: list[str] | None = None) -> dict:
    write_deltalake(str(path), table, mode="overwrite", partition_by=partition_by,
                    schema_mode="overwrite", commit_properties=CommitProperties(custom_metadata={"run_id": run_id}))
    dt = DeltaTable(str(path))
    rows = dt.to_pyarrow_dataset().count_rows()
    if rows != table.num_rows:
        raise RuntimeError(f"{path.name}: wrote {table.num_rows:,} rows but the Delta table has {rows:,}")
    dt.vacuum(retention_hours=RETENTION_HOURS, dry_run=False)
    return {"rows": rows, "version": dt.version()}


def write_lakehouse(silver_dir: Path = C.SILVER_DIR, gold_db: Path = C.GOLD_DB,
                    delta_dir: Path = DELTA_DIR, run_id: str = "") -> dict:
    out = {"silver": {}, "gold": {}}
    for table in SILVER_TABLES:
        folder = silver_dir / table
        if folder.exists():
            arrow = duckdb.sql(f"select * from {read_sql(folder.as_posix())}").to_arrow_table()
            out["silver"][table] = _write(arrow, delta_dir / "silver" / table, run_id, ["year"])
    con = duckdb.connect(str(gold_db), read_only=True)
    try:
        tables = [r[0] for r in con.execute("select table_name from information_schema.tables "
                                            "where table_schema = 'main' order by 1").fetchall()]
        for table in tables:
            out["gold"][table] = _write(con.execute(f"select * from {table}").to_arrow_table(),
                                        delta_dir / "gold" / table, run_id)
    finally:
        con.close()
    return out
