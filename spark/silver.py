"""Silver layer in PySpark: bronze Parquet -> typed, standardised, rule-checked tables.

The same tables, typing, cleaning and rules as pipeline/silver.py and pipeline/rules.py:

    schools          profile1
    enrolment        enrolment1, item_group 1 (General/SC/ST/OBC: a complete split)
    enrolment_other  enrolment1, the other item_groups
    facilities       facility
    teachers         teacher

The rule list itself (names, order, severity, tables) is imported from pipeline/rules.py, so
there is one catalogue of rules; only the checks are written again, as Spark column
expressions (SPARK_CHECKS). Error rules keep the DuckDB pipeline's meaning: they run in
order and each one only sees the rows that passed the ones before it, so a quarantined
row has exactly one rule, the first it failed. Here that is done in one pass: the
row-level rules are computed side by side and a row gets the first rule (in order) that
gave it a reason; the duplicate rule, which compares rows with each other, is judged only
among rows that passed the rules before it (its window is partitioned by that as well as
by the key).

Bronze is read with mergeSchema (the Spark version of DuckDB's union_by_name): each
year's files have different columns, and without it the columns of whichever file is
read first win (the P7 benchmark lesson). Every file under the dataset folder is read
(year=... subfolders or not), and the year comes from the _year column in each file.
"""
from functools import reduce
from pathlib import Path
from typing import Callable

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from pipeline import config as C
from pipeline.rules import ENROLMENT_TABLES, ERROR, RULES, STATE_FILE, WARNING, code_set

# Same tables as pipeline.silver.TABLES (that module is not imported: it needs duckdb,
# which Databricks does not have). The filter is applied to the raw text of item_group.
def _item_group():  # a function: columns can only be built once a Spark session exists
    return F.coalesce(F.expr("try_cast(item_group as bigint)"), F.lit(-1))


TABLES: dict[str, tuple[str, Callable[[], object] | None]] = {
    "schools": ("profile1", None),
    "enrolment": ("enrolment1", lambda: _item_group() == C.ENROL_TOTAL_GROUP),
    "enrolment_other": ("enrolment1", lambda: _item_group() != C.ENROL_TOTAL_GROUP),
    "facilities": ("facility", None),
    "teachers": ("teacher", None),
}
KEY = ["year", "pseudocode"]
INT_TYPES = {"smallint", "integer", "bigint"}
FLOAT_TYPES = {"double precision", "numeric", "real"}
META = ["year", "district_file", "_source_file", "_source_row"]
# \z, not $: in Java regex $ also matches before a final newline; DuckDB's full match does not.
WHOLE = r"^-?[0-9]+\z"
# (?U): \s also matches Unicode spaces, like Python's str.strip() in the pandas version.
EDGE_SPACES, INNER_SPACES = r"(?U)^\s+|\s+$", r"(?U)\s+"

# persist(name, df) -> df: where the caller materialises a table (Databricks: write a Delta
# table and read it back; local: cache). The transforms themselves never cache.
Persist = Callable[[str, DataFrame], DataFrame]


def no_persist(name: str, df: DataFrame) -> DataFrame:
    return df


# --- Schema helpers (same as pipeline/silver.py) -------------------------------

def is_whole_number(entry: dict) -> bool:
    return entry.get("type", "") in INT_TYPES or entry.get("kind") in ("yes_no", "enum")


def column_entries(dataset: str, schema: dict) -> dict[str, dict]:
    """Schema entry per column, over all years (a later year's entry wins)."""
    out = {}
    for _, cols in sorted(schema["datasets"].get(dataset, {}).items()):
        out.update({e["column"]: e for e in cols})
    return out


def map_values(col, mapping: dict, default=None):
    """CASE WHEN col = k THEN v ... ELSE default (NULL stays default). Used instead of a map
    lookup, which raises on a missing key in ANSI mode on some Spark versions."""
    items = list(mapping.items())
    expr = F.when(col == items[0][0], F.lit(items[0][1]))
    for k, v in items[1:]:
        expr = expr.when(col == k, F.lit(v))
    plain = default is None or isinstance(default, (str, int, float))
    return expr.otherwise(F.lit(default) if plain else default)  # default: a value or a Column


def any_of(conditions: list):
    return reduce(lambda a, b: a | b, conditions)


def all_of(conditions: list):
    return reduce(lambda a, b: a & b, conditions)


def false_if_null(cond):
    return F.coalesce(cond, F.lit(False))


# --- Reading bronze with types ------------------------------------------------

def read_bronze(spark: SparkSession, bronze_root: str, dataset: str) -> DataFrame:
    """All Parquet files of one bronze dataset, columns merged over years.

    The files are listed here in Python and passed to Spark one by one, which works the same on
    local disk and on a Databricks volume (/Volumes/... is a normal folder there), and avoids
    Hadoop's directory listing, which needs winutils on Windows."""
    folder = Path(bronze_root) / dataset
    files = sorted(p.as_posix() for p in folder.rglob("*.parquet")) if folder.is_dir() else []
    reader = spark.read.option("mergeSchema", "true")
    if files:
        return reader.parquet(*files)
    return reader.option("recursiveFileLookup", "true").parquet(f"{bronze_root.rstrip('/')}/{dataset}")


def source_columns(bronze: DataFrame) -> list[str]:
    return [c for c in bronze.columns if not c.startswith("_")]


def typed(bronze: DataFrame, entries: dict, where=None) -> DataFrame:
    """Whole-number columns -> bigint when the text is a whole number; anything else is
    recorded in _cast_errors ('col=value; ...') for the not_a_number rule. Text stays text."""
    df = bronze.filter(where) if where is not None else bronze
    select = [F.col("_year").alias("year"), F.col("_district_file").alias("district_file"),
              "_source_file", "_source_row"]
    errors = []
    for col in source_columns(bronze):
        c, entry = F.col(col), entries.get(col, {})
        if is_whole_number(entry):
            ok = F.trim(c).rlike(WHOLE)
            select.append(F.when(ok, F.trim(c).cast("bigint")).alias(col))
            errors.append(F.when(c.isNotNull() & ~ok, F.concat(F.lit(f"{col}="), c)))
        elif entry.get("type", "") in FLOAT_TYPES:
            value = F.expr(f"try_cast(`{col}` as double)")
            select.append(value.alias(col))
            errors.append(F.when(c.isNotNull() & value.isNull(), F.concat(F.lit(f"{col}="), c)))
        else:
            select.append(c)
    cast_errors = F.concat_ws("; ", *errors) if errors else F.lit("")
    return df.select(*select, cast_errors.alias("_cast_errors"))


# --- Cleaning (same as pipeline/silver.py) ---------------------------------------

def normalise_district(col):
    cleaned = F.regexp_replace(F.upper(F.regexp_replace(col, EDGE_SPACES, "")), INNER_SPACES, " ")
    return map_values(cleaned, C.DISTRICT_ALIASES, default=cleaned)


def yes_no(col):
    """1 -> 1, 2 -> 0, anything else (e.g. 9 = not applicable) -> missing."""
    return map_values(col, {k: float(v) for k, v in C.YES_NO.items()})


def standardise_schools(df: DataFrame) -> DataFrame:
    block = F.upper(F.regexp_replace("block", EDGE_SPACES, ""))
    admin_region = false_if_null(block.contains("REGION"))
    group = map_values(F.col("managment"), C.MANAGEMENT_GROUP, default="Unmapped")
    return (df.withColumn("district_raw", F.col("district"))
              .withColumn("district", normalise_district(F.col("district")))
              .withColumn("block", block)
              .withColumn("block_is_admin_region", admin_region)
              .withColumn("block_clean", F.when(~F.col("block_is_admin_region"), F.col("block")))
              .withColumn("location", map_values(F.col("rural_urban"), C.RURAL_URBAN))
              .withColumn("school_type_label", map_values(F.col("school_type"), C.SCHOOL_TYPE))
              .withColumn("management_group", group)
              .withColumn("is_govt", F.col("management_group").isin(C.GOVT_GROUPS).cast("int")))


def facility_flags(df: DataFrame) -> DataFrame:
    """Facility indicators (1 = available, 0 = not, NULL = not reported / not applicable)."""
    col = F.col
    flags = {
        "has_electricity": false_if_null(col("electricity_availability") == 1),  # 3 = not functional
        "has_safe_water": any_of([false_if_null(col(c) == 1) for c in C.SAFE_WATER_COLS]),
        "has_girls_toilet": false_if_null(col("total_girls_func_toilet") + col("func_girls_cwsn_friendly") > 0),
        "has_boys_toilet": false_if_null(col("total_boys_func_toilet") + col("func_boys_cwsn_friendly") > 0),
    }
    out = df.select("*", *[c.cast("int").cast("double").alias(n) for n, c in flags.items()])
    out = out.select("*",
                     yes_no(col("handwash_near_toilet")).alias("has_handwash"),
                     yes_no(col("library_availability")).alias("has_library"),
                     yes_no(col("playground_available")).alias("has_playground"),
                     yes_no(col("availability_ramps")).alias("has_ramp"),
                     yes_no(col("internet")).alias("has_internet"))
    devices = sum(F.coalesce(col(c), F.lit(0)) for c in ["laptop", "desktop", "tablet"])
    # total_class_rooms is 0 for nearly all schools in 2024-25: rebuild it from the
    # condition breakdown (matches total_class_rooms exactly where that is filled in).
    classrooms = (col("classrooms_in_good_condition") + col("classrooms_needs_minor_repair")
                  + col("classrooms_needs_major_repair"))
    out = out.select("*", (devices > 0).cast("int").cast("double").alias("has_computer"),
                     classrooms.alias("classrooms"),
                     col("classrooms_in_good_condition").alias("classrooms_good"))
    # Half-to-even rounding, like pandas' .round(1) in the DuckDB pipeline.
    pct = F.bround(col("classrooms_needs_major_repair") / F.when(col("classrooms") != 0, col("classrooms"))
                   * 100, 1)
    return out.withColumn("pct_classrooms_major_repair", pct)


# --- Rules --------------------------------------------------------------------
# Each check returns a Column: the reason the row fails (text), or NULL if it passes.
# Helper columns prepared by prepare_rule_inputs(): _school_exists, _enr_total.

def count_columns(df: DataFrame, columns: dict) -> list[str]:
    """Numeric measure columns (counts), not codes (same as pipeline.rules.count_columns)."""
    return [c for c, e in columns.items() if e["kind"] == "numeric" and c in df.columns
            and c not in ("pseudocode", "item_group", "item_id")]


def describe_cells(bad: dict) -> object:
    """'col=value; col=value' for the columns whose condition is true; NULL if none."""
    if not bad:
        return F.lit(None).cast("string")
    text = F.concat_ws("; ", *[F.when(cond, F.concat(F.lit(f"{c}="), F.col(c).cast("string")))
                               for c, cond in bad.items()])
    return F.when(any_of(list(bad.values())), text)


def check_missing_required(df, table, columns):
    required = ["year", "pseudocode"] + (["district"] if table == "schools" else [])
    missing = [F.when(F.col(c).isNull(), F.lit(c)) for c in required]
    return F.when(any_of([F.col(c).isNull() for c in required]),
                  F.concat(F.lit("missing "), F.concat_ws(", ", *missing)))


def check_not_a_number(df, table, columns):
    return F.when(F.col("_cast_errors").isNotNull() & (F.col("_cast_errors") != ""), F.col("_cast_errors"))


def check_negative_count(df, table, columns):
    return describe_cells({c: false_if_null(F.col(c) < 0) for c in count_columns(df, columns)})


def check_unknown_district(df, table, columns):
    return F.when(~false_if_null(F.col("district").isin(C.DISTRICTS)),
                  F.concat(F.lit("district "), F.col("district"), F.lit(" is not one of the 41 districts")))


def check_duplicate_key(df, table, columns):
    """Extra copies of the same key. The copy from the state-wide file is kept, else the
    first in file order. Partitioned by _passed too: rows that failed an earlier rule do
    not compete (they are judged by that rule only)."""
    key = KEY + (["item_group", "item_id"] if table in ENROLMENT_TABLES else [])
    w = Window.partitionBy(*key, "_passed").orderBy(
        (F.col("district_file") != STATE_FILE).asc(), F.col("_source_file").asc(), F.col("_source_row").asc())
    kept = F.first("_source_file").over(w.rowsBetween(Window.unboundedPreceding, Window.unboundedFollowing))
    return F.when(F.row_number().over(w) > 1, F.concat(F.lit("extra copy; kept the row from "), kept))


def check_orphan_school(df, table, columns):
    return F.when(F.col("_school_exists").isNull(), F.lit("school not in silver schools for this year"))


def check_unknown_management(df, table, columns):
    return F.col("managment").isNotNull() & ~F.col("managment").isin(list(C.MANAGEMENT_GROUP))


def check_invalid_rural_urban(df, table, columns):
    return F.col("rural_urban").isNotNull() & ~F.col("rural_urban").isin(list(C.RURAL_URBAN))


def check_invalid_code(df, table, columns):
    bad = [F.col(c).isNotNull() & ~F.col(c).isin(list(code_set(e)))
           for c, e in columns.items() if code_set(e) and c in df.columns]
    return any_of(bad) if bad else F.lit(False)


def check_functional_toilets_exceed_total(df, table, columns):
    return ((F.col("total_girls_func_toilet") > F.col("total_girls_toilet"))
            | (F.col("total_boys_func_toilet") > F.col("total_boys_toilet")))


def check_classroom_total_mismatch(df, table, columns):
    # pandas: NaN != x is True, so a missing rebuilt total counts as a mismatch (null-safe compare)
    return (F.col("total_class_rooms") > 0) & ~F.col("total_class_rooms").eqNullSafe(F.col("classrooms"))


def check_no_teachers_with_students(df, table, columns):
    return (F.col("total_tch") == 0) & (F.col("_enr_total") > 0)


def check_high_ptr(df, table, columns):
    return F.col("_enr_total") / F.when(F.col("total_tch") != 0, F.col("total_tch")) > C.PTR_FLAG


def check_undocumented_item(df, table, columns, enrol_items):
    documented = [f"{y}|{g}|{i}" for y, items in enrol_items.items() for g, i in items]
    key = F.concat_ws("|", "year", F.col("item_group").cast("string"), F.col("item_id").cast("string"))
    return ~key.isin(documented)


SPARK_CHECKS = {
    "missing_required": check_missing_required,
    "not_a_number": check_not_a_number,
    "negative_count": check_negative_count,
    "unknown_district": check_unknown_district,
    "duplicate_school_year": check_duplicate_key,
    "duplicate_enrolment_row": check_duplicate_key,
    "orphan_school": check_orphan_school,
    "unknown_management": check_unknown_management,
    "invalid_rural_urban": check_invalid_rural_urban,
    "invalid_code": check_invalid_code,
    "functional_toilets_exceed_total": check_functional_toilets_exceed_total,
    "classroom_total_mismatch": check_classroom_total_mismatch,
    "no_teachers_with_students": check_no_teachers_with_students,
    "high_ptr": check_high_ptr,
    "undocumented_item": check_undocumented_item,
}


def rules_for(table: str, severity: str):
    return [r for r in RULES if table in r.tables and r.severity == severity]


def prepare_rule_inputs(df: DataFrame, table: str, schools: DataFrame | None,
                        enrolment_total: DataFrame | None) -> DataFrame:
    """Join what the cross-table rules need: does the school exist, its class 1-12 enrolment."""
    names = {r.name for r in rules_for(table, ERROR) + rules_for(table, WARNING)}
    if "orphan_school" in names:
        df = df.join(schools.select(*KEY).distinct().withColumn("_school_exists", F.lit(True)),
                     on=KEY, how="left")
    if names & {"no_teachers_with_students", "high_ptr"}:
        df = (df.join(enrolment_total.withColumnRenamed("enr_total", "_enr_total"), on=KEY, how="left")
                .withColumn("_enr_total", F.coalesce("_enr_total", F.lit(0))))
    return df


def apply_rules(df: DataFrame, table: str, columns: dict, enrol_items: dict,
                schools: DataFrame | None = None, enrolment_total: DataFrame | None = None
                ) -> tuple[DataFrame, DataFrame]:
    """Returns (rows that stay, with flag_<rule> columns; quarantined rows with _rule, _reason)."""
    df = prepare_rule_inputs(df, table, schools, enrolment_total)
    errors = rules_for(table, ERROR)
    # Row-level rules judge each row on its own, so their reasons can be computed side by side;
    # taking the first non-NULL reason in rule order then gives the same answer as running them
    # one after another. (Defining each reason in terms of the earlier ones instead makes the
    # expression tree double with every rule once Spark inlines it.)
    row_level = [r for r in errors if SPARK_CHECKS[r.name] is not check_duplicate_key]
    df = df.select("*", *[SPARK_CHECKS[r.name](df, table, columns).alias(f"_reason_{r.name}") for r in row_level])
    # Duplicates compare rows with each other: judged only among rows that passed the rules before.
    for i, rule in enumerate(errors):
        if SPARK_CHECKS[rule.name] is check_duplicate_key:
            before = [f"_reason_{r.name}" for r in errors[:i]]
            df = df.withColumn("_passed", all_of([F.col(c).isNull() for c in before]) if before else F.lit(True))
            df = df.withColumn(f"_reason_{rule.name}",
                               F.when(F.col("_passed"), check_duplicate_key(df, table, columns)))
    reason_cols = [f"_reason_{r.name}" for r in errors]
    none = F.lit(None).cast("string")
    first_rule = F.coalesce(*[F.when(F.col(f"_reason_{r.name}").isNotNull(), F.lit(r.name)) for r in errors], none)
    df = df.withColumn("_rule", first_rule).withColumn("_reason", F.coalesce(*reason_cols, none))
    helpers = reason_cols + ["_passed", "_school_exists", "_enr_total"]
    quarantined = df.filter(F.col("_rule").isNotNull()).drop(*[c for c in helpers if c in df.columns])
    clean = df.filter(F.col("_rule").isNull())
    for rule in rules_for(table, WARNING):
        check = SPARK_CHECKS[rule.name]
        cond = (check(clean, table, columns, enrol_items) if rule.name == "undocumented_item"
                else check(clean, table, columns))
        clean = clean.withColumn(f"flag_{rule.name}", false_if_null(cond).cast("tinyint"))
    clean = clean.drop(*[c for c in helpers + ["_rule", "_reason", "_cast_errors"] if c in clean.columns])
    return clean, quarantined


# --- Build ----------------------------------------------------------------------

def class_columns(df: DataFrame) -> list[str]:
    """Enrolment columns for classes 1-12 (c1_b ... c12_t), not pre-primary (cpp_*)."""
    return [c for c in df.columns if c.startswith("c") and c[1:].split("_")[0].isdigit()]


def enrolment_totals(enrolment: DataFrame) -> DataFrame:
    """year, pseudocode, enr_total (classes 1-12) from silver enrolment."""
    row_total = sum(F.coalesce(F.col(c), F.lit(0)) for c in class_columns(enrolment))
    return enrolment.groupBy(*KEY).agg(F.sum(row_total).alias("enr_total"))


def build_silver(spark: SparkSession, bronze_root: str, schema: dict | None = None,
                 persist: Persist = no_persist, run_id: str = "") -> dict:
    """Every silver table from bronze. Returns {"silver": {table: df}, "quarantine": {table: df},
    "summary": {...}} with the same summary fields as pipeline.silver.build_silver."""
    schema = schema or C.load_reference("schema_columns.json")
    enrol_items = {y: {(i["item_group"], i["item_id"]) for i in items}
                   for y, items in schema["enrolment1_items"].items()}
    bronze_cache: dict[str, DataFrame] = {}
    silver, quarantine, summary = {}, {}, {"tables": {}}
    schools = enrolment_total = None

    for table, (dataset, where) in TABLES.items():
        bronze = bronze_cache.setdefault(dataset, read_bronze(spark, bronze_root, dataset))
        entries = column_entries(dataset, schema)
        df = typed(bronze, entries, where() if where else None)
        if table == "schools":
            df = standardise_schools(df)
        elif table == "facilities":
            df = facility_flags(df)
        columns = {c: e for c, e in entries.items() if c in df.columns}
        clean, quarantined = apply_rules(df, table, columns, enrol_items, schools, enrolment_total)
        clean = persist(f"silver_{table}", clean)
        quarantined = persist(f"quarantine_{table}", quarantined.withColumn("_run_id", F.lit(run_id)))
        silver[table], quarantine[table] = clean, quarantined

        if table == "schools":
            schools = clean
        if table == "enrolment":
            enrolment_total = enrolment_totals(clean)

        by_rule = dict(quarantined.groupBy("_rule").count().collect())
        flags = [c for c in clean.columns if c.startswith("flag_")]
        flagged = clean.agg(*[F.sum(c).alias(c) for c in flags]).first().asDict() if flags else {}
        rows_silver, rows_q = clean.count(), sum(by_rule.values())
        summary["tables"][table] = {
            "source": dataset,
            "rows_in": rows_silver + rows_q,
            "rows_silver": rows_silver,
            "rows_quarantined": rows_q,
            "quarantined_by_rule": {r.name: int(by_rule.get(r.name, 0)) for r in rules_for(table, ERROR)},
            "flagged_by_rule": {c.removeprefix("flag_"): int(v or 0) for c, v in flagged.items()},
        }
    summary["reconciled"] = reconcile(bronze_cache, summary["tables"])
    return {"silver": silver, "quarantine": quarantine, "summary": summary}


def reconcile(bronze: dict[str, DataFrame], tables: dict) -> dict:
    """Every bronze row ends up in silver or quarantine: bronze = silver + quarantine, per dataset."""
    out = {}
    for dataset, df in bronze.items():
        rows = df.count()
        landed = sum(t["rows_silver"] + t["rows_quarantined"] for t in tables.values() if t["source"] == dataset)
        if rows != landed:
            raise RuntimeError(f"{dataset}: {rows:,} bronze rows but {landed:,} in silver + quarantine")
        out[dataset] = {"bronze": rows, "silver_plus_quarantine": landed}
    return out
