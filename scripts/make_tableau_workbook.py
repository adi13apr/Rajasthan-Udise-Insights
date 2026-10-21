"""Generate the Tableau workbook: tableau/rajasthan_udise_dashboard.twbx

    python scripts/make_tableau_workbook.py

A .twbx is a zip holding the workbook XML (.twb) and its data. This script writes
both from the committed district-level files, so the dashboard can be rebuilt after
any pipeline run and opened in Tableau Public (which then saves it to the web).

Data (district aggregates only, fine to publish):
    data/gold/exports/tableau_district_indicators.csv     41 districts x 2 years
    reports/analysis/a4_single_teacher_change_matched.csv  same schools in both years
    reports/analysis/a2_girls_share_by_sector_2025-26.csv  government vs non-government

Workbook: parameters Year and Indicator; calculated fields; 11 worksheets; dashboards
Overview, Girls in secondary, Teachers. docs/tableau_guide.md describes the same design.
"""
import os
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pandas as pd
from tableauhyperapi import (Connection, CreateMode, HyperProcess, Inserter, SqlType, TableDefinition, TableName,
                             Telemetry)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tableau" / "rajasthan_udise_dashboard.twbx"
TWB_NAME = "rajasthan_udise_dashboard.twb"
DATA_DIR = "Data/Datasources"
EXTRACT_DIR = "Data/Extracts"   # Tableau Public only accepts extracts (.hyper), not live files
USER_NS = "http://www.tableausoftware.com/xml/user"
ET.register_namespace("user", USER_NS)

SOURCE_NOTE = "Source: UDISE+, Ministry of Education, Government of India. District aggregates; patterns are associations, not causes."
BLUE, ORANGE, AQUA, GREY, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#d3d2cb", "#0b0b0b", "#52514e"
FONT = "Segoe UI"

FILES = {
    "districts": ROOT / "data" / "gold" / "exports" / "tableau_district_indicators.csv",
    "matched": ROOT / "reports" / "analysis" / "a4_single_teacher_change_matched.csv",
    "sector": ROOT / "reports" / "analysis" / "a2_girls_share_by_sector_2025-26.csv",
}
CAPTIONS = {"districts": "District indicators", "matched": "Single-teacher change (same schools)",
            "sector": "Girls by school sector"}
def tableau_id(seed: str, n: int = 28) -> str:
    """A stable id shaped like Tableau's own (28 lower-case letters and digits)."""
    import hashlib
    digest = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(digest >> (5 * i)) % 36] for i in range(n))


DS = {k: "federated." + tableau_id(k) for k in FILES}   # internal datasource names, Tableau-shaped
P_YEAR, P_IND = "[Parameters].[Parameter 1]", "[Parameters].[Parameter 2]"
INDICATORS = ["Infrastructure index", "Single-teacher schools (%)", "Students per teacher",
              "Girls' retention gap (points)"]
DESERT = ["JAISALMER", "BARMER", "PHALODI", "BALOTRA"]
TRIBAL_SOUTH = ["BANSWARA", "DUNGARPUR", "PRATAPGARH", "SALUMBAR", "UDAIPUR"]


def load(key: str) -> pd.DataFrame:
    df = pd.read_csv(FILES[key])
    df.insert(1, "district_label", df["district"].str.title())   # SAWAI MADHOPUR -> Sawai Madhopur
    for c in df.columns:
        if pd.api.types.is_float_dtype(df[c]) and df[c].notna().all() and (df[c] == df[c].round()).all():
            df[c] = df[c].astype("int64")                         # counts as whole numbers
    return df


DFS = {key: load(key) for key in FILES}


def stable_uuid(name: str) -> str:
    """The same id on every build, so the generated file only changes when the design does."""
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "rajasthan-udise:" + name)).upper() + "}"


def q(s: str) -> str:
    """A Tableau string literal."""
    return '"' + s.replace('"', '""') + '"'


# Calculated fields: (internal name, caption, datatype, role, type, formula)
CALCS = {
    "districts": [
        ("Calc_SelectedYear", "Selected year", "boolean", "dimension", "nominal", f"[year] = {P_YEAR}"),
        ("Calc_Indicator", "Indicator value", "real", "measure", "quantitative",
         f"CASE {P_IND} WHEN {q(INDICATORS[0])} THEN [avg_infra_index] "
         f"WHEN {q(INDICATORS[1])} THEN [pct_single_teacher] WHEN {q(INDICATORS[2])} THEN [ptr] "
         f"WHEN {q(INDICATORS[3])} THEN [girls_retention_gap] * 100 END"),
        ("Calc_Rank", "Rank (1 = worst)", "integer", "measure", "quantitative",
         f"CASE {P_IND} WHEN {q(INDICATORS[0])} THEN [rank_avg_infra_index] "
         f"WHEN {q(INDICATORS[1])} THEN [rank_pct_single_teacher] WHEN {q(INDICATORS[2])} THEN [rank_ptr] "
         f"WHEN {q(INDICATORS[3])} THEN [rank_girls_retention_gap] END"),
        ("Calc_Worst10", "Highlight", "string", "dimension", "nominal",
         "IF " + f"CASE {P_IND} WHEN {q(INDICATORS[0])} THEN [rank_avg_infra_index] "
         f"WHEN {q(INDICATORS[1])} THEN [rank_pct_single_teacher] WHEN {q(INDICATORS[2])} THEN [rank_ptr] "
         f"WHEN {q(INDICATORS[3])} THEN [rank_girls_retention_gap] END <= 10" + " THEN \"Worst 10\" ELSE \"Other districts\" END"),
        ("Calc_Worst10Flag", "Among the worst 10", "integer", "measure", "quantitative",
         "IF " + f"CASE {P_IND} WHEN {q(INDICATORS[0])} THEN [rank_avg_infra_index] "
         f"WHEN {q(INDICATORS[1])} THEN [rank_pct_single_teacher] WHEN {q(INDICATORS[2])} THEN [rank_ptr] "
         f"WHEN {q(INDICATORS[3])} THEN [rank_girls_retention_gap] END <= 10" + " THEN 1 ELSE 0 END"),
        ("Calc_TribalFlag", "Tribal-majority south", "integer", "measure", "quantitative",
         "IF [district] = " + " OR [district] = ".join(q(d) for d in TRIBAL_SOUTH) + " THEN 1 ELSE 0 END"),
        ("Calc_StudentsCrore", "Students (crore)", "real", "measure", "quantitative",
         "SUM([enrolment]) / 10000000"),
        ("Calc_TeachersLakh", "Teachers (lakh)", "real", "measure", "quantitative",
         "SUM([teachers]) / 100000"),
        ("Calc_StatePTR", "Students per teacher (state)", "real", "measure", "quantitative",
         "SUM([enrolment]) / SUM([teachers])"),
        ("Calc_StateSingle", "Single-teacher schools % (state)", "real", "measure", "quantitative",
         "SUM([pct_single_teacher] * [schools]) / SUM([schools])"),
        ("Calc_StateInfra", "Infrastructure index (state)", "real", "measure", "quantitative",
         "SUM([avg_infra_index] * [schools]) / SUM([schools])"),
        ("Calc_TribalSouth", "Region", "string", "dimension", "nominal",
         "IF [district] = " + " OR [district] = ".join(q(d) for d in TRIBAL_SOUTH)
         + " THEN \"Tribal-majority south\" ELSE \"Other districts\" END"),
        ("Calc_TribalLabel", "Label", "string", "dimension", "nominal",
         "IF [district] = " + " OR [district] = ".join(q(d) for d in TRIBAL_SOUTH) + " THEN [district_label] END"),
        ("Calc_PatternLabel", "Label", "string", "dimension", "nominal",
         "IF [pattern] <> \"other\" THEN [district_label] END"),
    ],
    "matched": [
        ("Calc_DesertFlag", "Western desert", "integer", "measure", "quantitative",
         "IF [district] = " + " OR [district] = ".join(q(d) for d in DESERT) + " THEN 1 ELSE 0 END"),
        ("Calc_Desert", "Region", "string", "dimension", "nominal",
         "IF [district] = " + " OR [district] = ".join(q(d) for d in DESERT)
         + " THEN \"Western desert\" ELSE \"Other districts\" END"),
    ],
    "sector": [
        ("Calc_GapPts", "Girls' retention gap (points)", "real", "measure", "quantitative",
         "[girls_retention_gap] * 100"),
    ],
}
FORMATS = {  # Tableau default number formats, by field
    "Calc_Indicator": "n#,##0.0;-#,##0.0", "avg_infra_index": "n#,##0.0;-#,##0.0",
    "pct_single_teacher": "n#,##0.0;-#,##0.0", "ptr": "n#,##0.0;-#,##0.0", "change_pp": "n#,##0.0;-#,##0.0",
    "Calc_StatePTR": "n#,##0.0;-#,##0.0", "Calc_StudentsCrore": "n#,##0.00;-#,##0.00",
    "Calc_TeachersLakh": "n#,##0.00;-#,##0.00", "Calc_StateSingle": "n#,##0.0;-#,##0.0",
    "Calc_StateInfra": "n#,##0.0;-#,##0.0", "Calc_GapPts": "n#,##0.0;-#,##0.0",
    "boys_progression": "n#,##0.00;-#,##0.00", "girls_progression": "n#,##0.00;-#,##0.00",
}
CAPTION_OF = {"district_label": "District", "avg_infra_index": "Infrastructure index", "pct_single_teacher": "Single-teacher schools (%)",
              "ptr": "Students per teacher", "boys_progression": "Boys' progression ratio",
              "girls_progression": "Girls' progression ratio", "change_pp": "Change in single-teacher schools (points)",
              "schools": "Schools", "enrolment": "Students (classes 1-12)", "teachers": "Teachers",
              "pattern": "Gender pattern", "sector": "School sector"}


# --- XML helpers --------------------------------------------------------------

def sub(parent, tag, attrs=None, text=None):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrs or {}).items()})
    if text is not None:
        el.text = text
    return el


def tableau_type(dtype) -> str:
    return "integer" if pd.api.types.is_integer_dtype(dtype) else \
        "real" if pd.api.types.is_float_dtype(dtype) else "string"


def datasource(parent, key: str, df: pd.DataFrame):
    path = FILES[key]
    ds = sub(parent, "datasource", {"caption": CAPTIONS[key], "inline": "true", "name": DS[key], "version": "18.1"})
    conn = sub(ds, "connection", {"class": "federated"})
    named = sub(sub(conn, "named-connections"), "named-connection",
                {"caption": path.stem, "name": f"textscan.{key}"})
    sub(named, "connection", {"class": "textscan", "directory": DATA_DIR, "filename": path.name,
                              "password": "", "server": ""})
    rel = sub(conn, "relation", {"connection": f"textscan.{key}", "name": path.name,
                                 "table": f"[{path.stem}#csv]", "type": "table"})
    cols = sub(rel, "columns", {"character-set": "UTF-8", "header": "yes", "locale": "en_US", "separator": ","})
    for i, (c, t) in enumerate(df.dtypes.items()):
        sub(cols, "column", {"datatype": tableau_type(t), "name": c, "ordinal": i})
    sub(ds, "aliases", {"enabled": "yes"})
    for c, t in df.dtypes.items():
        typ = tableau_type(t)
        attrs = {"datatype": typ, "name": f"[{c}]",
                 "role": "dimension" if typ == "string" else "measure",
                 "type": "nominal" if typ == "string" else "quantitative"}
        if c in CAPTION_OF:
            attrs = {"caption": CAPTION_OF[c], **attrs}
        if c in FORMATS:
            attrs["default-format"] = FORMATS[c]
        sub(ds, "column", attrs)
    for name, caption, dtype, role, typ, formula in CALCS[key]:
        col = sub(ds, "column", {"caption": caption, "datatype": dtype, "name": f"[{name}]", "role": role, "type": typ,
                                 **({"default-format": FORMATS[name]} if name in FORMATS else {})})
        sub(col, "calculation", {"class": "tableau", "formula": formula})
    # The schema wants <extract> after the columns and before <layout>.
    ext = sub(ds, "extract", {"count": "-1", "enabled": "true", "units": "records"})
    hyper = sub(ext, "connection", {"class": "hyper", "dbname": f"{EXTRACT_DIR}/{key}.hyper", "default-settings": "yes",
                                    "schema": "Extract", "tablename": "Extract",
                                    "update-time": "10/21/2026 10:00:00 AM"})
    sub(hyper, "relation", {"name": "Extract", "table": "[Extract].[Extract]", "type": "table"})
    sub(ds, "layout", {"dim-ordering": "alphabetic", "dim-percentage": "0.5", "measure-ordering": "alphabetic",
                       "measure-percentage": "0.4", "show-structure": "true"})
    if os.environ.get("TABLEAU_DS_COLORS") == "1":  # experiment: colour maps on the data source
        style = sub(ds, "style")
        rule = sub(style, "style-rule", {"element": "mark"})
        done = set()
        for s in SHEETS:
            if s.key == key and s.color and s.palette and s.color[0] not in done:
                done.add(s.color[0])
                enc = sub(rule, "encoding", {"attr": "color", "field": Sheet.inst(*s.color), "type": "palette"})
                for value, colour in s.palette:
                    sub(sub(enc, "map", {"to": colour}), "bucket", text=q(value))


def parameters(parent):
    ds = sub(parent, "datasource", {"hasconnection": "false", "inline": "true", "name": "Parameters", "version": "18.1"})
    sub(ds, "aliases", {"enabled": "yes"})
    for name, caption, values, default in (("Parameter 1", "Year", ["2024-25", "2025-26"], "2025-26"),
                                           ("Parameter 2", "Indicator", INDICATORS, INDICATORS[0])):
        col = sub(ds, "column", {"caption": caption, "datatype": "string", "name": f"[{name}]",
                                 "param-domain-type": "list", "role": "measure", "type": "nominal",
                                 "value": q(default)})
        sub(col, "calculation", {"class": "tableau", "formula": q(default)})
        members = sub(col, "members")
        for v in values:
            sub(members, "member", {"value": q(v)})


# --- worksheets ---------------------------------------------------------------

class Sheet:
    """One worksheet: shelves are lists of (datasource key, field, derivation)."""

    def __init__(self, name, key, mark, rows=(), cols=(), color=None, label=None, detail=(), text=None,
                 sort=None, year_filter=True, title=None, palette=None, size=None, label_marks=None):
        self.__dict__.update(locals())

    @staticmethod
    def inst(field, deriv):
        kind = {"none": "nk", "sum": "qk", "usr": "qk", "avg": "qk", "max": "qk"}[deriv]
        return f"[{deriv}:{field}:{kind}]"


def field_meta(key, field):
    for name, caption, dtype, role, typ, _ in CALCS[key]:
        if name == field:
            return dtype, role, typ
    typ = tableau_type(DFS[key][field].dtype)
    return typ, ("dimension" if typ == "string" else "measure"), ("nominal" if typ == "string" else "quantitative")


def worksheet(parent, s: Sheet):
    ws = sub(parent, "worksheet", {"name": s.name})
    if s.title:
        lo = sub(ws, "layout-options")
        ft = sub(sub(lo, "title"), "formatted-text")
        for text, attrs in s.title:
            sub(ft, "run", {"fontname": FONT, **attrs}, text)
    table = sub(ws, "table")
    view = sub(table, "view")
    sub(sub(view, "datasources"), "datasource", {"caption": CAPTIONS[s.key], "name": DS[s.key]})
    used = list(s.rows) + list(s.cols) + list(s.detail) + [x for x in (s.color, s.label, s.text, s.size) if x]
    if s.year_filter:
        used.append(("Calc_SelectedYear", "none"))
    if s.sort:
        used.append(s.sort[1])
    deps = sub(view, "datasource-dependencies", {"datasource": DS[s.key]})
    seen = set()
    for field, _ in used:
        if field in seen:
            continue
        seen.add(field)
        dtype, role, typ = field_meta(s.key, field)
        calc = next((c for c in CALCS[s.key] if c[0] == field), None)
        col = sub(deps, "column", {"datatype": dtype, "name": f"[{field}]", "role": role, "type": typ,
                                   **({"caption": calc[1]} if calc else {}),
                                   **({"default-format": FORMATS[field]} if field in FORMATS else {})})
        if calc:
            sub(col, "calculation", {"class": "tableau", "formula": calc[5]})
    seen = set()
    for field, deriv in used:
        if (field, deriv) in seen:
            continue
        seen.add((field, deriv))
        dtype, role, typ = field_meta(s.key, field)
        sub(deps, "column-instance", {"column": f"[{field}]", "derivation": {"none": "None", "sum": "Sum", "usr": "User",
                                                                           "avg": "Avg", "max": "Max"}[deriv],
                                      "name": Sheet.inst(field, deriv), "pivot": "key",
                                      "type": "nominal" if deriv == "none" else "quantitative"})
    ds = DS[s.key]
    if s.year_filter:
        f = sub(view, "filter", {"class": "categorical", "column": f"[{ds}].[none:Calc_SelectedYear:nk]"})
        sub(f, "groupfilter", {"function": "member", "level": "[none:Calc_SelectedYear:nk]", "member": "true",
                               f"{{{USER_NS}}}ui-domain": "database", f"{{{USER_NS}}}ui-enumeration": "inclusive",
                               f"{{{USER_NS}}}ui-marker": "enumerate"})
    if s.sort:
        dim, (by, byd) = s.sort[0], s.sort[1]
        sub(view, "sort", {"class": "computed", "column": f"[{ds}].{Sheet.inst(dim[0], dim[1])}",
                           "direction": s.sort[2], "using": f"[{ds}].{Sheet.inst(by, byd)}"})
    sub(view, "aggregation", {"value": "true"})
    style = sub(table, "style")
    if s.palette and s.color:
        mark_rule = sub(style, "style-rule", {"element": "mark"})
        name = palette_name(s)
        enc = sub(mark_rule, "encoding", {"attr": "color", "field": f"[{ds}].{Sheet.inst(*s.color)}",
                                          "palette": name, "type": "palette"})
        cp = sub(enc, "color-palette", {"custom": "true", "name": name, "type": "regular"})
        for _, colour in sorted(s.palette):
            sub(cp, "color", text=colour)
        for value, colour in s.palette:
            m = sub(enc, "map", {"to": colour})
            sub(m, "bucket", text=q(value))   # text categories are quoted in Tableau's XML
    panes = sub(table, "panes")
    pane = sub(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
    sub(sub(pane, "view"), "breakdown", {"value": "auto"})
    sub(pane, "mark", {"class": s.mark})
    if s.mark == "Text":
        sub(pane, "mark-sizing", {"mark-sizing-setting": "marks-scaling-off"})
    enc = sub(pane, "encodings")
    if s.color:
        sub(enc, "color", {"column": f"[{ds}].{Sheet.inst(*s.color)}"})
    if s.size:
        sub(enc, "size", {"column": f"[{ds}].{Sheet.inst(*s.size)}"})
    if s.label:
        sub(enc, "text", {"column": f"[{ds}].{Sheet.inst(*s.label)}"})
    if s.text:
        sub(enc, "text", {"column": f"[{ds}].{Sheet.inst(*s.text)}"})
    for d in s.detail:
        sub(enc, "lod", {"column": f"[{ds}].{Sheet.inst(*d)}"})
    pstyle = sub(pane, "style")
    mr = sub(pstyle, "style-rule", {"element": "mark"})

    if s.label or s.text:
        sub(mr, "format", {"attr": "mark-labels-show", "value": "true"})
        if s.label_marks:
            sub(mr, "format", {"attr": "mark-labels-mode", "value": s.label_marks})
    if s.mark == "Text":
        sub(mr, "format", {"attr": "font-size", "value": "26"})
        sub(mr, "format", {"attr": "font-family", "value": FONT})
    if not (s.color or s.mark == "Text"):
        sub(mr, "format", {"attr": "mark-color", "value": BLUE})
    sub(table, "rows", text=" / ".join(f"[{ds}].{Sheet.inst(*r)}" for r in s.rows) or None)
    sub(table, "cols", text=" / ".join(f"[{ds}].{Sheet.inst(*c)}" for c in s.cols) or None)


def palette_name(s: Sheet) -> str:
    return "Rajasthan " + s.name


def preferences(wb):
    """Workbook palettes, one per coloured sheet, so they also appear in Tableau's palette list."""
    prefs = sub(wb, "preferences")
    for s in SHEETS:
        if s.palette:
            cp = sub(prefs, "color-palette", {"custom": "true", "name": palette_name(s), "type": "regular"})
            for _, colour in sorted(s.palette):
                sub(cp, "color", text=colour)


def kpi(name, label, field, deriv):
    return Sheet(name, "districts", "Text", text=(field, deriv),
                 title=[(label, {"fontsize": "9", "fontcolor": MUTED})])


SHEETS = [
    kpi("KPI Schools", "Schools with students", "schools", "sum"),
    kpi("KPI Students", "Students, classes 1-12 (crore)", "Calc_StudentsCrore", "usr"),
    kpi("KPI Teachers", "Teachers (lakh)", "Calc_TeachersLakh", "usr"),
    kpi("KPI PTR", "Students per teacher", "Calc_StatePTR", "usr"),
    kpi("KPI Single", "Single-teacher schools (%)", "Calc_StateSingle", "usr"),
    kpi("KPI Infra", "Infrastructure index (0-100)", "Calc_StateInfra", "usr"),
    Sheet("District ranking", "districts", "Bar", rows=[("district_label", "none")], cols=[("Calc_Indicator", "sum")],
          color=("Calc_Worst10Flag", "max"),
          label=("Calc_Indicator", "sum"), sort=(("district_label", "none"), ("Calc_Rank", "sum"), "ASC"),
          title=[("<[Parameters].[Parameter 2]>, <[Parameters].[Parameter 1]>", {"bold": "true", "fontsize": "13"}),
                 ("  worst 10 in dark blue; rank 1 at the top", {"fontsize": "10", "fontcolor": MUTED})]),
    Sheet("Gender patterns", "districts", "Circle", rows=[("girls_progression", "sum")],
          cols=[("boys_progression", "sum")], color=("pattern", "none"), detail=[("district_label", "none")],
          label=("Calc_PatternLabel", "none"),
          palette=[("girls leaving", ORANGE), ("boys moving in", BLUE), ("boys leaving (tribal south)", AQUA),
                   ("other", GREY)],
          title=[("Boys' vs girls' progression ratio, <[Parameters].[Parameter 1]>", {"bold": "true", "fontsize": "13"}),
                 ("  students per grade in classes 9-12 / classes 1-5", {"fontsize": "10", "fontcolor": MUTED})]),
    Sheet("Government vs private", "sector", "Circle", rows=[("district_label", "none")],
          cols=[("Calc_GapPts", "sum")], color=("sector", "none"), year_filter=False,
          palette=[("Government", BLUE), ("Non-government", ORANGE)],
          sort=(("district_label", "none"), ("Calc_GapPts", "sum"), "ASC"),
          title=[("Girls' retention gap by school sector, 2025-26 (points)", {"bold": "true", "fontsize": "13"})]),
    Sheet("Single-teacher change", "matched", "Bar", rows=[("district_label", "none")], cols=[("change_pp", "sum")],
          color=("Calc_DesertFlag", "max"), year_filter=False,
          label=("change_pp", "sum"), sort=(("district_label", "none"), ("change_pp", "sum"), "DESC"),
          title=[("Change in single-teacher schools, 2024-25 to 2025-26 (points)", {"bold": "true", "fontsize": "13"}),
                 ("  same schools in both years; western desert in dark blue", {"fontsize": "10", "fontcolor": MUTED})]),
    Sheet("Infrastructure vs single-teacher", "districts", "Circle", rows=[("avg_infra_index", "sum")],
          cols=[("pct_single_teacher", "sum")], color=("Calc_TribalFlag", "max"), detail=[("district_label", "none")],
          label=("Calc_TribalLabel", "none"),
          title=[("Infrastructure vs single-teacher schools, <[Parameters].[Parameter 1]>", {"bold": "true", "fontsize": "13"}),
                 ("  tribal-majority south in dark blue", {"fontsize": "10", "fontcolor": MUTED})]),
]


# --- dashboards ---------------------------------------------------------------

class Ids:
    n = 0

    @classmethod
    def next(cls):
        cls.n += 1
        return cls.n


def zone(parent, x, y, w, h, **attrs):
    """Dashboard coordinates are in 1/100000 of the dashboard's width and height."""
    return sub(parent, "zone", {"h": h, "id": Ids.next(), "w": w, "x": x, "y": y, **attrs})


def text_zone(parent, x, y, w, h, runs):
    z = zone(parent, x, y, w, h, **{"type-v2": "text"})
    ft = sub(z, "formatted-text")
    for text, attrs in runs:
        sub(ft, "run", {"fontname": FONT, **attrs}, text)
    return z


def dashboard(parent, name, build):
    d = sub(parent, "dashboard", {"name": name})
    sub(d, "style")
    h = os.environ.get("TABLEAU_TEST_HEIGHT", "900")  # a smaller height only to check layouts on a small screen
    sub(d, "size", {"maxheight": h, "maxwidth": "1200", "minheight": h, "minwidth": "1200"})
    zones = sub(d, "zones")
    root = zone(zones, 0, 0, 100000, 100000, **{"type-v2": "layout-basic"})
    build(root)
    zs = sub(root, "zone-style")
    sub(zs, "format", {"attr": "background-color", "value": "#f7f6f2"})


def header(root, subtitle):
    text_zone(root, 1500, 800, 68000, 10000, [
        ("Rajasthan schools, district by district\n", {"bold": "true", "fontsize": "22", "fontcolor": INK}),
        (subtitle, {"fontsize": "11", "fontcolor": MUTED})])
    zone(root, 70500, 2000, 13500, 6500, **{"mode": "compact", "param": P_YEAR, "type-v2": "paramctrl"})
    text_zone(root, 1500, 96200, 97000, 3200, [(SOURCE_NOTE, {"fontsize": "8", "fontcolor": MUTED})])


def overview(root):
    header(root, "Schools, teachers and infrastructure in all 41 districts. Worst 10 in dark blue.")
    zone(root, 84500, 2000, 14000, 6500, **{"mode": "compact", "param": P_IND, "type-v2": "paramctrl"})
    names = ["KPI Schools", "KPI Students", "KPI Teachers", "KPI PTR", "KPI Single", "KPI Infra"]
    w = 16000
    for i, n in enumerate(names):
        zone(root, 1500 + i * (w + 400), 12000, w, 11000, name=n)
    zone(root, 1500, 24500, 97000, 71000, name="District ranking")


def girls(root):
    header(root, "One gender gap, three problems: girls leaving, boys moving in, boys leaving.")
    zone(root, 1500, 12000, 50000, 74000, name="Gender patterns")
    zone(root, 52500, 12000, 46000, 74000, name="Government vs private")
    # colour legends under each chart
    zone(root, 1500, 86500, 50000, 9300, name="Gender patterns", param=f"[{DS['districts']}].[none:pattern:nk]",
         **{"type-v2": "color"})
    zone(root, 52500, 86500, 46000, 9300, name="Government vs private", param=f"[{DS['sector']}].[none:sector:nk]",
         **{"type-v2": "color"})


def teachers(root):
    header(root, "Teachers are thinning out in the desert; the tribal south lags on both counts.")
    zone(root, 1500, 12000, 50000, 83500, name="Single-teacher change")
    zone(root, 52500, 12000, 46000, 83500, name="Infrastructure vs single-teacher")


# --- workbook -----------------------------------------------------------------

DASH_SHEETS = {
    "Overview": ["KPI Schools", "KPI Students", "KPI Teachers", "KPI PTR", "KPI Single", "KPI Infra", "District ranking"],
    "Girls in secondary": ["Gender patterns", "Government vs private"],
    "Teachers": ["Single-teacher change", "Infrastructure vs single-teacher"],
}


def windows(wb):
    """One window (tab) per sheet and dashboard; the Overview dashboard opens first."""
    wins = sub(wb, "windows", {"source-height": "30"})
    for s in SHEETS:
        w = sub(wins, "window", {"class": "worksheet", "name": s.name})
        cards = sub(w, "cards")
        left = sub(sub(cards, "edge", {"name": "left"}), "strip", {"size": "160"})
        for c in ("pages", "filters", "marks"):
            sub(left, "card", {"type": c})
        top = sub(cards, "edge", {"name": "top"})
        for c, size in (("columns", "2147483647"), ("rows", "2147483647"), ("title", "31")):
            sub(sub(top, "strip", {"size": size}), "card", {"type": c})
    for i, (name, sheets) in enumerate(DASH_SHEETS.items()):
        first = os.environ.get("TABLEAU_OPEN_ON", "Overview")  # which dashboard opens first
        w = sub(wins, "window", {"class": "dashboard", **({"maximized": "true"} if name == first else {}), "name": name})
        vps = sub(w, "viewpoints")
        for s in sheets:
            sub(sub(vps, "viewpoint", {"name": s}), "zoom", {"type": "entire-view"})
        sub(w, "active", {"id": "-1"})
        sub(w, "device-preview")


def build_twb() -> bytes:
    # Document format 18.1: Tableau Public 2026.3 loads it (it upgrades older formats on open). Its loader
    # does not accept the 26.x layout of Tableau's published XSD, so that format is not used.
    wb = ET.Element("workbook", {"original-version": "18.1", "source-build": "2018.1.0 (20181.18.0404.1112)",
                                 "source-platform": "win", "version": "18.1"})
    preferences(wb)
    dss = sub(wb, "datasources")
    parameters(dss)
    for key in FILES:
        datasource(dss, key, DFS[key])
    wss = sub(wb, "worksheets")
    for s in SHEETS:
        worksheet(wss, s)
    dbs = sub(wb, "dashboards")
    for name, fn in (("Overview", overview), ("Girls in secondary", girls), ("Teachers", teachers)):
        dashboard(dbs, name, fn)
    windows(wb)
    ET.indent(wb)
    return b"<?xml version='1.0' encoding='utf-8' ?>\n" + ET.tostring(wb, encoding="utf-8")


def write_extracts(folder: Path) -> dict[str, Path]:
    """One .hyper extract per CSV: schema Extract, table Extract (Tableau's convention)."""
    sql = {"integer": SqlType.big_int(), "real": SqlType.double(), "string": SqlType.text()}
    out = {}
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(folder)}) as hp:
        for key, path in FILES.items():
            df = DFS[key]
            target = folder / f"{key}.hyper"
            with Connection(hp.endpoint, str(target), CreateMode.CREATE_AND_REPLACE) as con:
                con.catalog.create_schema("Extract")
                table = TableDefinition(TableName("Extract", "Extract"),
                                        [TableDefinition.Column(c, sql[tableau_type(t)]) for c, t in df.dtypes.items()])
                con.catalog.create_table(table)
                rows = [[None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in r]
                        for r in df.itertuples(index=False)]
                with Inserter(con, table) as ins:
                    ins.add_rows(rows)
                    ins.execute()
            out[key] = target
    return out


def main():
    OUT.parent.mkdir(exist_ok=True)
    twb = build_twb()
    (OUT.parent / TWB_NAME).write_bytes(twb)  # plain XML copy, readable in a diff
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(TWB_NAME, twb)
        for key, path in FILES.items():
            z.writestr(f"{DATA_DIR}/{path.name}", DFS[key].to_csv(index=False))
        for key, hyper in write_extracts(Path(tmp)).items():
            z.write(hyper, f"{EXTRACT_DIR}/{key}.hyper")
    print(f"Written {OUT} ({OUT.stat().st_size:,} bytes): {len(SHEETS)} worksheets, 3 dashboards")


if __name__ == "__main__":
    main()
