"""Retrieval evaluation: run every question of retrieval_questions.jsonl through the four modes.

Usage:  python -m rag.evals.run_retrieval --check   (validate the questions against the index,
                                                     no search; allowed before review)
        python -m rag.evals.run_retrieval           (the evaluation; refuses to run while any
                                                     question still has needs_review: true)

A retrieved chunk is *relevant* when it comes from the expected document and its pages
(page..page_end) overlap the expected pages. For a Markdown document (no pages) the expected
`section` text must appear in the chunk's section path.

Metrics per mode (all on the same questions, same index, CPU):
  recall@5   share of questions with at least one relevant chunk in the top 5
             (a "hit rate": one relevant chunk is enough to answer)
  MRR@10     mean over questions of 1 / rank of the first relevant chunk in the top 10
             (0 if none): rewards putting the right chunk first, not just somewhere
  p50 / p95  latency of one search call, in seconds, after one warm-up query per mode (so
             loading the models is not counted)
hybrid_rerank returns 5 results in normal use; for MRR@10 it is asked for 10 (the top 10 of
the 30 it reranks), which costs the same: all 30 are scored either way.

Writes reports/rag_retrieval_eval.json (every question's rank of the first relevant chunk per
mode) and the results block of rag/evals/RETRIEVAL_RESULTS.md, then applies the selection
rule written in that file *before* the run (choose_default below is that rule, as code).
"""
import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config as C

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "retrieval_questions.jsonl"
RESULTS_MD = HERE / "RETRIEVAL_RESULTS.md"
REPORT = C.ROOT / "reports" / "rag_retrieval_eval.json"
TYPES = ("definition", "figure", "exact_code", "paraphrase")
MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")
START, END = "<!-- results:start -->", "<!-- results:end -->"


# --- questions -------------------------------------------------------------------------

def load_questions(path: Path = QUESTIONS) -> list[dict]:
    """Read and check the questions file; every problem is listed at once."""
    questions, problems, ids = [], [], set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"line {n}: not JSON ({e})")
            continue
        where = f"line {n} ({q.get('id', 'no id')})"
        for key in ("id", "question", "type", "expected"):
            if not q.get(key):
                problems.append(f"{where}: missing {key}")
        if q.get("id") in ids:
            problems.append(f"{where}: duplicate id")
        ids.add(q.get("id"))
        if q.get("type") and q["type"] not in TYPES:
            problems.append(f"{where}: type must be one of {', '.join(TYPES)}")
        for e in q.get("expected") or []:
            if not e.get("file") or not (e.get("pages") or e.get("section")):
                problems.append(f"{where}: each expected item needs file and pages (or section)")
        if not isinstance(q.get("needs_review"), bool):
            problems.append(f"{where}: needs_review must be true or false")
        questions.append(q)
    if problems:
        raise ValueError(f"{path}:\n  " + "\n  ".join(problems))
    return questions


def is_relevant(meta: dict, expected: list[dict]) -> bool:
    for e in expected:
        if meta.get("file") != e["file"]:
            continue
        if e.get("pages"):
            first, last = meta.get("page", 0), meta.get("page_end", meta.get("page", 0))
            if any(first <= p <= last for p in e["pages"]):
                return True
        elif e.get("section") and e["section"] in meta.get("section", ""):
            return True
    return False


def first_relevant_rank(metas: list[dict], expected: list[dict]) -> int | None:
    for rank, meta in enumerate(metas, start=1):
        if is_relevant(meta, expected):
            return rank
    return None


# --- metrics ---------------------------------------------------------------------------

def recall_at(ranks: list[int | None], k: int) -> float:
    return sum(r is not None and r <= k for r in ranks) / len(ranks) if ranks else 0.0


def mrr_at(ranks: list[int | None], k: int) -> float:
    return sum(1 / r for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100): an actual measured value, no interpolation."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, -(-q * len(ordered) // 100) - 1))]


def summarise(ranks: list[int | None], seconds: list[float]) -> dict:
    return {"recall@5": recall_at(ranks, 5), "mrr@10": mrr_at(ranks, 10),
            "p50_s": percentile(seconds, 50), "p95_s": percentile(seconds, 95),
            "n": len(ranks)}


def choose_default(m: dict) -> tuple[str, list[str]]:
    """The selection rule of RETRIEVAL_RESULTS.md, written before the run, as code.
    `m` maps mode -> {"recall@5": 0..1, "p95_s": seconds}. Returns (mode, reasons).

    1. The base is hybrid. If bm25 or vector has a recall@5 at least 5 points above hybrid's,
       the base is the better of the two (a tie goes to bm25: it needs no model).
    2. hybrid_rerank is chosen only if its recall@5 is at least 5 points above the base's AND
       its p95 latency is under 1.5 s. Otherwise the base is the default."""
    pts = {mode: round(100 * m[mode]["recall@5"], 6) for mode in MODES}
    reasons = []
    best_single = max(("bm25", "vector"), key=lambda x: (pts[x], x == "bm25"))
    gain = pts[best_single] - pts["hybrid"]
    if gain >= 5:
        base = best_single
        reasons.append(f"step 1: {best_single} recall@5 is {gain:.1f} points above hybrid "
                       f"(>= 5): the base is {best_single}")
    else:
        base = "hybrid"
        reasons.append(f"step 1: the best single mode ({best_single}) is {gain:+.1f} points "
                       "against hybrid (< +5): the base is hybrid")
    gain = pts["hybrid_rerank"] - pts[base]
    p95 = m["hybrid_rerank"]["p95_s"]
    if gain >= 5 and p95 < 1.5:
        reasons.append(f"step 2: hybrid_rerank recall@5 is {gain:.1f} points above {base} "
                       f"(>= 5) and its p95 is {p95:.3f} s (< 1.5 s): hybrid_rerank")
        return "hybrid_rerank", reasons
    why = [f"recall@5 is {gain:+.1f} points against {base} ({'>= 5' if gain >= 5 else '< +5'})",
           f"p95 is {p95:.3f} s ({'< 1.5 s' if p95 < 1.5 else 'not under 1.5 s'})"]
    reasons.append(f"step 2: hybrid_rerank not chosen: {'; '.join(why)}: {base}")
    return base, reasons


# --- run -------------------------------------------------------------------------------

def check(questions: list[dict], retriever) -> list[str]:
    """Every expected document is in the index and every expected page has a chunk."""
    have = {}
    for _, meta in retriever.chunks.values():
        have.setdefault(meta["file"], set()).update(
            range(meta.get("page", 0), meta.get("page_end", meta.get("page", 0)) + 1))
    problems = []
    for q in questions:
        for e in q["expected"]:
            if e["file"] not in have:
                problems.append(f"{q['id']}: {e['file']} is not in the index")
            for p in e.get("pages") or []:
                if p not in have.get(e["file"], set()):
                    problems.append(f"{q['id']}: {e['file']} has no chunk on page {p}")
    return problems


def run(questions: list[dict], retriever, modes=MODES) -> dict:
    per_mode = {}
    for mode in modes:
        retriever.search("warm-up: number of schools", mode=mode, k=10)   # loads the models
        ranks, seconds, rows = [], [], []
        for q in questions:
            t0 = time.perf_counter()
            hits = retriever.search(q["question"], mode=mode, k=10)
            dt = time.perf_counter() - t0
            rank = first_relevant_rank([h.metadata for h in hits], q["expected"])
            ranks.append(rank)
            seconds.append(dt)
            rows.append({"id": q["id"], "type": q["type"], "rank": rank, "seconds": round(dt, 4),
                         "top5": [f"{h.metadata.get('file')} {h.metadata.get('location')}"
                                  for h in hits[:5]]})
        by_type = {}
        for t in TYPES:
            idx = [i for i, q in enumerate(questions) if q["type"] == t]
            if idx:
                by_type[t] = {"n": len(idx), "recall@5": recall_at([ranks[i] for i in idx], 5),
                              "mrr@10": mrr_at([ranks[i] for i in idx], 10)}
        per_mode[mode] = {**summarise(ranks, seconds), "by_type": by_type, "questions": rows}
        print(f"{mode:14s} recall@5 {per_mode[mode]['recall@5']:.3f}  "
              f"MRR@10 {per_mode[mode]['mrr@10']:.3f}  p50 {per_mode[mode]['p50_s']:.3f} s  "
              f"p95 {per_mode[mode]['p95_s']:.3f} s", file=sys.stderr)
    return per_mode


def machine() -> dict:
    info = {"platform": platform.platform(), "processor": platform.processor(),
            "python": platform.python_version()}
    try:
        import torch
        info.update(torch=torch.__version__, torch_threads=torch.get_num_threads(),
                    device="cpu")
    except ImportError:
        pass
    return info


def results_block(per_mode: dict, choice: str, reasons: list[str], meta: dict) -> str:
    lines = [START, "",
             f"Run: {meta['run_at']} on {meta['machine']['processor'] or meta['machine']['platform']}"
             f" (CPU, torch threads {meta['machine'].get('torch_threads', '?')}), "
             f"{meta['questions']} questions, index {meta['index_records']} chunks.", "",
             "| Mode | recall@5 | MRR@10 | p50 latency | p95 latency |",
             "|---|---|---|---|---|"]
    for mode in MODES:
        m = per_mode[mode]
        lines.append(f"| `{mode}` | {100 * m['recall@5']:.1f}% | {m['mrr@10']:.3f} | "
                     f"{m['p50_s']:.3f} s | {m['p95_s']:.3f} s |")
    lines += ["", "recall@5 by question type:", "",
              "| Type | n | " + " | ".join(f"`{m}`" for m in MODES) + " |",
              "|---|---|" + "---|" * len(MODES)]
    for t in TYPES:
        if t in per_mode["bm25"]["by_type"]:
            n = per_mode["bm25"]["by_type"][t]["n"]
            cells = [f"{100 * per_mode[m]['by_type'][t]['recall@5']:.0f}%" for m in MODES]
            lines.append(f"| {t} | {n} | " + " | ".join(cells) + " |")
    lines += ["", "Rank of the first relevant chunk (— = not in the top 10):", "",
              "| Question | Type | " + " | ".join(f"`{m}`" for m in MODES) + " |",
              "|---|---|" + "---|" * len(MODES)]
    for i, row in enumerate(per_mode["bm25"]["questions"]):
        ranks = [per_mode[m]["questions"][i]["rank"] for m in MODES]
        lines.append(f"| {row['id']} | {row['type']} | "
                     + " | ".join("—" if r is None else str(r) for r in ranks) + " |")
    lines += ["", "**Rule applied (as written above, by `choose_default`):**", ""]
    lines += [f"- {r}" for r in reasons]
    lines += ["", f"**Default mode: `{choice}`**", "", END]
    return "\n".join(lines)


def write_results(block: str, path: Path = RESULTS_MD) -> None:
    text = path.read_text(encoding="utf-8")
    if START in text and END in text:
        text = text[:text.index(START)] + block + text[text.index(END) + len(END):]
    else:
        text = text.rstrip("\n") + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    from ..retrieve import Retriever

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=Path, default=QUESTIONS)
    parser.add_argument("--index-dir", type=Path, default=C.INDEX_DIR)
    parser.add_argument("--check", action="store_true",
                        help="only validate the questions against the index")
    args = parser.parse_args(argv)

    questions = load_questions(args.questions)
    retriever = Retriever(args.index_dir)
    problems = check(questions, retriever)
    if problems:
        print("Questions do not match the index:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if args.check:
        pending = sum(q["needs_review"] for q in questions)
        print(f"{len(questions)} questions OK against {len(retriever.ids)} chunks "
              f"({pending} still need review)")
        return 0
    pending = [q["id"] for q in questions if q["needs_review"]]
    if pending:
        print(f"{len(pending)} questions still have needs_review: true ({', '.join(pending)}). "
              "Review them first.", file=sys.stderr)
        return 2
    if "Selection rule" not in RESULTS_MD.read_text(encoding="utf-8"):
        print("Write the selection rule in RETRIEVAL_RESULTS.md before running.", file=sys.stderr)
        return 2

    per_mode = run(questions, retriever)
    choice, reasons = choose_default(per_mode)
    meta = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "machine": machine(), "questions": len(questions), "index_records": len(retriever.ids),
            "index_dir": C.display_path(args.index_dir)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({**meta, "default_mode": choice, "rule": reasons,
                                  "modes": per_mode}, indent=2) + "\n", encoding="utf-8")
    write_results(results_block(per_mode, choice, reasons, meta))
    print(f"Default mode by the rule: {choice}")
    for r in reasons:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
