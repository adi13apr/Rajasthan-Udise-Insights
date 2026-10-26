"""The parity comparison itself (spark/parity.py): it must catch every kind of difference.
Pure pandas, so it runs without Spark."""
import pandas as pd

from spark.parity import RATIO_TOLERANCE, Query, compare_query

Q = Query("q", "", ["district", "year"], {"share"})


def frame(rows):
    return pd.DataFrame(rows, columns=["district", "year", "schools", "share"])


BASE = [("AJMER", "2024-25", 100, 0.500), ("KOTA", "2024-25", 50, 0.250)]


def problems(duck_rows, spark_rows):
    return compare_query(Q, frame(duck_rows), frame(spark_rows))


def test_identical_results_pass():
    r = problems(BASE, BASE)
    assert r["mismatches"] == [] and r["rows"] == 2 and r["cells"] == 4 and r["max_ratio_diff"] == 0


def test_whole_number_must_match_exactly():
    r = problems(BASE, [("AJMER", "2024-25", 101, 0.500), BASE[1]])
    assert [(m["column"], m["duckdb"], m["spark"], m["kind"]) for m in r["mismatches"]] == \
        [("schools", 100, 101, "exact")]


def test_ratio_within_tolerance_passes_and_beyond_fails():
    ok = problems(BASE, [("AJMER", "2024-25", 100, 0.500 + RATIO_TOLERANCE), BASE[1]])
    assert ok["mismatches"] == [] and abs(ok["max_ratio_diff"] - RATIO_TOLERANCE) < 1e-12
    bad = problems(BASE, [("AJMER", "2024-25", 100, 0.502), BASE[1]])
    assert [m["column"] for m in bad["mismatches"]] == ["share"]


def test_missing_rows_and_nulls_are_mismatches():
    r = problems(BASE, BASE[:1])
    assert r["mismatches"][0]["problem"] == "row only in DuckDB"
    r = problems(BASE, [("AJMER", "2024-25", 100, None), BASE[1]])
    assert [m["column"] for m in r["mismatches"]] == ["share"]


def test_different_columns_are_a_mismatch():
    r = compare_query(Q, frame(BASE), frame(BASE).drop(columns="share"))
    assert r["mismatches"][0]["problem"] == "columns differ"
