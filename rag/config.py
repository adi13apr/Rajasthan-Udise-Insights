"""Settings for the RAG layer: where the documents and the index live, and chunk sizes.

Paths can be changed with environment variables, so several worktrees (or later phases)
can share one corpus folder and one index:

    UDISE_DOCS_DIR   folder with documents that are not in the repo (official reports).
                     Default E:\\udise-docs. Manifest entries can also point to files
                     inside the repo (e.g. docs/DSP_Schema_2024-25.pdf).
    UDISE_RAG_DIR    folder of the persistent ChromaDB index. Default data/rag/chroma/
                     (gitignored).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCS_DIR = Path(os.environ.get("UDISE_DOCS_DIR", r"E:\udise-docs"))
INDEX_DIR = Path(os.environ.get("UDISE_RAG_DIR", ROOT / "data" / "rag" / "chroma"))
MANIFEST = ROOT / "docs_corpus" / "manifest.csv"
REPORT = ROOT / "reports" / "rag_ingest.json"

COLLECTION = "udise_docs"
# Per-file state (content hash, chunk count) kept next to the index, so that whoever shares
# the index also shares the knowledge of what is already in it.
STATE_FILE = "rag_ingest_state.json"

# Chunk sizes, in tokens of the embedding model's own tokenizer (special tokens included).
# bge-small-en-v1.5 reads at most 512 tokens and silently ignores the rest, so 512 is a hard
# limit; the target leaves room so most chunks end at a sentence boundary well before it.
TARGET_TOKENS = 450
MAX_TOKENS = 512
OVERLAP_TOKENS = round(0.15 * TARGET_TOKENS)  # ~15% of a chunk is repeated in the next one

# Bump when the chunking logic changes in a way that changes chunks: every file is then
# re-chunked on the next run even if the file itself did not change.
CHUNKER_VERSION = 2   # bump when extraction or chunking changes, so indexed files are rebuilt


def display_path(path: Path) -> str:
    """Path relative to the project if it is inside it (for reports), else absolute."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()
