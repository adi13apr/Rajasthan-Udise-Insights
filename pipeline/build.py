"""Turn silver tables into an analysis-ready school-year table and district summaries.

Cleaning (district names, yes/no codes, classroom rebuild, management groups,
facility indicators) happens in pipeline/silver.py; this module only combines
and summarises.
"""
import numpy as np
import pandas as pd

from . import config as C
from .silver import normalise_district

KEY = ["year", "pseudocode"]


def class_cols(df: pd.DataFrame, classes, sex: str) -> list[str]:
    return [f"c{k}_{sex}" for k in classes if f"c{k}_{sex}" in df.columns]


# --- Enrolment --------------------------------------------------------------

def enrolment_per_school(enr1: pd.DataFrame) -> pd.DataFrame:
    """Enrolment by stage and sex, plus SC/ST/CWSN counts, one row per school-year."""
    total = enr1[enr1["item_group"] == C.ENROL_TOTAL_GROUP]
    out = pd.DataFrame(index=total.groupby(KEY).size().index)

    for stage, classes in C.STAGES.items():
        for sex in ("b", "g", "t"):  # transgender columns exist only from 2025-26
            cols = class_cols(total, classes, sex)
            if cols:
                out[f"enr_{stage}_{sex}"] = total.groupby(KEY)[cols].sum().sum(axis=1)

    out = out.fillna(0)
    for stage in C.STAGES:
        parts = [c for c in out.columns if c.startswith(f"enr_{stage}_")]
        out[f"enr_{stage}"] = out[parts].sum(axis=1)

    out["enr_total"] = out[[f"enr_{s}" for s in C.STAGES]].sum(axis=1)
    out["enr_girls"] = out[[f"enr_{s}_g" for s in C.STAGES]].sum(axis=1)
    out["enr_secondary_plus_girls"] = out["enr_secondary_g"] + out["enr_higher_secondary_g"]
    out["enr_secondary_plus"] = out["enr_secondary"] + out["enr_higher_secondary"]

    pp_cols = [c for c in total.columns if c.startswith("cpp_")]
    out["enr_pre_primary"] = total.groupby(KEY)[pp_cols].sum().sum(axis=1)

    all_class_cols = [c for c in total.columns if c.startswith("c") and c[1:2].isdigit()]
    for item_id, label in [(2, "sc"), (3, "st")]:
        sub = total[total["item_id"] == item_id]
        out[f"enr_{label}"] = sub.groupby(KEY)[all_class_cols].sum().sum(axis=1)

    cwsn = enr1[enr1["item_group"] == C.CWSN_GROUP]
    out["enr_cwsn"] = cwsn.groupby(KEY)[all_class_cols].sum().sum(axis=1)

    return out.fillna(0).reset_index()


# --- Facilities -------------------------------------------------------------

FACILITY_COLS = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_boys_toilet",
                 "has_handwash", "has_library", "has_playground", "has_ramp", "has_internet",
                 "has_computer", "classrooms", "classrooms_good", "pct_classrooms_major_repair"]
# Simple, transparent index: share of 8 basic facilities present (0-100).
# Playground is left out: its availability dropped by a reporting change (DATA_QUALITY_LOG #17).
INDEX_COLS = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_handwash",
              "has_library", "has_ramp", "has_internet", "has_computer"]


def facility_indicators(fac: pd.DataFrame) -> pd.DataFrame:
    """Facility indicators from silver (built by silver.facility_flags) + infrastructure index."""
    f = fac[KEY + FACILITY_COLS].copy()
    f["infra_index"] = (f[INDEX_COLS].astype(float).mean(axis=1) * 100).round(1)
    return f


# --- Teachers ---------------------------------------------------------------

def teacher_cols(tch: pd.DataFrame) -> pd.DataFrame:
    t = tch[KEY + ["total_tch", "female", "regular", "contract", "trained_cwsn"]].copy()
    return t.rename(columns={"female": "tch_female", "regular": "tch_regular",
                             "contract": "tch_contract", "trained_cwsn": "tch_trained_cwsn"})


# --- School-year table ------------------------------------------------------

def build_school_year(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per school and year. `t` holds silver tables (silver.tables_for_build)."""
    prof = t["profile1"][KEY + ["district_file", "state", "district", "block", "rural_urban",
                                "school_category", "school_type", "lowclass", "highclass",
                                "managment", "lgd_block_name", "block_is_admin_region",
                                "block_clean", "location", "school_type_label",
                                "management_group", "is_govt"]].copy()
    prof["district_file"] = normalise_district(prof["district_file"])

    df = (prof
          .merge(enrolment_per_school(t["enrolment1"]), on=KEY, how="left")
          .merge(facility_indicators(t["facility"]), on=KEY, how="left")
          .merge(teacher_cols(t["teacher"]), on=KEY, how="left"))

    enr_cols = [c for c in df.columns if c.startswith("enr_")]
    df[enr_cols] = df[enr_cols].fillna(0)
    df["girls_share_sec_plus"] = df["enr_secondary_plus_girls"] / df["enr_secondary_plus"].replace(0, np.nan)
    df["ptr"] = df["enr_total"] / df["total_tch"].replace(0, np.nan)
    df["single_teacher"] = (df["total_tch"] == 1).astype(int)

    # Row-level quality flags (kept in the data, not silently dropped).
    df["flag_no_enrolment"] = (df["enr_total"] == 0).astype(int)
    df["flag_no_teachers_with_students"] = ((df["total_tch"] == 0) & (df["enr_total"] > 0)).astype(int)
    df["flag_high_ptr"] = (df["ptr"] > C.PTR_FLAG).astype(int)
    return df


# --- District-year summary --------------------------------------------------

def summarise(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Retired from the pipeline (P5): the SQL marts in sql/11_marts_district.sql replace it.
    Kept as the reference implementation that tests/test_marts.py compares the marts with."""
    usable = df[df["flag_no_enrolment"] == 0]
    g = usable.groupby(by)
    out = pd.DataFrame({
        "schools": g.size(),
        "enrolment": g["enr_total"].sum(),
        "girls_share": g["enr_girls"].sum() / g["enr_total"].sum(),
        "girls_share_primary": g["enr_primary_g"].sum() / g["enr_primary"].sum(),
        "girls_share_sec_plus": g["enr_secondary_plus_girls"].sum() / g["enr_secondary_plus"].sum(),
        "st_share": g["enr_st"].sum() / g["enr_total"].sum(),
        "sc_share": g["enr_sc"].sum() / g["enr_total"].sum(),
        "teachers": g["total_tch"].sum(),
        "ptr": g["enr_total"].sum() / g["total_tch"].sum(),
        "pct_single_teacher": g["single_teacher"].mean() * 100,
        "avg_infra_index": g["infra_index"].mean(),
    })
    # Girls' share at secondary+ minus their share at primary. This cancels out
    # differences in the child sex ratio between districts: negative = girls
    # drop out more than boys after class 8; positive = boys leave more.
    out["girls_retention_gap"] = out["girls_share_sec_plus"] - out["girls_share_primary"]
    # Progression ratio: average enrolment per grade in classes 9-12 relative to
    # classes 1-5, for boys and girls separately. Lower = more students lost
    # after primary; a high boys' ratio can also mean boys moving IN (e.g. coaching hubs).
    for sex, label in (("b", "boys"), ("g", "girls")):
        sec = g[f"enr_secondary_{sex}"].sum() + g[f"enr_higher_secondary_{sex}"].sum()
        out[f"{label}_progression"] = (sec / 4) / (g[f"enr_primary_{sex}"].sum() / 5)
    for col in [c for c in df.columns if c.startswith("has_")]:
        out[f"pct_{col}"] = g[col].mean() * 100
    return out.round(3).reset_index()
