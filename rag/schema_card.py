"""Write rag/schema_card.md: what the agent reads before it writes SQL for rag/sql_tool.py.

    python -m rag.schema_card                     (full gold database, see UDISE_GOLD_DB)
    python -m rag.schema_card --gold-db PATH --out PATH

Table and column names, types and row counts come from the gold database itself, so the card
always matches the real schema. Column meanings are reused from scripts/make_data_dictionary.py
(one source of truth); fact_teachers' 37 count columns are grouped here to keep the card
short. Code lists (years, grades, management codes...) are read from the dimension tables.
A column with no description stops the script, as in the data dictionary.

The committed card is built from the full database. Only aggregate metadata goes in it (row
counts, codes); no school codes and no school-level values.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from scripts import make_data_dictionary as dd  # column meanings: one source of truth

from .config import ROOT
from .sql_tool import ALLOWED_TABLES, HIDDEN_COLUMNS, MAX_ROWS, default_gold_db

OUT = ROOT / "rag" / "schema_card.md"
SOURCE_NOTE = "Source: UDISE+, Ministry of Education, Government of India"


# Join keys per table: column -> what it points to.
JOINS = {
    "dim_district": {}, "dim_year": {}, "dim_management": {}, "dim_grade": {}, "dim_gender": {},
    "dim_social_category": {},
    "dim_school": {"district_key": "dim_district", "management_key": "dim_management"},
    "fact_enrolment": {"school_key": "dim_school", "year_key": "dim_year", "grade_key": "dim_grade",
                       "gender_key": "dim_gender", "social_category_key": "dim_social_category"},
    "fact_teachers": {"school_key": "dim_school", "year_key": "dim_year"},
    "fact_school_facilities": {"school_key": "dim_school", "year_key": "dim_year"},
    "int_school_year": {"school_key": "dim_school", "year_key": "dim_year",
                        "district_key": "dim_district", "management_key": "dim_management"},
    "mart_district_year": {}, "mart_district_year_management": {},
}
ORDER = ["int_school_year", "mart_district_year", "mart_district_year_management",
         "fact_enrolment", "fact_teachers", "fact_school_facilities", "dim_school", "dim_district",
         "dim_management", "dim_year", "dim_grade", "dim_gender", "dim_social_category"]
USE_FOR = {
    "int_school_year": "**Start here** for most questions: one row per school and year, already joined "
                       "to district, management, location, enrolment by stage and sex, teachers and facilities.",
    "mart_district_year": "District-level indicators (ratios and shares ready to use).",
    "mart_district_year_management": "The same indicators split by management group.",
    "fact_enrolment": "Enrolment by grade, gender and social category (finest detail).",
    "fact_teachers": "Teacher counts by sex, category, appointment, qualification, classes taught.",
    "fact_school_facilities": "Facility flags, classrooms and toilets per school-year.",
}

# fact_teachers: 37 count columns from the UDISE+ teacher file, grouped (each column = number of
# teachers in the school with that attribute). Every column must appear in exactly one group.
TEACHER_GROUPS = {
    "by sex": ["male", "female", "transgender"],
    "by social category": ["gen_tch", "sc_tch", "st_tch", "obc_tch"],
    "by appointment": ["regular", "contract", "part_time"],
    "highest academic qualification": ["below_graduate", "graduate", "post_graduate_and_above"],
    "highest professional qualification": [
        "diploma_certificate", "diploma_ele_edu", "bachelor_of_ee", "bed_equivalent", "med_equivalent",
        "early_childhood_tch", "bed_nursery", "diploma_special_edu", "pursuing_rpc", "other", "none"],
    "classes taught (pr = 1-5, upr = 6-8, sec = 9-10, hsec = 11-12; `class_taugt_...` is spelled so "
    "in the source)": [
        "class_taught_pr", "class_taught_upr", "class_taught_pr_upr", "class_taught_sec_only",
        "class_taught_hsec_only", "class_taught_upr_sec", "class_taught_sec_hsec",
        "class_taugt_pre_primary_only", "class_taught_pr_and_pre_pri"],
    "training and duties": ["trained_comp", "trained_cwsn", "teacher_involve_non_training_assignment"],
}

# Card-specific wording where the data dictionary's text is not enough for writing SQL.
OVERRIDES = {
    "school_category": "UDISE+ school category code; classes taught per code below (Codes).",
    "school_type": "1 = Boys, 2 = Girls, 3 = Co-educational.",
    "students": "Students (> 0; a missing row means 0). Sum over social categories for totals.",
    "is_government": "True for State, Central and Other Government (see Codes).",
    "total_tch": "Teachers in the school.",
}

SHORT_TYPES = {"INTEGER": "int", "BIGINT": "int", "HUGEINT": "int", "TINYINT": "int",
               "SMALLINT": "int", "DOUBLE": "double", "VARCHAR": "text", "BOOLEAN": "bool",
               "VARCHAR[]": "text[]"}


def _short_type(t: str) -> str:
    return SHORT_TYPES.get(t, "decimal" if t.startswith("DECIMAL") else t.lower())


# Five worked examples: question -> SQL. tests/test_rag_sql.py runs every one through the tool.
EXAMPLES = [
    ("How many schools, students (official definition, pre-primary included) and teachers did "
     "Rajasthan have in 2024-25?",
     """select count(*)                            as schools,
       sum(enr_total + enr_pre_primary)    as students_incl_pre_primary,
       sum(total_tch)                      as teachers
from int_school_year
where academic_year = '2024-25'"""),
    ("Which five districts had the highest pupil-teacher ratio in 2025-26?",
     """select district, ptr, enrolment, teachers
from mart_district_year
where year = '2025-26'
order by ptr desc
limit 5"""),
    ("How many girls were enrolled in classes 9-12 in each social category in 2024-25?",
     """select sc.social_category, sum(f.students) as girls
from fact_enrolment f
join dim_year y             using (year_key)
join dim_grade g            using (grade_key)
join dim_gender ge          using (gender_key)
join dim_social_category sc using (social_category_key)
where y.academic_year = '2024-25'
  and g.stage in ('secondary', 'higher_secondary')
  and ge.gender_code = 'G'
group by sc.social_category
order by girls desc"""),
    ("In each district, how many government schools had no working electricity in 2025-26, and "
     "what share of the government schools that reported it is that?",
     """select district_name,
       count(*) filter (where has_electricity = 0)         as schools_without_electricity,
       round(100 * (1 - avg(has_electricity)), 1)          as pct_without_electricity
from int_school_year
where academic_year = '2025-26' and is_government
group by district_name
order by schools_without_electricity desc"""),
    ("How did single-teacher schools change between 2024-25 and 2025-26, by management group?",
     """select management_group,
       sum(single_teacher) filter (where academic_year = '2024-25') as single_teacher_2024_25,
       sum(single_teacher) filter (where academic_year = '2025-26') as single_teacher_2025_26
from int_school_year
group by management_group
order by single_teacher_2025_26 desc"""),
]


def _columns(con, table: str) -> list[tuple[str, str]]:
    return [(c, t) for c, t in con.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = 'main' and table_name = ? order by ordinal_position", [table]).fetchall()
        if c not in HIDDEN_COLUMNS]


def _codes(con, facilities: dict) -> list[str]:
    def rows(sql):
        return con.execute(sql).fetchall()
    years = ", ".join(f"{k} = {y}" for k, y in rows("select year_key, academic_year from dim_year order by 1"))
    stages = {}
    for key, label, stage in rows("select grade_key, grade_code, stage from dim_grade order by 1"):
        stages.setdefault(stage, []).append((key, label))
    grade = "; ".join(f"{s}: grade_key {v[0][0]}" + (f"-{v[-1][0]}" if len(v) > 1 else "") +
                      (" (grade_code PP)" if s == "pre_primary" else "") for s, v in stages.items())
    gender = ", ".join(f"{k} = {c} ({lbl})" for k, c, lbl in
                       rows("select gender_key, gender_code, gender_label from dim_gender order by 1"))
    social = ", ".join(f"{k} = {c}" for k, c in
                       rows("select social_category_key, social_category from dim_social_category order by 1"))
    groups = {}
    for code, group, gov in rows("select management_code, management_group, is_government "
                                 "from dim_management order by management_code nulls first"):
        groups.setdefault((group, gov), []).append("none (key 0)" if code is None else str(code))
    mgmt = [f"  - {g} ({'government' if gov else 'non-government'}): codes {', '.join(c)}"
            for (g, gov), c in groups.items()]
    cats = rows("""select s.school_category, mode(f.lowclass), mode(f.highclass), count(*)
                   from fact_school_facilities f join dim_school s using (school_key)
                   group by 1 order by 1""")

    def cls(n):
        return "PP" if n == 0 else str(n)
    categories = ", ".join(f"{c} = {cls(lo)}-{cls(hi)}" if lo != hi else f"{c} = {cls(lo)} only"
                           for c, lo, hi, _ in cats)
    return [
        "## Codes", "",
        f"- **Years** (dim_year): {years}. Filter with `academic_year = '2024-25'` "
        "(`year` in the marts).",
        f"- **Grades** (dim_grade): {grade}. Stages: primary 1-5, upper_primary 6-8, secondary 9-10, "
        "higher_secondary 11-12.",
        f"- **Gender** (dim_gender): {gender}. Transgender is reported only from 2025-26.",
        f"- **Social category** (dim_social_category): {social}. A complete split of enrolment.",
        "- **Location**: `location` = 'Rural' / 'Urban' (`rural_urban` 1 = Rural, 2 = Urban).",
        "- **School type**: 1 = Boys, 2 = Girls, 3 = Co-educational (`school_type_label`).",
        f"- **School category**: classes taught, the most common lowest-highest class per code in "
        f"the data (the DCF names are not in the schema files): {categories}.",
        "- **Management** (dim_management; source column `managment`, spelled so): "
        "`is_government` = true for the three government groups.",
        *mgmt,
        "  - The codes are not documented in the schema PDFs. Codes 94, 95 and 101 were resolved "
        "as government using the official totals: counting them as government adds exactly the 7 "
        "schools and 170 teachers that were otherwise missing, so government teachers match the "
        "official 4,45,073 for 2024-25 (DATA_QUALITY_LOG #13). 97 and 98 are non-government. The "
        "exact names of 94, 95, 97, 98 and 101 are still to be confirmed with the UDISE+ DCF, so "
        "describe them only by their group.",
        "- **Facility flags** (`has_*`): 1 = yes, 0 = no, NULL = not reported or not applicable "
        "(source code 9). Never count NULL as no: `avg(has_x)` gives the share among schools that "
        "reported it; `count(*) filter (where has_x = 0)` counts the noes. `pct_has_*` in the marts "
        "= 100 * avg(has_*). Meanings: "
        + "; ".join(f"{k} = {v}" for k, v in facilities.items()) + ".",
    ]


RULES = [
    "## How to query", "",
    f"- One SELECT per call (CTEs, UNION and subqueries are fine), DuckDB dialect. Bare table names "
    f"only: no schema prefix, no files, no table functions. Results are capped at {MAX_ROWS} rows "
    f"(LIMIT {MAX_ROWS} is added if missing), so aggregate rather than list.",
    "- The school code is hidden. School-level tables are for counting and aggregating; do not "
    "list or single out individual schools (UDISE+ data-sharing terms).",
    "- Facts store `school_key`, the school *version* valid in that year (dim_school is SCD Type 2). "
    "Join `using (school_key)` with no year condition and the fact is placed in the right district "
    "for that year. Join `using (year_key)` to dim_year for the year.",
    "- Counting schools: one row per school and year in int_school_year, fact_school_facilities and "
    "fact_teachers, so `count(*)` per year is the number of schools.",
    "- Students: the official figure includes pre-primary (`enr_total + enr_pre_primary`, or all "
    "of fact_enrolment). `enr_total` and the marts' `enrolment` are classes 1-12 only.",
    "- The marts leave out schools with no students in classes 1-12, so their `schools` is a "
    "little below the official count; use int_school_year for official totals.",
    "- Government vs private: `is_government` (or `management_group`).",
    "- Districts: 41, canonical upper-case names (`district_name`, `district` in the marts); "
    "dim_district.aliases holds other spellings (JALOR -> JALORE). 189 schools changed district "
    "between the years, 180 of them ALWAR -> KOTPUTLI-BEHROR, so compare those districts' "
    "year-on-year changes with care.",
    "- Playground fell sharply in one year because of a reporting change: do not use it for trends "
    "(it is not in `infra_index`).",
    "- In 2025-26, 114 central-government schools have block 'JAIPUR REGION'; `block_clean` is "
    "blank for them.",
    f"- Quote the source with any figure: \"{SOURCE_NOTE}\".",
]


def build_card(gold_db: Path) -> str:
    descriptions = {**dd.source_descriptions(), **dd.DESCRIPTIONS, **OVERRIDES}
    grouped = {c: g for g, cols in TEACHER_GROUPS.items() for c in cols}
    con = duckdb.connect(str(gold_db), read_only=True)
    try:
        assert set(ORDER) == ALLOWED_TABLES == set(JOINS), "table lists out of step with sql_tool"
        counts = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in ORDER}
        lines = ["# Gold schema card (for the SQL tool)", "",
                 "Generated by `python -m rag.schema_card` from the gold DuckDB (star schema of "
                 "UDISE+ school data, Rajasthan, all 41 districts, 2024-25 and 2025-26). Read this "
                 "before writing SQL for `rag/sql_tool.py`.", "", *RULES, "", "## Tables", ""]
        missing, teacher_cols = [], []
        for t in ORDER:
            kind, grain = dd.TABLES[t]
            lines.append(f"### {t}")
            lines.append(f"{kind}; grain: {grain}; {counts[t]:,} rows." +
                         (f" {USE_FOR[t]}" if t in USE_FOR else ""))
            if JOINS[t]:
                lines.append("Joins: " + ", ".join(f"`{c}` -> {d}" for c, d in JOINS[t].items()) + ".")
            columns = _columns(con, t)
            if t == "mart_district_year_management":
                base = [c for c, _ in _columns(con, "mart_district_year")]
                extra = [c for c, _ in columns if c not in base]
                if set(base) - {c for c, _ in columns}:
                    raise SystemExit(f"{t} no longer has every column of mart_district_year")
                lines.append("Same columns as mart_district_year, plus "
                             + ", ".join(f"`{c}`" for c in extra) + ".")
                columns = []
            flags = [c for c, _ in columns if c.startswith(("has_", "pct_has_"))]
            for name, dtype in columns:
                if t == "fact_teachers" and name in grouped:
                    teacher_cols.append(name)
                    continue
                if name in JOINS[t]:
                    continue  # listed in Joins
                if name in flags:
                    if name == flags[0]:
                        pct = name.startswith("pct_")
                        lines.append(("- Facility shares (double, %, among schools reporting it): "
                                      if pct else "- Facility flags (double, 1 / 0 / NULL, see Codes): ")
                                     + ", ".join(f"`{c}`" for c in flags))
                    continue
                desc = descriptions.get(name)
                if not desc:
                    missing.append(f"{t}.{name}")
                desc = (desc or "").replace(" (UDISE+ schema)", "")
                lines.append(f"- `{name}` {_short_type(dtype)}: {desc}")
            if t == "fact_teachers":
                # Only the columns this database has (the synthetic sample has fewer); a column
                # with no group already stopped above as "no description".
                lines.append("- Teacher counts (int, number of teachers in the school), by group:")
                for g, cols in TEACHER_GROUPS.items():
                    present = [c for c in cols if c in teacher_cols]
                    if present:
                        lines.append(f"  - {g}: " + ", ".join(f"`{c}`" for c in present))
            lines.append("")
        if missing:
            raise SystemExit("No description for: " + ", ".join(missing))
        lines += _codes(con, dd.FACILITIES)
    finally:
        con.close()
    lines += ["", "## Examples (question -> SQL)", ""]
    for i, (q, sql) in enumerate(EXAMPLES, 1):
        lines += [f"{i}. {q}", "", "```sql", sql, "```", ""]
    lines += [f"{SOURCE_NOTE}.", ""]
    return "\n".join(lines)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gold-db", type=Path, default=None, help="default: UDISE_GOLD_DB or the main checkout's")
    p.add_argument("--out", type=Path, default=OUT)
    a = p.parse_args(argv)
    gold_db = a.gold_db or default_gold_db()
    text = build_card(gold_db)
    a.out.write_text(text, encoding="utf-8")
    print(f"Written {a.out} from {gold_db} ({len(text.split())} words)")


if __name__ == "__main__":
    main()
