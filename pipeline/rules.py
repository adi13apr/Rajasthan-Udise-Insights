"""Data quality rules for the silver layer, declared as data.

Each rule has a name, the silver tables it applies to, a severity, a description
and a check. A check returns, for every row, the reason the row fails (a string),
or None if it passes.

    error   -> the row is moved to data/quarantine/<table>.parquet with _rule,
               _reason and _run_id. Never silently dropped.
    warning -> the row stays in silver with a flag_<rule> column (1 = fails).

Error rules run in the order listed and each one only sees the rows that passed
the ones before it, so every quarantined row has exactly one rule: the first it
failed. (E.g. duplicates are judged only among rows with valid values.)
docs/quality_rules.md is generated from this list.
"""
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import config as C

ERROR, WARNING = "error", "warning"
SCHOOL_TABLES = ("schools", "facilities", "teachers")
CHILD_TABLES = ("facilities", "teachers", "enrolment", "enrolment_other")
ENROLMENT_TABLES = ("enrolment", "enrolment_other")
ALL_TABLES = SCHOOL_TABLES + ENROLMENT_TABLES
STATE_FILE = "RAJASTHAN"  # district part of the state-wide file names


@dataclass
class Context:
    """What the checks need beyond the table itself."""
    table: str
    columns: dict[str, dict] = field(default_factory=dict)   # column -> schema entry (kind, codes)
    schools: set = field(default_factory=set)                # {(year, pseudocode)} in silver schools
    enrolment_total: pd.DataFrame | None = None              # year, pseudocode, enr_total (classes 1-12)
    enrol_items: dict = field(default_factory=dict)          # year -> {(item_group, item_id)} in schema


@dataclass(frozen=True)
class Rule:
    name: str
    tables: tuple[str, ...]
    severity: str
    description: str
    check: Callable[[pd.DataFrame, Context], pd.Series]


# --- Helpers ----------------------------------------------------------------

def code_set(entry: dict | None) -> set[int] | None:
    """Allowed codes for a coded column from its schema entry (None if not coded)."""
    if entry and entry["kind"] == "yes_no":
        return {1, 2, C.NOT_APPLICABLE}  # 9 = not applicable, treated as missing
    if entry and entry["kind"] == "enum":
        return {int(k) for k in entry["codes"]}
    return None


def describe_cells(df: pd.DataFrame, bad: pd.DataFrame) -> pd.Series:
    """For each row, 'col=value; col=value' for the cells marked True in `bad`; None if none."""
    out = pd.Series(None, index=df.index, dtype=object)
    for col in bad.columns[bad.any()]:
        hit = bad[col]
        text = col + "=" + df.loc[hit, col].astype("Int64").astype(str)
        out.loc[hit] = np.where(out.loc[hit].isna(), text, out.loc[hit] + "; " + text)
    return out


def reason_if(mask: pd.Series, reason: str | pd.Series) -> pd.Series:
    out = pd.Series(None, index=mask.index, dtype=object)
    out[mask] = reason if isinstance(reason, str) else reason[mask]
    return out


def count_columns(df, ctx) -> list[str]:
    """Numeric measure columns (counts), not codes."""
    return [c for c, e in ctx.columns.items() if e["kind"] == "numeric" and c in df.columns
            and c not in ("pseudocode", "item_group", "item_id")]


# --- Checks -----------------------------------------------------------------

def missing_required(df, ctx):
    required = ["year", "pseudocode"] + (["district"] if ctx.table == "schools" else [])
    missing = df[required].isna()
    bad = missing.any(axis=1)
    reasons = pd.Series(None, index=df.index, dtype=object)
    for i, row in missing[bad].iterrows():  # only the failing rows
        reasons[i] = "missing " + ", ".join(row.index[row])
    return reasons


def not_a_number(df, ctx):
    errs = df["_cast_errors"]
    return errs.where(errs.notna() & (errs != ""), None)


def negative_count(df, ctx):
    cols = count_columns(df, ctx)
    return describe_cells(df, df[cols] < 0)


def unknown_district(df, ctx):
    bad = ~df["district"].isin(C.DISTRICTS)
    return reason_if(bad, "district " + df["district"].astype(str) + " is not one of the 41 districts")


def duplicate_key(df, ctx):
    """Extra copies of the same key. The copy from the state-wide file is kept
    (the Kota files were early test downloads); otherwise the first in file order."""
    key = ["year", "pseudocode"] + (["item_group", "item_id"] if ctx.table in ENROLMENT_TABLES else [])
    order = df.assign(_not_state=(df["district_file"] != STATE_FILE)).sort_values(
        ["_not_state", "_source_file", "_source_row"])
    extra = order.duplicated(key, keep="first").reindex(df.index)
    kept_file = order.groupby(key, sort=False)["_source_file"].transform("first").reindex(df.index)
    return reason_if(extra, "extra copy; kept the row from " + kept_file.astype(str))


def orphan_school(df, ctx):
    keys = pd.Series(list(zip(df["year"], df["pseudocode"])), index=df.index)
    return reason_if(~keys.isin(ctx.schools), "school not in silver schools for this year")


def unknown_management(df, ctx):
    bad = df["managment"].notna() & ~df["managment"].isin(list(C.MANAGEMENT_GROUP))
    return reason_if(bad, "managment " + df["managment"].astype(str))


def invalid_rural_urban(df, ctx):
    bad = df["rural_urban"].notna() & ~df["rural_urban"].isin(list(C.RURAL_URBAN))
    return reason_if(bad, "rural_urban " + df["rural_urban"].astype(str))


def invalid_code(df, ctx):
    bad = pd.DataFrame({c: df[c].notna() & ~df[c].isin(list(code_set(e)))
                        for c, e in ctx.columns.items() if code_set(e) and c in df.columns}, index=df.index)
    return describe_cells(df, bad)


def functional_toilets_exceed_total(df, ctx):
    girls = df["total_girls_func_toilet"] > df["total_girls_toilet"]
    boys = df["total_boys_func_toilet"] > df["total_boys_toilet"]
    return reason_if(girls | boys, "functional toilets > total toilets")


def classroom_total_mismatch(df, ctx):
    bad = (df["total_class_rooms"] > 0) & (df["total_class_rooms"] != df["classrooms"])
    return reason_if(bad, "total_class_rooms differs from good + minor + major repair")


def _enrolment(df, ctx):
    """Class 1-12 enrolment of each row's school (0 if it has no enrolment rows)."""
    merged = df[["year", "pseudocode"]].merge(ctx.enrolment_total, on=["year", "pseudocode"], how="left")
    return pd.Series(merged["enr_total"].fillna(0).to_numpy(), index=df.index)


def no_teachers_with_students(df, ctx):
    return reason_if((df["total_tch"] == 0) & (_enrolment(df, ctx) > 0), "students enrolled but 0 teachers")


def high_ptr(df, ctx):
    ptr = _enrolment(df, ctx) / df["total_tch"].replace(0, np.nan)
    return reason_if(ptr > C.PTR_FLAG, f"pupil-teacher ratio above {C.PTR_FLAG}")


def undocumented_item(df, ctx):
    pairs = pd.Series(list(zip(df["year"], df["item_group"], df["item_id"])), index=df.index)
    documented = {(y, g, i) for y, items in ctx.enrol_items.items() for g, i in items}
    return reason_if(~pairs.isin(documented), "item_group/item_id not in the schema")


# --- The rules --------------------------------------------------------------

RULES = [
    # Errors: in this order; the row goes to quarantine with the first rule it fails.
    Rule("missing_required", ALL_TABLES, ERROR,
         "Key fields are present: year and pseudocode (and district for schools).", missing_required),
    Rule("not_a_number", ALL_TABLES, ERROR,
         "Every column the schema says is numeric holds a whole number.", not_a_number),
    Rule("negative_count", ALL_TABLES, ERROR,
         "Counts (enrolment, teachers, rooms, toilets, ...) are not negative.", negative_count),
    Rule("unknown_district", ("schools",), ERROR,
         "District, after mapping old spellings, is one of the 41 districts (reference/districts.json).",
         unknown_district),
    Rule("duplicate_school_year", SCHOOL_TABLES, ERROR,
         "One row per (year, pseudocode). Extra copies (e.g. Kota schools also in the Kota test "
         "download) are quarantined; the copy from the state-wide file is kept.", duplicate_key),
    Rule("duplicate_enrolment_row", ENROLMENT_TABLES, ERROR,
         "One row per (year, pseudocode, item_group, item_id).", duplicate_key),
    Rule("orphan_school", CHILD_TABLES, ERROR,
         "The row's school exists in silver schools for the same year.", orphan_school),

    # Warnings: the row stays, with flag_<name> = 1.
    Rule("unknown_management", ("schools",), WARNING,
         "Management code is in reference/management_codes.json (else grouped as 'Unmapped').",
         unknown_management),
    Rule("invalid_rural_urban", ("schools",), WARNING,
         "rural_urban is 1 (Rural) or 2 (Urban).", invalid_rural_urban),
    Rule("invalid_code", ("schools", "facilities"), WARNING,
         "Coded columns use a code documented in the schema (Yes/No columns: 1, 2 or 9 = not "
         "applicable). Undocumented codes count as missing in derived indicators.", invalid_code),
    Rule("functional_toilets_exceed_total", ("facilities",), WARNING,
         "Functional toilets (girls, boys) are not more than total toilets.", functional_toilets_exceed_total),
    Rule("classroom_total_mismatch", ("facilities",), WARNING,
         "Where total_class_rooms is filled in, it equals good + minor repair + major repair.",
         classroom_total_mismatch),
    Rule("no_teachers_with_students", ("teachers",), WARNING,
         "A school with students in classes 1-12 has at least one teacher.", no_teachers_with_students),
    Rule("high_ptr", ("teachers",), WARNING,
         f"Pupil-teacher ratio (classes 1-12) is at most {C.PTR_FLAG}.", high_ptr),
    Rule("undocumented_item", ("enrolment_other",), WARNING,
         "(item_group, item_id) is documented in the schema (item_id 21 in group 4 is not).",
         undocumented_item),
]


def rules_for(table: str, severity: str) -> list[Rule]:
    return [r for r in RULES if table in r.tables and r.severity == severity]


def apply_rules(df: pd.DataFrame, ctx: Context) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Run the rules for one table.

    Returns (rows that stay, quarantined rows, {"quarantined": {rule: rows}, "flagged": {rule: rows}}).
    """
    quarantined, counts = [], {"quarantined": {}, "flagged": {}}
    for rule in rules_for(ctx.table, ERROR):
        reasons = rule.check(df, ctx)
        bad = reasons.notna()
        counts["quarantined"][rule.name] = int(bad.sum())
        if bad.any():
            quarantined.append(df[bad].assign(_rule=rule.name, _reason=reasons[bad]))
            df = df[~bad]
    df = df.copy()
    for rule in rules_for(ctx.table, WARNING):
        flag = rule.check(df, ctx).notna().astype("int8")
        df[f"flag_{rule.name}"] = flag
        counts["flagged"][rule.name] = int(flag.sum())
    q = pd.concat(quarantined) if quarantined else df.iloc[0:0].assign(_rule="", _reason="")
    return df, q, counts


def rules_markdown() -> str:
    """docs/quality_rules.md, generated from RULES."""
    lines = ["# Quality rules", "",
             "Generated from `pipeline/rules.py` (`python -m pipeline.rules`). Do not edit by hand.", "",
             "- **error**: the row is moved to `data/quarantine/<table>.parquet` with `_rule`, `_reason` "
             "and `_run_id`. Error rules run in this order; a row is quarantined by the first one it fails.",
             "- **warning**: the row stays in silver with a `flag_<rule>` column (1 = fails).", "",
             "| Rule | Severity | Tables | What it checks |", "|---|---|---|---|"]
    lines += [f"| `{r.name}` | {r.severity} | {', '.join(r.tables)} | {r.description} |" for r in RULES]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    (C.ROOT / "docs" / "quality_rules.md").write_text(rules_markdown(), encoding="utf-8")
    print("Written docs/quality_rules.md")
