"""Delta Lake copy: versions, time travel, partitions, run id in the history."""
from deltalake import DeltaTable

from helpers import write_sample_raw
from pipeline.bronze import load_bronze
from pipeline.gold import build_gold
from pipeline.lakehouse import write_lakehouse
from pipeline.silver import build_silver


def test_delta_tables_versioned_and_time_travel(tmp_path):
    write_sample_raw(tmp_path / "raw")
    load_bronze(tmp_path / "raw", tmp_path / "bronze", run_id="r1")
    build_silver(tmp_path / "bronze", tmp_path / "silver", tmp_path / "quarantine", run_id="r1")
    db = tmp_path / "udise.duckdb"
    build_gold(tmp_path / "silver", db, tmp_path / "parquet", tmp_path / "exports")

    first = write_lakehouse(tmp_path / "silver", db, tmp_path / "delta", run_id="run-1")
    second = write_lakehouse(tmp_path / "silver", db, tmp_path / "delta", run_id="run-2")
    assert first["silver"]["schools"] == {"rows": 3, "version": 0}
    assert second["silver"]["schools"]["version"] == 1
    assert set(second["gold"]) >= {"dim_school", "fact_enrolment", "mart_district_year"}

    path = str(tmp_path / "delta" / "silver" / "schools")
    assert DeltaTable(path).metadata().partition_columns == ["year"]
    old = DeltaTable(path, version=0).to_pyarrow_table()
    assert old.num_rows == 3                                      # time travel to the first run
    runs = [h.get("run_id") for h in DeltaTable(path).history()]
    assert runs[:2] == ["run-2", "run-1"]                          # newest first
