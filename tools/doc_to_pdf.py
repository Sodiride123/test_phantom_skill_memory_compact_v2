#!/usr/bin/env python3
"""
doc_to_pdf — convert an HTML or DOCX file to PDF (+ optional preview PNG)
without LibreOffice.

Motivation: this environment has no LibreOffice/soffice, so the docx skill's
soffice.py conversion fails (see issue #106). Ninja already drives a persistent
Chromium browser, which renders HTML to PDF perfectly — so we convert
everything to HTML and let the browser produce the PDF and a preview image.

Pipeline:
    .html  ->  (load in browser)        -> PDF + PNG
    .docx  ->  HTML (mammoth or builtin) -> (load in browser) -> PDF + PNG

DOCX -> HTML uses `mammoth` when available (best fidelity); otherwise a small
self-contained fallback parses word/document.xml directly (headings, bold,
italic, lists) so the tool still works with no third-party dependency.

Usage:
    python tools/doc_to_pdf.py report.html
    python tools/doc_to_pdf.py cv.docx --out cv.pdf --png cv_preview.png
    python tools/doc_to_pdf.py cv.docx --no-png

Output defaults (when flags omitted): <input>.pdf and <input>_preview.png
next to the input file. Prints a JSON summary of what was written.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Minimal, clean default styling wrapped around bare HTML/DOCX bodies so plain
# documents still render as a tidy page.
_DEFAULT_CSS = """
  @page { size: letter; margin: 0.75in; }
  body { font-family: Calibri, "Segoe UI", Arial, sans-serif; color: #1a1a1a;
         font-size: 11pt; line-height: 1.45; margin: 0.75in; }
  h1 { font-size: 20pt; margin: 0 0 6px; }
  h2 { font-size: 14pt; color: #333; border-bottom: 1px solid #ccc;
       padding-bottom: 3px; margin: 16px 0 8px; }
  h3 { font-size: 12pt; margin: 12px 0 4px; }
  p { margin: 4px 0; }
  ul, ol { margin: 4px 0; padding-left: 22px; }
  li { margin-bottom: 3px; }
  table { border-collapse: collapse; margin: 8px 0; }
  td, th { border: 1px solid #ccc; padding: 4px 8px; }
"""

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _wrap_html(body: str, title: str = "Document") -> str:
    """Wrap an HTML body fragment in a full document with default CSS."""
    lower = body.lower()
    if "<html" in lower:
        return body  # already a complete document
    return (
        f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{_html.escape(title)}</title><style>{_DEFAULT_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _docx_to_html_builtin(docx_path: str) -> str:
    """Dependency-free DOCX -> HTML: parse word/document.xml directly.

    Handles paragraphs, Heading1-3 styles, bold/italic runs, and bullet/number
    list items. Good enough for CVs, memos, and reports; not a full converter.
    """
    with zipfile.ZipFile(docx_path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(f"{W}body")
    out: list[str] = []
    for p in body.findall(f"{W}p") if body is not None else []:
        ppr = p.find(f"{W}pPr")
        style = ""
        is_list = False
        if ppr is not None:
            pstyle = ppr.find(f"{W}pStyle")
            if pstyle is not None:
                style = pstyle.get(f"{W}val", "") or ""
            if ppr.find(f"{W}numPr") is not None:
                is_list = True

        # Assemble runs with inline bold/italic.
        parts: list[str] = []
        for r in p.findall(f"{W}r"):
            text = "".join(t.text or "" for t in r.findall(f"{W}t"))
            if not text:
                continue
            esc = _html.escape(text)
            rpr = r.find(f"{W}rPr")
            if rpr is not None:
                if rpr.find(f"{W}b") is not None:
                    esc = f"<strong>{esc}</strong>"
                if rpr.find(f"{W}i") is not None:
                    esc = f"<em>{esc}</em>"
            parts.append(esc)
        content = "".join(parts).strip()
        if not content:
            out.append("<p>&nbsp;</p>")
            continue

        low = style.lower()
        if low.startswith("heading1") or low == "title":
            out.append(f"<h1>{content}</h1>")
        elif low.startswith("heading2"):
            out.append(f"<h2>{content}</h2>")
        elif low.startswith("heading3"):
            out.append(f"<h3>{content}</h3>")
        elif is_list:
            out.append(f"<li>{content}</li>")
        else:
            out.append(f"<p>{content}</p>")

    # Wrap consecutive <li> runs in <ul>.
    merged: list[str] = []
    in_list = False
    for line in out:
        if line.startswith("<li>"):
            if not in_list:
                merged.append("<ul>")
                in_list = True
            merged.append(line)
        else:
            if in_list:
                merged.append("</ul>")
                in_list = False
            merged.append(line)
    if in_list:
        merged.append("</ul>")
    return "\n".join(merged)


def docx_to_html(docx_path: str) -> str:
    """Convert DOCX -> HTML, preferring mammoth, falling back to the builtin."""
    try:
        import mammoth  # type: ignore

        with open(docx_path, "rb") as fh:
            result = mammoth.convert_to_html(fh)
        return _wrap_html(result.value, title=os.path.basename(docx_path))
    except ImportError:
        body = _docx_to_html_builtin(docx_path)
        return _wrap_html(body, title=os.path.basename(docx_path))


def render_html_to_pdf(html_path: str, out_pdf: str, out_png: str | None) -> None:
    """Load an HTML file in the persistent browser and save PDF (+ PNG)."""
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    browser_dir = os.path.join(REPO_ROOT, "browser")
    if browser_dir not in sys.path:
        sys.path.insert(0, browser_dir)
    from browser_interface import BrowserInterface

    browser = BrowserInterface.connect_cdp()
    try:
        browser.goto("file://" + os.path.abspath(html_path), wait_until="load")
        browser.sleep(1.2)
        browser.pdf(os.path.abspath(out_pdf))
        if out_png:
            browser.screenshot(os.path.abspath(out_png), full_page=True)
    finally:
        browser.stop()  # disconnect only; persistent browser stays alive


def convert(src: str, out_pdf: str | None = None, out_png: str | None = None,
            make_png: bool = True) -> dict:
    """Convert an HTML/DOCX file to PDF (+ optional PNG). Returns a summary dict."""
    src = os.path.abspath(src)
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    stem, ext = os.path.splitext(src)
    ext = ext.lower()

    if out_pdf is None:
        out_pdf = stem + ".pdf"
    if make_png and out_png is None:
        out_png = stem + "_preview.png"
    if not make_png:
        out_png = None

    tmp_html = None
    if ext in (".html", ".htm"):
        html_path = src
    elif ext == ".docx":
        html = docx_to_html(src)
        tmp_html = stem + ".__doc2pdf__.html"
        with open(tmp_html, "w", encoding="utf-8") as fh:
            fh.write(html)
        html_path = tmp_html
    else:
        raise ValueError(f"Unsupported input type: {ext} (use .html or .docx)")

    try:
        render_html_to_pdf(html_path, out_pdf, out_png)
    finally:
        if tmp_html and os.path.exists(tmp_html):
            os.remove(tmp_html)

    summary = {
        "input": src,
        "pdf": out_pdf if os.path.exists(out_pdf) else None,
        "png": out_png if (out_png and os.path.exists(out_png)) else None,
        "pdf_bytes": os.path.getsize(out_pdf) if os.path.exists(out_pdf) else 0,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="doc_to_pdf",
        description="Convert HTML or DOCX to PDF (+ preview PNG) via the browser, no LibreOffice.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", help="Input .html or .docx file")
    p.add_argument("--out", default=None, help="Output PDF path (default: <input>.pdf)")
    p.add_argument("--png", default=None, help="Preview PNG path (default: <input>_preview.png)")
    p.add_argument("--no-png", action="store_true", help="Skip the preview PNG")
    a = p.parse_args(argv)
    try:
        summary = convert(a.file, out_pdf=a.out, out_png=a.png, make_png=not a.no_png)
    except Exception as e:  # noqa: BLE001 - surface a clean message to the CLI
        sys.stderr.write(f"ERROR: {e}\n")
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
