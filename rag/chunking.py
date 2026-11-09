"""Split a document into chunks: heading-aware text chunks and whole-row table chunks.

Text: the text of one section (everything under one heading) is cut into sentences, and
sentences are added to a chunk until the next one would pass the target size (450 tokens).
A chunk never crosses a heading. The next chunk starts with the last ~15% of the previous
one (whole sentences when they fit, otherwise the last words), so a fact that sits on a
boundary is fully inside at least one chunk. A sentence longer than the target is cut into
word windows. No chunk is ever longer than the hard maximum (512 tokens).

Tables: every table becomes its own chunk(s), rendered as a Markdown pipe table under its
section heading. A long table is split between rows only, and every part starts with the
header row again, so each part can be read on its own. A single row is never cut: in the
rare case where one row alone is longer than 512 tokens it is kept whole and marked
`oversized` (the end of it will not be embedded; the ingest report counts these).

Every chunk text starts with its section heading, so the chunk says what it is about even
when read alone. Ids are deterministic: sha256(file | page | offset), where page is the PDF
page (Markdown: the section heading) and offset is the character offset where the chunk
starts (tables: table number and first row).
"""
import hashlib
import re
from dataclasses import dataclass

from . import config as C
from .extract import Document, Heading, Paragraph, Table
from .tokens import TokenCounter

SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[*`A-Z0-9])")
WORD = re.compile(r"\S+")
MAX_PDF_PATH = 3   # at most 3 stacked PDF headings in a section path


@dataclass
class Chunk:
    id: str
    text: str
    chunk_type: str              # text | table
    page: int                    # first page (0 = Markdown, no pages)
    page_end: int
    section: str                 # heading path, "" before the first heading
    location: str                # "p. 3", "pp. 3-4", or the section for Markdown
    n_tokens: int
    oversized: bool = False
    table_index: int | None = None
    row_start: int | None = None  # table rows in this chunk (0-based, header not counted)
    row_end: int | None = None


@dataclass
class Unit:
    """A sentence (or part of one) with where it starts in the source."""
    text: str
    page: int
    offset: int
    sep: str = " "               # what joins it to the previous unit ("\n" = new paragraph)


def chunk_id(file: str, page_key: str, offset) -> str:
    return hashlib.sha256(f"{file}|{page_key}|{offset}".encode("utf-8")).hexdigest()[:16]


def _render(head: str, units: list[Unit]) -> str:
    body = units[0].text + "".join(u.sep + u.text for u in units[1:]) if units else ""
    return f"{head}\n\n{body}" if head else body


def _sentences(par: Paragraph) -> list[Unit]:
    text = par.text.replace("\r", " ").replace("\n", " ") if not par.code else par.text
    if par.code:
        return [Unit(text, par.page, par.offset, "\n")]
    units, start = [], 0
    for m in SENTENCE_END.finditer(text):
        units.append(Unit(text[start:m.start()], par.page, par.offset + start))
        start = m.end()
    units.append(Unit(text[start:].rstrip(), par.page, par.offset + start))
    units = [u for u in units if u.text.strip()]
    if units:
        units[0].sep = "\n"
    return units


def _word_windows(unit: Unit, fits) -> list[Unit]:
    """Cut a too-long unit into the longest word windows that still fit."""
    words = list(WORD.finditer(unit.text))
    out, i = [], 0
    while i < len(words):
        j = i + 1
        while j < len(words) and fits(unit.text[words[i].start():words[j].end()]):
            j += 1
        text = unit.text[words[i].start():words[j - 1].end()]
        out.append(Unit(text, unit.page, unit.offset + words[i].start(),
                        unit.sep if i == 0 else " "))
        i = j
    return out


class Chunker:
    def __init__(self, count_tokens: TokenCounter, target: int = C.TARGET_TOKENS,
                 max_tokens: int = C.MAX_TOKENS, overlap: int = C.OVERLAP_TOKENS):
        if not 0 <= overlap < target <= max_tokens:
            raise ValueError("need 0 <= overlap < target <= max_tokens")
        self.count = count_tokens
        self.target, self.max, self.overlap = target, max_tokens, overlap

    # --- text --------------------------------------------------------------------------
    def _split_long(self, units: list[Unit], head: str) -> list[Unit]:
        out = []
        for u in units:
            if self.count(_render(head, [u])) <= self.target:
                out.append(u)
            else:
                out += _word_windows(u, lambda t: self.count(_render(head, [Unit(t, 0, 0)])) <= self.target)
        return out

    def _tail(self, units: list[Unit]) -> list[Unit]:
        """The overlap carried into the next chunk: whole sentences from the end if they fit
        the overlap budget, else the last words. Never the whole chunk (ids must differ)."""
        if self.overlap == 0:
            return []
        out = []
        for u in reversed(units[1:]):
            if self.count(_render("", [u] + out)) > self.overlap:
                break
            out = [u] + out
        if out:
            return out
        last = units[-1]
        words = list(WORD.finditer(last.text))
        first_allowed = 1 if len(units) == 1 else 0
        best = None
        for k in range(len(words) - 1, first_allowed - 1, -1):
            if self.count(last.text[words[k].start():]) > self.overlap:
                break
            best = k
        if best is None:
            return []
        start = words[best].start()
        return [Unit(last.text[start:], last.page, last.offset + start)]

    def _pack_text(self, units: list[Unit], head: str) -> list[list[Unit]]:
        units = self._split_long(units, head)
        groups, i, carry = [], 0, []
        while i < len(units):
            cur, new = list(carry), 0
            while i < len(units):
                n = self.count(_render(head, cur + [units[i]]))
                if n <= self.target or (new == 0 and n <= self.max):
                    cur.append(units[i])
                    i += 1
                    new += 1
                elif new == 0 and cur:       # overlap + next sentence too long: drop overlap
                    cur = []
                else:
                    break
            if new == 0:                      # cannot happen after _split_long; be safe
                cur = [units[i]]
                i += 1
            groups.append(cur)
            carry = self._tail(cur) if i < len(units) else []
        return groups

    # --- tables ------------------------------------------------------------------------
    @staticmethod
    def _row(cells: list[str]) -> str:
        return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"

    def _table_text(self, head: str, table: Table, rows) -> str:
        lines = []
        if table.header:
            lines += [self._row(table.header), "|" + "---|" * len(table.header)]
        lines += [self._row(r.cells) for r in rows]
        body = "\n".join(lines)
        return f"{head}\n\n{body}" if head else body

    def _pack_table(self, table: Table, head: str) -> list[tuple[int, list]]:
        """Groups of whole rows: (index of first row, rows)."""
        groups, cur, start = [], [], 0
        for i, row in enumerate(table.rows):
            if cur and self.count(self._table_text(head, table, cur + [row])) > self.target:
                groups.append((start, cur))
                cur, start = [], i
            cur.append(row)
        if cur or not table.rows:
            groups.append((start, cur))
        return groups

    # --- document ----------------------------------------------------------------------
    def chunk(self, doc: Document, file: str) -> list[Chunk]:
        chunks, pending, path, after_heading = [], [], [], False
        is_pdf = doc.kind == "pdf"

        def location(page, page_end, section):
            if not is_pdf:
                return section or "(start of document)"
            return f"p. {page}" if page == page_end else f"pp. {page}-{page_end}"

        def add(text, kind, first_page, last_page, offset, **extra):
            section = " > ".join(path)
            page_key = str(first_page) if is_pdf else section
            n = self.count(text)
            chunks.append(Chunk(chunk_id(file, page_key, offset), text, kind, first_page,
                                last_page, section, location(first_page, last_page, section),
                                n, oversized=n > self.max, **extra))

        def flush():
            head = " > ".join(path)
            for group in self._pack_text(pending, head):
                add(_render(head, group), "text", group[0].page, group[-1].page, group[0].offset)
            pending.clear()

        # Section path. Markdown: the usual heading stack by level. PDF headings have no level:
        # headings that follow each other directly stack up ("Data Sharing Portal - Schema >
        # School Profile 1"); a heading after some content starts a new path. A PDF path keeps
        # its last MAX_PDF_PATH headings, so a long run of heading-like lines can never make
        # a section path that crowds the text out of every chunk.
        md_stack: list[tuple[int, str]] = []
        for el in doc.elements:
            if isinstance(el, Heading):
                flush()
                if el.level == 0:
                    path = (path + [el.text])[-MAX_PDF_PATH:] if after_heading else [el.text]
                else:
                    md_stack = [(lvl, t) for lvl, t in md_stack if lvl < el.level] + [(el.level, el.text)]
                    path = [t for _, t in md_stack]
                after_heading = True
                continue
            after_heading = False
            if isinstance(el, Paragraph):
                pending += _sentences(el)
            elif isinstance(el, Table):
                flush()
                head = " > ".join(path)
                for start, rows in self._pack_table(el, head):
                    pages = [r.page for r in rows] or [el.page]
                    add(self._table_text(head, el, rows), "table", pages[0], pages[-1],
                        f"table{el.index}:row{start}",
                        table_index=el.index, row_start=start,
                        row_end=start + len(rows) - 1 if rows else start)
        flush()

        ids = [c.id for c in chunks]
        if len(ids) != len(set(ids)):
            raise RuntimeError(f"{file}: duplicate chunk ids (chunker bug)")
        return chunks
