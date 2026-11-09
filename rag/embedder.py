"""Embedders: text -> vector. Anything with this small interface can be used (and swapped).

Documents and queries are embedded by separate methods, because retrieval models are often
trained asymmetrically: bge-small-en-v1.5 expects a short instruction in front of a *query*
("Represent this sentence for searching relevant passages: "), but not in front of the
passages it searches. Putting the prefix on documents too would make them look like queries.
"""
from typing import Protocol


class Embedder(Protocol):
    name: str          # stored with the index; a different name means a different vector space
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class BgeSmallEmbedder:
    """BAAI/bge-small-en-v1.5 via sentence-transformers, on CPU, vectors normalised to length 1
    (so cosine similarity = dot product). Free and local: the model (~130 MB) is downloaded
    once from Hugging Face into the local cache on first use; no paid API."""

    MODEL = "BAAI/bge-small-en-v1.5"
    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
    MAX_TOKENS = 512
    dim = 384

    def __init__(self, model: str = MODEL, device: str = "cpu", batch_size: int = 32):
        self.name = model
        self.device, self.batch_size = device, batch_size
        self._model = None

    @property
    def model(self):
        if self._model is None:  # loaded on first use, so importing this module is cheap
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.name, device=self.device)
            self._model.max_seq_length = self.MAX_TOKENS
            self.dim = self._model.get_sentence_embedding_dimension()
        return self._model

    def count_tokens(self, text: str) -> int:
        """Tokens the model sees for this text, special tokens ([CLS], [SEP]) included."""
        return len(self.model.tokenizer(text, add_special_tokens=True, truncation=False,
                                        verbose=False)["input_ids"])

    count_tokens.counter_name = f"tokenizer:{MODEL}"

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                    convert_to_numpy=True, show_progress_bar=False)
        return vectors.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts) if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.QUERY_INSTRUCTION + text])[0]
