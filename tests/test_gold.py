"""Gold star schema on two synthetic years, with a school that changes district."""
import duckdb
import pytest

from helpers import PROFILE_ROWS_NEXT, write_sample_raw
from pipeline.bronze import load_bronze
from pipeline.gold import build_gold
from pipeline.silver import build_silver


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    d = tmp_path_factory.mktemp("gold_run")
    write_sample_raw(d / "raw")
    write_sample_raw(d / "raw", "2025-26", PROFILE_ROWS_NEXT)
    load_bronze(d / "raw", d / "bronze", run_id="r1")
    build_silver(d / "bronze", d / "silver", d / "quarantine", run_id="r1")
    summary = build_gold(d / "silver", d / "gold" / "udise.duckdb", d / "gold" / "parquet")
    con = duckdb.connect(str(d / "gold" / "udise.duckdb"), read_only=True)
    yield summary, con, d
    con.close()


def test_all_sql_data_tests_pass(gold):
    summary, _, _ = gold
    assert summary["tests_failed"] == 0 and summary["tests_passed"] >= 20
    assert all(t["failures"] == 0 for t in summary["tests"])


def test_scd2_two_versions_for_school_that_moved(gold):
    _, con, _ = gold
    rows = con.execute("""select version, valid_from_year, valid_to_year, is_current, district_name
                          from dim_school join dim_district using (district_key)
                          where pseudocode = 101 order by version""").fetchall()
    assert rows == [(1, 2024, 2024, False, "KOTA"), (2, 2025, 2025, True, "BUNDI")]
    # schools that did not change keep one version covering both years
    assert con.execute("select valid_from_year, valid_to_year, version from dim_school "
                       "where pseudocode = 102").fetchall() == [(2024, 2025, 1)]


def test_facts_point_to_the_version_valid_that_year(gold):
    _, con, _ = gold
    rows = con.execute("""select y.academic_year, d.district_name, sum(f.students)
                          from fact_enrolment f join dim_school s using (school_key)
                          join dim_district d using (district_key) join dim_year y using (year_key)
                          where s.pseudocode = 101 group by all order by 1""").fetchall()
    assert rows == [("2024-25", "KOTA", 5), ("2025-26", "BUNDI", 5)]   # duplicate row not counted


def test_surrogate_keys_are_deterministic(gold, tmp_path):
    _, con, d = gold
    again = build_gold(d / "silver", tmp_path / "udise.duckdb", tmp_path / "parquet")
    con2 = duckdb.connect(str(tmp_path / "udise.duckdb"), read_only=True)
    q = "select school_key, pseudocode, version from dim_school order by school_key"
    assert con.execute(q).fetchall() == con2.execute(q).fetchall()
    con2.close()
    assert again["tables"] == gold[0]["tables"]


def test_unknown_management_gets_key_0(gold):
    _, con, _ = gold
    assert con.execute("select management_key from dim_school where pseudocode = 104").fetchall() == [(0,)]


def test_parquet_exports(gold):
    summary, con, d = gold
    for table, n in summary["tables"].items():
        assert duckdb.sql(f"select count(*) from '{(d / 'gold' / 'parquet' / f'{table}.parquet').as_posix()}'"
                          ).fetchone()[0] == n
