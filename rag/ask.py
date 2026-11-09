"""Ask the agent a question from the command line (RAG phase 4).

    python -m rag.ask "How many government schools did Rajasthan have in 2024-25?"
    python -m rag.ask "What does code 9 mean in yes/no columns?" --json

Prints the answer, the route and its reason, the citations, the SQL, the LLM calls, tokens and
latency per step. Settings come from .env (see .env.example); every run is also appended to the
trace file (UDISE_TRACE_FILE, default data/rag/traces/agent_runs.jsonl).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from .agent import Agent, AgentSettings
from .llm import LLMConfigError


def _fmt_ms(ms) -> str:
    return "-" if ms is None else (f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms")


def print_result(res, out=sys.stdout) -> None:
    w = lambda s="": print(s, file=out)
    w(f"Route:  {res.route or '-'}" + (f"  ({res.route_reason})" if res.route_reason else ""))
    w(f"Status: {res.status}")
    f = res.filters or {}
    w(f"Filters: year={f.get('year') or 'none'}, doc_type={f.get('doc_type') or 'none'}")
    w()
    w(res.answer)
    w()
    w("Citations used: " + ("; ".join(res.citations) if res.citations else "none"))
    if res.sources:
        w("Retrieved:      " + "; ".join(res.sources))
    if res.sql:
        w(f"SQL ({res.sql_rows if res.sql_rows is not None else 'no'} rows):")
        w("  " + res.sql.replace("\n", "\n  "))
    if res.sql_error:
        w(f"SQL error: {res.sql_error}")
    checks = [e for e in res.steps if e["node"] == "ground_check"]
    for e in checks:
        w(f"Ground check ({e['input']}): " + ("passed" if e.get("ok") else f"FAILED: {e['output']}"))
    if not checks:
        w("Ground check: not run (no answer to check)")
    w()
    per = defaultdict(lambda: [0.0, 0, 0, 0])
    for e in res.steps:
        p = per[e["node"]]
        p[0] += e.get("latency_ms") or 0
        p[1] += e.get("prompt_tokens") or 0
        p[2] += e.get("completion_tokens") or 0
        p[3] += 1
    w(f"{'step':22s} {'calls':>5s} {'latency':>9s} {'prompt tok':>10s} {'output tok':>10s}")
    for node, (ms, pt, ct, n) in per.items():
        w(f"{node:22s} {n:5d} {_fmt_ms(ms):>9s} {pt or '-':>10} {ct or '-':>10}")
    w(f"Total: {res.llm_calls} LLM calls, {res.prompt_tokens:,} prompt + {res.completion_tokens:,} "
      f"output tokens, {_fmt_ms(res.latency_ms)}  (run {res.run_id}"
      + (f", trace {res.trace_file})" if res.trace_file else ")"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ask the UDISE+ agent a question.")
    parser.add_argument("question")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--max-llm-calls", type=int, help="override AGENT_MAX_LLM_CALLS")
    parser.add_argument("--card", choices=("sliced", "full"), help="override LLM_SCHEMA_CARD")
    parser.add_argument("--no-trace", action="store_true", help="do not write the trace file")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = AgentSettings.from_env()
    if args.max_llm_calls is not None:
        settings.max_llm_calls = args.max_llm_calls
    if args.card:
        settings.card_mode = args.card
    try:
        agent = Agent(settings=settings, trace_file=not args.no_trace)
        res = agent.ask(args.question)
    except LLMConfigError as e:
        print(f"LLM settings: {e}", file=sys.stderr)
        return 2
    if args.json:
        d = res.as_dict()
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    else:
        print_result(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
