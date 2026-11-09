"""Ingest the documents in docs_corpus/manifest.csv into the ChromaDB index.

Usage:  python -m rag.ingest                 (incremental: unchanged files are skipped)
        python -m rag.ingest --full-refresh  (drop the collection and ingest everything again)

For every manifest entry: read the file (PDF or Markdown), cut it into chunks, embed the
chunks locally and store them. Idempotent:
  * a file is skipped when nothing that affects its chunks has changed: its content (SHA-256),
    its manifest row, the chunker version and settings, the token counter and the embedder;
  * a changed file has all of its old chunks deleted before the new ones are added;
  * a file removed from the manifest has its chunks deleted.
Prints, per file, the pages, chunks and tables, and writes reports/rag_ingest.json.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C
from .chunking import Chunker
from .extract import Table, read_document
from .manifest import Entry, load_manifest
from .tokens import TokenCounter, counter_name

STATE_FORMAT = 1


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _settings(chunker: Chunker, count_tokens: TokenCounter, embedder) -> dict:
    return {"chunker_version": C.CHUNKER_VERSION, "target_tokens": chunker.target,
            "max_tokens": chunker.max, "overlap_tokens": chunker.overlap,
            "token_counter": counter_name(count_tokens), "embedder": embedder.name}


def state_hash(entry: Entry, sha: str, settings: dict) -> str:
    payload = json.dumps({"sha256": sha, "manifest": entry.row(), "settings": settings},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state(index_dir: Path) -> dict:
    path = Path(index_dir) / C.STATE_FILE
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("format") == STATE_FORMAT:
            return state
    return {"format": STATE_FORMAT, "files": {}}


def save_state(index_dir: Path, state: dict) -> None:
    path = Path(index_dir) / C.STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written state file


def metadata(entry: Entry, sha: str, chunk, index: int) -> dict:
    """Chroma metadata for one chunk (values must be str, int, float or bool: no None)."""
    meta = {"file": entry.file, "title": entry.title, "publisher": entry.publisher,
            "year": entry.year, "doc_type": entry.doc_type, "source_url": entry.source_url,
            "kind": entry.kind, "file_sha256": sha, "page": chunk.page,
            "page_end": chunk.page_end, "section": chunk.section, "location": chunk.location,
            "chunk_type": chunk.chunk_type, "chunk_index": index, "n_tokens": chunk.n_tokens,
            "oversized": chunk.oversized}
    if chunk.chunk_type == "table":
        meta.update(table_index=chunk.table_index, row_start=chunk.row_start,
                    row_end=chunk.row_end)
    return meta


def process(entry: Entry, sha: str, chunker: Chunker, embedder, store) -> dict:
    """Chunk, embed and store one file. Returns its statistics."""
    doc = read_document(entry.path, entry.kind)
    chunks = chunker.chunk(doc, entry.file)
    vectors = embedder.embed_documents([c.text for c in chunks])
    store.replace_file(entry.file, [c.id for c in chunks], vectors, [c.text for c in chunks],
                       [metadata(entry, sha, c, i) for i, c in enumerate(chunks)])
    sections = {c.section for c in chunks}
    return {"pages": doc.pages,
            "sections": len(sections),
            "tables": sum(isinstance(e, Table) for e in doc.elements),
            "chunks": len(chunks),
            "text_chunks": sum(c.chunk_type == "text" for c in chunks),
            "table_chunks": sum(c.chunk_type == "table" for c in chunks),
            "oversized_chunks": sum(c.oversized for c in chunks),
            "max_chunk_tokens": max((c.n_tokens for c in chunks), default=0),
            "empty_pages": doc.empty_pages}


def ingest(*, manifest: Path = C.MANIFEST, docs_dir: Path = C.DOCS_DIR,
           index_dir: Path = C.INDEX_DIR, embedder=None, count_tokens: TokenCounter | None = None,
           collection: str = C.COLLECTION, full_refresh: bool = False,
           report_path: Path | None = C.REPORT, root: Path = C.ROOT) -> dict:
    from .store import Store

    started = time.perf_counter()
    entries = load_manifest(manifest, docs_dir, root)   # stops here if the manifest is wrong
    if embedder is None:
        from .embedder import BgeSmallEmbedder
        embedder = BgeSmallEmbedder()
    count_tokens = count_tokens or embedder.count_tokens
    chunker = Chunker(count_tokens)
    settings = _settings(chunker, count_tokens, embedder)

    index_dir = Path(index_dir)
    store = Store(index_dir, collection, embedder.name, embedder.dim, reset=full_refresh)
    state = {"format": STATE_FORMAT, "files": {}} if full_refresh else load_state(index_dir)

    files = []
    for e in entries:
        t0 = time.perf_counter()
        sha = file_sha256(e.path)
        key = state_hash(e, sha, settings)
        prev = state["files"].get(e.file)
        if prev and prev["state_hash"] == key and len(store.file_ids(e.file)) == prev["stats"]["chunks"]:
            status, stats = "unchanged", prev["stats"]
        else:
            status = "new" if prev is None else (
                "changed" if prev["state_hash"] != key else "reindexed")  # index lost chunks
            stats = process(e, sha, chunker, embedder, store)
            state["files"][e.file] = {"state_hash": key, "sha256": sha, "stats": stats,
                                      "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            save_state(index_dir, state)
        files.append({"file": e.file, "title": e.title, "kind": e.kind, "status": status,
                      **stats, "seconds": round(time.perf_counter() - t0, 2)})

    # Files no longer in the manifest: delete their chunks (also any chunks the state does not
    # know about, e.g. after the state file was deleted).
    listed = {e.file for e in entries}
    removed = []
    for file in sorted((set(state["files"]) | store.files()) - listed):
        removed.append({"file": file, "chunks_deleted": store.delete_file(file)})
        state["files"].pop(file, None)
    save_state(index_dir, state)

    done = [f for f in files if f["status"] != "unchanged"]
    totals = {
        "files_in_manifest": len(files),
        "files_ingested": len(done),
        "files_skipped": len(files) - len(done),
        "files_removed": len(removed),
        "chunks_deleted_for_removed_files": sum(r["chunks_deleted"] for r in removed),
        "ingested": {k: sum(f[k] for f in done) for k in
                     ("pages", "chunks", "text_chunks", "table_chunks", "tables")},
        "index": {k: sum(f[k] for f in files) for k in
                  ("pages", "chunks", "text_chunks", "table_chunks", "tables", "oversized_chunks")},
        "index_records": store.count(),
    }
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.perf_counter() - started, 2),
        "manifest": C.display_path(manifest),
        "index_dir": C.display_path(index_dir),
        "collection": collection,
        "settings": settings,
        "files": files,
        "removed": removed,
        "empty_pages": {f["file"]: f["empty_pages"] for f in files if f["empty_pages"]},
        "totals": totals,
    }
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def print_report(report: dict, out=None) -> None:
    from tabulate import tabulate

    out = out or sys.stdout   # looked up at call time, so redirected output is respected
    rows = [[f["file"], f["status"], f["pages"] or "-", f["sections"], f["tables"],
             f["chunks"], f["text_chunks"], f["table_chunks"], f["max_chunk_tokens"]]
            for f in report["files"]]
    print(tabulate(rows, headers=["file", "status", "pages", "sections", "tables", "chunks",
                                  "text", "table", "max tokens"]), file=out)
    t = report["totals"]
    print(f"\nFiles: {t['files_in_manifest']} in manifest, {t['files_ingested']} ingested, "
          f"{t['files_skipped']} unchanged (skipped), {t['files_removed']} removed", file=out)
    i = t["ingested"]
    print(f"Ingested this run: {i['pages']} pages, {i['chunks']} chunks "
          f"({i['text_chunks']} text, {i['table_chunks']} table), {i['tables']} tables", file=out)
    x = t["index"]
    print(f"Index now: {t['index_records']} chunks ({x['text_chunks']} text, "
          f"{x['table_chunks']} table) from {t['files_in_manifest']} files, "
          f"{x['oversized_chunks']} oversized", file=out)
    for r in report["removed"]:
        print(f"Removed from manifest: {r['file']} ({r['chunks_deleted']} chunks deleted)", file=out)
    if report["empty_pages"]:
        print("Pages with no extractable text (skipped, no OCR):", file=out)
        for file, pages in report["empty_pages"].items():
            print(f"  {file}: pages {', '.join(map(str, pages))}", file=out)
    else:
        print("Pages with no extractable text: none", file=out)
    print(f"Index: {report['index_dir']} (collection {report['collection']}), "
          f"{report['seconds']} s", file=out)


def main(argv=None, embedder=None, count_tokens=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=C.MANIFEST)
    parser.add_argument("--docs-dir", type=Path, default=C.DOCS_DIR,
                        help="corpus folder (default: $UDISE_DOCS_DIR or E:\\udise-docs)")
    parser.add_argument("--index-dir", type=Path, default=C.INDEX_DIR,
                        help="ChromaDB folder (default: $UDISE_RAG_DIR or data/rag/chroma)")
    parser.add_argument("--report", type=Path, default=C.REPORT)
    parser.add_argument("--full-refresh", action="store_true",
                        help="drop the collection and ingest every file again")
    args = parser.parse_args(argv)
    report = ingest(manifest=args.manifest, docs_dir=args.docs_dir, index_dir=args.index_dir,
                    embedder=embedder, count_tokens=count_tokens,
                    full_refresh=args.full_refresh, report_path=args.report)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
