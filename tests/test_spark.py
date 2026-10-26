"""PySpark transforms (spark/) on synthetic data, checked against the DuckDB pipeline.

Skipped when pyspark or Java is not installed (the main CI job): run them with
    pip install -r requirements-spark.txt   (and Java 17: JAVA_HOME or java on PATH)
"""
import json

import pytest

from spark.session import spark_available

if not spark_available():
    pytest.skip("needs pyspark and Java 17 (requirements-spark.txt)", allow_module_level=True)

from helpers import PROFILE_ROWS_NEXT, write_sample_raw  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402
from pipeline import silver as duck_silver  # noqa: E402
from pipeline.bronze import load_bronze  # noqa: E402
from pipeline.gold import build_gold as duck_build_gold  # noqa: E402
from pipeline.rules import RULES  # noqa: E402
from scripts.make_sample_data import make_sample  # noqa: E402
from spark.checks import run_data_tests  # noqa: E402
from spark.gold import build_gold  # noqa: E402
from spark.parity import compare, duckdb_fingerprint, fingerprint_queries, spark_fingerprint, validation_sql  # noqa: E402
from spark.session import cache_and_count, local_session  # noqa: E402
from spark.silver import SPARK_CHECKS, TABLES, build_silver  # noqa: E402


@pytest.fixture(scope="module")
def spark():
    session = local_session("udise-tests", cores=2, memory="1g")
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def build_both(spark, d):
    """Bronze from d/raw, then silver + gold with DuckDB (into d/) and with Spark (in memory)."""
    load_bronze(d / "raw", d / "bronze", run_id="r1")
    duck = duck_silver.build_silver(d / "bronze", d / "silver", d / "quarantine", run_id="r1")
    duck_build_gold(d / "silver", d / "gold" / "udise.duckdb", d / "gold" / "parquet", d / "gold" / "exports")
    s = build_silver(spark, (d / "bronze").as_posix(), persist=cache_and_count, run_id="r1")
    return duck, s, build_gold(spark, s["silver"], persist=cache_and_count)


@pytest.fixture(scope="module")
def small(spark, tmp_path_factory):
    """tests/helpers.py data: one planted problem per rule; school 101 moves KOTA -> BUNDI."""
    d = tmp_path_factory.mktemp("spark_small")
    write_sample_raw(d / "raw")
    write_sample_raw(d / "raw", "2025-26", PROFILE_ROWS_NEXT)
    return (d, *build_both(spark, d))


@pytest.fixture(scope="module")
def sample(spark, tmp_path_factory):
    """scripts/make_sample_data.py: the synthetic sample CI uses (two years, transgender columns in 2025-26)."""
    d = tmp_path_factory.mktemp("spark_sample")
    expected = make_sample(d / "raw")
    return (d, expected, *build_both(spark, d))


def rows(df, *cols, where=None):
    df = df.filter(where) if where is not None else df
    return [tuple(r) for r in df.select(*cols).orderBy(*cols).collect()]


def test_same_tables_and_rules_as_the_duckdb_pipeline():
    assert list(TABLES) == list(duck_silver.TABLES)
    assert set(SPARK_CHECKS) == {r.name for r in RULES}


@pytest.mark.parametrize("data", ["small", "sample"])
def test_quarantine_and_flags_match_duckdb(data, request):
    _, *rest = request.getfixturevalue(data)
    duck, s = rest[-3], rest[-2]
    for table, d in duck["tables"].items():
        sp = s["summary"]["tables"][table]
        assert sp["quarantined_by_rule"] == d["quarantined_by_rule"], table
        assert sp["flagged_by_rule"] == d["flagged_by_rule"], table
        assert (sp["rows_silver"], sp["rows_quarantined"]) == (d["rows_silver"], d["rows_quarantined"]), table
    assert s["summary"]["reconciled"] == duck["reconciled"]


def test_planted_problems_quarantined_with_reason(small):
    _, _, s, _ = small
    q = {t: {r["pseudocode"]: (r["_rule"], r["_reason"], r["district_file"]) for r in df.collect()}
         for t, df in s["quarantine"].items()}
    assert q["schools"][103][0] == "unknown_district" and "NOWHERE" in q["schools"][103][1]
    assert q["schools"][105][:2] == ("missing_required", "missing district")
    assert q["schools"][101][0] == "duplicate_school_year" and q["schools"][101][2] == "KOTA"
    assert q["facilities"][103][0] == "orphan_school"
    assert q["facilities"][104][:2] == ("not_a_number", "laptop=x")
    assert q["teachers"][106][:2] == ("negative_count", "total_tch=-2")
    assert sorted(r["_rule"] for r in s["quarantine"]["enrolment"].collect()) == \
        ["duplicate_enrolment_row", "duplicate_enrolment_row", "orphan_school", "orphan_school"]
    assert {r["_run_id"] for r in s["quarantine"]["schools"].collect()} == {"r1"}


def test_cleaning_and_facility_flags(small):
    _, _, s, _ = small
    year = F.col("year") == "2024-25"
    sch = {r["pseudocode"]: r for r in s["silver"]["schools"].filter(year).collect()}
    assert (sch[102]["district"], sch[102]["district_raw"]) == ("JALORE", "JALOR")
    assert sch[104]["block_is_admin_region"] and sch[104]["block_clean"] is None
    assert sch[104]["management_group"] == "Unmapped" and sch[104]["flag_unknown_management"] == 1
    assert sch[102]["location"] == "Urban" and sch[101]["is_govt"] == 1
    fac = {r["pseudocode"]: r for r in s["silver"]["facilities"].filter(year).collect()}
    assert fac[101]["has_handwash"] is None                 # 9 = not applicable -> missing, not "No"
    assert fac[101]["solar_panel"] == 3 and fac[101]["flag_invalid_code"] == 1
    assert fac[102]["has_electricity"] == 0                 # 3 = connection not functional
    assert fac[102]["flag_functional_toilets_exceed_total"] == 1
    tch = {r["pseudocode"]: r for r in s["silver"]["teachers"].filter(year).collect()}
    assert tch[102]["flag_no_teachers_with_students"] == 1 and tch[104]["flag_high_ptr"] == 1


def test_scd2_two_versions_for_school_that_moved(small):
    _, _, _, g = small
    named = g["dim_school"].join(g["dim_district"], "district_key")
    assert rows(named, "version", "valid_from_year", "valid_to_year", "is_current", "district_name",
                where="pseudocode = 101") == [(1, 2024, 2024, False, "KOTA"), (2, 2025, 2025, True, "BUNDI")]
    assert rows(g["dim_school"], "valid_from_year", "valid_to_year", "version",
                where="pseudocode = 102") == [(2024, 2025, 1)]


def test_facts_point_to_the_version_valid_that_year(small):
    _, _, _, g = small
    f = (g["fact_enrolment"].join(g["dim_school"], "school_key").join(g["dim_district"], "district_key")
         .join(g["dim_year"], "year_key").filter("pseudocode = 101")
         .groupBy("academic_year", "district_name").sum("students"))
    assert rows(f, "academic_year", "district_name", "sum(students)") == [("2024-25", "KOTA", 5),
                                                                            ("2025-26", "BUNDI", 5)]


def test_transgender_only_from_2025_26(sample):
    _, _, _, _, g = sample
    t = g["fact_enrolment"].join(g["dim_gender"], "gender_key").join(g["dim_year"], "year_key") \
        .filter("gender_code = 'T'").select("academic_year").distinct()
    assert rows(t, "academic_year") == [("2025-26",)]


def test_sample_expectations(sample):
    d, expected, _, s, g = sample
    for table, rules in expected["quarantine"].items():
        got = {r: n for r, n in s["summary"]["tables"][table]["quarantined_by_rule"].items() if n}
        assert got == rules, table


def test_data_tests_pass_and_match_the_sql_tests(spark, sample):
    d, _, _, s, g = sample
    spark_tests = run_data_tests(spark, s["silver"], g)
    _, duck_tests = duckdb_fingerprint([], d / "gold" / "udise.duckdb", d / "silver", d / "quarantine")
    assert [t["check"] for t in spark_tests] == [t["check"] for t in duck_tests]
    assert all(t["failures"] == 0 for t in spark_tests), [t for t in spark_tests if t["failures"]]


def test_parity_with_duckdb_on_the_sample(spark, sample):
    d, _, _, s, g = sample
    queries = fingerprint_queries(validation_sql())
    duck, _ = duckdb_fingerprint(queries, d / "gold" / "udise.duckdb", d / "silver", d / "quarantine")
    results = compare(queries, duck, spark_fingerprint(spark, s["silver"], s["quarantine"], g, queries))
    mismatches = [m for r in results.values() for m in r["mismatches"]]
    assert mismatches == [], json.dumps(mismatches[:20], default=str, indent=1)
    assert all(r["rows"] > 0 for r in results.values())
