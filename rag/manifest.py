"""The document manifest: docs_corpus/manifest.csv, one row per document.

Columns: file, title, publisher, year, doc_type, source_url (source_url may be blank, e.g.
for the project's own documents). `file` is either a path inside the repo
(docs/design_decisions.md) or a file name inside the corpus folder (UDISE_DOCS_DIR), so
official reports can be added to that folder later without changing any code.
"""
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from . import config as C

COLUMNS = ["file", "title", "publisher", "year", "doc_type", "source_url"]
REQUIRED = ["file", "title", "publisher", "year", "doc_type"]
SUPPORTED = {".pdf": "pdf", ".md": "markdown"}
YEAR = re.compile(r"^\d{4}(-\d{2})?$")         # 2026 or 2024-25
DOC_TYPE = re.compile(r"^[a-z][a-z_]*$")        # schema, project_doc, official_report, ...


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    file: str        # as written in the manifest: the document's stable key
    title: str
    publisher: str
    year: str
    doc_type: str
    source_url: str
    path: Path       # where the file was found
    kind: str        # pdf | markdown

    def row(self) -> dict:
        return {c: getattr(self, c) for c in COLUMNS}


def resolve(file: str, root: Path, docs_dir: Path) -> Path:
    """A manifest `file` -> the real path: inside the repo, or inside the corpus folder."""
    candidates = [p for p in (root / file, docs_dir / file) if p.is_file()]
    if not candidates:
        raise ManifestError(f"{file}: not found in the repo ({C.display_path(root)}) "
                            f"or in the corpus folder ({docs_dir})")
    if len(candidates) == 2 and candidates[0].resolve() != candidates[1].resolve():
        raise ManifestError(f"{file}: exists both in the repo and in {docs_dir}; rename one")
    return candidates[0]


def load_manifest(path: Path = C.MANIFEST, docs_dir: Path = C.DOCS_DIR,
                  root: Path = C.ROOT) -> list[Entry]:
    """Read and check the manifest. Every problem is reported at once, then nothing is loaded."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ManifestError(f"{path}: columns must be {COLUMNS}, got {reader.fieldnames}")
        rows = list(reader)
    problems, entries, seen = [], [], set()
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        row = {k: (v or "").strip() for k, v in row.items()}
        file = row["file"].replace("\\", "/")
        where = f"line {n} ({file or 'no file'})"
        missing = [c for c in REQUIRED if not row[c]]
        if missing:
            problems.append(f"{where}: empty {', '.join(missing)}")
            continue
        if file in seen:
            problems.append(f"{where}: listed twice")
            continue
        seen.add(file)
        kind = SUPPORTED.get(Path(file).suffix.lower())
        if kind is None:
            problems.append(f"{where}: unsupported file type (use {', '.join(SUPPORTED)})")
        if not YEAR.match(row["year"]):
            problems.append(f"{where}: year must look like 2026 or 2024-25, got {row['year']!r}")
        if not DOC_TYPE.match(row["doc_type"]):
            problems.append(f"{where}: doc_type must be lower_case_words, got {row['doc_type']!r}")
        try:
            found = resolve(file, root, docs_dir)
        except ManifestError as e:
            problems.append(f"{where}: {e}")
            continue
        if kind:
            entries.append(Entry(file=file, title=row["title"], publisher=row["publisher"],
                                 year=row["year"], doc_type=row["doc_type"],
                                 source_url=row["source_url"], path=found, kind=kind))
    if problems:
        raise ManifestError(f"{path}:\n  " + "\n  ".join(problems))
    return entries
