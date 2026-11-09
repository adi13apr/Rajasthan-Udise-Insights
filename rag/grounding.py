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

The agent uses `check_claims`: the answer is a list of claims, each with its own sources
(document ids D1, D2 ... and/or "sql"), and each claim's numbers are checked against **that
claim's sources only**, not the pooled evidence. So a figure from the SQL result written in a
claim that cites a document fails, even though the figure is in the evidence somewhere. In a
claim with numbers, every listed source must also hold at least one of them, so a document
cannot ride along next to "sql" on a database-only figure. A
source id that was not given, or a citation written into the claim text (code renders them),
also fails. `check` (pooled evidence, one free-text answer) is kept for the number rules.
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
    claim_problems: list[str] = field(default_factory=list)   # check_claims: one line per problem

    def problems(self) -> list[str]:
        if self.claim_problems:          # check_claims already wrote every problem, per claim
            return list(self.claim_problems)
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


SQL_SOURCE = "sql"
_INLINE_SOURCE = re.compile(r"\[[^\[\]]*(?:\bpp?\.\s*\d|\bD\d+\b)[^\[\]]*\]|\(computed from the database\)", re.I)


def check_claims(claims, *, doc_texts: dict[str, str], sql_cells: list | None, sql_text: str = "",
                 question: str = "", require_doc_source: bool = False) -> GroundResult:
    """Check each claim against its own sources.

    `claims`: objects with `.text` and `.sources` (prompts.Claim). `doc_texts`: document id (D1 ...)
    -> chunk text, for the chunks given to the model. `sql_cells`: every cell of the SQL result,
    or None if there was no result (then "sql" is not a valid source). A claim's evidence is the
    union of its sources (a document's text; for "sql" the cells and the SQL text) plus the
    question's own numbers. A claim with numbers and no sources fails."""
    question_vals = evidence_values([question], [])
    sql_vals = evidence_values([sql_text], sql_cells) if sql_cells is not None else None
    doc_vals = {d: evidence_values([t], []) for d, t in doc_texts.items()}
    problems, unsupported, checked, doc_backed = [], [], 0, 0
    for n, claim in enumerate(claims, start=1):
        sources = list(claim.sources)
        unknown = [s for s in sources if not (s in doc_vals or (s == SQL_SOURCE and sql_vals is not None))]
        if unknown:
            given = ", ".join(list(doc_vals) + ([SQL_SOURCE] if sql_vals is not None else [])) or "none"
            problems.append(f"claim {n}: sources {', '.join(unknown)} were not given (given: {given})")
        if _INLINE_SOURCE.search(claim.text):
            problems.append(f"claim {n}: do not write citations in the claim text; list them in sources")
        per_source = {s: (doc_vals.get(s, set()) if s != SQL_SOURCE else (sql_vals or set())) for s in sources}
        evidence = set(question_vals).union(*per_source.values()) if per_source else set(question_vals)
        doc_backed += any(s in doc_vals for s in sources)
        nums = answer_numbers(claim.text)
        checked += len(nums)
        bad = list(dict.fromkeys(w for w, v, pct in nums if not matches(w, v, pct, evidence)))
        if bad:
            unsupported += bad
            where = ", ".join(sources) if sources else "no sources"
            problems.append(f"claim {n}: {', '.join(bad)} not found in its own sources ({where})")
        # A source must back something: in a claim with numbers, every listed source must hold at
        # least one of them. Otherwise [D2, "sql"] on a database-only figure would still print the
        # report's citation next to it (the Q5 bug, second form).
        if nums:
            for s, vals in per_source.items():
                if s in unknown:
                    continue
                if not any(matches(w, v, pct, vals) for w, v, pct in nums):
                    problems.append(f"claim {n}: source {s} contains none of the claim's numbers "
                                    f"({', '.join(w for w, _, _ in nums)}); remove it or split the claim")
    missing = require_doc_source and doc_backed == 0
    if missing:
        problems.append("no claim cites a document: give each document fact its document id in sources")
    return GroundResult(not problems, unsupported, [], missing, checked, problems)
