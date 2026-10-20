"""The SQL marts reproduce the pandas district summary they replaced (P5 regression test)."""
import duckdb
import numpy as np
import pandas as pd
import pytest

from helpers import PROFILE_ROWS_NEXT, write_sample_raw
from pipeline import config as C
from pipeline.bronze import load_bronze
from pipeline.build import build_school_year, summarise
from pipeline.gold import build_gold
from pipeline.silver import build_silver, tables_for_build

REFERENCE = C.ROOT / "tests" / "regression"
MARTS = [("mart_district_year", ["district", "year"], "district_year_pandas.csv"),
         ("mart_district_year_management", ["district", "year", "management_group"],
          "district_year_by_management_pandas.csv")]


def assert_same(mart: pd.DataFrame, ref: pd.DataFrame, key: list[str]):
    """Same rows and columns; numbers equal to within 0.001 (the last rounded digit:
    DuckDB rounds halves away from zero, pandas rounds them to even)."""
    mart = mart.sort_values(key).reset_index(drop=True)
    ref = ref.sort_values(key).reset_index(drop=True)
    assert mart[key].astype(str).values.tolist() == ref[key].astype(str).values.tolist()
    cols = [c for c in ref.columns if c not in key]
    a = mart[cols].astype(float).replace([np.inf, -np.inf], np.nan)  # pandas gives inf for x/0,
    b = ref[cols].astype(float).replace([np.inf, -np.inf], np.nan)   # SQL gives NULL; both undefined
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy(), rtol=0, atol=0.001 + 1e-9, equal_nan=True)


def test_marts_match_pandas_on_synthetic_data(tmp_path):
    raw = tmp_path / "raw"
    write_sample_raw(raw)
    write_sample_raw(raw, "2025-26", PROFILE_ROWS_NEXT)
    load_bronze(raw, tmp_path / "bronze", run_id="r1")
    build_silver(tmp_path / "bronze", tmp_path / "silver", tmp_path / "quarantine", run_id="r1")
    build_gold(tmp_path / "silver", tmp_path / "udise.duckdb", tmp_path / "parquet", tmp_path / "exports")
    school = build_school_year(tables_for_build(tmp_path / "silver"))
    con = duckdb.connect(str(tmp_path / "udise.duckdb"), read_only=True)
    for mart, key, _ in MARTS:
        expected = summarise(school, key)
        got = con.execute(f"select * from {mart}").df()[expected.columns]
        assert_same(got, expected, key)
    con.close()


@pytest.mark.skipif(not C.GOLD_DB.exists(), reason="needs the real gold database (local run only)")
@pytest.mark.parametrize("mart,key,reference", MARTS)
def test_marts_match_frozen_pandas_output_on_real_data(mart, key, reference):
    ref = pd.read_csv(REFERENCE / reference)
    con = duckdb.connect(str(C.GOLD_DB), read_only=True)
    got = con.execute(f"select * from {mart}").df()[ref.columns]
    con.close()
    assert_same(got, ref, key)
