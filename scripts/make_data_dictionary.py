"""Write docs/data_dictionary.md from the gold database.

    python scripts/make_data_dictionary.py      (after python run_pipeline.py)

Table and column names, types and row counts come from DuckDB itself, so the
dictionary always matches the real database. Descriptions come from DESCRIPTIONS
below; columns copied from the source files (e.g. most of fact_teachers) use the
official description from the UDISE+ schema (reference/schema_columns.json).
A column with no description at all stops the script.
"""
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import config as C  # noqa: E402

OUT = ROOT / "docs" / "data_dictionary.md"

TABLES = {
    "dim_year": ("Dimension", "one row per academic year"),
    "dim_district": ("Dimension", "one row per district (41)"),
    "dim_management": ("Dimension", "one row per UDISE+ management code, plus key 0 = unmapped"),
    "dim_school": ("Dimension, SCD Type 2", "one row per version of a school's attributes"),
    "dim_grade": ("Dimension", "one row per grade: pre-primary and classes 1-12"),
    "dim_gender": ("Dimension", "one row per gender code of the enrolment columns"),
    "dim_social_category": ("Dimension", "one row per social category (Enrolment_1 item_group 1)"),
    "fact_enrolment": ("Fact", "school x year x grade x gender x social category; rows with 0 students omitted"),
    "fact_teachers": ("Fact", "school x year (the source is already one row per school)"),
    "fact_school_facilities": ("Fact (periodic snapshot)", "school x year; one row for every school-year"),
    "int_school_year": ("Intermediate (school-level, never exported)", "school x year"),
    "mart_district_year": ("Mart", "district x year; schools with no students in classes 1-12 excluded"),
    "mart_district_year_management": ("Mart", "district x year x management group; same rules"),
}

FACILITIES = {"electricity": "electricity connection that works (code 1; 3 = not functional counts as no)",
              "safe_water": "a functional safe drinking-water source (hand pump, protected well, tap, other, packaged)",
              "girls_toilet": "at least one functional girls' toilet (incl. CWSN-friendly)",
              "boys_toilet": "at least one functional boys' toilet (incl. CWSN-friendly)",
              "handwash": "handwash facility near the toilets", "library": "library",
              "playground": "playground (dropped by a reporting change; not in the index)",
              "ramp": "ramps for children with disabilities", "internet": "internet",
              "computer": "at least one laptop, desktop or tablet"}

DESCRIPTIONS = {
    # keys and shared columns
    "year_key": "Surrogate key of dim_year.", "district_key": "Surrogate key of dim_district.",
    "management_key": "Surrogate key of dim_management (0 = code not in the reference).",
    "school_key": "Surrogate key of dim_school: one school VERSION (SCD2), not one school.",
    "grade_key": "Surrogate key of dim_grade (0 = pre-primary, 1-12 = class).",
    "gender_key": "Surrogate key of dim_gender.", "social_category_key": "Surrogate key of dim_social_category.",
    "academic_year": "Academic year, e.g. 2024-25.", "start_year": "First calendar year of the academic year.",
    "year": "Academic year, e.g. 2024-25.",
    "district_name": "District, canonical spelling.", "district": "District, canonical spelling.",
    "aliases": "Other spellings used by the portal in some years (e.g. JALOR).",
    "management_code": "UDISE+ management code (column `managment` in the source).",
    "management_group": "Management group: State / Central / Other Government, Private Unaided, "
                        "Other Non-Government, or Unmapped.",
    "is_government": "True for the three government groups (validated against official totals).",
    "pseudocode": "UDISE+ pseudonymised school code; stable across years.",
    "version": "Version number of the school's attributes (1, 2, ...).",
    "valid_from_year": "First start year in which this version applies.",
    "valid_to_year": "Last start year in which this version was observed.",
    "is_current": "True for the school's latest version.",
    "block": "UDISE+ block name (upper case).",
    "block_clean": "Block, blank where the block is an admin region (e.g. JAIPUR REGION).",
    "rural_urban": "1 = Rural, 2 = Urban.", "location": "Rural or Urban.",
    "school_category": "UDISE+ school category code (as per the UDISE+ DCF).",
    "school_type": "UDISE+ school type code.", "school_type_label": "Boys / Girls / Co-educational.",
    "grade_code": "PP or 1-12.", "grade_label": "Pre-primary or Class n.",
    "stage": "pre_primary, primary (1-5), upper_primary (6-8), secondary (9-10), higher_secondary (11-12).",
    "gender_code": "B, G or T (suffix of the enrolment columns).",
    "gender_label": "Boys, Girls, Transgender (T only from 2025-26; in 2024-25 boys include transgender).",
    "item_id": "Enrolment_1 item_id within item_group 1.", "social_category": "General, SC, ST or OBC.",
    "students": "Number of students (always > 0; a missing row means 0).",
    "lowclass": "Lowest class taught.", "highclass": "Highest class taught.",
    "classrooms": "Classrooms, rebuilt as good + minor repair + major repair (total_class_rooms is 0 "
                  "for almost every school in 2024-25).",
    "classrooms_good": "Classrooms in good condition.",
    "pct_classrooms_major_repair": "Share of classrooms needing major repair (%).",
    "flag_facility_invalid_code": "1 = a coded facility column has a code not in the schema (e.g. solar_panel = 3).",
    "flag_invalid_code": "1 = a coded column has a code not in the schema.",
    "flag_functional_toilets_exceed_total": "1 = more functional toilets than toilets reported.",
    "flag_classroom_total_mismatch": "1 = total_class_rooms filled in but different from the rebuilt count.",
    "flag_no_teachers_with_students": "1 = students in classes 1-12 but no teachers.",
    "flag_high_ptr": f"1 = pupil-teacher ratio above {C.PTR_FLAG}.",
    # int_school_year
    "enr_total": "Students in classes 1-12.", "enr_girls": "Girls in classes 1-12.",
    "enr_pre_primary": "Students in pre-primary.", "enr_primary": "Students in classes 1-5.",
    "enr_primary_b": "Boys in classes 1-5.", "enr_primary_g": "Girls in classes 1-5.",
    "enr_secondary_plus": "Students in classes 9-12.", "enr_secondary_plus_b": "Boys in classes 9-12.",
    "enr_secondary_plus_g": "Girls in classes 9-12.", "enr_sc": "SC students in classes 1-12.",
    "enr_st": "ST students in classes 1-12.", "total_tch": "Teachers.",
    "single_teacher": "1 = exactly one teacher.",
    "infra_index": "Infrastructure index (0-100): share of 8 facilities present among those reported "
                   "(electricity, safe water, girls' toilet, handwash, library, ramp, internet, computer).",
    # marts
    "schools": "Schools (with students in classes 1-12).", "enrolment": "Students in classes 1-12.",
    "girls_share": "Girls / students, classes 1-12.", "girls_share_primary": "Girls / students, classes 1-5.",
    "girls_share_sec_plus": "Girls / students, classes 9-12.", "st_share": "ST students / students.",
    "sc_share": "SC students / students.", "teachers": "Teachers.",
    "ptr": "Pupil-teacher ratio: students in classes 1-12 / teachers.",
    "pct_single_teacher": "Single-teacher schools (%).",
    "avg_infra_index": "Average school infrastructure index (0-100).",
    "girls_retention_gap": "girls_share_sec_plus - girls_share_primary. Negative: girls drop out more "
                           "after class 8; positive: boys leave more (or girls stay more).",
    "boys_progression": "(Boys per grade in classes 9-12) / (boys per grade in classes 1-5).",
    "girls_progression": "(Girls per grade in classes 9-12) / (girls per grade in classes 1-5).",
    **{f"has_{k}": f"1 = {v}; 0 = no; NULL = not reported / not applicable." for k, v in FACILITIES.items()},
    **{f"pct_has_{k}": f"Schools with {v} (%), among schools reporting it." for k, v in FACILITIES.items()},
}


def source_descriptions() -> dict:
    schema = C.load_reference("schema_columns.json")["datasets"]
    out = {}
    for ds in ("teacher", "facility", "profile1"):
        for cols in schema.get(ds, {}).values():
            for e in cols:
                out.setdefault(e["column"], e["description"] + " (UDISE+ schema)")
    return out


def main(gold_db: Path = C.GOLD_DB, out: Path = OUT) -> None:
    con = duckdb.connect(str(gold_db), read_only=True)
    source = source_descriptions()
    lines = ["# Data dictionary", "",
             "Every table and column of the gold database (`data/gold/udise.duckdb`, also exported to "
             "`data/gold/parquet/`). Generated by `scripts/make_data_dictionary.py` from the database "
             "itself; row counts are from the last full run.", "",
             "Star schema: facts hold the measures at a stated grain and point to dimensions through "
             "integer surrogate keys. `dim_school` is a Type 2 slowly changing dimension: facts join to the "
             "school version valid in that year, so a school that moved district (e.g. ALWAR -> "
             "KOTPUTLI-BEHROR) is counted in the right district each year.", "",
             "| Table | Kind | Grain | Rows |", "|---|---|---|---:|"]
    tables = [t for t in TABLES]
    counts = {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    lines += [f"| [`{t}`](#{t}) | {TABLES[t][0]} | {TABLES[t][1]} | {counts[t]:,} |" for t in tables]
    missing = []
    for t in tables:
        cols = con.execute("select column_name, data_type from information_schema.columns "
                           "where table_name = ? order by ordinal_position", [t]).fetchall()
        lines += ["", f"## {t}", "", f"{TABLES[t][0]}. Grain: {TABLES[t][1]}. Rows: {counts[t]:,}.", "",
                  "| Column | Type | Description |", "|---|---|---|"]
        for name, dtype in cols:
            desc = DESCRIPTIONS.get(name) or source.get(name)
            if not desc:
                missing.append(f"{t}.{name}")
            lines.append(f"| `{name}` | {dtype} | {desc or ''} |")
    con.close()
    if missing:
        raise SystemExit("No description for: " + ", ".join(missing))
    lines += ["", f"{C.SOURCE_NOTE}.", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written {out} ({len(tables)} tables)")


if __name__ == "__main__":
    main()
