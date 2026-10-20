"""Silver layer and quality rules on a synthetic dataset with planted problems."""
import numpy as np
import pandas as pd
import pytest

from helpers import write_sample_raw
from pipeline import config as C
from pipeline.bronze import load_bronze
from pipeline.build import build_school_year
from pipeline.rules import RULES, rules_markdown
from pipeline.silver import (build_silver, facility_flags, normalise_district, read_silver,
                             tables_for_build, yes_no)


@pytest.fixture(scope="module")
def silver(tmp_path_factory):
    d = tmp_path_factory.mktemp("silver_run")
    write_sample_raw(d / "raw")
    load_bronze(d / "raw", d / "bronze", run_id="r1")
    summary = build_silver(d / "bronze", d / "silver", d / "quarantine", run_id="r1")
    return summary, d


def quarantine(d, table):
    return pd.read_parquet(d / "quarantine" / f"{table}.parquet")


# --- Unit tests of the cleaning functions --------------------------------------

def test_normalise_district():
    s = pd.Series([" jalor ", "PRATAPGARH (RAJ.)", "Sawai  Madhopur", "KOTA"])
    assert normalise_district(s).tolist() == ["JALORE", "PRATAPGARH", "SAWAI MADHOPUR", "KOTA"]


def test_yes_no_treats_9_as_missing_never_no():
    out = yes_no(pd.Series([1, 2, 9, 3]))
    assert out.iloc[0] == 1 and out.iloc[1] == 0 and out.iloc[2:].isna().all()


def test_classroom_rebuild():
    fac = pd.DataFrame({"electricity_availability": [1], "total_girls_func_toilet": [1],
                        "func_girls_cwsn_friendly": [0], "total_boys_func_toilet": [0],
                        "func_boys_cwsn_friendly": [0], "handwash_near_toilet": [1],
                        "library_availability": [1], "playground_available": [1],
                        "availability_ramps": [1], "internet": [2], "laptop": [0], "desktop": [0],
                        "tablet": [0], "classrooms_in_good_condition": [4],
                        "classrooms_needs_minor_repair": [2], "classrooms_needs_major_repair": [2],
                        **{c: [2] for c in C.SAFE_WATER_COLS}})
    f = facility_flags(fac)
    assert f.loc[0, "classrooms"] == 8 and f.loc[0, "pct_classrooms_major_repair"] == 25.0
    assert f.loc[0, "has_internet"] == 0 and f.loc[0, "has_safe_water"] == 0


# --- Rules: every planted problem lands in the right place ----------------------

def test_every_bronze_row_is_in_silver_or_quarantine(silver):
    summary, _ = silver
    for dataset, r in summary["reconciled"].items():
        assert r["bronze"] == r["silver_plus_quarantine"], dataset


def test_school_errors_quarantined_with_reason(silver):
    _, d = silver
    q = quarantine(d, "schools").set_index("pseudocode")
    assert q.loc[103, "_rule"] == "unknown_district" and "NOWHERE" in q.loc[103, "_reason"]
    assert q.loc[105, "_rule"] == "missing_required" and q.loc[105, "_reason"] == "missing district"
    assert q.loc[101, "_rule"] == "duplicate_school_year"
    assert q.loc[101, "district_file"] == "KOTA"            # the Kota test copy is the one quarantined
    assert set(q["_run_id"]) == {"r1"}
    assert sorted(read_silver("schools", d / "silver")["pseudocode"]) == [101, 102, 104]


def test_child_table_errors(silver):
    _, d = silver
    fac = quarantine(d, "facilities").set_index("pseudocode")
    assert fac.loc[103, "_rule"] == "orphan_school"
    assert fac.loc[104, "_rule"] == "not_a_number" and fac.loc[104, "_reason"] == "laptop=x"
    tch = quarantine(d, "teachers").set_index("pseudocode")
    assert tch.loc[106, "_rule"] == "negative_count" and tch.loc[106, "_reason"] == "total_tch=-2"
    enr = quarantine(d, "enrolment")
    assert sorted(enr["_rule"]) == ["duplicate_enrolment_row", "orphan_school"]


def test_warnings_flag_but_keep_rows(silver):
    _, d = silver
    sch = read_silver("schools", d / "silver").set_index("pseudocode")
    assert sch.loc[104, "flag_unknown_management"] == 1 and sch.loc[104, "management_group"] == "Unmapped"
    assert sch.loc[104, "flag_invalid_code"] == 1 and sch.loc[102, "flag_invalid_code"] == 0  # 9 is valid
    fac = read_silver("facilities", d / "silver").set_index("pseudocode")
    assert fac.loc[101, "flag_invalid_code"] == 1 and fac.loc[101, "solar_panel"] == 3   # raw code kept
    assert np.isnan(fac.loc[101, "has_handwash"])                                        # 9 -> missing
    assert fac.loc[102, "flag_functional_toilets_exceed_total"] == 1
    tch = read_silver("teachers", d / "silver").set_index("pseudocode")
    assert tch.loc[102, "flag_no_teachers_with_students"] == 1 and tch.loc[104, "flag_high_ptr"] == 1
    oth = read_silver("enrolment_other", d / "silver").set_index("item_id")
    assert oth.loc[21, "flag_undocumented_item"] == 1 and oth.loc[1, "flag_undocumented_item"] == 0


def test_standardised_school_columns(silver):
    _, d = silver
    sch = read_silver("schools", d / "silver").set_index("pseudocode")
    assert (sch.loc[102, "district"], sch.loc[102, "district_raw"]) == ("JALORE", "JALOR")
    assert sch.loc[104, "block_is_admin_region"] and pd.isna(sch.loc[104, "block_clean"])
    assert sch.loc[102, "location"] == "Urban" and sch.loc[101, "is_govt"] == 1


def test_build_reads_silver(silver):
    _, d = silver
    school = build_school_year(tables_for_build(d / "silver")).set_index("pseudocode")
    assert school.loc[101, "enr_total"] == 5   # duplicate enrolment row was not double counted
    assert school.loc[104, "ptr"] == 100


def test_quality_rules_doc_is_up_to_date():
    assert (C.ROOT / "docs" / "quality_rules.md").read_text(encoding="utf-8") == rules_markdown()
    assert len({r.name for r in RULES}) == len(RULES)
