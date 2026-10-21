"""The statistical module's loader works on a synthetic gold database and never loads school codes."""
from helpers import PROFILE_ROWS_NEXT, write_sample_raw
from analysis.school_frame import load_districts, load_schools
from pipeline.bronze import load_bronze
from pipeline.gold import build_gold
from pipeline.silver import build_silver, read_silver


def test_load_schools_on_synthetic_gold(tmp_path):
    write_sample_raw(tmp_path / "raw")
    write_sample_raw(tmp_path / "raw", "2025-26", PROFILE_ROWS_NEXT)
    load_bronze(tmp_path / "raw", tmp_path / "bronze", run_id="r1")
    build_silver(tmp_path / "bronze", tmp_path / "silver", tmp_path / "quarantine", run_id="r1")
    db = tmp_path / "udise.duckdb"
    build_gold(tmp_path / "silver", db, tmp_path / "parquet", tmp_path / "exports")

    df = load_schools("2025-26", db, tmp_path / "silver")
    schools = read_silver("schools", tmp_path / "silver")
    assert len(df) == (schools["year"] == "2025-26").sum()
    assert "pseudocode" not in df.columns                       # school codes are never loaded
    assert {"enr_total", "total_tch", "infra_index", "location", "management_group"} <= set(df.columns)
    assert set(load_districts(db)["year"]) == {"2024-25", "2025-26"}
