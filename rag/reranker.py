"""Rerankers: score (question, passage) pairs. Anything with this small interface can be used.

A reranker is a *cross-encoder*: it reads the question and one passage together, in one pass of
the model, and gives one relevance score. That is more accurate than comparing two separately
made vectors (which is what the vector search does), but much slower, because the model runs
once per passage. So it is only used on a short list (the top 30 of the hybrid search).
"""
from typing import Protocol


class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class BgeReranker:
    """BAAI/bge-reranker-base via sentence-transformers' CrossEncoder, on CPU. Free and local:
    the model (~1.1 GB) is downloaded once into the Hugging Face cache on first use.

    The model reads at most 512 tokens, and that limit covers the question AND the passage
    together (plus 3 special tokens). Longer pairs are cut from the end of the longer side,
    i.e. the passage: a 500-token chunk with a 20-token question loses its last few lines."""

    MODEL = "BAAI/bge-reranker-base"
    MAX_TOKENS = 512

    def __init__(self, model: str = MODEL, device: str = "cpu", batch_size: int = 16):
        self.name = model
        self.device, self.batch_size = device, batch_size
        self._model = None

    @property
    def model(self):
        if self._model is None:  # loaded on first use, so importing this module is cheap
            from sentence_transformers import CrossEncoder
            self._model = CrossEncoder(self.name, device=self.device, max_length=self.MAX_TOKENS)
        return self._model

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        scores = self.model.predict([(query, p) for p in passages], batch_size=self.batch_size,
                                    show_progress_bar=False, convert_to_numpy=True)
        return [float(s) for s in scores]
