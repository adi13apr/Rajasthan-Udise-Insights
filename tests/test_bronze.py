"""Bronze layer tests on tiny synthetic zips (no real UDISE+ data)."""
import pandas as pd
import pytest

from helpers import make_zip
from pipeline.bronze import load_bronze, summarise_bronze
from pipeline.ingest import parse_name

META = ["_source_file", "_source_row", "_district_file", "_year", "_ingested_at", "_run_id"]

PROFILE = (
    "pseudocode,district,managment,lgd_ward_name\n"
    "0001,KOTA,1,dummy_ward\n"
    "0002,KOTA,9,\n"         # empty cell -> null
    "0003,KOTA,NA,x\n"       # literal text must survive as text
)


@pytest.fixture
def raw(tmp_path):
    r = tmp_path / "raw"
    make_zip(r / "2024-25" / "profile_data_1_KOTA_2024-25.zip", PROFILE)
    # Same schools again in a state-wide file: bronze must keep both copies.
    make_zip(r / "2024-25" / "profile_data_1_RAJASTHAN_2024-25.zip", PROFILE)
    make_zip(r / "2025-26" / "enrolment_data_1_PRATAPGARH (RAJ.)_2025-26.zip",
             "pseudocode,item_group,item_id,c1_b\n0001,1,1,5\n0001,1,2,-1\n")
    return r


def test_parse_name_handles_spaces_and_brackets(tmp_path):
    rf = parse_name(tmp_path / "enrolment_data_1_PRATAPGARH (RAJ.)_2024-25.zip")
    assert (rf.dataset, rf.district, rf.year) == ("enrolment1", "PRATAPGARH (RAJ.)", "2024-25")


def test_bronze_is_a_faithful_copy(raw, tmp_path):
    bronze = tmp_path / "bronze"
    result = load_bronze(raw, bronze, run_id="r1")
    out = bronze / "profile1" / "year=2024-25" / "profile_data_1_KOTA_2024-25.parquet"
    df = pd.read_parquet(out)

    assert list(df.columns) == ["pseudocode", "district", "managment", "lgd_ward_name"] + META
    assert df["pseudocode"].tolist() == ["0001", "0002", "0003"]   # leading zeros kept
    assert df["managment"].tolist() == ["1", "9", "NA"]            # no recoding, "NA" stays text
    assert df["lgd_ward_name"].isna().tolist() == [False, True, False]
    assert df["_source_row"].tolist() == [1, 2, 3]
    assert set(df["_run_id"]) == {"r1"} and set(df["_year"]) == {"2024-25"}
    assert result["files"][0]["columns"] == 4


def test_bronze_keeps_duplicates_and_counts_rows(raw, tmp_path):
    s = summarise_bronze(load_bronze(raw, tmp_path / "bronze", run_id="r1"))
    assert s["files_loaded"] == 3
    assert s["rows_by_dataset_year"] == {"profile1": {"2024-25": 6}, "enrolment1": {"2025-26": 2}}


def test_incremental_skips_unchanged_and_reloads_changed(raw, tmp_path):
    bronze = tmp_path / "bronze"
    load_bronze(raw, bronze, run_id="r1")

    second = load_bronze(raw, bronze, run_id="r2")
    assert {e["status"] for e in second["files"]} == {"skipped"}

    make_zip(raw / "2024-25" / "profile_data_1_KOTA_2024-25.zip", PROFILE + "0004,KOTA,1,y\n")
    third = load_bronze(raw, bronze, run_id="r3")
    status = {e["source"]: e["status"] for e in third["files"]}
    assert status["2024-25/profile_data_1_KOTA_2024-25.zip"] == "loaded"
    assert list(status.values()).count("skipped") == 2
    kota = pd.read_parquet(bronze / "profile1" / "year=2024-25" / "profile_data_1_KOTA_2024-25.parquet")
    assert len(kota) == 4 and set(kota["_run_id"]) == {"r3"}


def test_missing_bronze_file_is_reloaded_and_full_refresh_reloads_all(raw, tmp_path):
    bronze = tmp_path / "bronze"
    load_bronze(raw, bronze, run_id="r1")
    (bronze / "profile1" / "year=2024-25" / "profile_data_1_KOTA_2024-25.parquet").unlink()
    assert summarise_bronze(load_bronze(raw, bronze, run_id="r2"))["files_loaded"] == 1
    assert summarise_bronze(load_bronze(raw, bronze, run_id="r3", full_refresh=True))["files_loaded"] == 3


def test_duckdb_reads_year_partitions(raw, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    bronze = tmp_path / "bronze"
    load_bronze(raw, bronze, run_id="r1")
    glob = (bronze / "*" / "*" / "*.parquet").as_posix()
    rows = duckdb.sql(f"select year, count(*) from read_parquet('{glob}', hive_partitioning=true, "
                      f"union_by_name=true) where year = '2024-25' group by year").fetchall()
    assert rows == [("2024-25", 6)]
