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

Questions reviewed and approved by Aditya on 2026-11-09, unchanged from the draft.

<!-- results:start -->

Run: 2026-11-09T11:33:53+00:00 on Intel64 Family 6 Model 154 Stepping 3, GenuineIntel (CPU, torch threads 12), 33 questions, index 2320 chunks.

| Mode | recall@5 | MRR@10 | p50 latency | p95 latency |
|---|---|---|---|---|
| `bm25` | 60.6% | 0.526 | 0.006 s | 0.009 s |
| `vector` | 57.6% | 0.478 | 0.057 s | 0.219 s |
| `hybrid` | 63.6% | 0.545 | 0.107 s | 0.187 s |
| `hybrid_rerank` | 66.7% | 0.533 | 14.124 s | 15.614 s |

recall@5 by question type:

| Type | n | `bm25` | `vector` | `hybrid` | `hybrid_rerank` |
|---|---|---|---|---|---|
| definition | 7 | 14% | 57% | 43% | 43% |
| figure | 11 | 73% | 55% | 73% | 73% |
| exact_code | 8 | 100% | 75% | 75% | 88% |
| paraphrase | 7 | 43% | 43% | 57% | 57% |

Rank of the first relevant chunk (— = not in the top 10):

| Question | Type | `bm25` | `vector` | `hybrid` | `hybrid_rerank` |
|---|---|---|---|---|---|
| q01 | exact_code | 1 | 1 | 1 | 1 |
| q02 | exact_code | 1 | 1 | 1 | 1 |
| q03 | exact_code | 1 | — | 6 | — |
| q04 | exact_code | 1 | 1 | 1 | 1 |
| q05 | exact_code | 1 | 1 | 1 | 1 |
| q06 | exact_code | 4 | 4 | 3 | 3 |
| q07 | exact_code | 1 | 2 | 1 | 2 |
| q08 | exact_code | 1 | — | 7 | 5 |
| q09 | definition | — | 1 | — | — |
| q10 | definition | — | — | — | — |
| q11 | definition | — | — | — | — |
| q12 | definition | 7 | 5 | 4 | 6 |
| q13 | definition | 10 | 1 | 7 | 2 |
| q14 | definition | 1 | 1 | 1 | 1 |
| q15 | definition | 6 | — | 4 | 1 |
| q16 | figure | — | 1 | 5 | — |
| q17 | figure | 3 | 1 | 1 | 1 |
| q18 | figure | 1 | — | 1 | 7 |
| q19 | figure | 2 | 1 | 1 | 2 |
| q20 | figure | 7 | — | 6 | 1 |
| q21 | figure | — | — | — | — |
| q22 | figure | 1 | 1 | 1 | 2 |
| q23 | figure | 1 | 1 | 1 | 2 |
| q24 | figure | 1 | — | — | 1 |
| q25 | figure | 1 | 6 | 3 | 1 |
| q26 | figure | 4 | 1 | 1 | 1 |
| q27 | paraphrase | — | — | — | — |
| q28 | paraphrase | 1 | 3 | 1 | 4 |
| q29 | paraphrase | 1 | 3 | 1 | 1 |
| q30 | paraphrase | 7 | — | 1 | 1 |
| q31 | paraphrase | — | — | — | — |
| q32 | paraphrase | — | — | — | — |
| q33 | paraphrase | 3 | 1 | 1 | 1 |

**Rule applied (as written above, by `choose_default`):**

- step 1: the best single mode (bm25) is -3.0 points against hybrid (< +5): the base is hybrid
- step 2: hybrid_rerank not chosen: recall@5 is +3.0 points against hybrid (< +5); p95 is 15.614 s (not under 1.5 s): hybrid

**Default mode: `hybrid`**

<!-- results:end -->

## What the run shows (written after the run; the rule above was not changed)

- **The rule picks `hybrid`.** It is the best mode that is fast: 63.6 % recall@5, MRR@10 0.545,
  p95 0.19 s. `hybrid_rerank` answered one more question in the top 5 (+3.0 points, below the
  5-point bar) and took 14.1 s (p50) / 15.6 s (p95) per question on this laptop CPU: scoring 30
  passages of up to 512 tokens with a 278M-parameter cross-encoder is too slow here. It fails both
  conditions; either alone would have rejected it.
- **BM25 wins exact codes** (8 of 8 in the top 5, vector 6 of 8), as expected. `code 9` (q01),
  which vector search missed in phase 1, is now rank 1 in every mode (the Data Quality Log is in
  the index and the tokenizer keeps the 9).
- **Vectors win definitions** (4 of 7 vs BM25's 1 of 7), also as expected.
- **Fusion helps paraphrases** (hybrid 4 of 7 vs 3 of 7 for either list alone), and on figures it
  matches BM25 (8 of 11).
- **Recall is modest overall (21 of 33 in the top 5).** Seven questions are missed in the top 10 by
  every mode. Three patterns, read from the per-question results in `reports/rag_retrieval_eval.json`:
  1. Definitions from the metadata note (q10 NER vs ANER, q11 dropout rate) lose to the report
     tables of the same indicator (ANER table p. 112, dropout table p. 120), which use the same words.
  2. Rajasthan figures lose to neighbouring tables of the same family: q21 (government teachers,
     Table 8.2 p. 157) returns the teacher tables on pp. 54-59; q16 (Rajasthan schools) finds
     p. 156 only at rank 5 in hybrid.
  3. Paraphrased figure questions (q27 "kids per teacher", q31 "no students at all", q32 "leave
     school during classes 9 and 10") land on the right topic but the wrong table or page.
- **Candidate improvements for a later phase**, each to be judged by a new rule written before a new
  run: a metadata filter (doc_type, year) chosen from the question; a faster cross-encoder (e.g. a
  MiniLM reranker) or a smaller rerank pool; repeating the table title in every row group of a long
  table for the vector side.
