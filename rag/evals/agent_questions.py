"""The held-out agent questions (RAG phase 5): load them and check their labels.

Usage:  python -m rag.evals.agent_questions      (check only: no LLM call, no search)

Each line of agent_questions.jsonl is one question:
  id, category            a01..a30; docs | sql | both | unanswerable
  expected_route          docs / sql / both (null for unanswerable); acceptable_routes lists
                          every route that counts as right (usually just the expected one)
  expected_filters        {year, doc_type}: what the router should end with, i.e. a value only
                          where the question states it (two years stated -> year null)
  expected_chunks         chunk ids of the shared index that hold the answer (role "answer") or
                          one part of it (role "partial"), each with an `evidence` string that
                          must appear in the chunk's text; labelled per chunk, not per section
  expected_values         figures the answer must give (SQL and both questions), each with its
                          decimals, year and accepted alternatives
  reference_sql           the query that produced expected_values (run again by this check)
  expect_abstain          true for the 3 questions nothing can answer
  answer, note            the reference answer and the labeller's notes
  needs_review            true until Aditya has checked the question

The check opens the index and the gold database read-only and verifies, for every question:
the chunk ids exist with that file and page and contain the evidence; the reference SQL still
returns every expected value; the year label agrees with the years written in the question;
and no question repeats one of the phase 2 retrieval questions (word overlap).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from .. import config as C

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "agent_questions.jsonl"
PHASE2_QUESTIONS = HERE / "retrieval_questions.jsonl"
CATEGORIES = {"docs": 10, "sql": 10, "both": 7, "unanswerable": 3}
ROUTES = {"docs", "sql", "both", "out_of_scope"}
MAX_OVERLAP = 0.6          # word Jaccard with any phase 2 question above this = a reworded copy
_YEAR = re.compile(r"\b(20\d\d)\s*[-–/]\s*(?:20)?(\d\d)\b")


def load(path: Path = QUESTIONS) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def years_in(question: str) -> list[str]:
    return sorted({f"{a}-{b}" for a, b in _YEAR.findall(question)})


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower())) - {"the", "a", "an", "of", "in", "and", "what", "how",
                                                          "is", "was", "were", "did", "does", "do", "to", "for"}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def value_found(expected: dict, cells: list) -> bool:
    """An expected value is in the SQL result if a numeric cell equals it after rounding to its
    decimals; a percent may be stored as a share (0.464 -> 46.4)."""
    targets = [expected["value"], *expected.get("alternatives", [])]
    d = expected.get("decimals", 0)
    for c in cells:
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            continue
        for x in (c, c * 100) if expected.get("unit") == "percent" else (c,):
            if any(round(float(x), d) == round(float(t), d) for t in targets):
                return True
    return False


def check(questions: list[dict], index_dir: Path | None = None, gold_db: Path | None = None) -> list[str]:
    """Returns the list of problems (empty = every label checks out)."""
    problems: list[str] = []
    ids = [q["id"] for q in questions]
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    counts = Counter(q["category"] for q in questions)
    if dict(counts) != CATEGORIES:
        problems.append(f"categories {dict(counts)} != {CATEGORIES}")

    # ---- labels that need no index or database
    phase2 = [(p["id"], words(p["question"])) for p in load(PHASE2_QUESTIONS)]
    doc_types = {row.split(",")[-2] for row in C.MANIFEST.read_text(encoding="utf-8").splitlines()[1:] if row}
    for q in questions:
        qid, f = q["id"], q["expected_filters"]
        ys = years_in(q["question"])
        want_year = ys[0] if len(ys) == 1 else None
        if f.get("year") != want_year:
            problems.append(f"{qid}: expected year {f.get('year')!r}, question states {ys or 'none'}")
        if f.get("doc_type") is not None and f["doc_type"] not in doc_types:
            problems.append(f"{qid}: doc_type {f['doc_type']!r} not in the manifest")
        if q["category"] == "unanswerable":
            if not q["expect_abstain"] or q["expected_route"] is not None:
                problems.append(f"{qid}: unanswerable must have expect_abstain and no route")
        else:
            if q["expected_route"] not in ROUTES or q["expected_route"] not in q["acceptable_routes"]:
                problems.append(f"{qid}: bad expected_route / acceptable_routes")
        if q["category"] in ("docs", "both") and not any(c["role"] == "answer" for c in q["expected_chunks"]):
            problems.append(f"{qid}: no chunk with role 'answer'")
        if q["category"] in ("sql", "both") and not (q["expected_values"] and q["reference_sql"]):
            problems.append(f"{qid}: SQL questions need expected_values and reference_sql")
        best = max(phase2, key=lambda p: jaccard(words(q["question"]), p[1]))
        overlap = jaccard(words(q["question"]), best[1])
        if overlap > MAX_OVERLAP:
            problems.append(f"{qid}: too close to phase 2 {best[0]} (word overlap {overlap:.2f})")

    # ---- chunk labels against the index
    import chromadb
    from chromadb.config import Settings
    client = chromadb.PersistentClient(path=str(index_dir or C.INDEX_DIR), settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(C.COLLECTION)
    want = sorted({c["id"] for q in questions for c in q["expected_chunks"]})
    got = col.get(ids=want, include=["documents", "metadatas"])
    found = {i: (t, m) for i, t, m in zip(got["ids"], got["documents"], got["metadatas"])}
    for q in questions:
        for c in q["expected_chunks"]:
            if c["id"] not in found:
                problems.append(f"{q['id']}: chunk {c['id']} not in the index")
                continue
            text, meta = found[c["id"]]
            if meta["file"] != c["file"] or (meta.get("page") or None) != c["page"]:
                problems.append(f"{q['id']}: chunk {c['id']} is {meta['file']} p.{meta.get('page')}, label says {c['file']} p.{c['page']}")
            if " ".join(c["evidence"].split()) not in " ".join(text.split()):
                problems.append(f"{q['id']}: evidence not in chunk {c['id']}: {c['evidence']!r}")

    # ---- reference SQL against the gold database (read-only)
    import duckdb
    from ..sql_tool import default_gold_db
    con = duckdb.connect(str(gold_db or default_gold_db()), read_only=True)
    try:
        for q in questions:
            if not q["reference_sql"]:
                continue
            cells = [c for row in con.execute(q["reference_sql"]).fetchall() for c in row]
            plain = [v for v in q["expected_values"] if v["name"] != "change"]
            for v in plain:
                if not value_found(v, cells):
                    problems.append(f"{q['id']}: {v['name']} = {v['value']} not returned by the reference SQL")
            for v in q["expected_values"]:
                if v["name"] == "change" and len(plain) == 2 and plain[1]["value"] - plain[0]["value"] != v["value"]:
                    problems.append(f"{q['id']}: change {v['value']} != {plain[1]['value']} - {plain[0]['value']}")
    finally:
        con.close()
    return problems


def main() -> int:
    questions = load()
    problems = check(questions)
    counts = Counter(q["category"] for q in questions)
    ys = Counter("two years" if len(years_in(q["question"])) > 1 else ("year" if years_in(q["question"]) else "no year")
                 for q in questions)
    print(f"{len(questions)} questions {dict(counts)}; {dict(ys)}; "
          f"{sum(len(q['expected_chunks']) for q in questions)} chunk labels; "
          f"{sum(1 for q in questions if q['needs_review'])} still need review")
    for p in problems:
        print("PROBLEM:", p)
    print("labels OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
