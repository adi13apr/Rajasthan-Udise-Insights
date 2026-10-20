"""Read what the analysis needs from the gold database (read-only)."""
import duckdb
import numpy as np
import pandas as pd

from pipeline import config as C

LATEST, EARLIER = "2025-26", "2024-25"
WESTERN_DESERT = ["JAISALMER", "BARMER", "PHALODI", "BALOTRA"]


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(C.GOLD_DB), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def district_year(year: str | None = None) -> pd.DataFrame:
    df = query("select * from mart_district_year order by district, year")
    return df[df["year"] == year].reset_index(drop=True) if year else df


def state_totals(year: str) -> dict:
    """State figures computed the same way as the marts (schools with students in classes 1-12)."""
    row = query("""select count(*) as schools, sum(enr_total) as enrolment, sum(total_tch) as teachers,
                          100.0 * avg(single_teacher) as pct_single_teacher, avg(infra_index) as avg_infra_index,
                          sum(enr_total) / sum(total_tch) as ptr
                   from int_school_year where academic_year = ? and enr_total > 0""", [year])
    return row.iloc[0].to_dict()


def patterns(dy: pd.DataFrame) -> pd.Series:
    """Descriptive grouping of districts by how secondary enrolment differs for girls and boys.

    Thresholds are descriptive choices (stated in the notebook), not statistical tests:
      girls leaving     retention gap <= -0.02 and boys' progression < 1
      boys moving in    retention gap <= -0.01 and boys' progression > 1 (more boys per grade in
                        classes 9-12 than in 1-5: boys arriving, e.g. coaching hubs)
      boys leaving      retention gap >= +0.015 in a majority-ST district
    """
    out = pd.Series("other", index=dy.index)
    out[(dy["girls_retention_gap"] <= -0.02) & (dy["boys_progression"] < 1)] = "girls leaving"
    out[(dy["girls_retention_gap"] <= -0.01) & (dy["boys_progression"] > 1)] = "boys moving in"
    out[(dy["girls_retention_gap"] >= 0.015) & (dy["st_share"] >= 0.5)] = "boys leaving (tribal south)"
    return out


def girls_share_by_sector(year: str = LATEST) -> pd.DataFrame:
    """Girls' share at primary and at secondary+ by district, government vs non-government schools."""
    return query("""
        select district_name as district,
               case when is_government then 'Government' else 'Non-government' end as sector,
               sum(enr_primary_g) / sum(enr_primary)                       as girls_share_primary,
               sum(enr_secondary_plus_g) / nullif(sum(enr_secondary_plus), 0) as girls_share_sec_plus,
               sum(enr_secondary_plus)                                      as enr_secondary_plus
        from int_school_year
        where academic_year = ? and enr_total > 0
        group by all order by 1, 2""", [year]).assign(
        girls_retention_gap=lambda d: d["girls_share_sec_plus"] - d["girls_share_primary"])


def regression_frame(year: str = LATEST, min_secondary: int = 20) -> pd.DataFrame:
    """One row per co-educational school with at least `min_secondary` students in classes 9-12.
    Kept in memory only (school-level data is never written out)."""
    df = query("""
        select i.district_name as district, i.management_group, i.location, i.school_category,
               i.enr_secondary_plus_g * 1.0 / i.enr_secondary_plus as girls_share_sec,
               i.has_girls_toilet, i.total_tch,
               (i.has_electricity + i.has_safe_water + i.has_handwash + i.has_library + i.has_ramp
                + i.has_internet + i.has_computer) / 7.0 * 100 as infra_other
        from int_school_year i join dim_school ds using (school_key)
        where i.academic_year = ? and ds.school_type = 3            -- co-educational
          and i.enr_secondary_plus >= ? and i.total_tch > 0""", [year, min_secondary]).dropna()
    df["log_teachers"] = np.log(df["total_tch"])
    df["infra_other_10"] = df["infra_other"] / 10
    return df


def matched_single_teacher() -> pd.DataFrame:
    """Single-teacher schools by district among schools present in BOTH years, each school
    assigned to its 2025-26 district (so the ALWAR -> KOTPUTLI-BEHROR move does not create
    an artificial change)."""
    df = query(f"""
        with matched as (select pseudocode from int_school_year group by pseudocode
                         having count(distinct academic_year) = 2),
             now as (select pseudocode, district_name from int_school_year where academic_year = '{LATEST}')
        select now.district_name as district, i.academic_year,
               count(*) as schools, sum(i.single_teacher) as single_teacher_schools,
               100.0 * avg(i.single_teacher) as pct_single_teacher, sum(i.total_tch) as teachers
        from int_school_year i join matched using (pseudocode) join now using (pseudocode)
        group by all""")
    wide = df.pivot(index="district", columns="academic_year",
                    values=["schools", "single_teacher_schools", "pct_single_teacher", "teachers"])
    wide.columns = [f"{a}_{b[:4]}" for a, b in wide.columns]
    wide["change_pp"] = wide["pct_single_teacher_2025"] - wide["pct_single_teacher_2024"]
    wide["teachers_change_pct"] = (wide["teachers_2025"] / wide["teachers_2024"] - 1) * 100
    return wide.reset_index().sort_values("change_pp", ascending=False).reset_index(drop=True)
