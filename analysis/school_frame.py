"""School-level and district-level tables for the statistical module (notebooks/statistical_analysis.ipynb).

Read from the gold star schema (plus the year of establishment from silver). School-level
rows stay in memory: the notebook shows aggregates, test statistics and model metrics,
never individual schools (UDISE+ data-sharing terms). The school code is not even loaded.
"""
from pathlib import Path

import duckdb
import pandas as pd

from pipeline import config as C

SCHOOL_SQL = """
select
    i.academic_year                          as year,
    i.district_name                          as district,
    s.block_clean                            as block,       -- NULL for central schools listed under a region
    i.location,                                              -- Rural / Urban
    i.management_group,
    i.is_government,
    s.school_type,                                           -- 1 boys, 2 girls, 3 co-educational
    sf.lowclass, sf.highclass,
    i.enr_total, i.enr_girls, i.enr_pre_primary,
    i.enr_primary, i.enr_secondary_plus, i.enr_secondary_plus_g,
    i.total_tch,
    t.female                                 as tch_female,
    t.contract                               as tch_contract,
    t.graduate + t.post_graduate_and_above   as tch_graduate_plus,
    sf.classrooms, sf.classrooms_good, sf.pct_classrooms_major_repair,
    sf.total_girls_func_toilet, sf.total_boys_func_toilet,
    i.has_electricity, i.has_safe_water, i.has_girls_toilet, i.has_boys_toilet, i.has_handwash,
    i.has_library, i.has_ramp, i.has_internet, i.has_computer, i.has_playground,
    cast(i.infra_index as double)            as infra_index,
    {estd_year}                              as estd_year
from int_school_year i
join dim_school s               using (school_key)
join fact_school_facilities sf  using (school_key, year_key)
left join fact_teachers t       using (school_key, year_key)
left join read_parquet('{silver}/*/*.parquet', hive_partitioning = true, union_by_name = true) sv
       on sv.pseudocode = s.pseudocode and sv.year = i.academic_year
where i.academic_year = ?
"""


def load_schools(year: str = "2025-26", gold_db: Path = C.GOLD_DB, silver_dir: Path = C.SILVER_DIR) -> pd.DataFrame:
    """One row per school for one academic year (in memory only; no school code).
    Year of establishment is published only from 2025-26; it is NULL where the files lack it."""
    silver = (silver_dir / "schools").as_posix()
    con = duckdb.connect(str(gold_db), read_only=True)
    try:
        cols = {r[0] for r in con.execute(f"describe select * from read_parquet('{silver}/*/*.parquet', "
                                          f"hive_partitioning = true, union_by_name = true)").fetchall()}
        sql = (SCHOOL_SQL.replace("{silver}", silver)
               .replace("{estd_year}", "sv.estd_year" if "estd_year" in cols else "null::bigint"))
        return con.execute(sql, [year]).df()
    finally:
        con.close()


def load_districts(gold_db: Path = C.GOLD_DB) -> pd.DataFrame:
    """District x year mart (41 districts x 2 years)."""
    con = duckdb.connect(str(gold_db), read_only=True)
    try:
        return con.execute("select * from mart_district_year order by district, year").df()
    finally:
        con.close()
