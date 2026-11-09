"""The retrieval evaluation's own logic (rag/evals/run_retrieval.py): what counts as relevant,
recall@5, MRR@10, the latency percentile, the selection rule, and that the committed questions
file is well formed. No index and no model needed."""
import json

import pytest

from rag.evals.run_retrieval import (MODES, QUESTIONS, choose_default, first_relevant_rank,
                                     is_relevant, load_questions, mrr_at, percentile, recall_at)

PDF = {"file": "report.pdf", "page": 74, "page_end": 75, "section": "Table 3.1"}
MD = {"file": "DATA_QUALITY_LOG.md", "page": 0, "page_end": 0,
      "section": "Data Quality Log > Issues in the data itself"}


def test_relevance_by_page_overlap_or_markdown_section():
    assert is_relevant(PDF, [{"file": "report.pdf", "pages": [75]}])           # page_end counts
    assert not is_relevant(PDF, [{"file": "report.pdf", "pages": [73, 76]}])
    assert not is_relevant(PDF, [{"file": "other.pdf", "pages": [74]}])        # wrong document
    assert is_relevant(PDF, [{"file": "other.pdf", "pages": [1]},
                             {"file": "report.pdf", "pages": [74]}])           # any expected item
    assert is_relevant(MD, [{"file": "DATA_QUALITY_LOG.md", "section": "Issues in the data"}])
    assert not is_relevant(MD, [{"file": "DATA_QUALITY_LOG.md", "section": "Decisions"}])


def test_first_relevant_rank():
    metas = [{"file": "x.pdf", "page": 1, "page_end": 1}, PDF, PDF]
    assert first_relevant_rank(metas, [{"file": "report.pdf", "pages": [74]}]) == 2
    assert first_relevant_rank(metas, [{"file": "report.pdf", "pages": [9]}]) is None


def test_recall_and_mrr():
    ranks = [1, 3, None, 7, 5]
    assert recall_at(ranks, 5) == pytest.approx(3 / 5)          # ranks 1, 3 and 5
    assert mrr_at(ranks, 10) == pytest.approx((1 + 1 / 3 + 1 / 7 + 1 / 5) / 5)
    assert mrr_at(ranks, 5) == pytest.approx((1 + 1 / 3 + 1 / 5) / 5)
    assert recall_at([], 5) == 0.0


def test_percentile_is_a_measured_value():
    values = [0.1 * k for k in range(1, 21)]                     # 0.1 .. 2.0
    assert percentile(values, 50) == pytest.approx(1.0)
    assert percentile(values, 95) == pytest.approx(1.9)
    assert percentile([0.3], 95) == 0.3


def _m(bm25, vector, hybrid, rerank, p95):
    rec = dict(zip(MODES, (bm25, vector, hybrid, rerank)))
    return {mode: {"recall@5": r, "p95_s": p95 if mode == "hybrid_rerank" else 0.05}
            for mode, r in rec.items()}


@pytest.mark.parametrize("metrics, expected", [
    (_m(0.60, 0.50, 0.70, 0.80, 1.20), "hybrid_rerank"),   # +10 points, fast enough
    (_m(0.60, 0.50, 0.70, 0.80, 1.50), "hybrid"),          # p95 must be UNDER 1.5 s
    (_m(0.60, 0.50, 0.70, 0.73, 0.50), "hybrid"),          # +3 points is not enough
    (_m(0.80, 0.50, 0.70, 0.83, 0.50), "bm25"),            # bm25 +10 over hybrid; rerank +3 over bm25
    (_m(0.80, 0.80, 0.70, 0.70, 0.50), "bm25"),            # bm25 and vector tie: bm25 (no model)
    (_m(0.50, 0.90, 0.70, 0.93, 0.50), "vector"),          # rerank +23 over hybrid but only +3 over vector
])
def test_selection_rule(metrics, expected):
    choice, reasons = choose_default(metrics)
    assert choice == expected
    assert len(reasons) == 2 and reasons[0].startswith("step 1") and reasons[1].startswith("step 2")


def test_exactly_five_points_counts():
    # With 20 questions one question is 5 points: 15/20 = 75 %, 16/20 = 80 %. Exactly +5 counts
    # (floating-point 80.0 - 75.0 must not come out as 4.999...).
    choice, _ = choose_default(_m(0.5, 0.5, 15 / 20, 16 / 20, 1.0))
    assert choice == "hybrid_rerank"                            # 80.0 - 75.0 = 5.0 points


def test_the_questions_file_is_well_formed():
    questions = load_questions(QUESTIONS)
    assert len(questions) >= 25
    assert {q["type"] for q in questions} == {"definition", "figure", "exact_code", "paraphrase"}
    texts = [q["question"] for q in questions]
    assert "What does code 9 mean in yes/no columns?" in texts


def test_bad_questions_are_all_reported(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join([
        json.dumps({"id": "a", "question": "Q?", "type": "figure",
                    "expected": [{"file": "r.pdf", "pages": [1]}], "needs_review": True}),
        json.dumps({"id": "a", "question": "Q2?", "type": "guess",
                    "expected": [{"file": "r.pdf"}], "needs_review": "yes"}),
        "{not json",
    ]), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_questions(path)
    for text in ("duplicate id", "type must be", "needs file and pages", "needs_review must",
                 "not JSON"):
        assert text in str(e.value)
