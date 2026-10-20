"""Profiling tests: synthetic zips -> bronze -> profile, with deliberately bad values."""
import pandas as pd
import pytest

from helpers import make_zip
from pipeline.bronze import load_bronze
from pipeline.profiling import bronze_tables, profile_layer, write_readme


def col(name, kind, numeric=True, codes=None):
    return {"column": name, "type": "smallint" if numeric else "varchar", "numeric": numeric,
            "kind": kind, "codes": codes, "remarks": "Yes=1, No=2" if kind == "yes_no" else ""}


YES_NO = {"1": "Yes", "2": "No"}
SCHEMA = {
    "datasets": {
        "facility": {
            y: [col("pseudocode", "numeric"), col("toilets", "numeric"), col("library_yn", "yes_no", codes=YES_NO),
                col("rooms", "numeric"), col("documented_only", "numeric")]
            for y in ("2024-25", "2025-26")
        },
        "enrolment1": {y: [col("pseudocode", "numeric"), col("item_group", "numeric"),
                           col("item_id", "numeric"), col("c1_b", "numeric")] for y in ("2024-25",)},
    },
    "enrolment1_items": {"2024-25": [{"item_group": 1, "item_id": 1, "label": "General"}]},
}

# 2024-25: a duplicate school, a negative count, a non-number, yes/no codes 0 and 9,
# 'rooms' is 0 everywhere, an extra column not in the schema, 'documented_only' missing.
FAC_2024 = ("pseudocode,toilets,library_yn,rooms,extra\n"
            "1,2,1,0,a\n1,2,1,0,a\n2,-1,2,0,b\n3,x,0,0,\n4,1,9,0,\n")
FAC_2025 = ("pseudocode,toilets,library_yn,rooms,documented_only\n"
            "1,2,1,5,1\n2,1,2,7,1\n3,1,1,9,1\n4,0,2,3,1\n")
ENR_2024 = "pseudocode,item_group,item_id,c1_b\n1,1,1,5\n1,4,21,2\n"


@pytest.fixture
def profiled(tmp_path):
    raw, bronze, out = tmp_path / "raw", tmp_path / "bronze", tmp_path / "profiling"
    make_zip(raw / "2024-25" / "facility_data_RAJASTHAN_2024-25.zip", FAC_2024)
    make_zip(raw / "2025-26" / "facility_data_RAJASTHAN_2025-26.zip", FAC_2025)
    make_zip(raw / "2024-25" / "enrolment_data_1_KOTA_2024-25.zip", ENR_2024)
    load_bronze(raw, bronze, run_id="r1")
    res = profile_layer("bronze", bronze_tables(bronze), out, schema=SCHEMA)
    counts = write_readme({"bronze": res}, out)
    return res, counts, out


def row(res, table, column, year):
    p = res[table]["profile"]
    return p[(p["column"] == column) & (p["year"] == year)].iloc[0]


def test_basic_statistics(profiled):
    res, _, out = profiled
    r = row(res, "facility", "toilets", "2024-25")
    assert (r["rows"], r["null_count"], r["distinct_count"]) == (5, 0, 4)
    assert (r["min"], r["max"], r["negative_count"], r["non_numeric_count"]) == (-1, 2, 1, 1)
    assert row(res, "facility", "extra", "2024-25")["null_pct"] == 40.0
    assert (out / "bronze__facility.csv").exists()


def test_yes_no_codes_allow_9_but_not_0(profiled):
    res, _, _ = profiled
    r = row(res, "facility", "library_yn", "2024-25")
    assert r["invalid_count"] == 1 and r["invalid_values"] == "0 (1)"
    assert "1, 2, 9" == r["allowed_values"]


def test_problems_listed(profiled):
    res, _, out = profiled
    problems = lambda c, y: row(res, "facility", c, y)["problems"]
    assert "negative" in problems("toilets", "2024-25") and "non-numeric" in problems("toilets", "2024-25")
    assert problems("extra", "2024-25") == "in data, not in schema"
    assert problems("documented_only", "2024-25") == "in schema, not in data"
    assert "reporting change" in problems("rooms", "2024-25")  # 0 everywhere in one year only
    assert pd.isna(problems("pseudocode", "2025-26")) or problems("pseudocode", "2025-26") == ""
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "`library_yn` | 2024-25" in readme
    assert "`pseudocode`" not in readme  # key columns are not 'the same value in every row' problems


def test_one_row_per_column_and_year(profiled):
    res, _, _ = profiled
    p = res["facility"]["profile"]
    assert not p.duplicated(["column", "year"]).any()


def test_school_codes_never_written(profiled):
    res, _, out = profiled
    r = row(res, "facility", "pseudocode", "2024-25")
    assert r["top_values"].startswith("(hidden") and pd.isna(r["min"]) and r["distinct_count"] == 4
    written = pd.read_csv(out / "bronze__facility.csv")
    codes = written[written["column"] == "pseudocode"]
    assert (codes["top_values"] == "(hidden: school identifier)").all()
    assert codes["min"].isna().all() and codes["max"].isna().all()


def test_table_checks(profiled):
    res, _, _ = profiled
    fac = res["facility"]["checks"]
    assert fac["rows"] == {"2024-25": 5, "2025-26": 4}
    assert fac["duplicate_rows"] == {"2024-25": 1, "2025-26": 0}
    assert res["enrolment1"]["checks"]["undocumented_items"] == [
        {"year": "2024-25", "item_group": 4, "item_id": 21, "rows": 1}]
