"""Column profiles for every table of a layer (bronze and silver).

For each table, column and year:
    dtype, null %, distinct count, values that are not numbers (for columns the
    schema says are numeric), min / max, negative values, the 5 most common
    values, and invalid codes: values outside the allowed set taken from
    reference/schema_columns.json (e.g. yes/no columns must be 1, 2 or 9) or the
    other reference JSON files (management codes, district names).
Per table: rows per year and duplicate rows on the table's natural key.

How: DuckDB unpivots the table into (year, column, value), counts every distinct
value once, and all statistics are computed from that small value-count table.

Output: reports/profiling/<layer>__<table>.csv and reports/profiling/README.md,
which lists only the columns with problems.
"""
from pathlib import Path

import duckdb
import pandas as pd

from . import config as C
from .rules import code_set

# Natural key of each table (plus year): one row per school, or per school and item for enrolment.
NATURAL_KEYS = {
    "profile1": ["pseudocode"],
    "profile2": ["pseudocode"],
    "facility": ["pseudocode"],
    "teacher": ["pseudocode"],
    "safety": ["pseudocode"],
    "enrolment1": ["pseudocode", "item_group", "item_id"],
    # silver
    "schools": ["pseudocode"],
    "facilities": ["pseudocode"],
    "teachers": ["pseudocode"],
    "enrolment": ["pseudocode", "item_group", "item_id"],
    "enrolment_other": ["pseudocode", "item_group", "item_id"],
}
TOP_N = 5
# School codes are never written to reports (UDISE+ terms: no re-identification of
# schools). Their counts (nulls, distinct, duplicates) are still profiled.
IDENTIFIER_COLUMNS = {"pseudocode"}
HIDDEN = "(hidden: school identifier)"
# A column whose most common value covers this many percentage points more of
# the rows in one year than in another has changed how it is reported.
DRIFT_PP = 50


def bronze_tables(bronze_dir: Path = C.BRONZE_DIR) -> dict[str, str]:
    """Table name -> folder of year=... partitions, for every dataset in bronze."""
    return {p.name: p.as_posix() for p in sorted(bronze_dir.iterdir()) if p.is_dir()}


def read_sql(folder: str) -> str:
    return (f"read_parquet('{folder}/*/*.parquet', hive_partitioning = true, "
            f"union_by_name = true)")


def columns_by_year(con, folder: str) -> dict[str, list[str]]:
    """Columns actually present in each year's files (union_by_name fills the rest with NULL)."""
    out = {}
    for part in sorted(Path(folder).glob("year=*")):
        rows = con.sql(f"describe select * from read_parquet('{part.as_posix()}/*.parquet', "
                       f"union_by_name = true, hive_partitioning = false)").fetchall()
        out[part.name.split("=", 1)[1]] = [r[0] for r in rows]
    return out


# --- Allowed values ---------------------------------------------------------

def allowed_values(dataset: str, year: str, column: str, entry: dict | None,
                   enrol_items: dict) -> tuple[set | None, str]:
    """Allowed set for a column and where it comes from; None = no rule for this column."""
    if column == "managment":
        return set(C.MANAGEMENT_GROUP), "reference/management_codes.json"
    if column == "rural_urban":
        return set(C.RURAL_URBAN), "config.RURAL_URBAN (checked against village/urban-body names)"
    if column == "district":
        return set(C.DISTRICTS) | set(C.DISTRICT_ALIASES), "reference/districts.json (names + aliases)"
    if dataset == "enrolment1" and column == "item_group":
        return {i["item_group"] for i in enrol_items.get(year, [])}, "schema item_group/item_id list"
    codes = code_set(entry)
    if codes is None:
        return None, ""
    if entry["kind"] == "yes_no":
        return codes, "schema (Yes=1, No=2) + 9 = not applicable"
    return codes, f"schema ({entry['remarks']})"


def is_valid(values: pd.Series, numbers: pd.Series, allowed: set) -> pd.Series:
    if all(isinstance(a, int) for a in allowed):
        whole = numbers.notna() & (numbers == numbers.round())
        return whole & numbers.where(whole).isin(allowed)
    return values.isin(allowed)


def top(df: pd.DataFrame, n: int = TOP_N) -> pd.DataFrame:
    """Most common values; ties broken by value so reports only change when the data does."""
    return df.sort_values(["n", "val"], ascending=[False, True], na_position="first").head(n)


def fmt_counts(df: pd.DataFrame) -> str:
    return "; ".join(f"{v} ({n:,})" for v, n in zip(df["val"], df["n"]))


# --- Profiling --------------------------------------------------------------

def profile_table(con, layer: str, table: str, folder: str, schema: dict,
                  dataset: str | None = None) -> tuple[pd.DataFrame, dict]:
    """`dataset` is the bronze dataset whose schema applies (silver 'schools' -> 'profile1')."""
    dataset = dataset or table
    src = read_sql(folder)
    present = columns_by_year(con, folder)
    dtypes = {r[0]: r[1] for r in con.sql(f"describe select * from {src}").fetchall()}
    cols = [c for c in dtypes if not c.startswith("_") and c != "year"]

    # One pass: count every distinct value of every column per year.
    casts = ", ".join(f'cast("{c}" as varchar) as "{c}"' for c in cols)
    names = ", ".join(f'"{c}"' for c in cols)
    vc = con.sql(f"""
        with t as (select year, {casts} from {src})
        select year, col, val, count(*) as n
        from t unpivot include nulls (val for col in ({names}))
        group by all""").df()
    vc["num"] = pd.to_numeric(vc["val"], errors="coerce")

    spec = schema["datasets"].get(dataset, {})
    enrol_items = schema["enrolment1_items"]
    rows = []
    for (year, col), g in vc.groupby(["year", "col"], sort=True):
        entries = {e["column"]: e for e in spec.get(year, [])}
        entry = entries.get(col)
        in_source = col in present.get(year, [])
        notnull = g[g["val"].notna()]
        n_rows, n_null = int(g["n"].sum()), int(g.loc[g["val"].isna(), "n"].sum())
        non_numeric = notnull[notnull["num"].isna()]
        allowed, allowed_src = allowed_values(dataset, year, col, entry, enrol_items)
        invalid = (notnull[~is_valid(notnull["val"], notnull["num"], allowed)] if allowed is not None
                   else notnull.iloc[0:0])
        numeric = notnull["num"].notna().any()
        rec = {
            "layer": layer, "table": table, "year": year, "column": col,
            "in_source": in_source,
            "in_schema": entry is not None,
            "dtype": dtypes[col],
            "schema_type": entry["type"] if entry else "",
            "schema_kind": entry["kind"] if entry else "",
            "rows": n_rows,
            "null_count": n_null,
            "null_pct": round(n_null / n_rows * 100, 2) if n_rows else None,
            "distinct_count": len(notnull),
            "non_numeric_count": int(non_numeric["n"].sum()),
            "min": notnull["num"].min() if numeric else None,
            "max": notnull["num"].max() if numeric else None,
            "negative_count": int(notnull.loc[notnull["num"] < 0, "n"].sum()),
            "allowed_values": ", ".join(map(str, sorted(allowed))) if allowed and len(allowed) <= 20
                              else (f"{len(allowed)} values" if allowed else ""),
            "allowed_source": allowed_src,
            "invalid_count": int(invalid["n"].sum()),
            "invalid_values": fmt_counts(top(invalid)),
            "top_values": fmt_counts(top(g).fillna({"val": "<null>"})),
            "top_value_share_pct": round(notnull["n"].max() / (n_rows - n_null) * 100, 2)
                                   if n_rows > n_null else None,
        }
        rec["problems"] = "; ".join(column_problems(rec, entry, col in NATURAL_KEYS.get(table, []),
                                                    extra_columns_ok=layer != "bronze"))
        if col in IDENTIFIER_COLUMNS:
            rec.update({"min": None, "max": None, "top_values": HIDDEN,
                        "invalid_values": HIDDEN if rec["invalid_count"] else ""})
        rows.append(rec)

    # Columns the schema lists that are in no year's files at all (the others got a
    # row above, because union_by_name gives them NULLs in the years they are missing).
    for year, entries in spec.items():
        if year not in present:
            continue
        for e in entries:
            if e["column"] not in cols:
                rows.append({"layer": layer, "table": table, "year": year, "column": e["column"],
                             "in_source": False, "in_schema": True, "schema_type": e["type"],
                             "schema_kind": e["kind"], "problems": "in schema, not in data"})

    profile = pd.DataFrame(rows).sort_values(["column", "year"], kind="stable")
    add_drift_problems(profile)
    return profile, table_checks(con, table, src, schema, dataset)


def column_problems(r: dict, entry: dict | None, is_key: bool = False,
                    extra_columns_ok: bool = False) -> list[str]:
    """extra_columns_ok: silver adds its own columns (district_raw, has_*, flag_*, ...)."""
    if not r["in_source"]:
        # Missing from this year's files: a problem only if the schema says it should be there.
        return ["in schema, not in data"] if entry else []
    p = []
    if not r["in_schema"] and not extra_columns_ok:
        p.append("in data, not in schema")
    if r["null_count"] == r["rows"]:
        p.append("all values empty")
    if entry and entry["numeric"] and r["non_numeric_count"]:
        p.append(f"{r['non_numeric_count']:,} non-numeric values")
    if r["negative_count"]:
        p.append(f"{r['negative_count']:,} negative values")
    if r["invalid_count"]:
        p.append(f"{r['invalid_count']:,} invalid codes: {r['invalid_values']}")
    # A count that is the same non-zero number for every school is suspicious; one that is 0
    # everywhere can be genuinely rare (e.g. transgender enrolment by religion or disability).
    if (entry and entry["kind"] == "numeric" and not is_key and r["distinct_count"] == 1
            and r["null_count"] < r["rows"] and r["min"] != 0):
        p.append(f"same value in every row ({r['top_values']})")
    return p


def add_drift_problems(profile: pd.DataFrame) -> None:
    """Flag columns whose most common value covers far more rows in one year than another
    (e.g. a field left at 0 by almost every school in one release only)."""
    live = profile[profile["in_source"] & profile["top_value_share_pct"].notna()]
    for col, g in live.groupby("column"):
        if len(g) < 2:
            continue
        hi, lo = g.loc[g["top_value_share_pct"].idxmax()], g.loc[g["top_value_share_pct"].idxmin()]
        if hi["top_value_share_pct"] - lo["top_value_share_pct"] > DRIFT_PP:
            top = hi["top_values"].split(";")[0].split(" (")[0]
            msg = (f"value {top} in {hi['top_value_share_pct']}% of rows, vs at most "
                   f"{lo['top_value_share_pct']}% for any value in {lo['year']} (reporting change?)")
            old = profile.at[hi.name, "problems"]
            profile.at[hi.name, "problems"] = f"{old}; {msg}" if old else msg


def table_checks(con, table: str, src: str, schema: dict, dataset: str) -> dict:
    out = {"rows": dict(con.sql(f"select year, count(*) from {src} group by 1 order by 1").fetchall())}
    key = NATURAL_KEYS.get(table)
    if key:
        k = ", ".join(key)
        out["natural_key"] = ["year"] + key
        out["duplicate_rows"] = dict(con.sql(
            f"select year, count(*) - count(distinct ({k})) from {src} group by 1 order by 1").fetchall())
    if dataset == "enrolment1":
        pairs = con.sql(f"""select year, try_cast(item_group as int) g, try_cast(item_id as int) i,
                                   count(*) n from {src} group by all order by all""").fetchall()
        documented = {y: {(e["item_group"], e["item_id"]) for e in items}
                      for y, items in schema["enrolment1_items"].items()}
        out["undocumented_items"] = [
            {"year": y, "item_group": g, "item_id": i, "rows": n}
            for y, g, i, n in pairs if (g, i) not in documented.get(y, set())]
    return out


def profile_layer(layer: str, tables: dict[str, str], out_dir: Path = C.PROFILING_DIR,
                  schema: dict | None = None, datasets: dict[str, str] | None = None) -> dict:
    """Profile every table of a layer; write one CSV per table. Returns a summary.

    datasets maps a table to the bronze dataset whose schema applies (default: same name)."""
    schema = schema or C.load_reference("schema_columns.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    results = {}
    for table, folder in tables.items():
        prof, checks = profile_table(con, layer, table, folder, schema, (datasets or {}).get(table))
        prof.to_csv(out_dir / f"{layer}__{table}.csv", index=False)
        results[table] = {"profile": prof, "checks": checks}
    return results


# --- README -----------------------------------------------------------------

def write_readme(layers: dict[str, dict], out_dir: Path = C.PROFILING_DIR) -> dict:
    """Short summary listing only the columns with problems. Returns problem counts."""
    lines = ["# Data profiling", "",
             "Generated by `pipeline/profiling.py` on every run. Full per-column profiles are in",
             "the CSV files next to this one (`<layer>__<table>.csv`, one row per column and year).",
             "Allowed codes come from `reference/schema_columns.json` (parsed from the UDISE+ schema",
             "PDFs) and the other files in `reference/`.", "",
             "Source: UDISE+, Ministry of Education, Government of India.", ""]
    counts = {}
    for layer, results in layers.items():
        lines += [f"## {layer.title()} layer", "", "| Table | Year | Rows | Duplicate rows on natural key | Columns with problems |",
                  "|---|---|---:|---:|---:|"]
        for table, res in results.items():
            prof, chk = res["profile"], res["checks"]
            for year, n in chk["rows"].items():
                bad = prof[(prof["year"] == year) & (prof["problems"].fillna("") != "")]
                dup = chk.get("duplicate_rows", {}).get(year)
                lines.append(f"| {table} | {year} | {n:,} | {'' if dup is None else f'{dup:,}'} | {len(bad)} |")
        lines.append("")
        lines.append("Natural keys: " + "; ".join(
            f"{t} = ({', '.join(r['checks']['natural_key'])})"
            for t, r in results.items() if "natural_key" in r["checks"]) + ".")
        lines.append("")

        for table, res in results.items():
            prof = res["profile"]
            bad = prof[prof["problems"].fillna("") != ""]
            counts[f"{layer}__{table}"] = len(bad)
            undocumented = res["checks"].get("undocumented_items", [])
            if bad.empty and not undocumented:
                continue
            lines += [f"### {layer}__{table}", ""]
            if len(bad):
                lines += ["| Column | Year | Problem |", "|---|---|---|"]
                for _, r in bad.iterrows():
                    lines.append(f"| `{r['column']}` | {r['year']} | {r['problems']} |")
                lines.append("")
            if undocumented:
                lines.append("(item_group, item_id) pairs not documented in the schema:")
                lines.append("")
                lines += ["| Year | item_group | item_id | Rows |", "|---|---:|---:|---:|"]
                lines += [f"| {u['year']} | {u['item_group']} | {u['item_id']} | {u['rows']:,} |"
                          for u in undocumented]
                lines.append("")
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return counts
