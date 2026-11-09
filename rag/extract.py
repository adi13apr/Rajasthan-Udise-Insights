"""Read a document into a simple list of elements: headings, paragraphs and tables.

Both PDFs and Markdown files become the same element types, so the chunker does not care
where the text came from.

PDF (pdfplumber):
  * Text is read page by page and every element keeps its page number.
  * Tables are found with pdfplumber's table finder. Their area is cut out of the page text,
    so table cells never also appear as loose text.
  * A table row whose text is all bold is a header row; a bold row with a single value
    ("School Profile 1") is a section title. A table that starts at the top of a page with no
    header of its own continues the table above it (same number of columns, nothing in
    between), so it keeps that table's header. A repeated header row is not repeated as data.
  * A row with an empty first cell is the rest of the row above it (a cell that wrapped onto
    the next row) when its second cell is also empty or repeats the row above; it is joined
    to that row, so a row is never cut in two. A row with an empty first cell but a new value
    in the second cell is a row of its own (the 2024-25 schema has no S.No. for Item_group).
  * Lines that are only a page number are dropped. Pages with no text and no table cells
    are reported as empty (no OCR in this phase).
  * A text line is a heading if it is short and all bold, or clearly bigger than body text.
    Not a heading: a line that starts with a lower-case letter (the wrapped rest of a
    sentence), a contents line with dot leaders ("Section 1 ........ 27"), or a line that is
    mostly numbers (chart labels, map legends); a bold one-value table row with no letters
    is dropped (a chart label read as a table). On a page whose text is mostly bold (the
    narrative "Key Results" pages of the UDISE+ reports are set in bold), bold says nothing,
    so there only bigger text makes a heading.

Markdown: '#' headings, pipe tables, fenced code blocks and paragraphs (each list item is its
own paragraph). There are no pages: the section heading takes the place of the page number.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Heading:
    text: str
    level: int        # Markdown: 1-6; PDF: 0 (flat, see chunking.section_path)
    page: int         # 0 for Markdown


@dataclass
class Paragraph:
    text: str
    page: int
    offset: int       # character offset: in the page text (PDF) or in the whole file (Markdown)
    code: bool = False


@dataclass
class TableRow:
    cells: list[str]
    page: int


@dataclass
class Table:
    index: int                       # 0, 1, 2 ... in document order
    header: list[str] | None
    rows: list[TableRow] = field(default_factory=list)
    page: int = 0                    # page of the header row (0 for Markdown)


@dataclass
class Document:
    kind: str                        # pdf | markdown
    elements: list                   # Heading | Paragraph | Table, in reading order
    pages: int = 0                   # PDF pages (0 for Markdown)
    empty_pages: list[int] = field(default_factory=list)


def read_document(path: Path, kind: str) -> Document:
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "markdown":
        return parse_markdown(Path(path).read_text(encoding="utf-8"))
    raise ValueError(f"unsupported document kind: {kind}")


# --- PDF -------------------------------------------------------------------------------

PAGE_NUMBER = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
BOLD_SHARE = 0.9        # a row or line is bold if at least 90% of its letters are bold
HEADING_SIZE = 1.2      # ... or a heading if its letters are 20% bigger than body text
HEADING_MAX_CHARS = 120
BOLD_BODY_SHARE = 0.6   # a page whose text-line letters are >= 60% bold has bold body text ...
BOLD_BODY_MIN_LETTERS = 300   # ... if it has enough text to tell (a title page does not)
DOT_LEADER = re.compile(r"(\.\s*){4,}|…{2,}")   # contents lines: "Section 1 ........ 27"


def _clean(cell) -> str:
    return " ".join(str(cell).split()) if cell is not None else ""


def _center_in(obj, bbox) -> bool:
    x0, top, x1, bottom = bbox
    return (x0 <= (obj["x0"] + obj["x1"]) / 2 <= x1
            and top <= (obj["top"] + obj["bottom"]) / 2 <= bottom)


def _is_bold(chars) -> bool:
    chars = [c for c in chars if c["text"].strip()]
    return bool(chars) and sum("bold" in c["fontname"].lower() for c in chars) >= BOLD_SHARE * len(chars)


def _body_size(pdf) -> float:
    sizes = Counter(round(c["size"], 1) for p in pdf.pages for c in p.chars if c["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _mostly_words(text: str) -> bool:
    """At least half of the visible characters are letters ("Map 2.1" yes, "79.1 - 89.0" no)."""
    visible = [ch for ch in text if not ch.isspace()]
    return bool(visible) and sum(ch.isalpha() for ch in visible) >= 0.5 * len(visible)


def _is_heading(line, body_size: float, bold_body: bool = False) -> bool:
    text = line["text"].strip()
    if not text or len(text) > HEADING_MAX_CHARS or not _mostly_words(text):
        return False
    if text.endswith((".", ",", ";")) or text[0].islower() or DOT_LEADER.search(text):
        return False
    chars = [c for c in line["chars"] if c["text"].strip()]
    size = sorted(c["size"] for c in chars)[len(chars) // 2] if chars else 0
    bigger = body_size > 0 and size >= HEADING_SIZE * body_size
    return bigger or (_is_bold(chars) and not bold_body)


def _bold_body(lines) -> bool:
    """True if most letters of the page's text lines are bold (bold is then the body font)."""
    letters = [c for line in lines for c in line["chars"] if c["text"].isalpha()]
    if len(letters) < BOLD_BODY_MIN_LETTERS:
        return False
    return sum("bold" in c["fontname"].lower() for c in letters) >= BOLD_BODY_SHARE * len(letters)


def _continues(row: TableRow, cells: list[str]) -> bool:
    """True if `cells` is the wrapped rest of `row`: first cell empty and the second cell
    (usually the name or key column) empty or already in the row above."""
    if cells[0]:
        return False
    return len(cells) < 2 or len(row.cells) < 2 or not cells[1] or cells[1] in row.cells[1]


def _merge_into(row: TableRow, cells: list[str]) -> None:
    """Join a wrapped continuation row into the row above (skip text the row already has)."""
    for i, value in enumerate(cells[:len(row.cells)]):
        if value and value not in row.cells[i]:
            row.cells[i] = f"{row.cells[i]} {value}".strip()


def extract_pdf(path: Path) -> Document:
    import pdfplumber  # imported here so Markdown-only use does not need it

    elements, empty_pages = [], []
    table: Table | None = None     # the table that the next rows may continue
    n_tables = 0
    with pdfplumber.open(path) as pdf:
        body = _body_size(pdf)
        for pno, page in enumerate(pdf.pages, start=1):
            found = page.find_tables()
            boxes = [t.bbox for t in found]
            chars = [c for c in page.chars if c["text"].strip()]
            lines = [line for line in page.extract_text_lines(return_chars=True)
                     if not any(_center_in(line, b) for b in boxes)
                     and not PAGE_NUMBER.match(line["text"])]
            bold_body = _bold_body(lines)
            items = [(line["top"], 0, line) for line in lines]
            items += [(t.bbox[1], 1, t) for t in found]
            items.sort(key=lambda item: (item[0], item[1]))

            offset, has_content, para = 0, False, []   # para: [(offset, text)] of body lines

            def flush_para():
                if para:
                    elements.append(Paragraph("\n".join(t for _, t in para), pno, para[0][0]))
                    para.clear()

            for _, what, obj in items:
                if what == 0:                          # a text line outside any table
                    text = obj["text"].strip()
                    has_content = True
                    table = None                       # text between tables ends a table
                    if _is_heading(obj, body, bold_body):
                        flush_para()
                        elements.append(Heading(text, 0, pno))
                    else:
                        para.append((offset, text))
                    offset += len(text) + 1            # page text = kept lines joined by "\n"
                    continue
                flush_para()
                first_row = True                       # first row of this table area
                for row, raw in zip(obj.rows, obj.extract()):
                    cells = [_clean(c) for c in raw]
                    values = {c for c in cells if c}
                    if not values:
                        continue
                    has_content = True
                    bold = _is_bold([c for c in chars if _center_in(c, row.bbox)])
                    if bold and len(values) == 1:      # a title row inside the table
                        title = values.pop()
                        if re.search(r"[A-Za-z]", title):   # "391" (a chart label) is dropped
                            elements.append(Heading(title, 0, pno))
                        table = None
                        continue
                    if table is not None and cells == table.header:
                        pass                           # header repeated on a new page
                    elif (table is None or len(cells) != len(table.header or cells)
                          or (bold and first_row)):
                        # A new table. Its first row is the header: bold, or (if the PDF has
                        # no bold header) simply the first row, the usual layout. A bold row
                        # further down (e.g. a "Total" row) stays a data row.
                        table = Table(n_tables, cells, page=pno)
                        n_tables += 1
                        elements.append(table)
                    elif table.rows and _continues(table.rows[-1], cells):
                        _merge_into(table.rows[-1], cells)
                    else:
                        table.rows.append(TableRow(cells, pno))
                    first_row = False
            flush_para()
            if not has_content:
                empty_pages.append(pno)
        n_pages = len(pdf.pages)
    return Document("pdf", elements, n_pages, empty_pages)


# --- Markdown --------------------------------------------------------------------------

MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
MD_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
MD_FENCE = re.compile(r"^\s*(```|~~~)")


def md_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def parse_markdown(text: str) -> Document:
    lines = text.splitlines(keepends=True)
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line)

    elements, para, n_tables = [], [], 0      # para: indexes of lines in the open paragraph

    def flush():
        if para:
            first, last = para[0], para[-1]
            body = text[starts[first]:starts[last] + len(lines[last].rstrip("\r\n"))]
            elements.append(Paragraph(body, 0, starts[first]))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r\n")
        if MD_FENCE.match(line):                      # fenced code: one block, never split
            flush()
            j = i + 1
            while j < len(lines) and not MD_FENCE.match(lines[j]):
                j += 1
            j = min(j, len(lines) - 1)
            body = text[starts[i]:starts[j] + len(lines[j].rstrip("\r\n"))]
            elements.append(Paragraph(body, 0, starts[i], code=True))
            i = j + 1
            continue
        heading = MD_HEADING.match(line)
        if heading:
            flush()
            elements.append(Heading(heading.group(2), len(heading.group(1)), 0))
        elif (line.lstrip().startswith("|") and i + 1 < len(lines)
              and MD_TABLE_SEP.match(lines[i + 1].rstrip("\r\n"))):
            flush()
            table = Table(n_tables, md_cells(line))
            n_tables += 1
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                table.rows.append(TableRow(md_cells(lines[i].rstrip("\r\n")), 0))
                i += 1
            elements.append(table)
            continue
        elif not line.strip():
            flush()
        elif MD_LIST_ITEM.match(line):
            flush()
            para.append(i)
        else:
            para.append(i)
        i += 1
    flush()
    return Document("markdown", elements)
