"""Generate the Power BI project: powerbi/RajasthanUDISE.pbip

    python scripts/make_powerbi_project.py
    python scripts/make_powerbi_project.py --data-folder D:/somewhere/powerbi/data

A .pbip project is Power BI Desktop's text format: the semantic model in TMDL files and the
report in JSON files, instead of one binary .pbix. Writing it from code means the model is
reviewable in git and rebuilt the same way every time:

    RajasthanUDISE.pbip                     open this in Power BI Desktop, then Refresh
    RajasthanUDISE.SemanticModel/definition tables (CSV import in Power Query M, typed columns),
                                            relationships, the two calculated columns and every
                                            measure, taken from powerbi/MEASURES.md (one source of truth)
    RajasthanUDISE.Report/definition        three pages: State overview, District comparison, Validation

The project holds no data: Power BI loads powerbi/data/*.csv on refresh and caches them in
.pbi/cache.abf, which is gitignored (school-level data, UDISE+ data-sharing terms).
Column types come from the CSVs written by scripts/export_powerbi.py, so run that first.
"""
import argparse
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import config as C  # noqa: E402
from scripts.export_powerbi import OUT_DIR as CSV_DIR, TABLES  # noqa: E402

PBI_DIR = C.ROOT / "powerbi"
NAME = "RajasthanUDISE"
MEASURES_MD = PBI_DIR / "MEASURES.md"
MEASURE_TABLE = "_Measures"
NS = uuid.UUID("6f1c2a52-2f0c-4a43-9a0e-7d3b5b1f0c11")  # stable lineage tags: same input -> same files
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
SOURCE_NOTE = (f"{C.SOURCE_NOTE}. District indicators count schools with students in classes 1-12. "
               "Norms: RTE Act 2009 (classes 1-8), RMSA (9-10), 30:1 assumed for 11-12.")

LOADED_TABLES = [*TABLES, "official_totals", "norms"]
RELATIONSHIPS = [  # (many side, one side)
    ("fact_enrolment.school_key", "dim_school.school_key"),
    ("fact_enrolment.year_key", "dim_year.year_key"),
    ("fact_enrolment.grade_key", "dim_grade.grade_key"),
    ("fact_enrolment.gender_key", "dim_gender.gender_key"),
    ("fact_enrolment.social_category_key", "dim_social_category.social_category_key"),
    ("fact_teachers.school_key", "dim_school.school_key"),
    ("fact_teachers.year_key", "dim_year.year_key"),
    ("fact_school_facilities.school_key", "dim_school.school_key"),
    ("fact_school_facilities.year_key", "dim_year.year_key"),
    ("agg_school_year.school_key", "dim_school.school_key"),
    ("agg_school_year.year_key", "dim_year.year_key"),
    ("dim_school.district_key", "dim_district.district_key"),
    ("dim_school.management_key", "dim_management.management_key"),
]
SORT_BY = {("dim_year", "academic_year"): "start_year", ("dim_grade", "grade_label"): "grade_key"}
# DuckDB type of the CSV column -> (Power Query type, TMDL dataType)
TYPES = {"BIGINT": ("Int64.Type", "int64"), "INTEGER": ("Int64.Type", "int64"), "HUGEINT": ("Int64.Type", "int64"),
         "DOUBLE": ("type number", "double"), "BOOLEAN": ("type logical", "boolean"),
         "VARCHAR": ("type text", "string")}
COUNT, DEC1, PCT1 = "#,0", "0.0", "0.0%"
SIGNED_DEC, SIGNED_PCT, SIGNED_COUNT = "+0.0;-0.0;0.0", "+0.0%;-0.0%;0.0%", "+#,0;-#,0;0"
FORMATS = {  # measure -> format string (default: COUNT; text measures get none)
    "PTR": DEC1, "PTR PY": DEC1, "PTR (State)": DEC1, "PTR (Mean of Districts)": DEC1,
    "Infrastructure Index": DEC1, "Infrastructure Index (State)": DEC1,
    "PTR vs State": SIGNED_DEC, "Infrastructure Index vs State": SIGNED_DEC, "PTR YoY Change": "+0.00;-0.00;0.00",
    "PTR vs State %": SIGNED_PCT, "Enrolment 1-12 YoY %": SIGNED_PCT, "Teachers YoY %": SIGNED_PCT,
    "Single-Teacher Schools % YoY Change": SIGNED_PCT, "Single-Teacher Schools % vs State": SIGNED_PCT,
    "Girls' Share 1-12 vs State": SIGNED_PCT, "Teacher Shortfall % vs State": SIGNED_PCT,
    "Girls' Retention Gap": SIGNED_PCT, "Difference %": "0.000%", "Difference": SIGNED_COUNT,
    "Net Teacher Gap": SIGNED_COUNT, "Teacher Shortfall YoY Change": SIGNED_COUNT, "PTR Above State": "0",
}
TEXT_MEASURES = {"Match Status", "Match Colour", "Validation Summary"}


def tag(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def q(name: str) -> str:
    """TMDL object name: quoted unless a plain identifier."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


# --- measures from MEASURES.md ----------------------------------------------------------

def parse_measures(md: Path = MEASURES_MD) -> tuple[list[dict], list[dict]]:
    """Every ```dax block "Name = expression"; the folder is the section's display folder.
    Blocks in the "calculated columns" subsection are columns of agg_school_year."""
    measures, columns, folder, in_columns = [], [], None, False
    lines = md.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if m := re.match(r"## .*\(display folder `(.+)`\)", line):
            folder, in_columns = m.group(1), False
        elif line.startswith("### "):
            in_columns = "calculated columns" in line
        elif line.strip() == "```dax":
            j = lines.index("```", i + 1)
            text = "\n".join(lines[i + 1:j])
            name, expr = re.match(r"(.+?) =\s(.*)", text, re.DOTALL).groups()  # "Name = x" or "Name =\n..."
            item = {"name": name.strip(), "expression": expr.strip(), "folder": folder}
            (columns if in_columns else measures).append(item)
            i = j
        i += 1
    return measures, columns


def expression_block(expr: str, indent: int) -> str:
    """A DAX or M expression as TMDL: one line stays on the same line, several go below, indented."""
    lines = [l.rstrip() for l in expr.strip().splitlines() if l.strip()]
    if len(lines) == 1:
        return " " + lines[0]
    return "\n" + "\n".join("\t" * indent + l for l in lines)


# --- semantic model (TMDL) ---------------------------------------------------------------

def csv_schema(csv_dir: Path) -> dict[str, list[tuple[str, str]]]:
    con = duckdb.connect()
    out = {}
    for t in LOADED_TABLES:
        path = csv_dir / f"{t}.csv"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found: run python scripts/export_powerbi.py first")
        out[t] = [(r[0], r[1]) for r in con.execute(
            f"describe select * from read_csv('{path.as_posix()}', header = true, sample_size = -1)").fetchall()]
    con.close()
    return out


def partition_m(table: str, cols: list[tuple[str, str]]) -> str:
    types = ", ".join(f'{{"{c}", {TYPES[t][0]}}}' for c, t in cols)
    return f"""let
    Source = Csv.Document(File.Contents(DataFolder & "{table}.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(Headers, {{{types}}}, "en-US")
in
    Typed"""


def table_tmdl(table: str, cols: list[tuple[str, str]], calc_columns: list[dict]) -> str:
    is_fact = table.startswith(("fact_", "agg_"))
    out = [f"table {q(table)}", f"\tlineageTag: {tag(table)}", ""]
    for c, t in cols:
        numeric = TYPES[t][1] in ("int64", "double")
        hidden = c.endswith("_key")
        out += [f"\tcolumn {q(c)}", f"\t\tdataType: {TYPES[t][1]}"]
        if hidden:
            out.append("\t\tisHidden")
        if numeric:
            out.append(f"\t\tformatString: {'0' if hidden or not is_fact else '#,0'}")
        out += [f"\t\tlineageTag: {tag(table, c)}",
                f"\t\tsummarizeBy: {'sum' if numeric and is_fact and not hidden else 'none'}",
                f"\t\tsourceColumn: {c}"]
        if (table, c) in SORT_BY:
            out.append(f"\t\tsortByColumn: {SORT_BY[(table, c)]}")
        out += ["", "\t\tannotation SummarizationSetBy = Automatic", ""]
    for cc in calc_columns:
        out += [f"\tcolumn {q(cc['name'])} ={expression_block(cc['expression'], 3)}",
                "\t\tformatString: 0", f"\t\tlineageTag: {tag(table, cc['name'])}", "\t\tsummarizeBy: sum", "",
                "\t\tannotation SummarizationSetBy = Automatic", ""]
    out += [f"\tpartition {q(table)} = m", "\t\tmode: import",
            f"\t\tsource ={expression_block(partition_m(table, cols), 3)}", "",
            "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(out)


def measures_tmdl(measures: list[dict]) -> str:
    out = [f"table {q(MEASURE_TABLE)}", f"\tlineageTag: {tag(MEASURE_TABLE)}", ""]
    for m in measures:
        out.append(f"\tmeasure {q(m['name'])} ={expression_block(m['expression'], 3)}")
        if m["name"] not in TEXT_MEASURES:
            out.append(f"\t\tformatString: {FORMATS.get(m['name'], PCT1 if '%' in m['name'] or 'Share' in m['name'] else COUNT)}")
        out += [f"\t\tdisplayFolder: {m['folder']}", f"\t\tlineageTag: {tag(MEASURE_TABLE, m['name'])}", ""]
    # a measure table still needs one column and a partition: a hidden, empty placeholder
    out += ["\tcolumn Placeholder", "\t\tdataType: string", "\t\tisHidden", f"\t\tlineageTag: {tag(MEASURE_TABLE, 'col')}",
            "\t\tsummarizeBy: none", "\t\tsourceColumn: Placeholder", "",
            f"\tpartition {q(MEASURE_TABLE)} = m", "\t\tmode: import",
            "\t\tsource = #table(type table [Placeholder = text], {})", "",
            "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(out)


def write_model(root: Path, schema: dict, measures: list[dict], calc_columns: list[dict], data_folder: Path):
    d = root / f"{NAME}.SemanticModel"
    defn = d / "definition"
    (defn / "tables").mkdir(parents=True)
    write_json(d / "definition.pbism", {
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "settings": {}})
    (defn / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1600\n\n", encoding="utf-8")
    order = ["DataFolder", *LOADED_TABLES, MEASURE_TABLE]
    model = ["model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tsourceQueryCulture: en-US", "\tdataAccessOptions", "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull",
             "", "\tannotation __PBI_TimeIntelligenceEnabled = 0", "",
             f"\tannotation PBI_QueryOrder = {json.dumps(order)}", ""]
    model += [f"ref table {q(t)}" for t in [MEASURE_TABLE, *LOADED_TABLES]]
    (defn / "model.tmdl").write_text("\n".join(model) + "\n", encoding="utf-8")
    folder = str(data_folder).rstrip("\\/") + "\\"
    (defn / "expressions.tmdl").write_text(
        f'expression DataFolder = "{folder}" meta [IsParameterQuery = true, Type = "Text", IsParameterQueryRequired = true]\n'
        f"\tlineageTag: {tag('DataFolder')}\n\n\tannotation PBI_ResultType = Text\n", encoding="utf-8")
    rels = []
    for many, one in RELATIONSHIPS:
        rels += [f"relationship {tag('rel', many, one)}", f"\tfromColumn: {many}", f"\ttoColumn: {one}", ""]
    (defn / "relationships.tmdl").write_text("\n".join(rels), encoding="utf-8")
    for t, cols in schema.items():
        calc = calc_columns if t == "agg_school_year" else []
        (defn / "tables" / f"{t}.tmdl").write_text(table_tmdl(t, cols, calc), encoding="utf-8")
    (defn / "tables" / f"{MEASURE_TABLE}.tmdl").write_text(measures_tmdl(measures), encoding="utf-8")


# --- report (PBIR) -----------------------------------------------------------------------

def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def field(ref: str) -> dict:
    """'Table[Column]' -> column reference; a bare name -> measure in _Measures."""
    if m := re.fullmatch(r"(\w+)\[(.+)\]", ref):
        return {"Column": {"Expression": {"SourceRef": {"Entity": m.group(1)}}, "Property": m.group(2)}}
    return {"Measure": {"Expression": {"SourceRef": {"Entity": MEASURE_TABLE}}, "Property": ref}}


def projection(ref: str) -> dict:
    f = field(ref)
    kind = next(iter(f))
    entity, prop = f[kind]["Expression"]["SourceRef"]["Entity"], f[kind]["Property"]
    return {"field": f, "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop}


def literal(value) -> dict:
    v = ("true" if value else "false") if isinstance(value, bool) else (
        f"{value}D" if isinstance(value, (int, float)) else "'" + str(value).replace("'", "''") + "'")
    return {"expr": {"Literal": {"Value": v}}}


def title_obj(text: str) -> dict:
    return {"title": [{"properties": {"show": literal(True), "text": literal(text)}}]}


def card_objects(size: int) -> dict:
    """Full numbers (display units: none) at a font size that fits the card."""
    return {"labels": [{"properties": {"labelDisplayUnits": literal(1), "fontSize": literal(size)}}],
            "categoryLabels": [{"properties": {"show": literal(True), "fontSize": literal(10)}}]}


def gradient(measure: str, high_is_bad: bool = True) -> dict:
    """Background colour of a table column: diverging scale centred on 0 (the state value)."""
    bad, good = "'#E06666'", "'#6FA8DC'"
    return {"properties": {"backColor": {"solid": {"color": {"expr": {"FillRule": {
        "Input": field(measure),
        "FillRule": {"linearGradient3": {
            "min": {"color": {"Literal": {"Value": good if high_is_bad else bad}}},
            "mid": {"color": {"Literal": {"Value": "'#FFFFFF'"}}, "value": {"Literal": {"Value": "0D"}}},
            "max": {"color": {"Literal": {"Value": bad if high_is_bad else good}}},
            "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}}}}}}}}}},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"{MEASURE_TABLE}.{measure}"}}


def font_colour(measure: str, colour_measure: str) -> dict:
    """Font colour of a table column taken from a measure that returns a hex colour."""
    return {"properties": {"fontColor": {"solid": {"color": {"expr": field(colour_measure)}}}},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"{MEASURE_TABLE}.{measure}"}}


class Page:
    def __init__(self, name: str, display: str):
        self.name, self.display, self.visuals = name, display, []

    def add(self, vid: str, vtype: str, x, y, w, h, roles: dict | None = None, *, objects=None,
            container=None, sort=None):
        visual = {"visualType": vtype}
        if roles:
            visual["query"] = {"queryState": {role: {"projections": [projection(r) for r in refs]}
                                              for role, refs in roles.items()}}
            if sort:
                ref, direction = sort
                visual["query"]["sortDefinition"] = {"sort": [{"field": field(ref), "direction": direction}],
                                                     "isDefaultSort": True}
        if objects:
            visual["objects"] = objects
        if container:
            visual["visualContainerObjects"] = container
        visual["drillFilterOtherVisuals"] = True
        self.visuals.append({"$schema": f"{SCHEMA}/item/report/definition/visualContainer/1.0.0/schema.json",
                             "name": vid, "position": {"x": x, "y": y, "z": len(self.visuals) * 1000,
                                                       "height": h, "width": w, "tabOrder": len(self.visuals) * 1000},
                             "visual": visual})

    def text(self, vid, x, y, w, h, text, size=10, bold=False):
        style = {"fontSize": f"{size}pt", **({"fontWeight": "bold"} if bold else {})}
        self.add(vid, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": [
            {"textRuns": [{"value": text, "textStyle": style}]}]}}]},
                 container={"visualHeader": [{"properties": {"show": literal(False)}}]})

    def year_slicer(self, vid, column, default=None):
        objects = {"data": [{"properties": {"mode": literal("Dropdown")}}],
                   "selection": [{"properties": {"singleSelect": literal(True), "strictSingleSelect": literal(True)}}]}
        if default:
            entity, prop = re.fullmatch(r"(\w+)\[(.+)\]", column).groups()
            objects["general"] = [{"properties": {"filter": {"filter": {
                "Version": 2, "From": [{"Name": "y", "Entity": entity, "Type": 0}],
                "Where": [{"Condition": {"In": {
                    "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "y"}}, "Property": prop}}],
                    "Values": [[{"Literal": {"Value": f"'{default}'"}}]]}}}]}}}}]
        self.add(vid, "slicer", 1064, 8, 200, 56, {"Values": [column]}, objects=objects)


def pages(latest_year: str) -> list[Page]:
    p1 = Page("state_overview", "State overview")
    p1.text("title", 16, 8, 900, 40, "Rajasthan school education: state overview", 18, True)
    p1.year_slicer("year", "dim_year[academic_year]", latest_year)
    cards = ["Schools", "Enrolment (incl. pre-primary)", "Teachers", "PTR", "Single-Teacher Schools %",
             "Teacher Shortfall", "Classroom Shortfall"]
    for i, m in enumerate(cards):
        p1.add(f"card_{i}", "card", 16 + i * 180, 64, 172, 96, {"Values": [m]}, objects=card_objects(20))
    yoy = ["Enrolment 1-12 YoY %", "Teachers YoY %", "PTR YoY Change", "Single-Teacher Schools % YoY Change",
           "Teacher Shortfall YoY Change"]
    for i, m in enumerate(yoy):
        p1.add(f"yoy_{i}", "card", 16 + i * 252, 164, 244, 84, {"Values": [m]}, objects=card_objects(18))
    p1.add("enrolment_grade", "clusteredBarChart", 16, 256, 410, 412,
           {"Category": ["dim_grade[grade_label]"], "Y": ["Enrolment (incl. pre-primary)"], "Series": ["dim_gender[gender_label]"]},
           container=title_obj("Enrolment by grade and gender"), sort=("dim_grade[grade_label]", "Ascending"))
    p1.add("management", "clusteredBarChart", 434, 256, 410, 412,
           {"Category": ["dim_management[management_group]"], "Y": ["Schools"]},
           container=title_obj("Schools by management"), sort=("Schools", "Descending"))
    p1.add("facilities", "clusteredBarChart", 852, 256, 412, 196,
           {"Y": ["% Schools with Electricity", "% Schools with Safe Water", "% Schools with Girls' Toilet"]},
           container=title_obj("Schools with basic facilities"))
    p1.add("gaps", "clusteredBarChart", 852, 460, 412, 208,
           {"Y": ["Teacher Shortfall", "Teacher Surplus", "Classroom Shortfall"]},
           container=title_obj("Teachers missing vs above the norm (school by school)"))
    p1.text("source", 16, 676, 1248, 36, SOURCE_NOTE, 8)

    p2 = Page("district_comparison", "District comparison")
    p2.text("title", 16, 8, 900, 40, "District comparison: how far each district is from the state", 18, True)
    p2.year_slicer("year", "dim_year[academic_year]", latest_year)
    p2.add("districts", "tableEx", 16, 72, 760, 596,
           {"Values": ["dim_district[district_name]", "Enrolment 1-12", "PTR", "PTR vs State",
                       "Single-Teacher Schools %", "Single-Teacher Schools % vs State", "Infrastructure Index",
                       "Infrastructure Index vs State", "Teacher Shortfall", "Teacher Shortfall %",
                       "Teacher Shortfall % vs State", "Classroom Shortfall"]},
           sort=("PTR", "Descending"),
           objects={"values": [gradient("PTR vs State"), gradient("Single-Teacher Schools % vs State"),
                               gradient("Infrastructure Index vs State", high_is_bad=False),
                               gradient("Teacher Shortfall % vs State")]})
    p2.add("ptr_vs_state", "lineClusteredColumnComboChart", 784, 72, 480, 296,
           {"Category": ["dim_district[district_name]"], "Y": ["PTR"], "Y2": ["PTR (State)"]},
           container=title_obj("Pupils per teacher, with the state value"), sort=("PTR", "Descending"))
    p2.add("shortfall", "clusteredBarChart", 784, 376, 480, 292,
           {"Category": ["dim_district[district_name]"], "Y": ["Teacher Shortfall", "Teacher Surplus"]},
           container=title_obj("Teacher shortfall and surplus by district"), sort=("Teacher Shortfall", "Descending"))
    p2.text("source", 16, 676, 1248, 36,
            "District = the district a school belonged to in that year: in 2025-26, 180 schools moved from Alwar to "
            "Kotputli-Behror. 'vs State' = district minus the pooled state value. " + C.SOURCE_NOTE + ".", 8)

    p3 = Page("validation", "Validation")
    p3.text("title", 16, 8, 900, 40, "Validation: pipeline totals vs official UDISE+ figures", 18, True)
    p3.add("summary", "card", 16, 64, 700, 88, {"Values": ["Validation Summary"]}, objects=card_objects(20))
    p3.year_slicer("year", "official_totals[year]")
    p3.add("validation", "tableEx", 16, 160, 1248, 420,
           {"Values": ["official_totals[year]", "official_totals[indicator_label]", "Official Value", "Pipeline Value",
                       "Difference", "Difference %", "Match Status"]},
           objects={"values": [font_colour("Match Status", "Match Colour")],
                    "total": [{"properties": {"totals": literal(False)}}]})  # a total of different indicators means nothing
    p3.text("notes", 16, 588, 1248, 120,
            "Official figures: UDISE+ 2024-25 report (Table 2.2) and Lok Sabha / Rajya Sabha answers for government "
            "schools. Enrolment includes pre-primary, as published. Government = management codes grouped as "
            "government in reference/management_codes.json. Pipeline Value is computed by the DAX measures from the "
            "star schema facts. The same comparison, with a second independent calculation from the CSVs, is in "
            "powerbi/reconciliation.xlsx. " + C.SOURCE_NOTE + ".", 9)
    return [p1, p2, p3]


def write_report(root: Path, latest_year: str, active_page: str | None = None):
    d = root / f"{NAME}.Report"
    write_json(d / "definition.pbir", {
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    defn = d / "definition"
    write_json(defn / "version.json", {"$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
                                       "version": "2.0.0"})
    write_json(defn / "report.json", {
        "$schema": f"{SCHEMA}/item/report/definition/report/1.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": "5.61",
                                          "type": "SharedResources"}},
        "layoutOptimization": "None",
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True}})
    ps = pages(latest_year)
    write_json(defn / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.name for p in ps], "activePageName": active_page or ps[0].name})
    for p in ps:
        write_json(defn / "pages" / p.name / "page.json", {
            "$schema": f"{SCHEMA}/item/report/definition/page/1.0.0/schema.json",
            "name": p.name, "displayName": p.display, "displayOption": "FitToPage", "height": 720, "width": 1280})
        for v in p.visuals:
            write_json(defn / "pages" / p.name / "visuals" / v["name"] / "visual.json", v)


def build(out: Path = PBI_DIR, csv_dir: Path = CSV_DIR, data_folder: Path | None = None,
          active_page: str | None = None) -> dict:
    schema = csv_schema(csv_dir)
    measures, calc_columns = parse_measures()
    con = duckdb.connect()
    latest = con.execute(f"select max(academic_year) from read_csv('{(csv_dir / 'dim_year.csv').as_posix()}')").fetchone()[0]
    con.close()
    for sub in (f"{NAME}.SemanticModel", f"{NAME}.Report"):
        shutil.rmtree(out / sub, ignore_errors=True)   # generated: rebuilt in full (keeps .pbi/ caches out)
    write_model(out, schema, measures, calc_columns, (data_folder or csv_dir).resolve())
    write_report(out, latest, active_page)
    write_json(out / f"{NAME}.pbip", {
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json", "version": "1.0",
        "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})
    return {"tables": len(schema) + 1, "measures": len(measures), "calculated_columns": len(calc_columns),
            "relationships": len(RELATIONSHIPS), "pages": 3}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-folder", type=Path, default=None,
                   help="folder Power BI reads the CSVs from (default: powerbi/data of this checkout)")
    p.add_argument("--active-page", default=None, help="page shown on opening (default: State overview)")
    a = p.parse_args()
    print(build(data_folder=a.data_folder, active_page=a.active_page))


if __name__ == "__main__":
    main()
