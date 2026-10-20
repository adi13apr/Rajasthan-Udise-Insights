"""End to end: `python run_pipeline.py --sample` on synthetic data, in a temporary folder."""
import json
import os
import subprocess
import sys

import duckdb
import pytest

from pipeline import config as C


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("e2e")
    env = {**os.environ, "UDISE_SAMPLE_DIR": str(d / "out"), "UDISE_SAMPLE_RAW": str(d / "raw"),
           "PYTHONIOENCODING": "utf-8"}
    runs = [subprocess.run([sys.executable, "run_pipeline.py", "--sample"], cwd=C.ROOT, env=env,
                           capture_output=True, text=True) for _ in range(2)]
    for r in runs:
        assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads((d / "out" / "reports" / "run_manifest.json").read_text(encoding="utf-8"))
    expected = json.loads((d / "raw" / "expected.json").read_text(encoding="utf-8"))
    return d, manifest, expected, runs


def test_every_planted_problem_is_quarantined_by_the_right_rule(sample_run):
    _, manifest, expected, _ = sample_run
    for table, rules in expected["quarantine"].items():
        got = {r: n for r, n in manifest["stages"]["silver"]["tables"][table]["quarantined_by_rule"].items() if n}
        assert got == rules, table


def test_rows_reconcile_and_gold_tests_pass(sample_run):
    _, manifest, expected, _ = sample_run
    for r in manifest["stages"]["silver"]["reconciled"].values():
        assert r["bronze"] == r["silver_plus_quarantine"]
    gold = manifest["stages"]["gold"]
    assert gold["tests_failed"] == 0 and gold["tests_passed"] >= 30
    assert gold["dim_school"]["district_moves"] == expected["district_moves"]


def test_outputs_written_in_sample_folder_only(sample_run):
    d, manifest, expected, _ = sample_run
    out = d / "out"
    for path in ["gold/udise.duckdb", "gold/exports/mart_district_year.csv", "reports/validation.md",
                 "reports/qa_report.md", "reports/profiling/README.md", "quarantine/schools.parquet"]:
        assert (out / path).exists(), path
    con = duckdb.connect(str(out / "gold" / "udise.duckdb"), read_only=True)
    schools = dict(con.execute("select academic_year, count(*) from int_school_year group by 1").fetchall())
    con.close()
    assert schools == expected["schools_per_year_in_silver"]
    assert manifest["sample"] is True


def test_second_run_is_incremental(sample_run):
    _, manifest, _, runs = sample_run
    bronze = manifest["stages"]["bronze"]       # manifest of the second run
    assert bronze["files_loaded"] == 0 and bronze["files_skipped"] == bronze["files_total"] > 0
    assert "22 files loaded" in runs[0].stdout
