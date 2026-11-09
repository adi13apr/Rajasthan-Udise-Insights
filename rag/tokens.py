"""Token counting. The chunker takes any function text -> number of tokens.

In real runs the counter is the embedding model's own tokenizer (BgeSmallEmbedder.count_tokens),
so "512 tokens" means exactly what the model will read. Tests use estimate_tokens, a word-count
estimate that needs no model download.
"""
import math
from typing import Callable

TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """About 1.3 tokens per word (English subword tokenizers), plus the 2 special tokens."""
    return math.ceil(len(text.split()) * 1.3) + 2


def counter_name(count_tokens: TokenCounter) -> str:
    """A stable name for a counter, stored in the ingest state (a new counter = re-chunk)."""
    return getattr(count_tokens, "counter_name", None) or getattr(
        count_tokens, "__qualname__", type(count_tokens).__name__)
