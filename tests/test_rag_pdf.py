"""PDF extraction on synthetic PDFs built with reportlab: page numbers, headings, tables that
run over a page break, repeated header rows, wrapped rows and empty pages."""
import re

import pytest

pytest.importorskip("reportlab")

from rag import config as C  # noqa: E402
from rag.chunking import Chunker  # noqa: E402
from rag.extract import Heading, Paragraph, Table, TableRow, _merge_into, extract_pdf  # noqa: E402
from rag.tokens import estimate_tokens  # noqa: E402
from rag_helpers import make_pdf  # noqa: E402

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
