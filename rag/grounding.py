"""Deterministic ground check of an answer (RAG phase 4). No LLM.

1. **Numbers.** Every number written in the answer must appear in the evidence: a cell (or a
   number inside a text cell) of the SQL result, a number in a retrieved chunk, or a number in
   the SQL text itself (a filter such as `ptr > 60`) or in the question. Written forms are
   normalised: Indian and Western thousands separators are dropped (1,06,302 = 106302), and the
   sign is ignored ("fell by 2.3 points" for -2.3).
   Rounding is allowed: the answer's 24.5 matches 24.53 (rounded to the answer's decimals,
   half-up or half-even). A number followed by % also matches a share 100x smaller (0.4823 ->
   48.2%), because the marts store girls_share as a fraction. No other conversion is accepted:
   "1.06 lakh" for 106302 fails, and so does any figure the model computed itself; computed
   figures must come from SQL.
   Not checked: academic years (2024-25), calendar years 1990-2035 standing alone, class
   numbers in "class 9" / "classes 9-12", and the page numbers inside citations.
2. **Citations.** Every [title, p. N] in the answer must be the citation of a retrieved chunk.
   On the docs route, an answer that does not say the evidence is insufficient must cite at
   least one chunk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

# A number: Indian (1,06,302) or Western (106,302) grouping, or plain digits; optional decimals.
_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?(?![\w])|(?<![\w.,])\d+(?:\.\d+)?(?![\w])")
_ACADEMIC_YEAR = re.compile(r"\b(?:19|20)\d{2}\s*[-–/]\s*\d{2,4}\b")
_CLASS_RANGE = re.compile(r"\b(?:class(?:es)?|grades?|std\.?|standard)\s+\d{1,2}(?:\s*(?:-|–|to|and)\s*\d{1,2})?\b", re.I)
_CITATION = re.compile(r"\[[^\[\]]+\]")
_YEAR_ALONE = range(1990, 2036)


@dataclass
class GroundResult:
    ok: bool
    unsupported_numbers: list[str] = field(default_factory=list)
    unknown_citations: list[str] = field(default_factory=list)
    missing_citation: bool = False
    numbers_checked: int = 0

    def problems(self) -> list[str]:
        out = []
        if self.unsupported_numbers:
            out.append("numbers not found in the evidence: " + ", ".join(self.unsupported_numbers))
        if self.unknown_citations:
            out.append("citations that are not among the documents given: " + ", ".join(self.unknown_citations))
        if self.missing_citation:
            out.append("document facts must be cited as [title, p. N] from the cite attributes")
        return out


def to_decimal(token: str) -> Decimal | None:
    try:
        return abs(Decimal(token.replace(",", "")))
    except InvalidOperation:
        return None


def numbers_in(text: str) -> list[tuple[str, Decimal, bool]]:
    """(as written, value, followed by %) for each number of a text."""
    out = []
    for m in _NUM.finditer(text):
        value = to_decimal(m.group(0))
        if value is not None:
            pct = text[m.end():m.end() + 2].lstrip().startswith("%") or \
                  text[m.end():m.end() + 9].lower().lstrip().startswith("per cent") or \
                  text[m.end():m.end() + 8].lower().lstrip().startswith("percent")
            out.append((m.group(0), value, pct))
    return out


def _blank(pattern: re.Pattern, text: str) -> str:
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def answer_numbers(answer: str) -> list[tuple[str, Decimal, bool]]:
    """The numbers of an answer that need evidence (exemptions in the module docstring)."""
    text = _blank(_CITATION, answer)
    text = _blank(_ACADEMIC_YEAR, text)
    text = _blank(_CLASS_RANGE, text)
    return [n for n in numbers_in(text)
            if not ("," not in n[0] and "." not in n[0] and int(n[1]) in _YEAR_ALONE)]


def evidence_values(texts: list[str], cells: list) -> set[Decimal]:
    values: set[Decimal] = set()
    for t in texts:
        values.update(v for _, v, _ in numbers_in(t or ""))
    for c in cells:
        if c is None or isinstance(c, bool):
            continue
        if isinstance(c, (int, float, Decimal)):
            try:
                values.add(abs(Decimal(str(c))))
            except InvalidOperation:
                pass
        else:
            values.update(v for _, v, _ in numbers_in(str(c)))
    return values


def _decimals(written: str) -> int:
    return len(written.split(".")[1]) if "." in written else 0


def matches(written: str, value: Decimal, pct: bool, evidence: set[Decimal]) -> bool:
    if value in evidence:
        return True
    q = Decimal(1).scaleb(-_decimals(written))
    for e in evidence:
        candidates = (e, e * 100) if pct else (e,)
        for c in candidates:
            for rounding in (ROUND_HALF_UP, ROUND_HALF_EVEN):
                try:
                    if c.quantize(q, rounding=rounding) == value:
                        return True
                except InvalidOperation:
                    continue
    return False


def normalise_citation(c: str) -> str:
    return " ".join(c.strip().strip("[]").split()).lower()


def check(answer: str, *, sql_cells: list | None = None, chunk_texts: list[str] | None = None,
          extra_texts: list[str] | None = None, allowed_citations: list[str] | None = None,
          require_citation: bool = False) -> GroundResult:
    evidence = evidence_values((chunk_texts or []) + (extra_texts or []), sql_cells or [])
    nums = answer_numbers(answer)
    unsupported = list(dict.fromkeys(w for w, v, pct in nums if not matches(w, v, pct, evidence)))

    allowed = {normalise_citation(c) for c in (allowed_citations or [])}
    titles = {a.rsplit(",", 1)[0] for a in allowed}
    unknown, cited = [], 0
    for m in _CITATION.finditer(answer):
        c = normalise_citation(m.group(0))
        if c in allowed:
            cited += 1
        elif re.search(r"\bpp?\.\s*\d", c) or c.rsplit(",", 1)[0] in titles:
            unknown.append(m.group(0))   # looks like a citation but is not one we gave
    missing = require_citation and cited == 0
    ok = not unsupported and not unknown and not missing
    return GroundResult(ok, unsupported, unknown, missing, len(nums))
