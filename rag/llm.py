"""LLM client for the agent (RAG phase 4): the OpenAI Python SDK pointed at any
OpenAI-compatible endpoint (OpenAI, Azure OpenAI, Google Gemini, Groq, a local server).

Settings come from environment variables, read from the gitignored `.env` at the repo root
(see `.env.example`); the key is never in the code or the repo:

    LLM_BASE_URL, LLM_MODEL, LLM_API_KEY        where to send requests, which model, the key
    LLM_TPM_LIMIT, LLM_RPM_LIMIT                 the key's tokens / requests per minute (0 = none)
    LLM_TIMEOUT_S                                seconds before one call is abandoned
    LLM_REASONING_EFFORT                         optional (none / low / medium ...)
    LLM_JSON_MODE                                json_schema (default) | json_object | none

Every call asks for JSON and the reply is validated with a Pydantic model
(`complete_json`). Invalid JSON is not "fixed" here: it raises `LLMOutputInvalid` with the
validation error, and the agent decides whether it has budget for one retry.

Rate limits, two layers:
1. **Before the call** a rolling 60-second window (`RateLimiter`) waits if this call would go
   over the tokens-per-minute or requests-per-minute limit. It counts the real usage the
   provider reports, so it does not need the provider's tokenizer.
2. **If the provider still says 429** (or 500/503: overloaded), wait the time it asks for
   (Retry-After header, or Gemini's `retryDelay`), else 2, 4, 8 s, never more than 20 s per
   wait, at most 3 retries; then raise `LLMRateLimited` instead of crashing the program.
The SDK's own retries are switched off (`max_retries=0`) so that only this logic runs and the
trace shows every attempt.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .config import ROOT

ENV_FILE = ROOT / ".env"
JSON_MODES = ("json_schema", "json_object", "none")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Any failure of an LLM call; the message says what happened."""


class LLMConfigError(LLMError):
    """Settings are missing (no key, no model)."""


class LLMRateLimited(LLMError):
    """The provider kept answering 429 / overloaded after the allowed retries."""


class LLMTimeout(LLMError):
    """The call took longer than LLM_TIMEOUT_S."""


class LLMOutputInvalid(LLMError):
    """The reply was not valid JSON for the expected schema. `raw` holds the reply."""

    def __init__(self, message: str, raw: str = "", usage: "LLMUsage | None" = None):
        super().__init__(message)
        self.raw, self.usage = raw, usage


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0   # includes the model's thinking tokens, if it reports them
    reasoning_tokens: int = 0    # the thinking part of completion_tokens (0 if not reported)
    latency_ms: float = 0.0
    attempts: int = 1            # 1 + the number of rate-limit retries
    waited_s: float = 0.0        # time spent waiting for the rate limiter or a backoff
    model: str = ""


def load_env(env_file: Path | None = ENV_FILE) -> None:
    """Read `.env` into the environment; variables already set win."""
    if env_file is not None and Path(env_file).exists():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass
class LLMSettings:
    api_key: str
    model: str
    base_url: str | None = None
    timeout_s: float = 60.0
    tpm_limit: int = 0
    rpm_limit: int = 0
    reasoning_effort: str | None = None
    json_mode: str = "json_schema"
    temperature: float = 0.0
    max_output_tokens: int = 4096   # thinking models count their thinking here too
    max_rate_limit_retries: int = 3
    max_backoff_s: float = 20.0

    @classmethod
    def from_env(cls, env_file: Path | None = ENV_FILE) -> "LLMSettings":
        """Read the settings; variables already set in the environment win over `.env`."""
        load_env(env_file)
        key =os.environ.get("LLM_API_KEY", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip()
        if not key or not model:
            missing = [n for n, v in (("LLM_API_KEY", key), ("LLM_MODEL", model)) if not v]
            raise LLMConfigError(f"{', '.join(missing)} not set: copy .env.example to .env and fill it in")
        json_mode = os.environ.get("LLM_JSON_MODE", "").strip() or "json_schema"
        if json_mode not in JSON_MODES:
            raise LLMConfigError(f"LLM_JSON_MODE must be one of {', '.join(JSON_MODES)}")
        return cls(api_key=key, model=model,
                   base_url=os.environ.get("LLM_BASE_URL", "").strip() or None,
                   timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "").strip() or 60),
                   tpm_limit=_int_env("LLM_TPM_LIMIT", 0), rpm_limit=_int_env("LLM_RPM_LIMIT", 0),
                   reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "").strip() or None,
                   max_output_tokens=_int_env("LLM_MAX_OUTPUT_TOKENS", 4096),
                   json_mode=json_mode)


def estimate_tokens(text: str) -> int:
    """Rough token count for the rate limiter only (about 4 characters per token)."""
    return math.ceil(len(text) / 4)


class RateLimiter:
    """Rolling 60-second window of (time, tokens) per request. `acquire(n)` waits until a request
    of n tokens fits under both limits, then reserves it; `settle` replaces the estimate with the
    real usage. A request larger than the whole TPM limit waits for an empty window and then goes
    (it can never fit, and waiting forever would hang). Clock and sleep are injectable for tests."""

    WINDOW_S = 60.0

    def __init__(self, tpm: int = 0, rpm: int = 0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.tpm, self.rpm, self.clock, self.sleep = tpm, rpm, clock, sleep
        self.events: deque[list] = deque()      # [time, tokens]
        self.lock = threading.Lock()

    def _drop_old(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= self.WINDOW_S:
            self.events.popleft()

    def _wait_needed(self, tokens: int, now: float) -> float:
        self._drop_old(now)
        if not self.events:
            return 0.0
        used = sum(e[1] for e in self.events)
        over_tpm = self.tpm and used + tokens > self.tpm
        over_rpm = self.rpm and len(self.events) + 1 > self.rpm
        if not over_tpm and not over_rpm:
            return 0.0
        # Wait until enough old requests leave the window.
        if over_rpm and not over_tpm:
            k = len(self.events) + 1 - self.rpm
        else:
            k, freed = 0, 0
            need = used + tokens - self.tpm
            for e in self.events:
                k += 1
                freed += e[1]
                if freed >= need:
                    break
            if over_rpm:
                k = max(k, len(self.events) + 1 - self.rpm)
        return max(0.0, self.events[k - 1][0] + self.WINDOW_S - now) + 0.01

    def acquire(self, tokens: int) -> tuple[list, float]:
        """Wait until the request fits, reserve it; returns (reservation, seconds waited)."""
        waited = 0.0
        while True:
            with self.lock:
                now = self.clock()
                wait = self._wait_needed(tokens, now)
                if wait <= 0:
                    entry = [now, tokens]
                    self.events.append(entry)
                    return entry, waited
            self.sleep(wait)
            waited += wait

    def settle(self, entry: list, tokens: int) -> None:
        with self.lock:
            entry[1] = tokens


_RETRY_DELAY = re.compile(r'"?retryDelay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', re.I)


def retry_after_seconds(err: Exception) -> float | None:
    """The wait a 429/503 asks for: the Retry-After header, else Gemini's retryDelay ("23s")."""
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    m = _RETRY_DELAY.search(str(getattr(err, "body", "")) + " " + str(err))
    return float(m.group(1)) if m else None


def provider_message(err: Exception, limit: int = 300) -> str:
    """The provider's own error text ("model X is no longer available..."), not just the code.
    OpenAI sends {"error": {"message": ...}}; Gemini's compatible endpoint a list of those."""
    body = getattr(err, "body", None)
    items = body if isinstance(body, list) else [body]
    for item in items:
        if isinstance(item, dict):
            inner = item.get("error", item)
            msg = inner.get("message") if isinstance(inner, dict) else None
            if msg:
                return " ".join(str(msg).split())[:limit]
    return " ".join(str(err).split())[:limit]


def strip_fences(text: str) -> str:
    """Some providers wrap JSON in ```json ... ``` even in JSON mode."""
    t = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    return m.group(1) if m else t


def parse_json_reply(raw: str, schema: type[T]) -> T:
    try:
        return schema.model_validate_json(strip_fences(raw or ""))
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'reply'}: {x['msg']}"
                             for x in e.errors()[:5])
        raise LLMOutputInvalid(f"reply is not valid {schema.__name__} JSON: {problems}", raw) from None


class OpenAICompatibleLLM:
    """`complete_json(messages, schema)` -> (validated object, LLMUsage)."""

    def __init__(self, settings: LLMSettings | None = None, client=None,
                 limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings or LLMSettings.from_env()
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url,
                            timeout=self.settings.timeout_s, max_retries=0)
        self.client = client
        self.limiter = limiter or RateLimiter(self.settings.tpm_limit, self.settings.rpm_limit)
        self.sleep = sleep

    @property
    def model(self) -> str:
        return self.settings.model

    def _response_format(self, schema: type[BaseModel]) -> dict | None:
        mode = self.settings.json_mode
        if mode == "json_schema":
            return {"type": "json_schema",
                    "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}
        if mode == "json_object":
            return {"type": "json_object"}
        return None

    def complete_json(self, messages: list[dict], schema: type[T],
                      max_output_tokens: int | None = None) -> tuple[T, LLMUsage]:
        import openai

        s = self.settings
        max_output_tokens = max_output_tokens or s.max_output_tokens
        kwargs = {"model": s.model, "messages": messages, "temperature": s.temperature,
                  "max_completion_tokens": max_output_tokens}
        fmt = self._response_format(schema)
        if fmt:
            kwargs["response_format"] = fmt
        if s.reasoning_effort:
            kwargs["reasoning_effort"] = s.reasoning_effort

        estimate = sum(estimate_tokens(m.get("content") or "") for m in messages) + max_output_tokens
        usage = LLMUsage(model=s.model, attempts=0)
        t0 = time.perf_counter()
        for attempt in range(s.max_rate_limit_retries + 1):
            usage.attempts += 1
            entry, waited = self.limiter.acquire(estimate)
            usage.waited_s += waited
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except openai.APITimeoutError:
                self.limiter.settle(entry, 0)
                raise LLMTimeout(f"no reply within {s.timeout_s:g} s") from None
            except (openai.RateLimitError, openai.InternalServerError) as e:
                self.limiter.settle(entry, 0)
                kind = "rate limit" if isinstance(e, openai.RateLimitError) else "provider overloaded"
                asked = retry_after_seconds(e)
                if asked is not None and asked > s.max_backoff_s:
                    # e.g. a daily quota (Gemini free tier: retryDelay of hours): retrying now
                    # would only spend more requests.
                    raise LLMRateLimited(f"{kind}: the provider asks to wait {asked:,.0f} s, more than "
                                         f"the {s.max_backoff_s:g} s cap, so not retrying: "
                                         f"{provider_message(e, 200)}") from None
                if attempt == s.max_rate_limit_retries:
                    raise LLMRateLimited(f"{kind} after {usage.attempts} attempts: "
                                         f"{provider_message(e, 200)}") from None
                wait = asked if asked is not None else min(s.max_backoff_s, 2.0 * 2 ** attempt)
                self.sleep(wait)
                usage.waited_s += wait
                continue
            except openai.APIConnectionError as e:
                self.limiter.settle(entry, 0)
                raise LLMError(f"cannot reach {s.base_url or 'the API'}: {e}") from None
            except openai.APIStatusError as e:   # 400, 401, 403, 404: retrying will not help
                self.limiter.settle(entry, 0)
                raise LLMError(f"HTTP {e.status_code}: {provider_message(e)}") from None
            break
        usage.latency_ms = round((time.perf_counter() - t0) * 1000 - usage.waited_s * 1000, 1)
        u = getattr(resp, "usage", None)
        usage.prompt_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
        usage.completion_tokens = int(getattr(u, "completion_tokens", 0) or 0)
        details = getattr(u, "completion_tokens_details", None)
        usage.reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
        self.limiter.settle(entry, (usage.prompt_tokens + usage.completion_tokens) or estimate)
        choice = resp.choices[0] if resp.choices else None
        raw = (choice.message.content if choice and choice.message else None) or ""
        reason = getattr(choice, "finish_reason", None)
        if reason == "length":
            raise LLMOutputInvalid(f"reply cut off at the output limit of {max_output_tokens} tokens "
                                   f"({usage.reasoning_tokens} of them thinking); raise LLM_MAX_OUTPUT_TOKENS "
                                   "or lower LLM_REASONING_EFFORT", raw, usage)
        if not raw.strip():
            raise LLMOutputInvalid(f"empty reply (finish_reason={reason})", raw, usage)
        try:
            obj = parse_json_reply(raw, schema)
        except LLMOutputInvalid as e:
            e.usage = usage
            raise
        return obj, usage


def describe_schema(schema: type[BaseModel]) -> str:
    """One-line JSON shape for the prompt (providers without json_schema support need it)."""
    props = schema.model_json_schema().get("properties", {})

    def kind(p: dict) -> str:
        if "anyOf" in p:                       # Optional[...] -> "a" | "b" | null
            return " | ".join(kind(x) for x in p["anyOf"])
        if "enum" in p:
            return " | ".join(json.dumps(v) for v in p["enum"])
        if p.get("type") == "array":
            items = p.get("items", {})
            return "[" + (" | ".join(json.dumps(v) for v in items["enum"]) if "enum" in items
                          else items.get("type") or "string") + ", ...]"
        return p.get("type", "string")

    return "{" + ", ".join(f'"{name}": {kind(p)}' for name, p in props.items()) + "}"
