"""Find the chunks that answer a question. Four modes, no LLM:

    bm25           keyword search (rank_bm25, BM25Okapi) over the full text of every chunk
    vector         nearest chunks to the question's bge vector in the ChromaDB index
    hybrid         both lists fused with Reciprocal Rank Fusion (k = 60)
    hybrid_rerank  the top 30 of hybrid re-scored by a cross-encoder (bge-reranker-base)

Usage:  python -m rag.retrieve "What does code 9 mean in yes/no columns?"
        python -m rag.retrieve "number of schools in Rajasthan 2024-25" --mode bm25 --k 10

Why two kinds of search. Keyword search (BM25) is exact: "code 9", "item_group 4" or "94"
must appear in the chunk. It cannot know that "pupils" means "students". Vector search
compares meanings, so paraphrases work, but a short code like "9" barely moves a vector, so
exact-code questions are its weak spot (phase 1: "What does code 9 mean?" missed). Fusing the
two lists gives a ranking that is good when either one is.

Reciprocal Rank Fusion. Each list votes 1 / (k + rank) for each chunk it returned (rank 1 =
best); a chunk's score is the sum of its votes. It uses ranks only, never the raw scores, so
it does not matter that BM25 scores (0 to ~30) and cosine similarities (0 to 1) are on
different scales. k = 60 (the value from the original paper) keeps the top few ranks from
dominating: rank 1 gives 1/61, rank 10 still gives 1/70.

BM25 text. The chunk text is indexed in full (Chroma stores it whole), so the two oversized
Item_group rows (522 and 525 tokens, cut at 512 for the vector) are searchable to the end. The
document title is put in front of it, because the 2024-25 and 2025-26 reports have the same
tables: the title's year is what tells them apart. The reranker reads the same title + text.

Tokenizer. Lower case; numbers and codes are kept, single digits included ("code 9" ->
code, 9); Indian-style thousands separators are dropped (1,06,302 -> 106302); identifiers and
ranges are kept whole and also split (item_group -> item_group, item, group; 2024-25 ->
2024-25, 2024, 25); decimals stay whole (24.5). A short list of English stopwords is removed;
numbers are never removed. No stemming: codes and table words must match as written.
"""
import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C

MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")
DEFAULT_MODE = "hybrid"   # picked by the rule in rag/evals/RETRIEVAL_RESULTS.md (eval of 2026-11-09)
RRF_K = 60                # Reciprocal Rank Fusion constant
CANDIDATES = 50           # how many results of each list go into the fusion
RERANK_POOL = 30          # how many fused results the reranker re-scores
TOP_K = 5                 # results returned

STOPWORDS = frozenset("""
a an and are as at be been but by can do does did for from had has have how i if in into is it
its me my not of on or our so than that the their them then there these they this those to was
were what when where which who whom why will with would you your
""".split())

_NUMBER_COMMA = re.compile(r"(?<=\d),(?=\d)")
_TOKEN = re.compile(r"[a-z0-9]+(?:[_\-/.][a-z0-9]+)*")
_DECIMAL = re.compile(r"\d+(\.\d+)+")
_PARTS = re.compile(r"[_\-/.]")


_ACADEMIC_YEAR = re.compile(r"^\d{4}-\d{2}$")   # 2024-25; '2026' or '' is not an academic year


@dataclass(frozen=True)
class Filters:
    """Metadata filters (phase 4): `year` is an academic year ('2024-25'), `doc_type` a manifest
    doc_type ('official_report'). None = no filter on that field.

    A year filter keeps the chunks of that academic year AND the chunks of documents that belong
    to no academic year (year blank, like the general metadata note, or a calendar year, like the
    project's Markdown docs, year 2026): they apply to every year. An empty Filters() filters
    nothing, and `search` then runs exactly the phase 2 code path."""
    year: str | None = None
    doc_type: str | None = None

    def is_empty(self) -> bool:
        return not self.year and not self.doc_type

    def matches(self, meta: dict) -> bool:
        if self.doc_type and meta.get("doc_type") != self.doc_type:
            return False
        if self.year:
            y = str(meta.get("year") or "")
            if _ACADEMIC_YEAR.match(y) and y != self.year:
                return False
        return True

    def as_dict(self) -> dict:
        return {"year": self.year, "doc_type": self.doc_type}


def tokenize(text: str) -> list[str]:
    """Words and codes of a text, for BM25 (the same function for chunks and questions)."""
    out = []
    for tok in _TOKEN.findall(_NUMBER_COMMA.sub("", text.lower())):
        if tok in STOPWORDS:
            continue
        out.append(tok)
        if not _DECIMAL.fullmatch(tok) and _PARTS.search(tok):
            out += [p for p in _PARTS.split(tok) if p and p not in STOPWORDS]
    return out


def rrf(rankings: dict[str, list[str]], k: int = RRF_K) -> list[tuple[str, float, dict[str, int]]]:
    """Reciprocal Rank Fusion of ranked id lists (best first).

    Returns (id, score, {list name: rank}) sorted by score, highest first. score = sum over the
    lists that contain the id of 1 / (k + rank), rank starting at 1. Ties (e.g. rank 1 in one
    list and rank 1 in the other) are broken by the best single rank, then by id, so the order
    is always the same."""
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in rankings.items():
        for rank, id_ in enumerate(ids, start=1):
            ranks.setdefault(id_, {}).setdefault(name, rank)   # first occurrence counts
    fused = [(id_, sum(1.0 / (k + r) for r in rs.values()), rs) for id_, rs in ranks.items()]
    fused.sort(key=lambda x: (-x[1], min(x[2].values()), x[0]))
    return fused


@dataclass
class Hit:
    id: str
    text: str                 # the chunk as stored (section heading + body)
    metadata: dict
    score: float              # the score of the mode that ranked it (bm25, cosine, rrf, rerank)
    rank: int                 # 1 = best
    scores: dict = field(default_factory=dict)   # every stage's score and rank, for debugging

    @property
    def citation(self) -> str:
        m = self.metadata
        return f"{m.get('title', m.get('file'))}, {m.get('location', '')}".rstrip(", ")


class Retriever:
    """Loads every chunk of the index once (ids, text, metadata) and builds the BM25 index in
    memory (about 2,300 chunks: well under a second). The embedder and the reranker are loaded
    only when a mode needs them, so `bm25` never loads a model."""

    def __init__(self, index_dir: Path = C.INDEX_DIR, embedder=None, reranker=None,
                 collection: str = C.COLLECTION, candidates: int = CANDIDATES,
                 rerank_pool: int = RERANK_POOL, rrf_k: int = RRF_K):
        import chromadb
        from chromadb.config import Settings
        from rank_bm25 import BM25Okapi

        self.index_dir = Path(index_dir)
        if not self.index_dir.exists():
            raise FileNotFoundError(f"no index at {self.index_dir}; run python -m rag.ingest")
        client = chromadb.PersistentClient(path=str(self.index_dir),
                                           settings=Settings(anonymized_telemetry=False))
        try:
            self.collection = client.get_collection(collection)
        except Exception as e:   # chromadb raises NotFoundError / ValueError depending on version
            raise FileNotFoundError(f"no collection {collection!r} in {self.index_dir}; "
                                    "run python -m rag.ingest") from e
        self.index_embedder = (self.collection.metadata or {}).get("embedder")
        self._embedder, self._reranker = embedder, reranker
        if embedder is not None:
            self._check_embedder(embedder)
        self.candidates, self.rerank_pool, self.rrf_k = candidates, rerank_pool, rrf_k

        got = self.collection.get(include=["documents", "metadatas"])
        rows = sorted(zip(got["ids"], got["documents"], got["metadatas"]))   # stable order
        if not rows:
            raise ValueError(f"collection {collection!r} in {self.index_dir} is empty")
        self.ids = [r[0] for r in rows]
        self.chunks = {r[0]: (r[1], r[2]) for r in rows}
        self.bm25 = BM25Okapi([tokenize(self.search_text(i)) for i in self.ids])

    # --- models, loaded on first use ----------------------------------------------------
    def _check_embedder(self, embedder) -> None:
        if embedder.name != self.index_embedder:
            raise RuntimeError(f"the index was built with {self.index_embedder!r}, not "
                               f"{embedder.name!r}: query vectors would not match")

    @property
    def embedder(self):
        if self._embedder is None:
            from .embedder import BgeSmallEmbedder
            self._embedder = BgeSmallEmbedder()
            self._check_embedder(self._embedder)
        return self._embedder

    @property
    def reranker(self):
        if self._reranker is None:
            from .reranker import BgeReranker
            self._reranker = BgeReranker()
        return self._reranker

    # --- the four modes -----------------------------------------------------------------
    def search_text(self, id_: str) -> str:
        """What BM25 and the reranker read: the document title, then the chunk."""
        text, meta = self.chunks[id_]
        return f"{meta.get('title', '')}\n{text}"

    def bm25_ranking(self, query: str, n: int, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """Top n (id, BM25 score); only chunks that share at least one word with the query.
        `allowed` (filters) keeps only those ids; the index and its IDF stay the full corpus's,
        so a chunk's score is the same with or without a filter."""
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        order = sorted(range(len(self.ids)), key=lambda i: (-scores[i], self.ids[i]))
        if allowed is not None:
            order = [i for i in order if self.ids[i] in allowed]
        return [(self.ids[i], float(scores[i])) for i in order[:n] if scores[i] > 0]

    def chroma_where(self, filters: Filters) -> dict | None:
        """The same filters as a ChromaDB `where`. The year rule ("that year or no academic
        year") becomes `year $in [...]` over the year values present in the index."""
        clauses = []
        if filters.doc_type:
            clauses.append({"doc_type": {"$eq": filters.doc_type}})
        if filters.year:
            years = sorted({str(m.get("year") or "") for _, m in self.chunks.values()
                            if Filters(year=filters.year).matches(m)})
            clauses.append({"year": {"$in": years or [filters.year]}})
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    def vector_ranking(self, query: str, n: int, filters: Filters | None = None,
                       allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """Top n (id, cosine similarity) for the query vector (bge query prefix included)."""
        vector = self.embedder.embed_query(query)
        if filters is None or filters.is_empty():
            res = self.collection.query(query_embeddings=[vector], n_results=min(n, len(self.ids)),
                                        include=["distances"])
        else:
            if not allowed:
                return []
            res = self.collection.query(query_embeddings=[vector], n_results=min(n, len(allowed)),
                                        where=self.chroma_where(filters), include=["distances"])
        pairs = [(i, 1.0 - d) for i, d in zip(res["ids"][0], res["distances"][0])]
        if allowed is not None:
            pairs = [p for p in pairs if p[0] in allowed]   # belt and braces: Chroma's where = ours
        return sorted(pairs, key=lambda p: (-p[1], p[0]))

    def _hit(self, id_: str, score: float, rank: int, scores: dict) -> Hit:
        text, meta = self.chunks[id_]
        return Hit(id_, text, meta, score, rank, scores)

    def allowed_ids(self, filters: Filters | None) -> set[str] | None:
        """Ids that pass the filters; None when there is nothing to filter."""
        if filters is None or filters.is_empty():
            return None
        return {i for i, (_, meta) in self.chunks.items() if filters.matches(meta)}

    def search(self, query: str, mode: str = DEFAULT_MODE, k: int = TOP_K,
               filters: Filters | None = None) -> list[Hit]:
        """`filters` None or empty: exactly the phase 2 search (allowed = None everywhere)."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
        allowed = self.allowed_ids(filters)
        if mode == "bm25":
            return [self._hit(i, s, r, {"bm25": s})
                    for r, (i, s) in enumerate(self.bm25_ranking(query, k, allowed), start=1)]
        if mode == "vector":
            return [self._hit(i, s, r, {"vector": s})
                    for r, (i, s) in enumerate(self.vector_ranking(query, k, filters, allowed), start=1)]

        bm25 = self.bm25_ranking(query, self.candidates, allowed)
        vector = self.vector_ranking(query, self.candidates, filters, allowed)
        raw = {"bm25": dict(bm25), "vector": dict(vector)}
        fused = rrf({"bm25": [i for i, _ in bm25], "vector": [i for i, _ in vector]}, self.rrf_k)

        def stage_scores(id_, score, ranks):
            out = {"rrf": score}
            for name in ("bm25", "vector"):
                if name in ranks:
                    out[name], out[f"{name}_rank"] = raw[name][id_], ranks[name]
            return out

        if mode == "hybrid":
            return [self._hit(i, s, r, stage_scores(i, s, ranks))
                    for r, (i, s, ranks) in enumerate(fused[:k], start=1)]

        pool = fused[:self.rerank_pool]
        scores = self.reranker.score(query, [self.search_text(i) for i, _, _ in pool])
        if len(scores) != len(pool):
            raise RuntimeError(f"reranker returned {len(scores)} scores for {len(pool)} passages")
        # Highest reranker score first; equal scores keep the hybrid order.
        order = sorted(range(len(pool)), key=lambda j: (-scores[j], j))
        hits = []
        for r, j in enumerate(order[:k], start=1):
            i, s, ranks = pool[j]
            hits.append(self._hit(i, scores[j], r,
                                  {**stage_scores(i, s, ranks), "hybrid_rank": j + 1,
                                   "rerank": scores[j]}))
        return hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Search the UDISE+ document index (no LLM).")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=MODES, default=DEFAULT_MODE)
    parser.add_argument("--k", type=int, default=TOP_K)
    parser.add_argument("--index-dir", type=Path, default=C.INDEX_DIR,
                        help="ChromaDB folder (default: $UDISE_RAG_DIR or data/rag/chroma)")
    parser.add_argument("--year", help="academic year filter, e.g. 2024-25 (keeps documents with no year)")
    parser.add_argument("--doc-type", help="doc_type filter from the manifest, e.g. official_report")
    args = parser.parse_args(argv)
    filters = Filters(args.year, args.doc_type)
    hits = Retriever(args.index_dir).search(args.question, args.mode, args.k,
                                            None if filters.is_empty() else filters)
    out = sys.stdout
    if not hits:
        print("no results", file=out)
    for h in hits:
        snippet = " ".join(h.text.split())[:240]
        print(f"{h.rank}. [{h.score:.4f}] {h.citation}  (id {h.id})\n   {snippet}\n", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
