# Retrieval evaluation (RAG phase 2)

Which retrieval mode should be the default: `bm25`, `vector`, `hybrid` or `hybrid_rerank`?
This file fixes the rule **before** any run, then records what the run gave and what the
rule picks. The rule is applied exactly as written, even if the result is disappointing:
choosing the rule after seeing the numbers would let the numbers choose the rule.

## Set-up

- **Questions:** `retrieval_questions.jsonl`, about 30 questions, each with the document and
  page(s) where the answer is. Four types: `definition` (schema PDFs, metadata note),
  `figure` (numbers from the two reports' tables, some Rajasthan rows), `exact_code`
  (questions about codes, where keyword search should win) and `paraphrase` (the words of the
  question differ from the words of the document, where vectors should win). Every question
  was checked by hand (Aditya) before the run.
- **Index:** the shared index `E:\udise-rag-index` (7 documents; the chunk count is printed
  with the results).
- **Relevant:** a retrieved chunk from the expected document whose pages overlap the
  expected pages (Markdown: the expected section).
- **Metrics:**
  - recall@5: share of questions with at least one relevant chunk in the top 5.
  - MRR@10: mean of 1 / rank of the first relevant chunk within the top 10 (0 if none).
  - p50 / p95 latency: of one search call on this laptop's CPU, after one warm-up query per
    mode (model loading not counted). Nearest-rank percentiles: real measured values.
- **Run:** `python -m rag.evals.run_retrieval` (refuses to run while a question still has
  `needs_review: true`). Per-question detail: `reports/rag_retrieval_eval.json`.

## Selection rule

Written on 2026-11-09, before any evaluation run. Coded as `choose_default` in
`run_retrieval.py` (tested in `tests/test_rag_eval.py`).

1. **The base is `hybrid`.** If `bm25` or `vector` has a recall@5 at least 5 percentage points
   above `hybrid`'s, the base is the better of those two instead (a tie goes to `bm25`,
   which needs no model).
2. **Pick `hybrid_rerank` only if** its recall@5 is at least 5 percentage points above the
   base's **and** its p95 latency is under 1.5 s. Otherwise the default is the base.

MRR@10 and p50 are reported but do not decide. With 30 questions, one question is 3.3
points, so "5 points" means at least two more questions answered in the top 5: one lucky
question is not enough to change the default.

Why these thresholds: a reranker costs a 1.1 GB model and a model pass per candidate, so it
must earn its place by a clear margin, and 1.5 s is about the most a person waits for the
search step before the answer step (phase 4) adds its own time. Hybrid is the base because
it is the usual safe choice when both exact codes and paraphrases must work; a single mode
replaces it only if it is clearly better on these questions.

## Results

Not run yet: the questions are waiting for review.
