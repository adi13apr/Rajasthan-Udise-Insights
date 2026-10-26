"""Gold layer in PySpark: the star schema and district marts of sql/01-11, from silver DataFrames.

Each function ports one SQL file and says which. Surrogate keys use the same row_number()
ordering as the SQL, so both engines give every row the same key (the parity check
compares sums of keys to prove it).

    dim_year, dim_district, dim_management, dim_grade, dim_gender, dim_social_category
    dim_school                SCD Type 2 (sql/05_dim_school.sql)
    fact_enrolment            unpivot of the wide c<grade>_<sex> columns, zero rows dropped
    fact_teachers, fact_school_facilities
    int_school_year           school-year intermediate (never exported)
    mart_district_year, mart_district_year_management
"""
import re

import pandas as pd
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from pipeline import config as C

from .silver import Persist, no_persist

ENROL_COLUMN = re.compile(r"^c(pp|[0-9]+)_[bgt]$")
INFRA_FLAGS = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_handwash",
               "has_library", "has_ramp", "has_internet", "has_computer"]  # no playground (DQ log #17)
HAS_FLAGS = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_boys_toilet", "has_handwash",
             "has_library", "has_playground", "has_ramp", "has_internet", "has_computer"]


def start_year(year_col):
    return F.substring(year_col, 1, 4).cast("int")


def small_table(spark: SparkSession, rows: list[tuple], schema: str) -> DataFrame:
    """A small fixed table (lookup dimension) from Python rows, e.g. schema "k int, name string".

    Goes through pandas so Spark converts it with Arrow on the driver. Straight from a list,
    every partition starts a Python worker, which on Windows took 26 s for a 2-row table
    against 1 s this way."""
    columns = [part.strip().split()[0] for part in schema.split(",")]
    return spark.createDataFrame(pd.DataFrame(rows, columns=columns, dtype=object), schema)


def nullif_zero(col):
    return F.when(col != 0, col)


# --- Dimensions -----------------------------------------------------------------

def dim_year(schools: DataFrame) -> DataFrame:
    """sql/01_dim_year.sql"""
    years = schools.select(F.col("year").alias("academic_year")).distinct()
    return years.select(F.row_number().over(Window.orderBy("academic_year")).cast("int").alias("year_key"),
                        "academic_year", start_year(F.col("academic_year")).alias("start_year"))


def dim_district(spark: SparkSession) -> DataFrame:
    """sql/02_dim_district.sql: the 41 districts with their old spellings (sorted)."""
    aliases: dict[str, list[str]] = {}
    for alias, name in C.DISTRICT_ALIASES.items():
        aliases.setdefault(name, []).append(alias)
    rows = [(key, name, sorted(aliases.get(name, [])))
            for key, name in enumerate(sorted(C.DISTRICTS), start=1)]
    return small_table(spark, rows, "district_key int, district_name string, aliases array<string>")


def dim_management(spark: SparkSession) -> DataFrame:
    """sql/03_dim_management.sql: key 0 = Unmapped, then codes in ascending order."""
    codes = C.load_reference("management_codes.json")["codes"]
    rows = [(0, None, "Unmapped", False)] + [
        (key, code, codes[str(code)]["group"], bool(codes[str(code)]["is_government"]))
        for key, code in enumerate(sorted(int(k) for k in codes), start=1)]
    return small_table(spark, rows, "management_key int, management_code int, management_group string, "
                                    "is_government boolean")


def dim_grade(spark: SparkSession) -> DataFrame:
    """sql/04_dim_lookups.sql: pre-primary (0) and classes 1-12 with their stage."""
    def stage(g):
        if g == 0:
            return "pre_primary"
        return next(name for name, grades in C.STAGES.items() if g in grades)
    rows = [(g, "PP" if g == 0 else str(g), "Pre-primary" if g == 0 else f"Class {g}", stage(g))
            for g in range(13)]
    return small_table(spark, rows, "grade_key int, grade_code string, grade_label string, stage string")


def dim_gender(spark: SparkSession) -> DataFrame:
    rows = [(1, "B", "Boys"), (2, "G", "Girls"), (3, "T", "Transgender")]
    return small_table(spark, rows, "gender_key int, gender_code string, gender_label string")


def dim_social_category(spark: SparkSession) -> DataFrame:
    rows = [(k, k, C.SOCIAL_CATEGORY[k]) for k in sorted(C.SOCIAL_CATEGORY)]
    return small_table(spark, rows, "social_category_key int, item_id int, social_category string")


SCD2_ATTRIBUTES = ["district_key", "block", "block_clean", "management_key", "rural_urban", "location",
                   "school_category", "school_type", "school_type_label"]


def dim_school(schools: DataFrame, districts: DataFrame, management: DataFrame) -> DataFrame:
    """sql/05_dim_school.sql: SCD Type 2. A new version starts in the first year where any
    tracked attribute (district, block, management code, rural/urban, category, type)
    differs from the year before; facts join to the version valid in their year."""
    s = schools.alias("s")
    yearly = (s.join(districts.alias("d"), F.col("d.district_name") == F.col("s.district"))
               .join(management.alias("m"), F.col("m.management_code") == F.col("s.managment"), "left")
               .select("s.pseudocode", start_year(F.col("s.year")).alias("start_year"), "d.district_key",
                       "s.block", "s.block_clean",
                       F.coalesce("m.management_key", F.lit(0)).alias("management_key"),
                       "s.rural_urban", "s.location", "s.school_category", "s.school_type",
                       "s.school_type_label",
                       # one value that changes whenever any tracked attribute changes
                       F.md5(F.concat_ws("|", *[F.col(c).cast("string") for c in
                                               ["d.district_key", "s.block", "s.managment", "s.rural_urban",
                                                "s.school_category", "s.school_type"]])).alias("attr_hash")))
    w = Window.partitionBy("pseudocode").orderBy("start_year")
    numbered = (yearly
                .withColumn("is_new_version",
                            F.when(~F.lag("attr_hash").over(w).eqNullSafe(F.col("attr_hash")), 1).otherwise(0))
                .withColumn("version", F.sum("is_new_version").over(
                    w.rowsBetween(Window.unboundedPreceding, Window.currentRow))))
    # Attributes are equal within a version; take them from its last year. DuckDB's arg_max
    # skips NULL values, so max_by only looks at years where the attribute is not NULL.
    versions = numbered.groupBy("pseudocode", "version").agg(
        F.min("start_year").alias("valid_from_year"),
        F.max("start_year").alias("valid_to_year"),
        *[F.max_by(c, F.when(F.col(c).isNotNull(), F.col("start_year"))).alias(c) for c in SCD2_ATTRIBUTES])
    return versions.select(
        F.row_number().over(Window.orderBy("pseudocode", "version")).cast("int").alias("school_key"),
        "pseudocode",
        F.col("version").cast("int").alias("version"),
        "valid_from_year", "valid_to_year",
        (F.col("version") == F.max("version").over(Window.partitionBy("pseudocode"))).alias("is_current"),
        *SCD2_ATTRIBUTES)


def school_version(df: DataFrame, years: DataFrame, schools: DataFrame) -> DataFrame:
    """Join rows that have pseudocode and year to dim_year and to the dim_school version valid that year."""
    return (df.join(years.alias("y"), F.col("y.academic_year") == F.col("src.year"))
              .join(schools.alias("ds"), (F.col("ds.pseudocode") == F.col("src.pseudocode"))
                    & F.col("y.start_year").between(F.col("ds.valid_from_year"), F.col("ds.valid_to_year"))))


# --- Facts ------------------------------------------------------------------------

def fact_enrolment(enrolment: DataFrame, years: DataFrame, schools: DataFrame, grades: DataFrame,
                   genders: DataFrame, categories: DataFrame) -> DataFrame:
    """sql/06_fact_enrolment.sql. Grain: school x year x grade x gender x social category;
    rows with 0 students are dropped (a missing row means 0)."""
    value_cols = [c for c in enrolment.columns if ENROL_COLUMN.match(c)]
    long = (enrolment.select("pseudocode", "year", "item_id", *value_cols)
            .unpivot(["pseudocode", "year", "item_id"], value_cols, "source_column", "students")
            .filter(F.col("students") > 0)
            .select("pseudocode", "year", "item_id", "students",
                    F.upper(F.regexp_extract("source_column", r"^c(pp|[0-9]+)_", 1)).alias("grade"),
                    F.upper(F.substring("source_column", -1, 1)).alias("gender_code")))
    return (school_version(long.alias("src"), years, schools)
            .join(grades.alias("g"), F.col("g.grade_code") == F.col("src.grade"))
            .join(genders.alias("ge"), F.col("ge.gender_code") == F.col("src.gender_code"))
            .join(categories.alias("sc"), F.col("sc.item_id") == F.col("src.item_id"))
            .select("ds.school_key", "y.year_key", "g.grade_key", "ge.gender_key", "sc.social_category_key",
                    F.col("src.students").cast("int").alias("students")))


def fact_teachers(teachers: DataFrame, years: DataFrame, schools: DataFrame) -> DataFrame:
    """sql/07_fact_teachers.sql. Grain: school x year; the teacher columns as published."""
    drop = {"pseudocode", "year", "district_file", "_source_file", "_source_row"}
    return (school_version(teachers.alias("src"), years, schools)
            .select("ds.school_key", "y.year_key", *[F.col(f"src.{c}") for c in teachers.columns if c not in drop]))


def fact_school_facilities(schools_silver: DataFrame, facilities: DataFrame, years: DataFrame,
                           schools: DataFrame) -> DataFrame:
    """sql/08_fact_school_facilities.sql. Grain: school x year, one row per silver school-year."""
    f = facilities.alias("f")
    cols = HAS_FLAGS + [
        "classrooms", "classrooms_good", "classrooms_needs_minor_repair", "classrooms_needs_major_repair",
        "pct_classrooms_major_repair", "total_girls_toilet", "total_girls_func_toilet",
        "total_boys_toilet", "total_boys_func_toilet"]
    return (school_version(schools_silver.alias("src"), years, schools)
            .join(f, (F.col("f.pseudocode") == F.col("src.pseudocode")) & (F.col("f.year") == F.col("src.year")),
                  "left")
            .select("ds.school_key", "y.year_key", "src.lowclass", "src.highclass",
                    *[F.col(f"f.{c}") for c in cols],
                    F.col("f.flag_invalid_code").alias("flag_facility_invalid_code"),
                    "f.flag_functional_toilets_exceed_total", "f.flag_classroom_total_mismatch"))


# --- Intermediate and marts --------------------------------------------------------

def int_school_year(fact_enr: DataFrame, facilities: DataFrame, teachers: DataFrame, years: DataFrame,
                    schools: DataFrame, districts: DataFrame, management: DataFrame, grades: DataFrame,
                    genders: DataFrame, categories: DataFrame) -> DataFrame:
    """sql/10_int_school_year.sql: one row per school and year, from the star schema only."""
    e = (fact_enr.join(grades, "grade_key").join(genders, "gender_key").join(categories, "social_category_key"))
    grade, stage, sex, cat = F.col("grade_key"), F.col("stage"), F.col("gender_code"), F.col("social_category")
    sec_plus = stage.isin("secondary", "higher_secondary")

    def total(cond):
        return F.sum(F.when(cond, F.col("students")))

    enrolment = e.groupBy("school_key", "year_key").agg(
        total(grade.between(1, 12)).alias("enr_total"),
        total(grade.between(1, 12) & (sex == "G")).alias("enr_girls"),
        total(grade == 0).alias("enr_pre_primary"),
        total(stage == "primary").alias("enr_primary"),
        total((stage == "primary") & (sex == "B")).alias("enr_primary_b"),
        total((stage == "primary") & (sex == "G")).alias("enr_primary_g"),
        total(sec_plus).alias("enr_secondary_plus"),
        total(sec_plus & (sex == "B")).alias("enr_secondary_plus_b"),
        total(sec_plus & (sex == "G")).alias("enr_secondary_plus_g"),
        total(grade.between(1, 12) & (cat == "SC")).alias("enr_sc"),
        total(grade.between(1, 12) & (cat == "ST")).alias("enr_st"))
    enr_cols = [c for c in enrolment.columns if c.startswith("enr_")]

    sf = F.col
    present = sum(sf(f"sf.{c}").isNotNull().cast("int") for c in INFRA_FLAGS)
    have = sum(F.coalesce(sf(f"sf.{c}"), F.lit(0.0)) for c in INFRA_FLAGS)
    # Share of 8 basic facilities present (0-100) over those reported; exact decimal so district
    # averages do not depend on summation order.
    infra = F.round(F.lit(100.0) * have / nullif_zero(present), 1).cast("decimal(4,1)")
    return (facilities.alias("sf")
            .join(years.alias("y"), "year_key")
            .join(schools.alias("s"), "school_key")
            .join(districts.alias("d"), F.col("d.district_key") == F.col("s.district_key"))
            .join(management.alias("m"), F.col("m.management_key") == F.col("s.management_key"))
            .join(enrolment.alias("e"), ["school_key", "year_key"], "left")
            .join(teachers.select("school_key", "year_key", "total_tch").alias("t"), ["school_key", "year_key"],
                  "left")
            .select("school_key", "year_key", "y.academic_year", "s.pseudocode", "s.district_key",
                    "d.district_name", "s.management_key", "m.management_group", "m.is_government",
                    "s.location", "s.school_category",
                    *[F.coalesce(F.col(f"e.{c}"), F.lit(0)).alias(c) for c in enr_cols],
                    "t.total_tch",
                    F.coalesce(F.col("t.total_tch") == 1, F.lit(False)).cast("int").alias("single_teacher"),
                    *[F.col(f"sf.{c}") for c in HAS_FLAGS],
                    infra.alias("infra_index")))


def _district_metrics(isy: DataFrame, group: list[str]) -> DataFrame:
    """sql/11_marts_district.sql (the sums and shares CTEs) for one grouping."""
    col = F.col
    sums = isy.filter(col("enr_total") > 0).groupBy(*group).agg(
        F.expr("bool_and(is_government)").alias("is_government"),
        F.count(F.lit(1)).alias("schools"),
        F.sum("enr_total").alias("enrolment"),
        *[F.sum(c).alias(c) for c in ["enr_girls", "enr_primary", "enr_primary_b", "enr_primary_g",
                                       "enr_secondary_plus", "enr_secondary_plus_b", "enr_secondary_plus_g",
                                       "enr_sc", "enr_st"]],
        F.sum("total_tch").alias("teachers"),
        (F.avg("single_teacher") * 100).alias("pct_single_teacher"),
        (F.sum("infra_index").cast("double") / nullif_zero(F.count("infra_index"))).alias("avg_infra_index"),
        *[(F.avg(c) * 100).alias(f"pct_{c}") for c in HAS_FLAGS])
    shares = sums.select(
        "*",
        (col("enr_girls") / nullif_zero(col("enrolment"))).alias("girls_share"),
        (col("enr_primary_g") / nullif_zero(col("enr_primary"))).alias("girls_share_primary"),
        (col("enr_secondary_plus_g") / nullif_zero(col("enr_secondary_plus"))).alias("girls_share_sec_plus"),
        (col("enr_st") / nullif_zero(col("enrolment"))).alias("st_share"),
        (col("enr_sc") / nullif_zero(col("enrolment"))).alias("sc_share"),
        (col("enrolment") / nullif_zero(col("teachers"))).alias("ptr"),
        ((col("enr_secondary_plus_b") / 4.0) / nullif_zero(col("enr_primary_b") / 5.0)).alias("boys_progression"),
        ((col("enr_secondary_plus_g") / 4.0) / nullif_zero(col("enr_primary_g") / 5.0)).alias("girls_progression"))

    def r3(c):
        return F.round(col(c), 3).alias(c)

    keys = [col("district_name").alias("district"), col("academic_year").alias("year")]
    if "management_group" in group:
        keys += ["management_group", "is_government"]
    return shares.select(
        *keys, "schools", "enrolment",
        *[r3(c) for c in ["girls_share", "girls_share_primary", "girls_share_sec_plus", "st_share", "sc_share"]],
        "teachers", r3("ptr"), r3("pct_single_teacher"), r3("avg_infra_index"),
        F.round(col("girls_share_sec_plus") - col("girls_share_primary"), 3).alias("girls_retention_gap"),
        r3("boys_progression"), r3("girls_progression"),
        *[r3(f"pct_{c}") for c in HAS_FLAGS])


def mart_district_year(isy: DataFrame) -> DataFrame:
    return _district_metrics(isy, ["district_name", "academic_year"])


def mart_district_year_management(isy: DataFrame) -> DataFrame:
    return _district_metrics(isy, ["district_name", "academic_year", "management_group"])


# --- Build ---------------------------------------------------------------------------

def build_gold(spark: SparkSession, silver: dict[str, DataFrame], persist: Persist = no_persist
               ) -> dict[str, DataFrame]:
    """Every gold table, in dependency order. persist() is called on each (see silver.Persist)."""
    g: dict[str, DataFrame] = {}

    def add(name: str, df: DataFrame) -> DataFrame:
        g[name] = persist(name, df)
        return g[name]

    years = add("dim_year", dim_year(silver["schools"]))
    districts = add("dim_district", dim_district(spark))
    management = add("dim_management", dim_management(spark))
    grades = add("dim_grade", dim_grade(spark))
    genders = add("dim_gender", dim_gender(spark))
    categories = add("dim_social_category", dim_social_category(spark))
    schools = add("dim_school", dim_school(silver["schools"], districts, management))
    enr = add("fact_enrolment", fact_enrolment(silver["enrolment"], years, schools, grades, genders, categories))
    tch = add("fact_teachers", fact_teachers(silver["teachers"], years, schools))
    fac = add("fact_school_facilities", fact_school_facilities(silver["schools"], silver["facilities"],
                                                               years, schools))
    isy = add("int_school_year", int_school_year(enr, fac, tch, years, schools, districts, management,
                                                 grades, genders, categories))
    add("mart_district_year", mart_district_year(isy))
    add("mart_district_year_management", mart_district_year_management(isy))
    return g
