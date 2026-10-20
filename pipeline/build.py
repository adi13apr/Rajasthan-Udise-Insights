"""Turn raw tables into an analysis-ready school-year table and district summaries."""
import numpy as np
import pandas as pd

from . import config as C

KEY = ["year", "pseudocode"]


def normalise_district(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip().str.upper().str.replace(r"\s+", " ", regex=True)
    return s.replace(C.DISTRICT_ALIASES)


def yes_no(s: pd.Series) -> pd.Series:
    """1 -> 1, 2 -> 0, anything else (e.g. 9 = not applicable) -> missing."""
    return s.map(C.YES_NO).astype("Float64")


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

def facility_flags(fac: pd.DataFrame) -> pd.DataFrame:
    f = fac[KEY].copy()
    f["has_electricity"] = (fac["electricity_availability"] == 1).astype(int)  # 3 = not functional
    f["has_safe_water"] = (fac[C.SAFE_WATER_COLS] == 1).any(axis=1).astype(int)
    f["has_girls_toilet"] = ((fac["total_girls_func_toilet"] + fac["func_girls_cwsn_friendly"]) > 0).astype(int)
    f["has_boys_toilet"] = ((fac["total_boys_func_toilet"] + fac["func_boys_cwsn_friendly"]) > 0).astype(int)
    f["has_handwash"] = yes_no(fac["handwash_near_toilet"])
    f["has_library"] = yes_no(fac["library_availability"])
    f["has_playground"] = yes_no(fac["playground_available"])
    f["has_ramp"] = yes_no(fac["availability_ramps"])
    f["has_internet"] = yes_no(fac["internet"])
    f["has_computer"] = ((fac[["laptop", "desktop", "tablet"]].fillna(0).sum(axis=1)) > 0).astype(int)
    # total_class_rooms is 0 for every school in the 2024-25 release, so the
    # classroom count is rebuilt from the condition breakdown (which matches
    # total_class_rooms exactly wherever that field is filled in).
    f["classrooms"] = (fac["classrooms_in_good_condition"] + fac["classrooms_needs_minor_repair"]
                       + fac["classrooms_needs_major_repair"])
    f["classrooms_good"] = fac["classrooms_in_good_condition"]
    f["pct_classrooms_major_repair"] = (fac["classrooms_needs_major_repair"]
                                        / f["classrooms"].replace(0, float("nan")) * 100).round(1)

    # Simple, transparent index: share of 8 basic facilities present (0-100).
    index_cols = ["has_electricity", "has_safe_water", "has_girls_toilet", "has_handwash",
                  "has_library", "has_ramp", "has_internet", "has_computer"]
    f["infra_index"] = (f[index_cols].astype(float).mean(axis=1) * 100).round(1)
    flag_cols = [c for c in f.columns if c.startswith("has_")]
    f[flag_cols] = f[flag_cols].astype(float)  # NaN = not reported / not applicable
    return f


# --- Teachers ---------------------------------------------------------------

def teacher_cols(tch: pd.DataFrame) -> pd.DataFrame:
    t = tch[KEY + ["total_tch", "female", "regular", "contract", "trained_cwsn"]].copy()
    return t.rename(columns={"female": "tch_female", "regular": "tch_regular",
                             "contract": "tch_contract", "trained_cwsn": "tch_trained_cwsn"})


# --- School-year table ------------------------------------------------------

def build_school_year(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    prof = t["profile1"][KEY + ["district_file", "state", "district", "block", "rural_urban",
                                "school_category", "school_type", "lowclass", "highclass",
                                "managment", "lgd_block_name"]].copy()
    prof["district"] = normalise_district(prof["district"])
    prof["district_file"] = normalise_district(prof["district_file"])
    prof["block"] = prof["block"].str.strip().str.upper()
    # Some central-govt schools list an admin region (e.g. "JAIPUR REGION") as their block.
    prof["block_is_admin_region"] = prof["block"].str.contains("REGION", na=False)
    prof["block_clean"] = prof["block"].where(~prof["block_is_admin_region"])
    prof["location"] = prof["rural_urban"].map(C.RURAL_URBAN)
    prof["school_type_label"] = prof["school_type"].map(C.SCHOOL_TYPE)
    prof["management_group"] = prof["managment"].map(C.MANAGEMENT_GROUP).fillna("Unmapped")
    prof["is_govt"] = prof["management_group"].isin(C.GOVT_GROUPS).astype(int)

    df = (prof
          .merge(enrolment_per_school(t["enrolment1"]), on=KEY, how="left")
          .merge(facility_flags(t["facility"]), on=KEY, how="left")
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
