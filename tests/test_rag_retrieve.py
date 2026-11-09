"""Retrieval (rag/retrieve.py): the tokenizer, the Reciprocal Rank Fusion maths and the order of
results in all four modes. A tiny ChromaDB index is written in a temp folder with a fake
keyword embedder and searched with a fake reranker: no model is downloaded."""
import math

import pytest

pytest.importorskip("chromadb")
pytest.importorskip("rank_bm25")

from rag.retrieve import MODES, Filters, Retriever, rrf, tokenize  # noqa: E402
from rag.store import Store  # noqa: E402

VOCAB = ["toilet", "teacher", "enrolment", "electricity", "code", "girls", "library", "computer"]


class KeywordEmbedder:
    """A vector = how often each VOCAB word appears (normalised), so 'similar meaning' is
    predictable in tests. Records the queries it embedded."""
    name = "fake-keyword-8"
    dim = len(VOCAB)

    def __init__(self):
        self.queries = []

    @staticmethod
    def _vector(text):
        words = text.lower().split()
        v = [sum(w.startswith(t) for w in words) + 0.01 * (i + 1) for i, t in enumerate(VOCAB)]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return self._vector(text)


class FakeReranker:
    """Scores a passage by how many times it contains `word`; records what it was given."""
    name = "fake-reranker"

    def __init__(self, word="library"):
        self.word, self.calls = word, []

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        return [float(p.lower().count(self.word)) for p in passages]


DOCS = [
    ("c-code9", "Schema", "Yes/No columns: code 9 means not applicable; treat as missing."),
    ("c-code1", "Schema", "Yes/No columns: code 1 means yes and code 2 means no."),
    ("c-mgmt", "Schema", "Management code 94 and code 95 are government schools."),
    ("c-toilet", "Report 2024-25", "Schools with a functional girls toilet toilet toilet."),
    ("c-teacher", "Report 2024-25", "Pupil teacher ratio and teacher teacher numbers."),
    ("c-item", "Schema", "Item_group 1 is General, SC, ST, OBC; item_group 4 is disability."),
    ("c-y2526", "Report 2025-26", "Number of schools by management, all categories."),
] + [(f"c-filler{n:02d}", "Report 2024-25",
      f"Filler chunk {n} about electricity and enrolment" + " library" * (n % 7))
     for n in range(35)]


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    path = tmp_path_factory.mktemp("rag-index")
    emb = KeywordEmbedder()
    store = Store(path, "udise_docs", emb.name, emb.dim)
    ids = [d[0] for d in DOCS]
    texts = [d[2] for d in DOCS]
    metas = [{"file": f"{d[1]}.pdf", "title": d[1], "location": "p. 1", "page": 1} for d in DOCS]
    store.replace_file("all", ids, emb.embed_documents(texts), texts,
                       [{**m, "file": "all"} for m in metas])
    return path


@pytest.fixture
def retriever(index):
    return Retriever(index, embedder=KeywordEmbedder(), reranker=FakeReranker())


# --- tokenizer -------------------------------------------------------------------------

def test_tokenizer_keeps_digits_and_codes():
    assert tokenize("What does code 9 mean in yes/no columns?") == [
        "code", "9", "mean", "yes/no", "yes", "no", "columns"]
    assert tokenize("item_group 4, management code 94") == [
        "item_group", "item", "group", "4", "management", "code", "94"]


def test_tokenizer_numbers_years_and_decimals():
    assert tokenize("1,06,302 schools") == ["106302", "schools"]       # Indian thousands
    assert tokenize("UDISE+ 2024-25") == ["udise", "2024-25", "2024", "25"]
    assert tokenize("PTR 24.5 and 2.3.6") == ["ptr", "24.5", "2.3.6"]  # decimals stay whole


# --- Reciprocal Rank Fusion ------------------------------------------------------------

def test_rrf_scores_are_sums_of_one_over_k_plus_rank():
    fused = rrf({"bm25": ["a", "b", "c"], "vector": ["b", "d"]}, k=60)
    assert [f[0] for f in fused] == ["b", "a", "d", "c"]
    score = {f[0]: f[1] for f in fused}
    assert score["b"] == pytest.approx(1 / 62 + 1 / 61)   # rank 2 in bm25, rank 1 in vector
    assert score["a"] == pytest.approx(1 / 61)
    assert score["d"] == pytest.approx(1 / 62)            # in one list only: one vote
    assert score["c"] == pytest.approx(1 / 63)
    assert dict((f[0], f[2]) for f in fused)["b"] == {"bm25": 2, "vector": 1}


def test_rrf_uses_ranks_not_scores_and_breaks_ties_the_same_way_every_time():
    fused = rrf({"bm25": ["y"], "vector": ["x"]})
    assert [f[0] for f in fused] == ["x", "y"]            # equal score, equal rank -> by id
    assert fused[0][1] == fused[1][1] == pytest.approx(1 / 61)
    # A chunk found by both lists at modest ranks beats one found by a single list at rank 1.
    fused = rrf({"bm25": ["solo", "both"], "vector": ["other", "both"]})
    assert fused[0][0] == "both"
    # The same id twice in one list counts once, at its best rank.
    assert rrf({"bm25": ["a", "a"]})[0][1] == pytest.approx(1 / 61)


def test_rrf_k_changes_how_much_the_top_ranks_dominate():
    lists = {"bm25": ["a", "b"], "vector": ["c", "b"]}
    assert [f[0] for f in rrf(lists, k=60)][0] == "b"     # 2 x 1/62 > 1/61
    # k = 0: a = 1/1, c = 1/1, b = 1/2 + 1/2: a three-way tie at 1.0, broken by the best
    # single rank (a and c have rank 1, b only rank 2), then by id -> a, c, b.
    assert [f[0] for f in rrf(lists, k=0)] == ["a", "c", "b"]


# --- the four modes --------------------------------------------------------------------

def test_bm25_finds_the_exact_code(retriever):
    hits = retriever.search("What does code 9 mean?", mode="bm25", k=3)
    assert hits[0].id == "c-code9"
    assert [h.rank for h in hits] == [1, 2, 3]
    assert hits[0].score > hits[1].score > 0
    assert retriever.search("code 94", mode="bm25", k=1)[0].id == "c-mgmt"
    assert retriever.search("item_group 4", mode="bm25", k=1)[0].id == "c-item"


def test_bm25_searches_the_title_and_returns_nothing_without_a_shared_word(retriever):
    assert retriever.search("2025-26 management", mode="bm25", k=1)[0].id == "c-y2526"
    assert retriever.search("zzzz qqqq", mode="bm25") == []
    assert retriever.search("the of and", mode="bm25") == []       # stopwords only


def test_vector_uses_embed_query_and_cosine_order(retriever):
    hits = retriever.search("where are the toilets", mode="vector", k=3)
    assert hits[0].id == "c-toilet"
    assert retriever.embedder.queries == ["where are the toilets"]
    assert all(a.score >= b.score for a, b in zip(hits, hits[1:]))
    assert hits[0].score == pytest.approx(1.0, abs=0.05)


def test_hybrid_is_the_rrf_of_the_two_lists(retriever):
    q = "teacher code 9"
    bm25 = [i for i, _ in retriever.bm25_ranking(q, retriever.candidates)]
    vector = [i for i, _ in retriever.vector_ranking(q, retriever.candidates)]
    expected = [f[0] for f in rrf({"bm25": bm25, "vector": vector})][:5]
    hits = retriever.search(q, mode="hybrid", k=5)
    assert [h.id for h in hits] == expected
    top = hits[0]
    assert top.score == pytest.approx(sum(1 / (60 + top.scores[f"{n}_rank"])
                                          for n in ("bm25", "vector") if f"{n}_rank" in top.scores))


def test_hybrid_rerank_rescores_the_top_30_and_returns_5(retriever):
    q = "electricity enrolment library"
    pool = [f[0] for f in rrf({
        "bm25": [i for i, _ in retriever.bm25_ranking(q, retriever.candidates)],
        "vector": [i for i, _ in retriever.vector_ranking(q, retriever.candidates)]})][:30]
    hits = retriever.search(q, mode="hybrid_rerank")
    (call,) = retriever.reranker.calls
    assert call[0] == q
    assert len(call[1]) == 30                                   # only the top 30 are scored
    assert call[1] == [retriever.search_text(i) for i in pool]  # in hybrid order, with title
    assert len(hits) == 5
    counts = [h.text.lower().count("library") for h in hits]
    assert counts == sorted(counts, reverse=True)               # reranker order wins
    assert hits[0].score == counts[0]
    # equal reranker scores keep the hybrid order
    ties = [h.scores["hybrid_rank"] for h in hits if h.score == hits[0].score]
    assert ties == sorted(ties)


def test_every_mode_returns_ranked_hits_with_citations(retriever):
    for mode in MODES:
        hits = retriever.search("teacher toilet code", mode=mode, k=4)
        assert 1 <= len(hits) <= 4
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
        assert hits[0].citation.endswith("p. 1")


def test_bad_mode_wrong_embedder_and_missing_index(index, tmp_path):
    with pytest.raises(ValueError, match="unknown mode"):
        Retriever(index, embedder=KeywordEmbedder()).search("x", mode="dense")

    class Other(KeywordEmbedder):
        name = "another-model"
    with pytest.raises(RuntimeError, match="built with"):
        Retriever(index, embedder=Other())
    with pytest.raises(FileNotFoundError):
        Retriever(tmp_path / "nothing-here")


# --- metadata filters (phase 4) ----------------------------------------------------------
QUERIES = ["code 9", "girls toilet", "teacher ratio", "item_group 4 disability", "library computer",
           "management code 94", "electricity enrolment", "nothing shared here zzz"]


def _same(a, b):
    return [(h.id, h.rank, h.score, h.scores) for h in a] == [(h.id, h.rank, h.score, h.scores) for h in b]


@pytest.mark.parametrize("mode", MODES)
def test_no_filters_is_exactly_the_phase2_search(retriever, mode):
    """filters=None and an empty Filters() give the same ids, ranks and every stage's scores as
    the phase 2 call search(q, mode, k), so the phase 2 evaluation stays valid."""
    for q in QUERIES:
        phase2 = retriever.search(q, mode=mode, k=5)
        assert _same(retriever.search(q, mode=mode, k=5, filters=None), phase2)
        assert _same(retriever.search(q, mode=mode, k=5, filters=Filters()), phase2)


# Documents like the real corpus: reports and schemas per academic year, the metadata note with
# no year, the project's Markdown docs with year 2026 (a calendar year, not an academic year).
FILTER_DOCS = [
    ("r24-a", "Report 2024-25", "2024-25", "official_report", "Schools with a girls toilet in 2024-25 toilet."),
    ("r24-b", "Report 2024-25", "2024-25", "official_report", "Teacher numbers teacher ratio 2024-25."),
    ("r25-a", "Report 2025-26", "2025-26", "official_report", "Schools with a girls toilet in 2025-26 toilet."),
    ("r25-b", "Report 2025-26", "2025-26", "official_report", "Teacher numbers teacher ratio 2025-26."),
    ("s24", "Schema", "2024-25", "schema", "Yes/No columns: code 9 means not applicable toilet."),
    ("s25", "Schema (2025-26)", "2025-26", "schema", "Yes/No columns: code 9 means not applicable teacher."),
    ("meta", "General metadata", "", "official_metadata", "Toilet means a functional toilet; teacher defined."),
    ("dql", "Data Quality Log", "2026", "project_doc", "Code 9 treated as missing; toilet flags; teacher."),
]


@pytest.fixture(scope="module")
def filter_index(tmp_path_factory):
    path = tmp_path_factory.mktemp("rag-filter-index")
    emb = KeywordEmbedder()
    store = Store(path, "udise_docs", emb.name, emb.dim)
    ids, texts = [d[0] for d in FILTER_DOCS], [d[4] for d in FILTER_DOCS]
    metas = [{"file": "all", "title": d[1], "year": d[2], "doc_type": d[3], "location": "p. 1", "page": 1}
             for d in FILTER_DOCS]
    store.replace_file("all", ids, emb.embed_documents(texts), texts, metas)
    return path


@pytest.fixture
def fretriever(filter_index):
    return Retriever(filter_index, embedder=KeywordEmbedder(), reranker=FakeReranker())


def _years(hits):
    return {h.metadata["year"] for h in hits}


def test_year_filter_keeps_that_year_and_documents_without_an_academic_year(fretriever):
    hits = fretriever.search("toilet teacher code", mode="hybrid", k=8, filters=Filters(year="2024-25"))
    ids = {h.id for h in hits}
    assert "r25-a" not in ids and "r25-b" not in ids and "s25" not in ids    # the other year is gone
    assert {"meta", "dql"} <= ids                       # no year / calendar year: apply to every year
    assert _years(hits) <= {"2024-25", "", "2026"}


@pytest.mark.parametrize("mode", ["bm25", "vector"])
@pytest.mark.parametrize("filters", [Filters(year="2024-25"), Filters(year="2025-26"),
                                     Filters(doc_type="official_report"), Filters(doc_type="schema"),
                                     Filters(year="2025-26", doc_type="official_report"),
                                     Filters(doc_type="official_metadata")])
def test_chroma_where_and_bm25_filter_select_the_same_chunks(fretriever, mode, filters):
    """With k = all chunks, each list returns exactly the chunks that pass the filter (vector:
    through Chroma's `where`; bm25: in Python), so both halves of hybrid agree on the rule."""
    expected = {d[0] for d in FILTER_DOCS if filters.matches({"year": d[2], "doc_type": d[3]})}
    q = "toilet teacher code 9 girls ratio numbers functional missing columns schools"
    got = {h.id for h in fretriever.search(q, mode=mode, k=len(FILTER_DOCS), filters=filters)}
    assert got == expected


def test_doc_type_filter_and_unchanged_bm25_scores(fretriever):
    plain = {h.id: h.score for h in fretriever.search("teacher ratio", mode="bm25", k=8)}
    hits = fretriever.search("teacher ratio", mode="bm25", k=8, filters=Filters(doc_type="official_report"))
    assert hits and all(h.metadata["doc_type"] == "official_report" for h in hits)
    assert all(h.score == plain[h.id] for h in hits)    # the filter does not change BM25 scores
    assert fretriever.search("teacher", mode="vector", k=5, filters=Filters(doc_type="no_such_type")) == []
