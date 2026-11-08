"""Build the lakehouse in Athena: bronze tables -> silver (+ quarantine) -> gold star schema.

    python -m aws.athena_build                      every step
    python -m aws.athena_build --steps gold,tests   only some steps

Steps (all in the Glue database udise, workgroup udise-wg, bucket udise-lake-aditya-2026):
    bronze      register one external table per bronze dataset over s3://.../bronze/<dataset>/,
                partitioned by year (bronze_profile1, bronze_enrolment1, bronze_facility,
                bronze_teacher). Columns come from the bronze contract. Metadata only.
    reference   ref_districts, ref_district_aliases, ref_management from reference/*.json
                (Parquet under reference/).
    silver      silver_<table> and quarantine_<table>, the same tables, typing, cleaning and
                rules as pipeline/silver.py + pipeline/rules.py. The rule catalogue (names, order,
                severity, tables) is imported from pipeline/rules.py; the checks are written
                again as SQL (as spark/silver.py does for Spark). Error rules: a row is
                quarantined by the first rule it fails (row-level rules are evaluated side by
                side and the first one in rule order wins; duplicates are judged only among rows
                that passed the rules before). Silver: Parquet under silver/<table>/year=<Y>/;
                quarantined rows under quarantine/rows/<table>/.
    gold        the star schema and marts from sql/athena/*.sql (CTAS, Parquet under
                gold/<table>/; facts partitioned by year_key).
    tests       sql/athena/tests/*.sql: every check must return 0 failures.
    validation  sql/athena/validation/: the official UDISE+ indicators vs
                reference/official_totals.json (pipeline.validation.compare).

Writes reports/aws_build.json: row counts, rule counts, test results, validation and, per
step, the number of queries, seconds and bytes Athena scanned (aggregates only; no
school-level data). Exit code 1 if a data test fails or an official figure differs.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

from pipeline import config as C
from pipeline.rules import ENROLMENT_TABLES, ERROR, STATE_FILE, WARNING, code_set, rules_for

from . import settings as S
from .athena import Athena
from .bronze_validator.validator import load_contract

SQL_DIR = C.ROOT / "sql" / "athena"
REPORT = C.REPORTS_DIR / "aws_build.json"
STEPS = ["bronze", "reference", "silver", "gold", "tests", "validation"]

# Same tables, order and bronze sources as pipeline/silver.py TABLES. Schools first (the others
# are checked against it), enrolment before teachers (pupil-teacher ratio).
ITEM_GROUP = "coalesce(try_cast(item_group as bigint), -1)"
SILVER_TABLES = {
    "schools": ("profile1", None),
    "enrolment": ("enrolment1", f"{ITEM_GROUP} = {C.ENROL_TOTAL_GROUP}"),
    "enrolment_other": ("enrolment1", f"{ITEM_GROUP} <> {C.ENROL_TOTAL_GROUP}"),
    "facilities": ("facility", None),
    "teachers": ("teacher", None),
}
KEY = ["year", "pseudocode"]
INT_TYPES = {"smallint", "integer", "bigint"}
FLOAT_TYPES = {"double precision", "numeric", "real"}
BRONZE_TYPES = {"string": "string", "int64": "bigint", "timestamp[us, tz=UTC]": "timestamp"}
# \A and \z: start and end of the whole text (^ and $ can also match at line breaks).
WHOLE = r"\A-?[0-9]+\z"
EDGE_SPACES = r"\A\s+|\s+\z"
ENROLMENT_COLUMN = re.compile(r"^c(pp|[0-9]+)_[bgt]$")
CLASS_COLUMN = re.compile(r"^c[0-9]+_[bgt]$")  # classes 1-12, not pre-primary


# --- SQL text helpers ---------------------------------------------------------------------------

def q(col: str) -> str:
    return f'"{col}"'


def lit(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def in_list(values) -> str:
    return ", ".join(lit(v) for v in values)


def case_map(expr: str, mapping: dict, default: str = "NULL") -> str:
    whens = " ".join(f"WHEN {expr} = {lit(k)} THEN {lit(v)}" for k, v in mapping.items())
    return f"CASE {whens} ELSE {default} END"


def join_texts(parts: list[str], sep: str = "; ") -> str:
    """concat_ws over many expressions (NULLs skipped), in chunks below Athena's argument limit."""
    if not parts:
        return "''"
    if len(parts) <= 100:
        return f"concat_ws({lit(sep)}, {', '.join(parts)})"
    chunks = [f"nullif({join_texts(parts[i:i + 100], sep)}, '')" for i in range(0, len(parts), 100)]
    return f"concat_ws({lit(sep)}, {', '.join(chunks)})"


def any_of(conds: list[str]) -> str:
    return "(" + " OR ".join(conds) + ")" if conds else "false"


def strip_comment_lines(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")).strip()


# --- Bronze -------------------------------------------------------------------------------------

def bronze_layout(contract: dict) -> dict[str, dict]:
    """dataset -> {"columns": [(name, athena type)], "years": [...]}, columns in first-seen order."""
    out: dict[str, dict] = {}
    for key in sorted(contract["files"]):
        f = contract["files"][key]
        d = out.setdefault(f["dataset"], {"columns": [], "years": set()})
        d["years"].add(f["year"])
        seen = {n for n, _ in d["columns"]}
        for name, arrow_type in contract["schemas"][f["schema"]]:
            if name not in seen:
                d["columns"].append((name, BRONZE_TYPES[arrow_type]))
                seen.add(name)
    for d in out.values():
        d["years"] = sorted(d["years"])
    return out


def register_bronze(ath: Athena, layout: dict) -> dict:
    for dataset, d in layout.items():
        table, location = f"bronze_{dataset}", f"s3://{ath.bucket}/{S.BRONZE_PREFIX}{dataset}/"
        ath.drop(table)  # external table: dropping it removes only the catalog entry, not the files
        cols = ",\n  ".join(f"`{n}` {t}" for n, t in d["columns"])
        ath.execute(f"CREATE EXTERNAL TABLE {table} (\n  {cols}\n)\nPARTITIONED BY (`year` string)\n"
                    f"STORED AS PARQUET\nLOCATION '{location}'", f"register {table}")
        parts = " ".join(f"PARTITION (`year` = '{y}') LOCATION '{location}year={y}/'" for y in d["years"])
        ath.execute(f"ALTER TABLE {table} ADD IF NOT EXISTS {parts}", f"partitions {table}")
    return {f"bronze_{ds}": ath.count(f"bronze_{ds}") for ds in layout}


# --- Reference tables ---------------------------------------------------------------------------

def values_sql(rows: list[tuple], columns: list[tuple[str, str]]) -> str:
    body = ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows)
    select = ", ".join(f"cast({n} as {t}) as {n}" for n, t in columns)
    return f"SELECT {select} FROM (VALUES {body}) AS t({', '.join(n for n, _ in columns)})"


def build_reference(ath: Athena) -> dict:
    mgmt = C.load_reference("management_codes.json")["codes"]
    tables = {
        "ref_districts": values_sql([(d,) for d in C.DISTRICTS], [("district_name", "varchar")]),
        "ref_district_aliases": values_sql(list(C.DISTRICT_ALIASES.items()),
                                           [("alias", "varchar"), ("district_name", "varchar")]),
        "ref_management": values_sql([(int(k), v["group"], bool(v["is_government"])) for k, v in mgmt.items()],
                                     [("code", "integer"), ("management_group", "varchar"),
                                      ("is_government", "boolean")]),
    }
    for name, sql in tables.items():
        ath.ctas(name, sql, f"reference/{name[4:]}/")
    return {name: ath.count(name) for name in tables}


# --- Silver: typing, cleaning, rules ------------------------------------------------------------

def is_whole_number(entry: dict) -> bool:
    return entry.get("type", "") in INT_TYPES or entry.get("kind") in ("yes_no", "enum")


def column_entries(dataset: str, schema: dict) -> dict[str, dict]:
    """Schema entry per column, over all years (as pipeline/silver.py)."""
    out = {}
    for _, cols in sorted(schema["datasets"].get(dataset, {}).items()):
        out.update({e["column"]: e for e in cols})
    return out


def typed_sql(dataset: str, source_cols: list[str], entries: dict, where: str | None) -> str:
    """bronze text -> types; values that are not whole numbers / numbers go to _cast_errors."""
    select = [f"{q('_year')} AS year", f"{q('_district_file')} AS district_file", q("_source_file"),
              q("_source_row")]
    errors = []
    for col in source_cols:
        c, entry = q(col), entries.get(col, {})
        if is_whole_number(entry):
            ok = f"regexp_like(trim({c}), {lit(WHOLE)})"
            select.append(f"CASE WHEN {ok} THEN CAST(trim({c}) AS bigint) END AS {c}")
            errors.append(f"CASE WHEN {c} IS NOT NULL AND NOT {ok} THEN {lit(col + '=')} || {c} END")
        elif entry.get("type", "") in FLOAT_TYPES:
            select.append(f"try_cast({c} AS double) AS {c}")
            errors.append(f"CASE WHEN {c} IS NOT NULL AND try_cast({c} AS double) IS NULL "
                          f"THEN {lit(col + '=')} || {c} END")
        else:
            select.append(c)
    select.append(f"{join_texts(errors)} AS _cast_errors")
    return (f"SELECT {', '.join(select)}\nFROM bronze_{dataset}"
            + (f"\nWHERE {where}" if where else ""))


def schools_columns(cols: list[str]) -> dict[str, str]:
    """Output column -> expression for silver schools (pipeline.silver.standardise_schools)."""
    cleaned = f"regexp_replace(upper(regexp_replace(district, {lit(EDGE_SPACES)}, '')), '\\s+', ' ')"
    block = f"upper(regexp_replace(block, {lit(EDGE_SPACES)}, ''))"
    admin = f"coalesce(strpos({block}, 'REGION') > 0, false)"
    group = case_map("managment", C.MANAGEMENT_GROUP, lit("Unmapped"))
    out = {c: q(c) for c in cols}
    out.update({
        "district": case_map(cleaned, C.DISTRICT_ALIASES, cleaned),
        "block": block,
        "district_raw": "district",
        "block_is_admin_region": admin,
        "block_clean": f"CASE WHEN NOT {admin} THEN {block} END",
        "location": case_map("rural_urban", C.RURAL_URBAN),
        "school_type_label": case_map("school_type", C.SCHOOL_TYPE),
        "management_group": group,
        "is_govt": f"CAST({group} IN ({in_list(C.GOVT_GROUPS)}) AS integer)",
    })
    return out


def facilities_columns(cols: list[str]) -> dict[str, str]:
    """Output column -> expression for silver facilities (pipeline.silver.facility_flags)."""
    def flag(cond):  # 1/0 as double, a missing comparison counts as 0
        return f"CAST(CAST(coalesce({cond}, false) AS integer) AS double)"

    def yes_no(c):  # 1 -> 1, 2 -> 0, anything else (e.g. 9 = not applicable) -> missing
        return f"CASE {q(c)} WHEN 1 THEN CAST(1 AS double) WHEN 2 THEN CAST(0 AS double) END"

    classrooms = "(classrooms_in_good_condition + classrooms_needs_minor_repair + classrooms_needs_major_repair)"
    # pandas .round(1) rounds half to even on the value times 10 (numpy); Athena's round() rounds
    # half away from zero, so do it by hand: y = value * 10, then halves go to the even neighbour.
    y = f"(CAST(classrooms_needs_major_repair AS double) / nullif({classrooms}, 0) * 100 * 10)"
    half_even = (f"(CASE WHEN {y} - floor({y}) = 0.5 THEN "
                 f"(CASE WHEN mod(floor({y}), 2) = 0 THEN floor({y}) ELSE floor({y}) + 1 END) "
                 f"ELSE round({y}) END) / 10")
    out = {c: q(c) for c in cols}
    out.update({
        "has_electricity": flag("electricity_availability = 1"),  # 3 = available but not functional
        "has_safe_water": flag(any_of([f"coalesce({c} = 1, false)" for c in C.SAFE_WATER_COLS])),
        "has_girls_toilet": flag("total_girls_func_toilet + func_girls_cwsn_friendly > 0"),
        "has_boys_toilet": flag("total_boys_func_toilet + func_boys_cwsn_friendly > 0"),
        "has_handwash": yes_no("handwash_near_toilet"),
        "has_library": yes_no("library_availability"),
        "has_playground": yes_no("playground_available"),
        "has_ramp": yes_no("availability_ramps"),
        "has_internet": yes_no("internet"),
        "has_computer": flag("coalesce(laptop, 0) + coalesce(desktop, 0) + coalesce(tablet, 0) > 0"),
        "classrooms": classrooms,
        "classrooms_good": "classrooms_in_good_condition",
        "pct_classrooms_major_repair": half_even,
    })
    return out


def count_columns(cols: list[str], entries: dict) -> list[str]:
    """Numeric measure columns (counts), not codes (pipeline.rules.count_columns)."""
    return [c for c, e in entries.items() if e["kind"] == "numeric" and c in cols
            and c not in ("pseudocode", "item_group", "item_id")]


def describe_cells(bad: dict[str, str]) -> str:
    """'col=value; col=value' for the columns whose condition holds; NULL if none."""
    if not bad:
        return "CAST(NULL AS varchar)"
    texts = [f"CASE WHEN {cond} THEN {lit(c + '=')} || CAST({q(c)} AS varchar) END" for c, cond in bad.items()]
    return f"CASE WHEN {any_of(list(bad.values()))} THEN {join_texts(texts)} END"


def error_reason(rule: str, table: str, cols: list[str], entries: dict) -> str:
    """SQL for a row-level error rule: the reason a row fails, or NULL (pipeline.rules checks)."""
    if rule == "missing_required":
        required = KEY + (["district"] if table == "schools" else [])
        names = ", ".join(f"CASE WHEN {q(c)} IS NULL THEN {lit(c)} END" for c in required)
        return (f"CASE WHEN {any_of([f'{q(c)} IS NULL' for c in required])} "
                f"THEN 'missing ' || concat_ws(', ', {names}) END")
    if rule == "not_a_number":
        return "CASE WHEN _cast_errors IS NOT NULL AND _cast_errors <> '' THEN _cast_errors END"
    if rule == "negative_count":
        return describe_cells({c: f"{q(c)} < 0" for c in count_columns(cols, entries)})
    if rule == "unknown_district":
        return (f"CASE WHEN NOT coalesce(district IN ({in_list(C.DISTRICTS)}), false) "
                f"THEN 'district ' || district || ' is not one of the 41 districts' END")
    if rule == "orphan_school":
        return "CASE WHEN NOT _school_exists THEN 'school not in silver schools for this year' END"
    raise KeyError(rule)


def warning_condition(rule: str, cols: list[str], entries: dict, enrol_items: dict) -> str:
    if rule == "unknown_management":
        return f"managment IS NOT NULL AND managment NOT IN ({in_list(C.MANAGEMENT_GROUP)})"
    if rule == "invalid_rural_urban":
        return f"rural_urban IS NOT NULL AND rural_urban NOT IN ({in_list(C.RURAL_URBAN)})"
    if rule == "invalid_code":
        return any_of([f"({q(c)} IS NOT NULL AND {q(c)} NOT IN ({in_list(sorted(code_set(e)))}))"
                       for c, e in entries.items() if code_set(e) and c in cols])
    if rule == "functional_toilets_exceed_total":
        return ("(total_girls_func_toilet > total_girls_toilet) "
                "OR (total_boys_func_toilet > total_boys_toilet)")
    if rule == "classroom_total_mismatch":
        # pandas: NaN != x is True, so a missing rebuilt total counts as a mismatch
        return "total_class_rooms > 0 AND total_class_rooms IS DISTINCT FROM classrooms"
    if rule == "no_teachers_with_students":
        return "total_tch = 0 AND _enr_total > 0"
    if rule == "high_ptr":
        return f"CAST(_enr_total AS double) / nullif(total_tch, 0) > {C.PTR_FLAG}"
    if rule == "undocumented_item":
        documented = [f"{y}|{g}|{i}" for y, items in enrol_items.items() for g, i in items]
        return (f"concat_ws('|', year, CAST(item_group AS varchar), CAST(item_id AS varchar)) "
                f"NOT IN ({in_list(sorted(documented))})")
    raise KeyError(rule)


def silver_sql(table: str, dataset: str, where: str | None, source_cols: list[str], entries: dict,
               enrol_items: dict, class_cols: list[str], run_id: str) -> tuple[str, str]:
    """(SELECT for silver_<table>, SELECT for quarantine_<table>), both with year last."""
    typed = typed_sql(dataset, source_cols, entries, where)
    meta = ["district_file", "_source_file", "_source_row"]
    if table == "schools":
        outputs = schools_columns(meta + source_cols)
    elif table == "facilities":
        outputs = facilities_columns(meta + source_cols)
    else:
        outputs = {c: q(c) for c in meta + source_cols}
    cols = list(outputs)                         # every silver column except year
    std = ("SELECT year, " + ", ".join(f"{e} AS {q(c)}" for c, e in outputs.items())
           + ", _cast_errors FROM typed")
    col_entries = {c: e for c, e in entries.items() if c in cols}

    errors = [r.name for r in rules_for(table, ERROR)]
    warnings = [r.name for r in rules_for(table, WARNING)]
    joins, extra = [], []
    if "orphan_school" in errors:
        joins.append("LEFT JOIN (SELECT DISTINCT year AS _o_year, pseudocode AS _o_code FROM silver_schools) o "
                     "ON o._o_year = std.year AND o._o_code = std.pseudocode")
        extra.append("o._o_code IS NOT NULL AS _school_exists")
    if {"no_teachers_with_students", "high_ptr"} & set(warnings):
        row_total = " + ".join(f"coalesce({q(c)}, 0)" for c in class_cols)
        joins.append(f"LEFT JOIN (SELECT year AS _e_year, pseudocode AS _e_code, sum({row_total}) AS _enr_total "
                     f"FROM silver_enrolment GROUP BY year, pseudocode) e "
                     f"ON e._e_year = std.year AND e._e_code = std.pseudocode")
        extra.append("coalesce(e._enr_total, 0) AS _enr_total")
    prep = "SELECT std.*" + "".join(f", {x}" for x in extra) + " FROM std " + " ".join(joins)

    # r1: the row-level error rules side by side (each row judged on its own).
    duplicate = next((r for r in errors if r.startswith("duplicate_")), None)
    row_level = [r for r in errors if r != duplicate]
    reasons = [f"{error_reason(r, table, cols, col_entries)} AS {q('_reason_' + r)}" for r in row_level]
    r1 = "SELECT prep.*, " + ", ".join(reasons) + " FROM prep"
    # r2: did the row pass every rule before the duplicate rule? (only those rows compete)
    before = errors[:errors.index(duplicate)] if duplicate else []
    passed = " AND ".join(f"{q('_reason_' + r)} IS NULL" for r in before) or "true"
    r2 = f"SELECT r1.*, ({passed}) AS _passed FROM r1"
    # r3: duplicates. Extra copies of a key; the copy from the state-wide file is kept, else the
    # first in file order.
    if duplicate:
        key = KEY + (["item_group", "item_id"] if table in ENROLMENT_TABLES else [])
        window = (f"PARTITION BY {', '.join(q(k) for k in key)}, _passed "
                  f"ORDER BY (district_file <> {lit(STATE_FILE)}), {q('_source_file')}, {q('_source_row')}")
        r3 = (f"SELECT r2.*, CASE WHEN _passed AND row_number() OVER ({window}) > 1 THEN "
              f"'extra copy; kept the row from ' || first_value({q('_source_file')}) OVER ({window} "
              f"ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) END AS {q('_reason_' + duplicate)} "
              f"FROM r2")
    else:
        r3 = "SELECT * FROM r2"
    # judged: the first rule (in catalogue order) that gave a reason.
    first_rule = ", ".join(f"CASE WHEN {q('_reason_' + r)} IS NOT NULL THEN {lit(r)} END" for r in errors)
    first_reason = ", ".join(q("_reason_" + r) for r in errors)
    judged = (f"SELECT r3.*, coalesce({first_rule}, CAST(NULL AS varchar)) AS _rule, "
              f"coalesce({first_reason}, CAST(NULL AS varchar)) AS _reason FROM r3")

    with_clause = (f"WITH typed AS (\n{typed}\n),\nstd AS ({std}),\nprep AS ({prep}),\n"
                   f"r1 AS ({r1}),\nr2 AS ({r2}),\nr3 AS ({r3}),\njudged AS ({judged})\n")
    flags = [f"CAST(coalesce({warning_condition(r, cols, col_entries, enrol_items)}, false) AS tinyint) "
             f"AS {q('flag_' + r)}" for r in warnings]
    out_cols = ", ".join(q(c) for c in cols)
    silver = (with_clause + f"SELECT {out_cols}" + "".join(f", {f}" for f in flags)
              + ", year FROM judged WHERE _rule IS NULL")
    quarantine = (with_clause + f"SELECT {out_cols}, _cast_errors, _rule, _reason, {lit(run_id)} AS _run_id, "
                  f"year FROM judged WHERE _rule IS NOT NULL")
    return silver, quarantine


def build_silver(ath: Athena, layout: dict, run_id: str) -> dict:
    schema = C.load_reference("schema_columns.json")
    enrol_items = {y: {(i["item_group"], i["item_id"]) for i in items}
                   for y, items in schema["enrolment1_items"].items()}
    class_cols = [c for c, _ in layout["enrolment1"]["columns"] if CLASS_COLUMN.match(c)]
    out = {}
    for table, (dataset, where) in SILVER_TABLES.items():
        source_cols = [c for c, _ in layout[dataset]["columns"] if not c.startswith("_")]
        silver, quarantine = silver_sql(table, dataset, where, source_cols, column_entries(dataset, schema),
                                        enrol_items, class_cols, run_id)
        ath.ctas(f"silver_{table}", silver, f"silver/{table}/", partitioned_by="year")
        ath.ctas(f"quarantine_{table}", quarantine, f"quarantine/rows/{table}/", partitioned_by="year")
        by_rule = ath.query_df(f"SELECT _rule, count(*) AS n FROM quarantine_{table} GROUP BY _rule",
                               f"rules {table}")
        out[table] = {"source": f"bronze_{dataset}", "rows_silver": ath.count(f"silver_{table}"),
                      "rows_quarantined": int(by_rule["n"].sum()) if len(by_rule) else 0,
                      "quarantined_by_rule": {r: int(n) for r, n in zip(by_rule["_rule"], by_rule["n"])}}
    return out


# --- Gold, tests, validation --------------------------------------------------------------------

BLOCK = re.compile(r"^-- (table|view): (\w+)(?: partitioned_by=(\w+))?\s*$", re.M)


def sql_blocks(text: str):
    """(kind, name, partitioned_by, select) for each '-- table:' / '-- view:' block of a file."""
    marks = list(BLOCK.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield m.group(1), m.group(2), m.group(3), strip_comment_lines(text[m.end():end])


def placeholders(ath: Athena) -> dict[str, str]:
    enrol = [c for c, _ in ath.columns("silver_enrolment") if ENROLMENT_COLUMN.match(c)]
    excluded = {"pseudocode", "year", "district_file", "_source_file", "_source_row"}
    teachers = [c for c, _ in ath.columns("silver_teachers") if c not in excluded]
    return {
        "enrolment_column_names": ", ".join(lit(c) for c in enrol),
        "enrolment_columns": ", ".join(f"e.{q(c)}" for c in enrol),
        "enrolment_row_sum": " + ".join(f"coalesce({q(c)}, 0)" for c in enrol),
        "teacher_columns": ", ".join(f"t.{q(c)}" for c in teachers),
    }


def fill(sql: str, values: dict[str, str]) -> str:
    def sub(m):
        return values[m.group(1)]
    return re.sub(r"\{\{(\w+)\}\}", sub, sql)


def build_gold(ath: Athena, values: dict[str, str]) -> dict:
    tables, views = [], []
    for path in sorted(SQL_DIR.glob("*.sql")):
        for kind, name, part, select in sql_blocks(path.read_text(encoding="utf-8")):
            select = fill(select, values)
            if kind == "view":
                ath.view(name, select)
                views.append(name)
            else:
                ath.ctas(name, select, f"gold/{name}/", part)
                tables.append(name)
    for name in views:  # helpers only (the marts' shared GROUPING SETS query)
        ath.drop(name, view=True)
    return {t: ath.count(t) for t in tables}


def run_tests(ath: Athena, values: dict[str, str]) -> list[dict]:
    results = []
    for path in sorted((SQL_DIR / "tests").glob("*.sql")):
        df = ath.query_df(fill(strip_comment_lines(path.read_text(encoding="utf-8")), values), path.name)
        results += [{"file": path.name, "check": c, "failures": int(n)}
                    for c, n in zip(df["check_name"], df["failures"])]
    return results


def validation_sql() -> str:
    return strip_comment_lines((SQL_DIR / "validation" / "01_official_indicators.sql").read_text(encoding="utf-8"))


def run_validation(ath: Athena) -> dict:
    from pipeline.validation import compare
    table = compare(ath.query_df(validation_sql(), "official indicators"), C.OFFICIAL)
    checked = table[table["result"] != "no official figure"]
    return {"indicators_checked": len(checked),
            "exact_matches": int((checked["result"] == "exact match").sum()),
            "rows": [{k: (None if v != v else (int(v) if isinstance(v, float) and v == int(v) else v))
                      for k, v in r.items()} for r in table.to_dict("records")]}


# --- Main ---------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build bronze, silver and gold in Athena.")
    ap.add_argument("--profile", default=S.PROFILE)
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated, from: {','.join(STEPS)}")
    args = ap.parse_args(argv)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(steps) - set(STEPS)
    if unknown:
        ap.error(f"unknown steps: {sorted(unknown)}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ath = Athena(args.profile)
    layout = bronze_layout(load_contract())
    report: dict = {"run_id": run_id, "steps": {}, "region": S.REGION, "workgroup": S.WORKGROUP}
    t_all = time.perf_counter()
    ok = True
    for step in STEPS:
        if step not in steps:
            continue
        print(f"[{step}] ...", flush=True)
        t0, n0 = time.perf_counter(), len(ath.log)
        if step == "bronze":
            result = register_bronze(ath, layout)
        elif step == "reference":
            result = build_reference(ath)
        elif step == "silver":
            result = build_silver(ath, layout, run_id)
        elif step == "gold":
            result = build_gold(ath, placeholders(ath))
        elif step == "tests":
            result = run_tests(ath, placeholders(ath))
            failed = [t for t in result if t["failures"]]
            ok &= not failed
            for t in failed:
                print(f"  FAILED {t['file']}: {t['check']} ({t['failures']:,})")
        else:
            result = run_validation(ath)
            ok &= result["exact_matches"] == result["indicators_checked"]
            print(f"  official figures: {result['exact_matches']} of {result['indicators_checked']} exact")
        log = ath.log[n0:]
        report["steps"][step] = {"result": result, "queries": len(log),
                                 "seconds": round(time.perf_counter() - t0, 1),
                                 "bytes_scanned": sum(q["bytes_scanned"] for q in log)}
        print(f"[{step}] {len(log)} queries, {report['steps'][step]['seconds']} s, "
              f"{report['steps'][step]['bytes_scanned'] / 1e6:.1f} MB scanned", flush=True)
    report["total"] = {**ath.summary(), "wall_seconds": round(time.perf_counter() - t_all, 1)}
    report["passed"] = ok
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{'PASSED' if ok else 'FAILED'}: {report['total']['queries']} queries, "
          f"{report['total']['bytes_scanned'] / 1e6:.1f} MB scanned; report {C.display_path(REPORT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
