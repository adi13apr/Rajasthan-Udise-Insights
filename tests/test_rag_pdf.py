"""PDF extraction on synthetic PDFs built with reportlab: page numbers, headings, tables that
run over a page break, repeated header rows, wrapped rows and empty pages."""
import re

import pytest

pytest.importorskip("reportlab")

from rag import config as C  # noqa: E402
from rag.chunking import Chunker  # noqa: E402
from rag.extract import (Heading, Paragraph, Table, TableRow, _continues, _merge_into,  # noqa: E402
                         extract_pdf)
from rag.tokens import estimate_tokens  # noqa: E402
from rag_helpers import make_pdf, sentences  # noqa: E402

ROW = re.compile(r"\| District (\d{3}) \| (\d+) \| (\d+) \|")


@pytest.fixture(scope="module", params=[False, True], ids=["header-once", "header-repeated"])
def pdf(request, tmp_path_factory):
    path = tmp_path_factory.mktemp("pdf") / "report.pdf"
    make_pdf(path, table_rows=90, repeat_header=request.param)
    return path


def test_pages_headings_and_empty_pages(pdf):
    doc = extract_pdf(pdf)
    assert doc.pages == 5
    assert doc.empty_pages == [4]                        # the blank page, reported, no chunk
    headings = [(e.page, e.text) for e in doc.elements if isinstance(e, Heading)]
    assert headings == [(1, "Synthetic School Report"), (1, "1. Introduction"),
                        (1, "2. District table"), (5, "3. Annex")]
    assert [e.page for e in doc.elements if isinstance(e, Paragraph)] == [1, 5]


def test_a_table_over_a_page_break_is_one_table_with_its_header(pdf):
    tables = [e for e in extract_pdf(pdf).elements if isinstance(e, Table)]
    assert len(tables) == 1
    (t,) = tables
    assert t.header == ["District", "Schools", "Teachers"]
    assert len(t.rows) == 90                             # repeated headers are not data rows
    assert {r.page for r in t.rows} == {1, 2, 3}


def test_table_chunks_hold_whole_rows_header_first_and_right_pages(pdf):
    chunks = Chunker(estimate_tokens, target=150, max_tokens=200, overlap=20).chunk(
        extract_pdf(pdf), "report.pdf")
    tables = [c for c in chunks if c.chunk_type == "table"]
    assert len(tables) >= 3
    found = []
    for c in tables:
        lines = c.text.split("\n\n", 1)[1].splitlines()
        assert lines[0] == "| District | Schools | Teachers |"
        for ln in lines[2:]:
            m = ROW.fullmatch(ln)
            assert m, f"row cut or mangled: {ln!r}"
            k = int(m.group(1))
            assert (m.group(2), m.group(3)) == (str(100 + k), str(1000 + k))
            found.append(k)
        assert c.section == "2. District table"
        assert 1 <= c.page <= c.page_end <= 3
        assert c.location == (f"p. {c.page}" if c.page == c.page_end else f"pp. {c.page}-{c.page_end}")
    assert found == list(range(1, 91))
    text = [c for c in chunks if c.chunk_type == "text"]
    assert not any("District 0" in c.text for c in text)  # table cells are not repeated as text
    assert {c.page for c in text} == {1, 5}
    assert all(c.n_tokens <= 200 for c in chunks)


def test_real_size_chunking_of_the_pdf(pdf):
    chunks = Chunker(estimate_tokens).chunk(extract_pdf(pdf), "report.pdf")
    assert all(c.n_tokens <= C.MAX_TOKENS for c in chunks)
    assert sum(c.chunk_type == "table" for c in chunks) >= 1


def test_wrapped_row_is_joined_without_repeating_text():
    row = TableRow(["39", "cwsn_sch_type", "Type of Special", "smallint"], 1)
    _merge_into(row, ["", "cwsn_sch_type", "School", ""])    # echo + real wrapped text
    assert row.cells == ["39", "cwsn_sch_type", "Type of Special School", "smallint"]


def test_only_real_continuations_are_joined():
    row = TableRow(["1", "pseudocode", "Code of the school", "bigint"], 6)
    assert _continues(row, ["", "pseudocode", "", ""])          # echo of the name
    assert _continues(row, ["", "pseudoc", "", ""])             # cut-off echo
    assert _continues(row, ["", "", "(unique id)", ""])         # wrapped description
    # Blank S.No. but a new column name: its own row (2024-25 schema, Item_group).
    assert not _continues(row, ["", "Item_group", "Item Group =1 ...", ""])
    assert not _continues(row, ["2", "", "text", ""])


def _front_matter_pdf(path):
    """Report front matter: a narrative page set in bold (the body font is bold), a contents
    page with dot leaders, a number-only bold line and five bold headings in a row."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import PageBreak
    from reportlab.platypus import Paragraph as P
    from reportlab.platypus import SimpleDocTemplate

    styles = getSampleStyleSheet()
    bold = ParagraphStyle("BoldBody", parent=styles["BodyText"], fontName="Helvetica-Bold")
    narrative = " ".join(f"Item {k} explains that enrolment in district {k} rose in the year "
                         f"and that the trend continued" for k in range(1, 9))
    story = [P("1. Key results", styles["Heading2"]), P(narrative, bold), PageBreak(),
             P("Contents", styles["Heading2"])]
    story += [P(f"Section {k}: Highlights {'.' * 40} {20 + k}", bold) for k in range(1, 4)]
    story += [P("79.1 - 89.0", bold), PageBreak()]
    story += [P(f"Part {x}", bold) for x in "ABCDE"]
    story += [P(sentences("P", 3), styles["BodyText"])]
    SimpleDocTemplate(str(path), pagesize=A4, invariant=1).build(story)


def test_bold_body_text_contents_lines_and_numbers_are_not_headings(tmp_path):
    path = tmp_path / "front.pdf"
    _front_matter_pdf(path)
    doc = extract_pdf(path)
    headings = [(e.page, e.text) for e in doc.elements if isinstance(e, Heading)]
    assert headings == [(1, "1. Key results"), (2, "Contents"),
                        (3, "Part A"), (3, "Part B"), (3, "Part C"), (3, "Part D"), (3, "Part E")]
    page1 = " ".join(" ".join(e.text.split()) for e in doc.elements
                      if isinstance(e, Paragraph) and e.page == 1)
    assert "Item 1 explains" in page1 and "Item 8 explains" in page1   # all of it is body text
    page2 = " ".join(" ".join(e.text.split()) for e in doc.elements
                      if isinstance(e, Paragraph) and e.page == 2)
    assert "Section 3: Highlights" in page2 and "79.1 - 89.0" in page2


def test_stacked_pdf_headings_keep_the_last_three(tmp_path):
    path = tmp_path / "front.pdf"
    _front_matter_pdf(path)
    chunks = Chunker(estimate_tokens).chunk(extract_pdf(path), "front.pdf")
    (last,) = [c for c in chunks if c.page == 3]
    assert last.section == "Part C > Part D > Part E"
    assert all(len(c.section.split(" > ")) <= 3 for c in chunks)
