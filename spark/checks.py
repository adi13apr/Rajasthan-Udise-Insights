"""Data tests and official-total validation for the Spark gold tables.

DATA_TESTS ports sql/tests/*.sql to Spark SQL, check for check, with the same check names,
so the parity report can list both engines' results side by side. Each query returns the
number of failures, which must be 0.

The official indicators are computed by running sql/validation/01_official_indicators.sql
itself in Spark: that file is plain enough SQL (FILTER, UNPIVOT) for both engines.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pandas as pd

from pipeline import config as C

if TYPE_CHECKING:  # pyspark only for type hints: check_official() also runs without Spark (--from)
    from pyspark.sql import DataFrame, SparkSession

VALIDATION_SQL = C.ROOT / "sql" / "validation" / "01_official_indicators.sql"
ENROL_COLUMN = re.compile(r"^c(pp|[0-9]+)_[bgt]$")


def register_views(silver: dict[str, DataFrame], gold: dict[str, DataFrame]) -> None:
    """The names the SQL uses: src_<silver table> and the gold table names."""
    for table, df in silver.items():
        df.createOrReplaceTempView(f"src_{table}")
    for table, df in gold.items():
        df.createOrReplaceTempView(table)


def anti(fact: str, dim: str, key: str) -> str:
    return f"select count(*) from {fact} f left anti join {dim} d on f.{key} = d.{key}"


def data_tests(enrolment_columns: list[str]) -> list[tuple[str, str, str]]:
    """(file, check name, Spark SQL returning the failure count), in sql/tests order."""
    wide_total = " + ".join(f"coalesce({c}, 0)" for c in enrolment_columns if ENROL_COLUMN.match(c)) or "0"
    ri = "01_referential_integrity.sql"
    grain = "02_grain_and_scd2.sql"
    totals = "03_totals_preserved.sql"
    marts = "04_marts.sql"
    return [
        (ri, "fact_enrolment.school_key", anti("fact_enrolment", "dim_school", "school_key")),
        (ri, "fact_enrolment.year_key", anti("fact_enrolment", "dim_year", "year_key")),
        (ri, "fact_enrolment.grade_key", anti("fact_enrolment", "dim_grade", "grade_key")),
        (ri, "fact_enrolment.gender_key", anti("fact_enrolment", "dim_gender", "gender_key")),
        (ri, "fact_enrolment.social_category_key", anti("fact_enrolment", "dim_social_category",
                                                        "social_category_key")),
        (ri, "fact_teachers.school_key", anti("fact_teachers", "dim_school", "school_key")),
        (ri, "fact_teachers.year_key", anti("fact_teachers", "dim_year", "year_key")),
        (ri, "fact_school_facilities.school_key", anti("fact_school_facilities", "dim_school", "school_key")),
        (ri, "fact_school_facilities.year_key", anti("fact_school_facilities", "dim_year", "year_key")),
        (ri, "dim_school.district_key", anti("dim_school", "dim_district", "district_key")),
        (ri, "dim_school.management_key", anti("dim_school", "dim_management", "management_key")),

        (grain, "fact_enrolment grain unique",
         "select count(*) - count(distinct school_key, year_key, grade_key, gender_key, social_category_key) "
         "from fact_enrolment"),
        (grain, "fact_teachers one row per school-year",
         "select count(*) - count(distinct school_key, year_key) from fact_teachers"),
        (grain, "fact_school_facilities one row per school-year",
         "select count(*) - count(distinct school_key, year_key) from fact_school_facilities"),
        (grain, "fact_enrolment has no zero rows", "select count(*) from fact_enrolment where students <= 0"),
        (grain, "dim_school one current version per school",
         "select count(*) from (select pseudocode from dim_school group by pseudocode "
         "having sum(cast(is_current as int)) <> 1)"),
        (grain, "dim_school versions do not overlap",
         "select count(*) from dim_school a join dim_school b on a.pseudocode = b.pseudocode "
         "and a.version < b.version and a.valid_to_year >= b.valid_from_year"),
        (grain, "dim_school versions numbered 1..n without gaps",
         "select count(*) from (select pseudocode from dim_school group by pseudocode "
         "having max(version) <> count(*))"),
        (grain, "dim_school keys unique", "select count(*) - count(distinct school_key) from dim_school"),
        (grain, "dim_district has 41 districts", "select abs(count(*) - 41) from dim_district"),

        (totals, "enrolment total silver = gold",
         f"select abs((select sum({wide_total}) from src_enrolment) "
         f"- (select sum(students) from fact_enrolment))"),
        (totals, "teachers total silver = gold",
         "select abs((select sum(total_tch) from src_teachers) - (select sum(total_tch) from fact_teachers))"),
        (totals, "school-years silver = gold",
         "select abs((select count(*) from src_schools) - (select count(*) from fact_school_facilities))"),
        (totals, "teacher rows silver = gold",
         "select abs((select count(*) from src_teachers) - (select count(*) from fact_teachers))"),
        (totals, "schools silver = current versions in dim_school",
         "select abs((select count(distinct pseudocode) from src_schools) "
         "- (select count(*) from dim_school where is_current))"),

        (marts, "int_school_year one row per school-year",
         "select abs((select count(*) from int_school_year) - (select count(*) from fact_school_facilities))"),
        (marts, "mart_district_year one row per district-year",
         "select count(*) - count(distinct district, year) from mart_district_year"),
        (marts, "mart_district_year covers every district-year with students",
         "select abs((select count(*) from mart_district_year) - (select count(*) from (select distinct "
         "district_name, academic_year from int_school_year where enr_total > 0)))"),
        (marts, "mart enrolment = fact enrolment classes 1-12",
         "select abs((select sum(enrolment) from mart_district_year) "
         "- (select sum(students) from fact_enrolment where grade_key between 1 and 12))"),
        (marts, "management mart adds up to district mart: schools",
         "select abs((select sum(schools) from mart_district_year_management) "
         "- (select sum(schools) from mart_district_year))"),
        (marts, "management mart adds up to district mart: enrolment",
         "select abs((select sum(enrolment) from mart_district_year_management) "
         "- (select sum(enrolment) from mart_district_year))"),
        (marts, "management mart adds up to district mart: teachers",
         "select abs((select sum(teachers) from mart_district_year_management) "
         "- (select sum(teachers) from mart_district_year))"),
        (marts, "shares between 0 and 1",
         "select count(*) from mart_district_year where not (girls_share between 0 and 1 "
         "and st_share between 0 and 1 and sc_share between 0 and 1)"),
    ]


def run_data_tests(spark: SparkSession, silver: dict[str, DataFrame], gold: dict[str, DataFrame]) -> list[dict]:
    register_views(silver, gold)
    out = []
    for file, check, sql in data_tests(silver["enrolment"].columns):
        failures = spark.sql(sql).first()[0]
        out.append({"file": file, "check": check, "failures": int(failures or 0)})
    return out


def official_indicators(spark: SparkSession, gold: dict[str, DataFrame]) -> pd.DataFrame:
    """sql/validation/01_official_indicators.sql run in Spark: year, indicator, value."""
    gold["int_school_year"].createOrReplaceTempView("int_school_year")
    sql = VALIDATION_SQL.read_text(encoding="utf-8").strip().rstrip(";")
    return spark.sql(sql).toPandas()


def check_official(indicators: pd.DataFrame, official: dict | None = None) -> list[dict]:
    """Each published official figure vs ours: [{year, indicator, official, ours, match}]."""
    official = official if official is not None else C.OFFICIAL
    ours = {(r.year, r.indicator): r.value for r in indicators.itertuples()}
    return [{"year": year, "indicator": name, "official": value,
             "ours": None if pd.isna(ours.get((year, name))) else int(ours[(year, name)]),
             "match": ours.get((year, name)) == value}
            for year, values in official.items() for name, value in values.items()]
