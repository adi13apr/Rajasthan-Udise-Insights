"""Render docs/policy_brief.md to a two-page A4 PDF (docs/policy_brief.pdf).

    python scripts/make_brief_pdf.py

Markdown -> HTML with print CSS (python-markdown) -> PDF with headless Microsoft Edge
(installed with Windows 11). Fails if the PDF is not exactly two pages.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import markdown
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "policy_brief.md"
OUT = ROOT / "docs" / "policy_brief.pdf"
TMP = ROOT / "data" / "tmp" / "brief"
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
PAGES = 2

CSS = """
@page { size: A4; margin: 11mm 13mm 10mm 13mm; }
body { font-family: "Segoe UI", system-ui, sans-serif; font-size: 8.6pt; line-height: 1.36;
       color: #0b0b0b; margin: 0; }
h1 { font-size: 14.5pt; margin: 0 0 2pt 0; line-height: 1.2; }
h1 + p em { color: #52514e; }
h2 { font-size: 11pt; margin: 9pt 0 3pt 0; border-top: 1px solid #e1e0d9; padding-top: 6pt; }
p { margin: 3pt 0; }
ul { margin: 2pt 0 3pt 0; padding-left: 15pt; }
li { margin: 1pt 0; }
img { display: block; width: 76%; margin: 3pt auto 2pt auto; }
hr { border: 0; border-top: 1px solid #e1e0d9; margin: 7pt 0 4pt 0; }
hr ~ p { font-size: 7.6pt; color: #52514e; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 7.4pt; }
h2 { break-after: avoid; }
img { break-inside: avoid; }
"""


def main() -> None:
    edge = next((p for p in EDGE if p.exists()), None)
    if not edge:
        raise SystemExit("Microsoft Edge not found; open docs/policy_brief.md instead.")
    html = markdown.markdown(SRC.read_text(encoding="utf-8"), extensions=["extra"])
    # images: make the relative paths absolute, since the HTML is written elsewhere
    html = html.replace('src="../', f'src="{(ROOT).as_uri()}/')
    TMP.mkdir(parents=True, exist_ok=True)
    page = TMP / "policy_brief.html"
    page.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>Policy brief</title>"
                    f"<style>{CSS}</style></head><body>{html}</body></html>", encoding="utf-8")
    pdf = TMP / "policy_brief.pdf"
    pdf.unlink(missing_ok=True)
    subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--user-data-dir={TMP / 'edge-profile'}", f"--print-to-pdf={pdf}", page.as_uri()],
                   check=True, capture_output=True, timeout=120)
    pages = len(PdfReader(pdf).pages)
    if pages != PAGES:
        raise SystemExit(f"The brief is {pages} pages, not {PAGES}: shorten the text or the figures.")
    shutil.copyfile(pdf, OUT)
    print(f"Written {OUT} ({pages} pages)")


if __name__ == "__main__":
    sys.exit(main())
