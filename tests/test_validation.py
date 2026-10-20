"""Validation of gold against official figures, on synthetic data with made-up 'official' totals."""
import pandas as pd

from helpers import write_sample_raw
from pipeline.bronze import load_bronze
from pipeline.gold import build_gold
from pipeline.silver import build_silver
from pipeline.validation import indian, validate


def test_indian_number_format():
    assert [indian(n) for n in (215, 6117, 106302, 16364187, -4520)] == \
           ["215", "6,117", "1,06,302", "1,63,64,187", "-4,520"]


def test_validate_reports_matches_and_differences(tmp_path):
    write_sample_raw(tmp_path / "raw")
    load_bronze(tmp_path / "raw", tmp_path / "bronze", run_id="r1")
    build_silver(tmp_path / "bronze", tmp_path / "silver", tmp_path / "quarantine", run_id="r1")
    db = tmp_path / "udise.duckdb"
    build_gold(tmp_path / "silver", db, tmp_path / "parquet", tmp_path / "exports")
    # Synthetic silver has schools 101, 102, 104 with 1, 0 and 1 teachers.
    summary = validate(db, tmp_path / "reports", official={"2024-25": {"schools": 3, "teachers": 4}})
    assert summary == {"indicators_checked": 2, "exact_matches": 1, "max_abs_pct_difference": 50.0}
    table = pd.read_csv(tmp_path / "reports" / "validation.csv").set_index("indicator")
    assert table.loc["teachers", ["official", "ours", "difference"]].tolist() == [4, 2, -2]
    assert "1 of 2 published indicators match exactly" in (tmp_path / "reports" / "validation.md").read_text()
