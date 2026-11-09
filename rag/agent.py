"""The question-answering agent (RAG phase 4): a LangGraph StateGraph over the phase 2
retriever and the phase 3 SQL tool.

    START -> route --docs--------> retrieve --------------\
                   --sql---------> sql (generate, run,     > answer -> ground_check --ok--> finalize -> END
                   --both--> retrieve + sql (in parallel) /     ^            |
                   --out_of_scope--> out_of_scope -> END        +-- once ----+ (numbers or citations not found)
                   (LLM failed) ----> fallback -> END                        +-- again ---> fallback -> END

Nodes, in plain words:
- route: one LLM call, structured output (RouteDecision: route, one-sentence reason, tables).
- retrieve: the phase 2 default mode (hybrid), top 5 chunks, with a timeout.
- sql: one LLM call writes the SQL from the schema card (only the sections for the chosen tables
  unless LLM_SCHEMA_CARD=full); SqlTool checks and runs it (aggregates only); on any error one
  repair call gets the error message and the failed SQL.
- answer: one LLM call over the evidence (documents and SQL result marked as untrusted data).
- ground_check: deterministic (rag/grounding.py): every number in the evidence, every citation a
  retrieved chunk. Fails once -> answer is regenerated with the list of problems; fails again ->
  fallback: "could not verify" plus the raw evidence.
- finalize: adds the executed SQL, the management-code note when management categories are
  involved, and the source line. These are added by code, so they cannot be forgotten.

Budgets: at most AGENT_MAX_LLM_CALLS (default 5) LLM calls per question, counting retries after
invalid JSON; when the budget runs out the agent stops calling and goes to the fallback. Timeouts:
LLM_TIMEOUT_S per LLM call, AGENT_RETRIEVE_TIMEOUT_S for retrieval, SqlTool's 10 s per query.

One SqlTool and one Retriever per process (`shared_sql_tool`, `shared_retriever`): DuckDB refuses
a second connection to the same file with different settings, and the retriever loads ~2,300
chunks and a model. Tests pass their own fakes to `Agent(...)`.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from . import grounding
from . import prompts as P
from .llm import LLMError, LLMOutputInvalid, OpenAICompatibleLLM, describe_schema, load_env
from .trace import Tracer, default_trace_file, short

# ------------------------------------------------------------------ shared resources
_shared_lock = threading.Lock()
_shared: dict[str, Any] = {}
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="rag-retrieve")


def shared_sql_tool():
    """The one SqlTool of this process (opened on first use)."""
    with _shared_lock:
        if "sql_tool" not in _shared:
            from .sql_tool import SqlTool
            _shared["sql_tool"] = SqlTool()
        return _shared["sql_tool"]


def shared_retriever():
    """The one Retriever of this process (index loaded on first use)."""
    with _shared_lock:
        if "retriever" not in _shared:
            from .retrieve import Retriever
            _shared["retriever"] = Retriever()
        return _shared["retriever"]


# ------------------------------------------------------------------ settings and run context
@dataclass
class AgentSettings:
    max_llm_calls: int = 5
    card_mode: str = "sliced"          # sliced | full
    retrieve_timeout_s: float = 120.0  # the first query also loads the embedding model
    retrieve_k: int = 5
    retrieve_mode: str | None = None   # None = rag.retrieve.DEFAULT_MODE (hybrid)
    prompt_rows: int = P.PROMPT_ROWS

    @classmethod
    def from_env(cls) -> "AgentSettings":
        load_env()
        card = os.environ.get("LLM_SCHEMA_CARD", "").strip() or "sliced"
        if card not in ("sliced", "full"):
            raise ValueError("LLM_SCHEMA_CARD must be sliced or full")
        return cls(max_llm_calls=int(os.environ.get("AGENT_MAX_LLM_CALLS", "").strip() or 5),
                   card_mode=card,
                   retrieve_timeout_s=float(os.environ.get("AGENT_RETRIEVE_TIMEOUT_S", "").strip() or 120))


class AgentStop(Exception):
    """No answer can be produced by the LLM any more (budget used up or the LLM failed)."""


class Budget:
    def __init__(self, max_calls: int):
        self.max_calls, self.used = max_calls, 0
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.used >= self.max_calls:
                return False
            self.used += 1
            return True

    @property
    def left(self) -> int:
        with self.lock:
            return self.max_calls - self.used


@dataclass
class RunContext:
    tracer: Tracer
    budget: Budget
    llm: Any
    settings: AgentSettings
    prompt_tokens: int = 0
    completion_tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class AgentState(TypedDict, total=False):
    question: str
    route: str
    route_reason: str
    tables: list[str]
    hits: list
    retrieve_error: str | None
    sql: str | None
    sql_result: Any
    sql_error: str | None
    sql_attempts: int
    card_tables: list[str]
    draft: str | None
    draft_raw: str | None
    insufficient: bool
    answer_attempts: int
    ground: Any
    stop_reason: str | None
    status: str
    final: str


@dataclass
class AgentResult:
    question: str
    answer: str
    status: str                      # ok | insufficient_evidence | unverified | out_of_scope
    route: str | None
    route_reason: str | None
    citations: list[str]             # citations that appear in the answer
    sources: list[str]               # every retrieved chunk's citation
    sql: str | None                  # the SQL that ran (or the last one tried)
    sql_error: str | None
    sql_rows: int | None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    run_id: str
    trace_file: str | None
    steps: list[dict]                # the trace events of this run
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# ------------------------------------------------------------------ helpers
_MGMT_SQL = re.compile(r"\b(is_government|management_group|management_key|management_code|"
                       r"dim_management|mart_district_year_management)\b", re.I)
_MGMT_TEXT = re.compile(r"\b(government|govt|private|management|non-government)\b", re.I)


def needs_management_note(sql: str | None, text: str, columns: list[str] | None = None) -> bool:
    """True when management categories are used: in the SQL, the result's columns or the answer."""
    return bool((sql and _MGMT_SQL.search(sql)) or _MGMT_TEXT.search(text or "")
                or any(_MGMT_SQL.search(c or "") for c in (columns or [])))


def _ctx(config) -> RunContext:
    return config["configurable"]["ctx"]


def call_llm(ctx: RunContext, step: str, messages: list[dict], schema, input_summary: str = ""):
    """One budgeted LLM call with a structured output. Invalid JSON gets one retry (if the budget
    allows) with the validation error. Raises AgentStop when no valid reply can be had."""
    attempt_messages = messages
    for retry in (False, True):
        name = f"{step}.json_retry" if retry else step
        if not ctx.budget.take():
            ctx.tracer.event(name, input_summary, error="LLM call budget used up")
            raise AgentStop(f"LLM call budget of {ctx.budget.max_calls} used up")
        t0 = time.perf_counter()
        try:
            obj, usage = ctx.llm.complete_json(attempt_messages, schema)
        except LLMOutputInvalid as e:
            u = e.usage   # the tokens were spent even though the reply is unusable
            if u is not None:
                with ctx.lock:
                    ctx.prompt_tokens += u.prompt_tokens
                    ctx.completion_tokens += u.completion_tokens
            ctx.tracer.event(name, input_summary, short(e.raw),
                             u.latency_ms if u else round((time.perf_counter() - t0) * 1000, 1),
                             u.prompt_tokens if u else None, u.completion_tokens if u else None,
                             error=str(e), reasoning_tokens=u.reasoning_tokens if u else None)
            if retry:
                raise AgentStop(f"{step}: invalid structured output twice ({e})") from None
            attempt_messages = messages + [
                {"role": "assistant", "content": e.raw or ""},
                {"role": "user", "content": f"That reply was not valid: {e}. Reply with JSON only: "
                                            f"{describe_schema(schema)}"}]
            continue
        except LLMError as e:
            ctx.tracer.event(name, input_summary, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                             error=f"{type(e).__name__}: {e}")
            raise AgentStop(f"{step}: {type(e).__name__}: {e}") from None
        with ctx.lock:
            ctx.prompt_tokens += usage.prompt_tokens
            ctx.completion_tokens += usage.completion_tokens
        ctx.tracer.event(name, input_summary, short(obj.model_dump_json()), usage.latency_ms,
                         usage.prompt_tokens, usage.completion_tokens,
                         reasoning_tokens=usage.reasoning_tokens, attempts=usage.attempts, waited_s=round(usage.waited_s, 2), model=usage.model)
        return obj
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ the agent
class Agent:
    """`Agent().ask(question)` -> AgentResult. Everything it needs can be injected for tests:
    `llm` (an object with complete_json(messages, schema)), `retriever` (search(q, mode, k)),
    `sql_tool` (run(sql, require_aggregate=True)). Defaults are the real ones, created lazily."""

    def __init__(self, llm=None, retriever=None, sql_tool=None, settings: AgentSettings | None = None,
                 trace_file: Path | str | None | bool = True):
        self._llm, self._retriever, self._sql_tool = llm, retriever, sql_tool
        self.settings = settings or AgentSettings.from_env()
        self.trace_file = (default_trace_file() if trace_file is True
                           else Path(trace_file) if trace_file else None)
        self.graph = self._build()

    # lazily created resources
    @property
    def llm(self):
        if self._llm is None:
            self._llm = OpenAICompatibleLLM()
        return self._llm

    @property
    def retriever(self):
        return self._retriever if self._retriever is not None else shared_retriever()

    @property
    def sql_tool(self):
        return self._sql_tool if self._sql_tool is not None else shared_sql_tool()

    # --- graph -----------------------------------------------------------------------
    def _build(self):
        g = StateGraph(AgentState)
        g.add_node("route", self.route_node)
        g.add_node("retrieve", self.retrieve_node)
        g.add_node("sql", self.sql_node)
        g.add_node("answer", self.answer_node)
        g.add_node("ground_check", self.ground_node)
        g.add_node("finalize", self.finalize_node)
        g.add_node("fallback", self.fallback_node)
        g.add_node("out_of_scope", self.out_of_scope_node)
        g.add_edge(START, "route")
        g.add_conditional_edges("route", self.after_route, ["retrieve", "sql", "out_of_scope", "fallback"])
        g.add_edge("retrieve", "answer")
        g.add_edge("sql", "answer")
        g.add_conditional_edges("answer", self.after_answer, ["ground_check", "fallback"])
        g.add_conditional_edges("ground_check", self.after_ground, ["finalize", "answer", "fallback"])
        g.add_edge("finalize", END)
        g.add_edge("fallback", END)
        g.add_edge("out_of_scope", END)
        return g.compile()

    # --- route -----------------------------------------------------------------------
    def route_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        try:
            d = call_llm(ctx, "route", P.route_messages(state["question"]), P.RouteDecision,
                         short(state["question"]))
        except AgentStop as e:
            return {"route": None, "stop_reason": str(e)}
        tables = list(d.tables) if d.route in ("sql", "both") else []
        return {"route": d.route, "route_reason": d.reason, "tables": tables}

    @staticmethod
    def after_route(state: AgentState):
        route = state.get("route")
        if route is None:
            return "fallback"
        if route == "out_of_scope":
            return "out_of_scope"
        if route == "docs":
            return "retrieve"
        if route == "sql":
            return "sql"
        return ["retrieve", "sql"]

    # --- retrieve --------------------------------------------------------------------
    def retrieve_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        s = self.settings
        from .retrieve import DEFAULT_MODE
        mode = s.retrieve_mode or DEFAULT_MODE
        t0 = time.perf_counter()
        try:
            future = _executor.submit(lambda: self.retriever.search(state["question"], mode, s.retrieve_k))
            hits = future.result(timeout=s.retrieve_timeout_s)
        except concurrent.futures.TimeoutError:
            err = f"retrieval took longer than {s.retrieve_timeout_s:g} s"
        except Exception as e:   # index missing, model failed to load...
            err = f"{type(e).__name__}: {e}"
        else:
            ctx.tracer.event("retrieve", f"mode={mode} k={s.retrieve_k}",
                             "; ".join(P.cite_for(h.metadata) for h in hits),
                             round((time.perf_counter() - t0) * 1000, 1), hits=len(hits))
            return {"hits": hits, "retrieve_error": None}
        ctx.tracer.event("retrieve", f"mode={mode}", latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                         error=err)
        return {"hits": [], "retrieve_error": err}

    # --- sql -------------------------------------------------------------------------
    def _run_sql(self, ctx: RunContext, sql: str):
        from .sql_tool import SqlExecutionError, SqlRejected
        t0 = time.perf_counter()
        try:
            result = self.sql_tool.run(sql, require_aggregate=True)
        except SqlRejected as e:
            err = f"rejected by the SQL checks: {e}"
        except SqlExecutionError as e:
            err = f"DuckDB error: {e}"
        except Exception as e:   # the database is missing, etc.
            err = f"{type(e).__name__}: {e}"
        else:
            ctx.tracer.event("sql.run", short(result.sql_executed, 200),
                             f"{result.row_count} rows; columns: {', '.join(result.columns)}",
                             round((time.perf_counter() - t0) * 1000, 1), rows=result.row_count,
                             truncated=result.truncated)
            return result, None
        ctx.tracer.event("sql.run", short(sql, 200), latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                         error=err)
        return None, err

    def sql_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        q = state["question"]
        card_text, tables = P.card_for(q, state.get("tables") or [], self.settings.card_mode)
        base = P.sql_messages(q, card_text)
        summary = f"card={self.settings.card_mode} tables={','.join(tables)} ({len(card_text.split())} words)"
        try:
            draft = call_llm(ctx, "sql.generate", base, P.SqlDraft, summary)
        except AgentStop as e:
            return {"sql": None, "sql_result": None, "sql_error": str(e), "sql_attempts": 0,
                    "card_tables": tables}
        sql, attempts = draft.sql, 1
        result, err = self._run_sql(ctx, sql)
        if err:
            try:
                fixed = call_llm(ctx, "sql.repair", P.sql_repair_messages(base, sql, err), P.SqlDraft,
                                 f"error: {short(err, 150)}")
            except AgentStop as e:
                err = f"{err} (no repair: {e})"
            else:
                sql, attempts = fixed.sql, 2
                result, err = self._run_sql(ctx, sql)
        return {"sql": result.sql_executed if result else sql, "sql_result": result, "sql_error": err,
                "sql_attempts": attempts, "card_tables": tables}

    # --- answer ----------------------------------------------------------------------
    def _answer_base(self, state: AgentState) -> list[dict]:
        return P.answer_messages(state["question"], state.get("hits") or [], state.get("sql_result"),
                                 state.get("sql"), state.get("sql_error"))

    def answer_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        hits, result = state.get("hits") or [], state.get("sql_result")
        if not hits and result is None:   # nothing to answer from: no LLM call
            errors = [e for e in (state.get("retrieve_error"), state.get("sql_error")) if e]
            return {"draft": None,
                    "stop_reason": ("no evidence: " + "; ".join(errors)) if errors else "no evidence found"}
        attempts = state.get("answer_attempts", 0)
        messages = self._answer_base(state)
        step = "answer"
        if attempts:
            messages = P.regenerate_messages(messages, state.get("draft_raw") or "",
                                             state["ground"].problems())
            step = "answer.regenerate"
        evidence = f"{len(hits)} chunks" + (f", {result.row_count} SQL rows" if result is not None else "")
        try:
            d = call_llm(ctx, step, messages, P.AnswerDraft, evidence)
        except AgentStop as e:
            return {"draft": None, "stop_reason": str(e), "answer_attempts": attempts + 1}
        return {"draft": d.answer, "draft_raw": d.model_dump_json(), "insufficient": d.insufficient_evidence,
                "answer_attempts": attempts + 1}

    @staticmethod
    def after_answer(state: AgentState) -> str:
        return "ground_check" if state.get("draft") else "fallback"

    # --- ground check ----------------------------------------------------------------
    def ground_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        t0 = time.perf_counter()
        hits, result = state.get("hits") or [], state.get("sql_result")
        cells = [c for row in (result.rows if result is not None else []) for c in row]
        g = grounding.check(
            state["draft"], sql_cells=cells, chunk_texts=[h.text for h in hits],
            extra_texts=[state["question"], state.get("sql") or ""] if result is not None else [state["question"]],
            allowed_citations=[P.cite_for(h.metadata) for h in hits],
            require_citation=state.get("route") == "docs" and bool(hits) and not state.get("insufficient"))
        ctx.tracer.event("ground_check", f"attempt {state.get('answer_attempts', 0)}, {g.numbers_checked} numbers",
                         "ok" if g.ok else "; ".join(g.problems()),
                         round((time.perf_counter() - t0) * 1000, 1), ok=g.ok)
        return {"ground": g}

    def after_ground(self, state: AgentState) -> str:
        if state["ground"].ok:
            return "finalize"
        if state.get("answer_attempts", 0) < 2:
            return "answer"
        return "fallback"

    # --- endings ---------------------------------------------------------------------
    def _addenda(self, state: AgentState, text: str) -> list[str]:
        result = state.get("sql_result")
        out = []
        if result is not None:
            out.append("SQL (computed from UDISE+ school-level data, run read-only on the gold tables):\n"
                       f"```sql\n{result.sql_executed}\n```")
        if needs_management_note(state.get("sql") if result is not None else None, text,
                                 result.columns if result is not None else None):
            out.append(P.MANAGEMENT_NOTE)
        if result is not None or state.get("hits"):
            out.append(P.SOURCE_LINE)
        return out

    def finalize_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        text = state["draft"]
        final = "\n\n".join([text] + self._addenda(state, text))
        status = "insufficient_evidence" if state.get("insufficient") else "ok"
        ctx.tracer.event("finalize", status, short(final))
        return {"final": final, "status": status}

    def fallback_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        g = state.get("ground")
        if g is not None and not g.ok:
            why = "; ".join(g.problems())
        else:
            why = state.get("stop_reason") or "no answer could be produced"
        parts = [f"I couldn't verify an answer to this question ({why}). Here is the raw evidence instead."]
        result, hits = state.get("sql_result"), state.get("hits") or []
        if result is not None:
            parts.append(f"Database result ({result.row_count} rows):\n" + P.format_rows(result.columns, result.rows))
        elif state.get("sql_error"):
            parts.append(f"The database query failed: {state['sql_error']}"
                         + (f"\nLast SQL tried:\n```sql\n{state['sql']}\n```" if state.get("sql") else ""))
        if hits:
            parts.append("Document excerpts:\n" + "\n".join(
                f"- {P.cite_for(h.metadata)} {short(h.text, P.SNIPPET_CHARS)}" for h in hits))
        elif state.get("retrieve_error"):
            parts.append(f"Document search failed: {state['retrieve_error']}")
        body = "\n\n".join(parts)
        final = "\n\n".join([body] + (self._addenda(state, body) if (result is not None or hits) else []))
        ctx.tracer.event("fallback", short(why), short(final))
        return {"final": final, "status": "unverified"}

    def out_of_scope_node(self, state: AgentState, config) -> dict:
        ctx = _ctx(config)
        final = f"{P.OUT_OF_SCOPE}\n\n(Router: {state.get('route_reason', '')})"
        ctx.tracer.event("out_of_scope", "", short(final))
        return {"final": final, "status": "out_of_scope"}

    # --- entry point -----------------------------------------------------------------
    def ask(self, question: str) -> AgentResult:
        question = " ".join(str(question).split())
        if not question:
            raise ValueError("empty question")
        tracer = Tracer(question, self.trace_file)
        ctx = RunContext(tracer, Budget(self.settings.max_llm_calls), self.llm, self.settings)
        state = self.graph.invoke({"question": question, "answer_attempts": 0},
                                  config={"configurable": {"ctx": ctx}, "recursion_limit": 25})
        final = state.get("final", "")
        hits = state.get("hits") or []
        sources = list(dict.fromkeys(P.cite_for(h.metadata) for h in hits))
        cited = [c for c in sources if grounding.normalise_citation(c) in
                 {grounding.normalise_citation(m) for m in re.findall(r"\[[^\[\]]+\]", final)}]
        result = state.get("sql_result")
        g = state.get("ground")
        res = AgentResult(
            question=question, answer=final, status=state.get("status", "unverified"),
            route=state.get("route"), route_reason=state.get("route_reason"), citations=cited,
            sources=sources, sql=state.get("sql"), sql_error=state.get("sql_error"),
            sql_rows=result.row_count if result is not None else None,
            llm_calls=ctx.budget.used, prompt_tokens=ctx.prompt_tokens,
            completion_tokens=ctx.completion_tokens, latency_ms=tracer.elapsed_ms(), run_id=tracer.run_id,
            trace_file=str(self.trace_file) if self.trace_file else None, steps=list(tracer.events),
            problems=g.problems() if g is not None and not g.ok else [])
        tracer.event("run", short(question), f"status={res.status} route={res.route}", res.latency_ms,
                     res.prompt_tokens, res.completion_tokens, llm_calls=res.llm_calls)
        return res
