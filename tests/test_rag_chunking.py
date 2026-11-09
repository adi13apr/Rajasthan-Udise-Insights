"""Chunking: overlap, size limits, headings, tables split only between rows, stable ids.

Markdown input only (no extra libraries needed); PDF extraction is in test_rag_pdf.py.
"""
import re

from rag import config as C
from rag.chunking import Chunker, chunk_id
from rag.extract import parse_markdown
from rag.tokens import estimate_tokens
from rag_helpers import markdown_doc

FILE = "docs/test_guide.md"


def chunks_of(text, **kw):
    return Chunker(estimate_tokens, **kw).chunk(parse_markdown(text), FILE)


def body(chunk):
    """Chunk text without its heading line."""
    return chunk.text.split("\n\n", 1)[1] if chunk.section else chunk.text


def common_overlap(a: str, b: str) -> str:
    """Longest end of a that is also the start of b."""
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return b[:k]
    return ""


def test_consecutive_text_chunks_overlap_by_about_15_percent():
    chunks = [c for c in chunks_of(markdown_doc(n_a=150)) if c.section.endswith("Section A")]
    assert len(chunks) >= 3
    for prev, nxt in zip(chunks, chunks[1:]):
        shared = common_overlap(body(prev), body(nxt))
        assert shared, "next chunk must start with the end of the previous one"
        share = estimate_tokens(shared) / prev.n_tokens
        assert 0.05 <= share <= 0.20, share
        assert estimate_tokens(shared) <= C.OVERLAP_TOKENS
        # the overlap is whole sentences here (they are short)
        assert shared.startswith("Fact ") and shared.endswith(".")


def test_chunks_respect_target_and_hard_max():
    chunks = chunks_of(markdown_doc(n_a=300))
    assert all(c.n_tokens <= C.MAX_TOKENS for c in chunks)
    text = [c for c in chunks if c.chunk_type == "text"]
    assert all(c.n_tokens <= C.TARGET_TOKENS for c in text)
    assert all(not c.oversized for c in chunks)


def test_a_very_long_sentence_is_cut_into_windows_under_the_target():
    long_sentence = " ".join(f"word{k}" for k in range(1200))   # no full stop at all
    chunks = chunks_of(f"# Doc\n\n## Long\n\n{long_sentence}\n")
    assert len(chunks) >= 3
    assert all(c.n_tokens <= C.TARGET_TOKENS for c in chunks)
    words = " ".join(body(c) for c in chunks).split()
    assert set(words) == {f"word{k}" for k in range(1200)}      # nothing lost


def test_chunks_never_cross_a_heading_and_carry_the_section_path():
    chunks = chunks_of(markdown_doc())
    for c in chunks:
        assert c.text.startswith(c.section)
        labels = set(re.findall(r"of section (\w)", c.text))
        assert len(labels) <= 1, f"chunk mixes sections: {labels}"
        if labels == {"A"}:
            assert c.section == "Test Guide > Section A"
        if labels == {"B"}:
            assert c.section == "Test Guide > Section B"
    assert {c.section for c in chunks} >= {"Test Guide", "Test Guide > Section A",
                                           "Test Guide > Section B",
                                           "Test Guide > Section B > Table B1"}


def test_markdown_uses_the_section_in_place_of_a_page_number():
    for c in chunks_of(markdown_doc()):
        assert c.page == 0 and c.page_end == 0
        assert c.location == c.section


def test_tables_are_separate_chunks_split_only_between_rows_with_header_repeated():
    rows = 200
    chunks = chunks_of(markdown_doc(table_rows=rows))
    tables = [c for c in chunks if c.chunk_type == "table"]
    assert len(tables) >= 2, "a 200-row table must be split"
    header = "| Code | Name | Value |\n|---|---|---|"
    seen = []
    for c in tables:
        assert "Fact " not in c.text                     # no running text inside table chunks
        assert body(c).startswith(header)                # header repeated in every part
        lines = body(c).splitlines()[2:]
        assert all(re.fullmatch(r"\| R\d{3} \| district \d+ \| \d+ \|", ln) for ln in lines)
        assert c.row_end - c.row_start + 1 == len(lines)
        assert c.n_tokens <= C.MAX_TOKENS
        seen += lines
    expected = [f"| R{k:03d} | district {k} | {k * 10} |" for k in range(1, rows + 1)]
    assert seen == expected                              # every row once, whole, in order


def test_a_row_longer_than_the_limit_stays_whole_and_is_flagged():
    big = " ".join(f"w{k}" for k in range(600))
    text = f"# Doc\n\n| A | B |\n|---|---|\n| 1 | small |\n| 2 | {big} |\n| 3 | small |\n"
    tables = [c for c in chunks_of(text) if c.chunk_type == "table"]
    oversized = [c for c in tables if c.oversized]
    assert len(oversized) == 1 and big in oversized[0].text
    assert sum(t.text.count("| 2 |") for t in tables) == 1


def test_ids_are_stable_unique_and_depend_on_file_page_offset():
    first, second = chunks_of(markdown_doc()), chunks_of(markdown_doc())
    assert [c.id for c in first] == [c.id for c in second]
    assert len({c.id for c in first}) == len(first)
    assert chunk_id("a.md", "S", 10) == chunk_id("a.md", "S", 10)
    assert len({chunk_id("a.md", "S", 10), chunk_id("b.md", "S", 10),
                chunk_id("a.md", "T", 10), chunk_id("a.md", "S", 11)}) == 4
    other = Chunker(estimate_tokens).chunk(parse_markdown(markdown_doc()), "other.md")
    assert not {c.id for c in other} & {c.id for c in first}


def test_editing_one_section_keeps_ids_of_sections_before_it():
    before = chunks_of(markdown_doc())
    after = chunks_of(markdown_doc().replace("Fact 3 of section B", "Fact three of section B"))
    a_ids = lambda cs: [c.id for c in cs if c.section.endswith("Section A")]
    assert a_ids(before) == a_ids(after)


def test_fenced_code_is_kept_as_one_block():
    text = "# Doc\n\n```powershell\npython -m rag.ingest\npython -m pytest. Then more.\n```\n"
    (c,) = chunks_of(text)
    assert "python -m rag.ingest\npython -m pytest. Then more." in c.text
