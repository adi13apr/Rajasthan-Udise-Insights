"""Shared helpers for the RAG tests: a fake embedder and synthetic documents (no model, no real
data, nothing downloaded)."""
import csv
import hashlib
import math

from rag.manifest import COLUMNS


class FakeEmbedder:
    """Deterministic 16-dimensional unit vectors from a hash of the text. Counts its calls, so
    tests can check that unchanged files are not embedded again."""
    name = "fake-hash-16"
    dim = 16

    def __init__(self):
        self.documents_embedded = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        v = [b - 127.5 for b in hashlib.sha256(text.encode("utf-8")).digest()[:16]]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]

    def embed_documents(self, texts):
        self.documents_embedded += len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector("query: " + text)


def sentences(label: str, n: int) -> str:
    """n distinct sentences of ~10 words each, e.g. 'Fact 7 of section A says ...'."""
    return " ".join(f"Fact {k} of section {label} says that value {k} is recorded here."
                    for k in range(1, n + 1))


def markdown_doc(n_a: int = 120, n_b: int = 20, table_rows: int = 6) -> str:
    rows = "\n".join(f"| R{k:03d} | district {k} | {k * 10} |" for k in range(1, table_rows + 1))
    return (f"# Test Guide\n\nIntro line.\n\n## Section A\n\n{sentences('A', n_a)}\n\n"
            f"## Section B\n\n{sentences('B', n_b)}\n\n"
            f"### Table B1\n\n| Code | Name | Value |\n|---|---|---|\n{rows}\n")


def write_manifest(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({"publisher": "Test", "year": "2025-26", "doc_type": "test_doc",
                        "source_url": "", **r})


def make_pdf(path, table_rows: int = 90, repeat_header: bool = False, blank_page: bool = True):
    """A small report: title, two headed text sections, a table long enough to run over a page
    break (bold header row), optionally a blank page, then an annex."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

    styles = getSampleStyleSheet()
    story = [Paragraph("Synthetic School Report", styles["Title"]),
             Paragraph("1. Introduction", styles["Heading2"]),
             Paragraph(sentences("I", 30), styles["BodyText"]),
             Paragraph("2. District table", styles["Heading2"])]
    data = [["District", "Schools", "Teachers"]] + [
        [f"District {k:03d}", str(100 + k), str(1000 + k)] for k in range(1, table_rows + 1)]
    table = Table(data, repeatRows=1 if repeat_header else 0)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]))
    story.append(table)
    if blank_page:
        story += [PageBreak(), PageBreak()]
    story += [Paragraph("3. Annex", styles["Heading2"]),
              Paragraph(sentences("X", 8), styles["BodyText"])]
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=A4, invariant=1).build(story)
