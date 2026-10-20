"""Silver layer: cleaned, typed, standardised and rule-checked tables.

    bronze (text, as-is) -> type -> standardise -> rules -> data/silver/<table>/year=<YYYY-YY>/part-0.parquet
                                                        -> data/quarantine/<table>.parquet  (error rules)

Silver tables and their bronze source (one row per ...):
    schools          profile1                       school and year
    enrolment        enrolment1, item_group 1       school, year and social category (General/SC/ST/OBC:
                                                    a complete split, so it sums to total enrolment)
    enrolment_other  enrolment1, other item_groups  school, year and item (religion, BPL, CWSN, repeaters, EWS)
    facilities       facility                       school and year
    teachers         teacher                        school and year

Typing: columns the schema calls smallint/integer/bigint become whole numbers (a
value that is not a whole number is an error, rule not_a_number); text stays text.
Each year's partition keeps only the source columns that year's files have, plus
the columns added here. Silver is rebuilt in full on every run (a few seconds).
"""
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from . import config as C
from .profiling import columns_by_year, read_sql
from .rules import Context, apply_rules

KEY = ["year", "pseudocode"]
# Silver table -> (bronze dataset, SQL filter). Processed in this order: schools first
# (the others are checked against it), enrolment before teachers (pupil-teacher ratio).
TABLES = {
    "schools": ("profile1", None),
    "enrolment": ("enrolment1", f"coalesce(try_cast(item_group as bigint), -1) = {C.ENROL_TOTAL_GROUP}"),
    "enrolment_other": ("enrolment1", f"coalesce(try_cast(item_group as bigint), -1) <> {C.ENROL_TOTAL_GROUP}"),
    "facilities": ("facility", None),
    "teachers": ("teacher", None),
}
INT_TYPES = {"smallint", "integer", "bigint"}
FLOAT_TYPES = {"double precision", "numeric", "real"}
META = ["year", "district_file", "_source_file", "_source_row"]


def is_whole_number(entry: dict) -> bool:
    # Coded columns are whole numbers even where the PDF leaves the type blank
    # (playground_alt_yn has no type in either year's schema).
    return entry.get("type", "") in INT_TYPES or entry.get("kind") in ("yes_no", "enum")


# --- Cleaning functions (moved here from build.py) ---------------------------

def normalise_district(s: pd.Series) -> pd.Series:
    # .str keeps a missing district missing (astype(str) would turn it into the text "NAN")
    s = s.str.strip().str.upper().str.replace(r"\s+", " ", regex=True)
    return s.replace(C.DISTRICT_ALIASES)


def yes_no(s: pd.Series) -> pd.Series:
    """1 -> 1, 2 -> 0, anything else (e.g. 9 = not applicable) -> missing."""
    return s.map(C.YES_NO).astype("Float64")


def standardise_schools(prof: pd.DataFrame) -> pd.DataFrame:
    prof["district_raw"] = prof["district"]
    prof["district"] = normalise_district(prof["district"])
    prof["block"] = prof["block"].str.strip().str.upper()
    # Some central-govt schools list an admin region (e.g. "JAIPUR REGION") as their block.
    prof["block_is_admin_region"] = prof["block"].str.contains("REGION", na=False)
    prof["block_clean"] = prof["block"].where(~prof["block_is_admin_region"])
    prof["location"] = prof["rural_urban"].map(C.RURAL_URBAN)
    prof["school_type_label"] = prof["school_type"].map(C.SCHOOL_TYPE)
    prof["management_group"] = prof["managment"].map(C.MANAGEMENT_GROUP).fillna("Unmapped")
    prof["is_govt"] = prof["management_group"].isin(C.GOVT_GROUPS).astype(int)
    return prof


def facility_flags(fac: pd.DataFrame) -> pd.DataFrame:
    """Facility indicators (1 = available, 0 = not, missing = not reported / not applicable)."""
    f = pd.DataFrame(index=fac.index)
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
    # total_class_rooms is 0 for all but 6 schools in the 2024-25 release, so the
    # classroom count is rebuilt from the condition breakdown (which matches
    # total_class_rooms exactly wherever that field is filled in).
    f["classrooms"] = (fac["classrooms_in_good_condition"] + fac["classrooms_needs_minor_repair"]
                       + fac["classrooms_needs_major_repair"])
    f["classrooms_good"] = fac["classrooms_in_good_condition"]
    f["pct_classrooms_major_repair"] = (fac["classrooms_needs_major_repair"]
                                        / f["classrooms"].replace(0, float("nan")) * 100).round(1)
    flag_cols = [c for c in f.columns if c.startswith("has_")]
    f[flag_cols] = f[flag_cols].astype(float)  # NaN = not reported / not applicable
    return f


# --- Reading bronze with types ----------------------------------------------

def column_entries(dataset: str, schema: dict) -> dict[str, dict]:
    """Schema entry per column, over all years (a column keeps its type across years)."""
    out = {}
    for _, cols in sorted(schema["datasets"].get(dataset, {}).items()):
        out.update({e["column"]: e for e in cols})
    return out


def typed_sql(source: str, columns: list[str], entries: dict, where: str | None) -> str:
    whole = "regexp_full_match(trim({c}), '-?[0-9]+')"
    select, errors = [], []
    for col in columns:
        c = f'"{col}"'
        dtype = entries.get(col, {}).get("type", "")
        if is_whole_number(entries.get(col, {})):
            select.append(f"case when {whole.format(c=c)} then cast(trim({c}) as bigint) end as {c}")
            errors.append(f"case when {c} is not null and not {whole.format(c=c)} then '{col}=' || {c} end")
        elif dtype in FLOAT_TYPES:
            select.append(f"try_cast({c} as double) as {c}")
            errors.append(f"case when {c} is not null and try_cast({c} as double) is null then '{col}=' || {c} end")
        else:
            select.append(c)
    cast_errors = f"concat_ws('; ', {', '.join(errors)})" if errors else "''"
    return f"""
        select year, _district_file as district_file, _source_file, _source_row,
               {', '.join(select)}, {cast_errors} as _cast_errors
        from {source} {f'where {where}' if where else ''}
        order by year, _source_file, _source_row"""


def to_storage_types(df: pd.DataFrame, entries: dict) -> pd.DataFrame:
    """Whole-number columns -> nullable int64 in Parquet; text -> string."""
    out = df.copy()
    for col in out.columns:
        if is_whole_number(entries.get(col, {})) and out[col].dtype.kind == "f":
            out[col] = out[col].astype("Int64")
        elif out[col].dtype == object:
            out[col] = out[col].astype("string")
    return out


def write_partitioned(df: pd.DataFrame, folder: Path, absent: dict[str, list[str]]) -> None:
    """data/silver/<table>/year=<Y>/part-0.parquet. Written to a temp folder, then swapped in."""
    tmp = folder.with_name(folder.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    for year, part in df.groupby("year", sort=True):
        out = tmp / f"year={year}" / "part-0.parquet"
        out.parent.mkdir(parents=True)
        part.drop(columns=["year"] + absent.get(year, [])).to_parquet(out, index=False, compression="zstd")
    if folder.exists():
        shutil.rmtree(folder)  # silver is a rebuilt output of this pipeline
    tmp.rename(folder)


# --- Build ------------------------------------------------------------------

def build_silver(bronze_dir: Path = C.BRONZE_DIR, silver_dir: Path = C.SILVER_DIR,
                 quarantine_dir: Path = C.QUARANTINE_DIR, run_id: str = "",
                 schema: dict | None = None) -> dict:
    """Build every silver table from bronze. Returns a summary for the run manifest."""
    schema = schema or C.load_reference("schema_columns.json")
    enrol_items = {y: {(i["item_group"], i["item_id"]) for i in items}
                   for y, items in schema["enrolment1_items"].items()}
    con = duckdb.connect()
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    ctx_shared: dict = {}
    summary: dict = {"tables": {}}

    for table, (dataset, where) in TABLES.items():
        folder = bronze_dir / dataset
        if not folder.exists():
            continue
        entries = column_entries(dataset, schema)
        present = columns_by_year(con, folder.as_posix())
        source_cols = sorted({c for cols in present.values() for c in cols if not c.startswith("_")},
                             key=lambda c: min(cols.index(c) for cols in present.values() if c in cols))
        df = con.sql(typed_sql(read_sql(folder.as_posix()), source_cols, entries, where)
                     ).to_arrow_table().to_pandas()
        rows_in = len(df)

        if table == "schools":
            df = standardise_schools(df)
        elif table == "facilities":
            df = pd.concat([df, facility_flags(df)], axis=1)

        ctx = Context(table=table, columns={c: e for c, e in entries.items() if c in df.columns},
                      enrol_items=enrol_items, **ctx_shared)
        clean, quarantined, counts = apply_rules(df, ctx)
        clean = clean.drop(columns="_cast_errors")

        if table == "schools":
            ctx_shared["schools"] = set(zip(clean["year"], clean["pseudocode"]))
        if table == "enrolment":
            cls = [c for c in clean.columns if c.startswith("c") and c[1:].split("_")[0].isdigit()]
            ctx_shared["enrolment_total"] = (clean.groupby(KEY)[cls].sum().sum(axis=1)
                                             .rename("enr_total").reset_index())

        absent = {y: [c for c in source_cols if c not in cols] for y, cols in present.items()}
        write_partitioned(to_storage_types(clean, entries), silver_dir / table, absent)
        to_storage_types(quarantined.assign(_run_id=run_id), entries).to_parquet(
            quarantine_dir / f"{table}.parquet", index=False, compression="zstd")

        summary["tables"][table] = {
            "source": dataset,
            "rows_in": rows_in,
            "rows_silver": len(clean),
            "rows_quarantined": len(quarantined),
            "quarantined_by_rule": counts["quarantined"],
            "flagged_by_rule": counts["flagged"],
        }
    summary["reconciled"] = reconcile(con, bronze_dir, summary["tables"])
    return summary


def reconcile(con, bronze_dir: Path, tables: dict) -> dict:
    """Every bronze row ends up in silver or quarantine: bronze = silver + quarantine, per dataset."""
    out = {}
    for dataset in {t["source"] for t in tables.values()}:
        bronze = con.sql(f"select count(*) from {read_sql((bronze_dir / dataset).as_posix())}").fetchone()[0]
        landed = sum(t["rows_silver"] + t["rows_quarantined"] for t in tables.values() if t["source"] == dataset)
        if bronze != landed:
            raise RuntimeError(f"{dataset}: {bronze:,} bronze rows but {landed:,} in silver + quarantine")
        out[dataset] = {"bronze": bronze, "silver_plus_quarantine": landed}
    return out


# --- Reading silver ---------------------------------------------------------

def read_silver(table: str, silver_dir: Path = C.SILVER_DIR) -> pd.DataFrame:
    """A silver table as pandas, rows in source-file order. Whole-number columns with
    gaps come back as float (NaN for missing), as pandas' read_csv would give them."""
    return duckdb.sql(f"select * from {read_sql((silver_dir / table).as_posix())} "
                      f"order by year, _source_file, _source_row").to_arrow_table().to_pandas()


def silver_tables(silver_dir: Path = C.SILVER_DIR) -> dict[str, str]:
    return {t: (silver_dir / t).as_posix() for t in TABLES if (silver_dir / t).exists()}


def tables_for_build(silver_dir: Path = C.SILVER_DIR) -> dict[str, pd.DataFrame]:
    """Silver tables under the dataset names build.py and checks.py use."""
    return {
        "profile1": read_silver("schools", silver_dir),
        "enrolment1": pd.concat([read_silver("enrolment", silver_dir),
                                 read_silver("enrolment_other", silver_dir)], ignore_index=True),
        "facility": read_silver("facilities", silver_dir),
        "teacher": read_silver("teachers", silver_dir),
    }
