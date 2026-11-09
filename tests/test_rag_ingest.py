"""End-to-end ingestion into a temporary ChromaDB index with a fake embedder: metadata,
idempotent re-runs, changed and removed files, the manifest and the CLI."""
import json

import pytest

pytest.importorskip("chromadb")

from rag import ingest as I  # noqa: E402
from rag.embedder import BgeSmallEmbedder  # noqa: E402
from rag.manifest import ManifestError, load_manifest  # noqa: E402
from rag.store import Store  # noqa: E402
from rag.tokens import estimate_tokens  # noqa: E402
from rag_helpers import FakeEmbedder, make_pdf, markdown_doc, write_manifest  # noqa: E402

REQUIRED_META = {"file", "title", "publisher", "year", "doc_type", "source_url", "kind",
                 "file_sha256", "page", "page_end", "section", "location", "chunk_type",
                 "chunk_index", "n_tokens", "oversized"}


@pytest.fixture
def corpus(tmp_path):
    """A fake repo (one Markdown file in it) and a separate corpus folder (one PDF)."""
    root, docs = tmp_path / "repo", tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(markdown_doc(), encoding="utf-8")
    make_pdf(docs / "report.pdf")
    manifest = root / "docs_corpus" / "manifest.csv"
    write_manifest(manifest, [
        {"file": "docs/guide.md", "title": "Test Guide", "doc_type": "project_doc"},
        {"file": "report.pdf", "title": "Synthetic School Report", "doc_type": "official_report",
         "source_url": "https://example.org/report.pdf"}])
    return {"root": root, "docs": docs, "manifest": manifest, "index": tmp_path / "index",
            "report": tmp_path / "rag_ingest.json"}


def run(corpus, embedder, **kw):
    return I.ingest(manifest=corpus["manifest"], docs_dir=corpus["docs"], index_dir=corpus["index"],
                    embedder=embedder, count_tokens=estimate_tokens, root=corpus["root"],
                    report_path=corpus["report"], **kw)


def records(corpus, embedder):
    store = Store(corpus["index"], I.C.COLLECTION, embedder.name, embedder.dim)
    return store.collection.get(include=["metadatas", "documents"])


def test_first_run_stores_every_chunk_with_metadata(corpus):
    emb = FakeEmbedder()
    report = run(corpus, emb)
    assert [f["status"] for f in report["files"]] == ["new", "new"]
    got = records(corpus, emb)
    assert len(got["ids"]) == report["totals"]["index_records"] == report["totals"]["index"]["chunks"]
    assert emb.documents_embedded == len(got["ids"])
    for meta, text in zip(got["metadatas"], got["documents"]):
        assert REQUIRED_META <= set(meta), REQUIRED_META - set(meta)
        assert meta["chunk_type"] in {"text", "table"} and text
        if meta["chunk_type"] == "table":
            assert {"table_index", "row_start", "row_end"} <= set(meta)
        if meta["kind"] == "pdf":
            assert meta["title"] == "Synthetic School Report" and meta["page"] >= 1
            assert meta["source_url"] == "https://example.org/report.pdf"
        else:
            assert meta["page"] == 0 and meta["location"] == meta["section"]
            assert meta["source_url"] == ""
    pdf = next(f for f in report["files"] if f["kind"] == "pdf")
    assert pdf["pages"] == 5 and pdf["tables"] == 1 and pdf["table_chunks"] >= 1
    assert report["empty_pages"] == {"report.pdf": [4]}
    assert json.loads(corpus["report"].read_text(encoding="utf-8"))["totals"] == report["totals"]


def test_rerun_skips_unchanged_files_and_keeps_the_same_ids(corpus):
    run(corpus, FakeEmbedder())
    before = sorted(records(corpus, FakeEmbedder())["ids"])
    emb = FakeEmbedder()
    report = run(corpus, emb)
    assert [f["status"] for f in report["files"]] == ["unchanged", "unchanged"]
    assert emb.documents_embedded == 0                 # nothing re-embedded
    assert report["totals"]["files_ingested"] == 0
    assert sorted(records(corpus, emb)["ids"]) == before


def test_changed_file_has_its_chunks_replaced_and_nothing_stale_left(corpus):
    run(corpus, FakeEmbedder())
    guide = corpus["root"] / "docs" / "guide.md"
    guide.write_text("# Test Guide\n\n## Only section\n\nA much shorter guide.\n", encoding="utf-8")
    emb = FakeEmbedder()
    report = run(corpus, emb)
    status = {f["file"]: f["status"] for f in report["files"]}
    assert status == {"docs/guide.md": "changed", "report.pdf": "unchanged"}
    got = records(corpus, emb)
    md = [(m, d) for m, d in zip(got["metadatas"], got["documents"]) if m["file"] == "docs/guide.md"]
    assert len(md) == 1 and "much shorter" in md[0][1]
    assert emb.documents_embedded == 1


def test_manifest_change_alone_reingests_the_file(corpus):
    run(corpus, FakeEmbedder())
    text = corpus["manifest"].read_text(encoding="utf-8").replace("Test Guide", "Renamed Guide")
    corpus["manifest"].write_text(text, encoding="utf-8")
    report = run(corpus, FakeEmbedder())
    assert {f["file"]: f["status"] for f in report["files"]}["docs/guide.md"] == "changed"
    titles = {m["title"] for m in records(corpus, FakeEmbedder())["metadatas"] if m["kind"] == "markdown"}
    assert titles == {"Renamed Guide"}


def test_removed_file_has_its_chunks_deleted(corpus, capsys):
    run(corpus, FakeEmbedder())
    write_manifest(corpus["manifest"], [{"file": "docs/guide.md", "title": "Test Guide"}])
    report = run(corpus, FakeEmbedder())
    (removed,) = report["removed"]
    assert removed["file"] == "report.pdf" and removed["chunks_deleted"] > 0
    assert {m["file"] for m in records(corpus, FakeEmbedder())["metadatas"]} == {"docs/guide.md"}
    I.print_report(report)
    assert "Removed from manifest: report.pdf" in capsys.readouterr().out


def test_lost_index_chunks_are_rebuilt_even_if_the_state_says_done(corpus):
    run(corpus, FakeEmbedder())
    Store(corpus["index"], I.C.COLLECTION, FakeEmbedder.name, FakeEmbedder.dim).delete_file("report.pdf")
    report = run(corpus, FakeEmbedder())
    assert {f["file"]: f["status"] for f in report["files"]}["report.pdf"] == "reindexed"


def test_a_different_embedder_needs_a_full_refresh(corpus):
    run(corpus, FakeEmbedder())

    class Other(FakeEmbedder):
        name = "another-model"

    with pytest.raises(RuntimeError, match="full-refresh"):
        run(corpus, Other())
    report = run(corpus, Other(), full_refresh=True)
    assert {f["status"] for f in report["files"]} == {"new"}


def test_cli_prints_files_pages_chunks_tables_and_empty_pages(corpus, capsys):
    argv = ["--manifest", str(corpus["manifest"]), "--docs-dir", str(corpus["docs"]),
            "--index-dir", str(corpus["index"]), "--report", str(corpus["report"])]
    # The CLI resolves repo paths against the real repo root; point the Markdown entry at the
    # corpus folder instead so this test needs only the temporary files.
    (corpus["docs"] / "docs").mkdir()
    (corpus["docs"] / "docs" / "guide.md").write_text(markdown_doc(), encoding="utf-8")
    assert I.main(argv, embedder=FakeEmbedder(), count_tokens=estimate_tokens) == 0
    out = capsys.readouterr().out
    for text in ("report.pdf", "docs/guide.md", "Files: 2 in manifest, 2 ingested",
                 "Ingested this run:", "pages", "tables", "report.pdf: pages 4"):
        assert text in out


def test_manifest_problems_are_all_reported(tmp_path):
    root, docs = tmp_path / "repo", tmp_path / "corpus"
    root.mkdir()
    docs.mkdir()
    (root / "a.md").write_text("# A\n", encoding="utf-8")
    (docs / "a.md").write_text("# A elsewhere\n", encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    write_manifest(manifest, [
        {"file": "a.md", "title": "A"},                           # in both places: ambiguous
        {"file": "missing.pdf", "title": "M"},                    # not found
        {"file": "b.txt", "title": "", "year": "2025"},           # empty title
        {"file": "c.md", "title": "C", "year": "25-26"},          # bad year (and not found)
    ])
    with pytest.raises(ManifestError) as e:
        load_manifest(manifest, docs, root)
    msg = str(e.value)
    for text in ("exists both in the repo", "missing.pdf: not found", "empty title", "year must"):
        assert text in msg


def test_manifest_resolves_repo_paths_and_corpus_files(corpus):
    entries = load_manifest(corpus["manifest"], corpus["docs"], corpus["root"])
    where = {e.file: e.path for e in entries}
    assert where["docs/guide.md"] == corpus["root"] / "docs" / "guide.md"
    assert where["report.pdf"] == corpus["docs"] / "report.pdf"


def test_bge_query_prefix_only_on_queries():
    """No model download: a stand-in model records what it was asked to encode."""
    class Recorder:
        def __init__(self):
            self.seen = []

        def encode(self, texts, **kw):
            import numpy as np
            assert kw["normalize_embeddings"] is True
            self.seen += texts
            return np.zeros((len(texts), 384))

    emb = BgeSmallEmbedder()
    emb._model = Recorder()
    emb.embed_documents(["a passage"])
    emb.embed_query("how many schools?")
    assert emb._model.seen == ["a passage", BgeSmallEmbedder.QUERY_INSTRUCTION + "how many schools?"]
