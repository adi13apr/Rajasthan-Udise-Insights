"""Run traces of the agent (RAG phase 4): one JSON line per step, appended to a JSONL file.

    {"run_id": "...", "ts": "...", "question": "...", "node": "sql.generate", "input": "...",
     "output": "...", "latency_ms": 812.4, "prompt_tokens": 2104, "completion_tokens": 96,
     "error": null, ...}

The file is `UDISE_TRACE_FILE`, default data/rag/traces/agent_runs.jsonl (gitignored with the
rest of data/rag/). Summaries are short on purpose: the SQL result is logged as its row count
and column names, never its rows, and document chunks as their citations. Read with pandas or
DuckDB: `select node, avg(latency_ms) from read_json_auto('.../agent_runs.jsonl') group by 1`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT

DEFAULT_TRACE_FILE = ROOT / "data" / "rag" / "traces" / "agent_runs.jsonl"
MAX_SUMMARY = 300


def default_trace_file() -> Path:
    env = os.environ.get("UDISE_TRACE_FILE")
    return Path(env) if env else DEFAULT_TRACE_FILE


def short(text, limit: int = MAX_SUMMARY) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[:limit - 3] + "..."


class Tracer:
    """Collects the events of one run in memory (`events`) and appends each to the JSONL file
    as soon as it happens, so a crash still leaves the steps before it. `path=None` keeps the
    events in memory only (tests)."""

    def __init__(self, question: str, path: Path | None = None, run_id: str | None = None):
        self.question = question
        self.path = Path(path) if path else None
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, node: str, input: str = "", output: str = "", latency_ms: float | None = None,
              prompt_tokens: int | None = None, completion_tokens: int | None = None,
              error: str | None = None, **extra) -> dict:
        e = {"run_id": self.run_id, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             "question": short(self.question, 200), "node": node, "input": short(input),
             "output": short(output), "latency_ms": latency_ms, "prompt_tokens": prompt_tokens,
             "completion_tokens": completion_tokens, "error": short(error) if error else None, **extra}
        with self.lock:
            self.events.append(e)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        return e

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)
