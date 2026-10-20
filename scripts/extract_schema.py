"""Turn the UDISE+ schema PDFs into reference/schema_columns.json.

Run once (or when a new schema PDF is added):
    python scripts/extract_schema.py

For every dataset and year it records each column's name, description, type
and remarks, and parses the documented codes out of the remarks:
    "Yes=1, No=2"                         -> kind yes_no, codes {1: Yes, 2: No}
    "1:Yes,2:No, 3:Yes, but not functional" -> kind enum,  codes {1, 2, 3}
    "As per UDISE DCF"                    -> kind dcf (codes exist but are not in the schema)
It also extracts the documented (item_group, item_id) pairs of Enrolment_1.
The PDFs are parsed with pdfplumber's table extractor; the output JSON is
committed so the pipeline itself never needs to read a PDF.
"""
import json
import re
import sys
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {"2024-25": ROOT / "docs" / "DSP_Schema_2024-25.pdf",
           "2025-26": ROOT / "docs" / "DSP_Schema_2025-26.pdf"}
OUT = ROOT / "reference" / "schema_columns.json"

# Section title in the PDF -> dataset key used by the pipeline (pipeline/config.py DATASETS).
SECTIONS = {
    "school profile 1": "profile1",
    "school profile 2": "profile2",
    "school facility": "facility",
    "school enrolment_1": "enrolment1",
    "age wise enrolment_2": "enrolment2",
    "teachers profile": "teacher",
    "school safety": "safety",
}
# Column names that differ between the PDF and the data files. The PDF name is
# kept in "column_in_pdf" so the difference stays visible.
COLUMN_FIXES = {
    # PDF says "teachers_..." and cuts the name at 39 characters; the data says "teacher_..."
    "teachers_involve_non_training_assignmen": "teacher_involve_non_training_assignment",
}
NUMERIC_TYPES = {"smallint", "integer", "bigint", "double precision", "numeric", "real"}


def clean(cell) -> str:
    return re.sub(r"\s+", " ", (cell or "")).strip()


def section_titles(page) -> list[tuple[float, str]]:
    """(vertical position, dataset) of every section title on the page, top to bottom.

    Titles are located by position because pdfplumber sometimes returns them as a
    table row and sometimes leaves them out of the table cells.
    """
    found = [(m["top"], ds) for title, ds in SECTIONS.items()
             for m in page.search(title, regex=False, case=False)]
    return sorted(found)


def parse_codes(remarks: str, column: str) -> tuple[str, dict[str, str] | None]:
    r = remarks.strip()
    if re.fullmatch(r"yes\s*=\s*1\s*,\s*no\s*=\s*2", r, re.I):
        return "yes_no", {"1": "Yes", "2": "No"}
    pairs = re.findall(r"(\d+)\s*[-:]\s*(.+?)(?=,\s*\d+\s*[-:]|$)", r)
    if len(pairs) >= 2:
        return "enum", {k: v.strip(" ,") for k, v in pairs}
    if "udise dcf" in r.lower():
        return "dcf", None
    if not r and column.endswith("_yn"):
        return "yes_no", {"1": "Yes", "2": "No"}  # by naming convention, see codes_source
    return "", None


def extract(year: str, pdf_path: Path) -> tuple[dict, list]:
    tables: dict[str, list[dict]] = {}
    current, last_no = None, 0
    with pdfplumber.open(pdf_path) as pdf:
        text = " ".join(p.extract_text() or "" for p in pdf.pages)
        for page in pdf.pages:
            titles = section_titles(page)
            for found in page.find_tables():
                for row_box, row in zip(found.rows, found.extract()):
                    while titles and titles[0][0] < row_box.bbox[3]:  # title above this row's bottom
                        current, last_no = titles.pop(0)[1], 0
                    cells = [clean(c) for c in row]
                    if not (cells[0].isdigit() and cells[1]):
                        continue  # header rows and wrapped-text continuation rows
                    no = int(cells[0])
                    if current is None or no <= last_no:
                        sys.exit(f"{pdf_path.name}: column {cells[1]} has no section title "
                                 f"(numbering restarted at {no}); update SECTIONS")
                    last_no = no
                    pdf_name = cells[1]
                    name = COLUMN_FIXES.get(pdf_name.lower(), pdf_name.lower())  # data headers are lower case
                    kind, codes = parse_codes(cells[5], name)
                    dtype = cells[3].lower()
                    tables.setdefault(current, []).append({
                        "column": name,
                        **({"column_in_pdf": pdf_name} if pdf_name != name else {}),
                        "description": cells[2],
                        "type": dtype,
                        "numeric": dtype in NUMERIC_TYPES,
                        "remarks": cells[5],
                        "kind": kind or ("numeric" if dtype in NUMERIC_TYPES else "text"),
                        "codes": codes,
                        **({"codes_source": "column name ends in _yn; no remark in schema"}
                           if kind == "yes_no" and not cells[5] else {}),
                    })
    flat = re.sub(r"\s+", " ", text)
    items = re.findall(r"item\s*_?\s*group\s*=\s*(\d+)\s*,\s*item\s*_?\s*id\s*=\s*(\d+)\s*then\s*"
                       r"(.+?)(?=\s*item\s*_?\s*group\s*=|\s+item_group\b|\s+smallint|\s+\d+\s+item_id|$)", flat, re.I)
    enrol_items = [{"item_group": int(g), "item_id": int(i), "label": lbl.strip(" .")}
                   for g, i, lbl in items]
    return tables, enrol_items


def main():
    out = {"source": "UDISE+ Data Sharing Portal schema PDFs (docs/DSP_Schema_<year>.pdf), "
                     "Ministry of Education, Government of India",
           "generated_by": "scripts/extract_schema.py",
           "datasets": {}, "enrolment1_items": {}}
    for year, path in SCHEMAS.items():
        tables, items = extract(year, path)
        for ds, cols in tables.items():
            out["datasets"].setdefault(ds, {})[year] = cols
        out["enrolment1_items"][year] = items
        print(f"{year}: " + ", ".join(f"{ds} {len(c)} cols" for ds, c in tables.items())
              + f"; {len(items)} enrolment item codes")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Written {OUT}")


if __name__ == "__main__":
    main()
