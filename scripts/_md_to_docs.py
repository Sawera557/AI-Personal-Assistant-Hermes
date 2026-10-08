"""Convert the resume/cover-letter markdown into DOCX + PDF.

Deliberately narrow: handles exactly the markdown subset those two files use
(H1/H2/H3, bold-inline, italic-inline, bullets, horizontal rules, paragraphs).
Not a general markdown converter.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor
from xhtml2pdf import pisa

ACCENT = RGBColor(0x1A, 0x1A, 0x1A)
INLINE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*)")
MDLINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _add_runs(par, text: str, *, bold=False, size=None, color=None):
    """Split on **bold** / *italic* markers and emit runs.

    DOCX gets link text only — python-docx cannot emit hyperlink relationships
    without XML surgery, and the PDF is the primary artifact.
    """
    text = MDLINK.sub(r"\1", text)
    for piece in INLINE.split(text):
        if not piece:
            continue
        run = par.add_run()
        if piece.startswith("**") and piece.endswith("**"):
            run.text, run.bold = piece[2:-2], True
        elif piece.startswith("*") and piece.endswith("*"):
            run.text, run.italic = piece[1:-1], True
        else:
            run.text = piece
        if bold:
            run.bold = True
        if size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color


def build_docx(md: str, out: Path) -> None:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(9.5)
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(36)
        section.left_margin = section.right_margin = Pt(46)

    # Join wrapped lines inside paragraphs so DOCX reflows naturally.
    blocks: list[str] = []
    buf: list[str] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.startswith(("#", "- ", "---")):
            if buf:
                blocks.append(" ".join(buf))
                buf = []
            blocks.append(line)
        else:
            buf.append(line.strip())
    if buf:
        blocks.append(" ".join(buf))

    for line in blocks:
        s = line.strip()
        if not s or s == "---":
            continue
        if s.startswith("# "):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _add_runs(p, s[2:], bold=True, size=19, color=ACCENT)
            p.paragraph_format.space_after = Pt(2)
        elif s.startswith("## "):
            p = doc.add_paragraph()
            _add_runs(p, s[3:], bold=True, size=11.5, color=ACCENT)
            p.paragraph_format.space_before = Pt(11)
            p.paragraph_format.space_after = Pt(3)
        elif s.startswith("### "):
            p = doc.add_paragraph()
            _add_runs(p, s[4:], bold=True, size=10.5)
            p.paragraph_format.space_before = Pt(7)
            p.paragraph_format.space_after = Pt(2)
        elif s.startswith("- "):
            p = doc.add_paragraph(style="List Bullet")
            _add_runs(p, s[2:])
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.left_indent = Pt(14)
        else:
            p = doc.add_paragraph()
            _add_runs(p, s)
            p.paragraph_format.space_after = Pt(4)
    doc.save(out)


CSS = """
@page { size: A4; margin: 1.05cm 1.25cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 8.0pt;
       line-height: 1.22; color: #111; }
h1 { font-size: 17pt; text-align: center; margin: 0 0 1pt 0; letter-spacing: 0.5pt; }
h2 { font-size: 9.6pt; margin: 7pt 0 2pt 0; padding-bottom: 1.2pt;
     border-bottom: 0.8pt solid #333; text-transform: uppercase; letter-spacing: 0.6pt; }
h3 { font-size: 8.9pt; margin: 5.5pt 0 1pt 0; }
p  { margin: 0 0 2.8pt 0; }
ul { margin: 0 0 3pt 0; padding-left: 10pt; }
li { margin: 0 0 1.6pt 0; }
hr { border: none; border-top: 0.6pt solid #bbb; margin: 4pt 0; }
.sub { text-align: center; font-size: 9.4pt; margin-bottom: 1pt; }
.contact { text-align: center; font-size: 8.2pt; color: #444; margin-bottom: 1.5pt; }
.contact a { color: #1a4f8a; text-decoration: none; }
a.ln { color: #1a4f8a; text-decoration: none; }
em { color: #333; }
.tbl { width: 100%; border-collapse: collapse; margin: 3pt 0 6pt 0; }
.tbl th { background: #eee; border: 0.5pt solid #999; padding: 2.5pt 3.5pt;
          text-align: left; font-size: 7.6pt; }
.tbl td { border: 0.5pt solid #bbb; padding: 2.5pt 3.5pt; font-size: 7.6pt;
          vertical-align: top; }
"""


_URL = re.compile(r"\b((?:linkedin\.com|github\.com|sawera557\.github\.io)/[A-Za-z0-9._/-]+)")
_MAIL = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def linkify(html_fragment: str) -> str:
    """Make the contact row's URLs and email clickable in the PDF."""
    s = _URL.sub(r'<a href="https://\1">\1</a>', html_fragment)
    return _MAIL.sub(r'<a href="mailto:\1">\1</a>', s)


def md_to_html(md: str) -> str:
    out: list[str] = []
    in_ul = False
    lines = md.splitlines()

    def inline(t: str) -> str:
        # Pull md links out before escaping so the URL survives intact.
        links: list[tuple[str, str]] = []

        def stash(m):
            links.append((m.group(1), m.group(2)))
            return f"\x00{len(links) - 1}\x00"

        t = MDLINK.sub(stash, t)
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<em>\1</em>", t)
        for i, (label, url) in enumerate(links):
            t = t.replace(f"\x00{i}\x00",
                          f'<a class="ln" href="{html.escape(url, quote=True)}">'
                          f'{html.escape(label)}</a>')
        return t

    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para = []

    i = -1
    while i + 1 < len(lines):
        i += 1
        raw = lines[i]
        s = raw.strip()
        # --- markdown table: header row, separator row, then body rows ---
        if (s.startswith("|") and i + 1 < len(lines)
                and set(lines[i + 1].strip()) <= set("|-: ")
                and "-" in lines[i + 1]):
            flush_para()
            if in_ul:
                out.append("</ul>")
                in_ul = False

            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]

            out.append('<table class="tbl"><thead><tr>')
            out.extend(f"<th>{inline(c)}</th>" for c in cells(s))
            out.append("</tr></thead><tbody>")
            i += 1  # skip separator
            while i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
                i += 1
                out.append("<tr>")
                out.extend(f"<td>{inline(c)}</td>" for c in cells(lines[i]))
                out.append("</tr>")
            out.append("</tbody></table>")
            continue
        if s.startswith("- "):
            flush_para()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(s[2:])}</li>")
            continue
        if in_ul and not s.startswith("- ") and s:
            out.append("</ul>")
            in_ul = False
        if not s:
            flush_para()
            continue
        if s == "---":
            flush_para()
            continue
        if s.startswith("### "):
            flush_para()
            out.append(f"<h3>{inline(s[4:])}</h3>")
        elif s.startswith("## "):
            flush_para()
            out.append(f"<h2>{inline(s[3:])}</h2>")
        elif s.startswith("# "):
            flush_para()
            out.append(f"<h1>{inline(s[2:])}</h1>")
        # The lines under the H1 are the tagline + contact rows.
        elif i < 8 and "|" in s and not s.startswith("**"):
            flush_para()
            out.append(f'<p class="contact">{linkify(inline(s))}</p>')
        elif i < 6 and s.startswith("**") and s.endswith("**"):
            flush_para()
            out.append(f'<p class="sub">{inline(s)}</p>')
        else:
            para.append(s)
    flush_para()
    if in_ul:
        out.append("</ul>")
    return f"<html><head><style>{CSS}</style></head><body>{''.join(out)}</body></html>"


def build_pdf(md: str, out: Path) -> bool:
    with out.open("wb") as fh:
        result = pisa.CreatePDF(md_to_html(md), dest=fh, encoding="utf-8")
    return not result.err


if __name__ == "__main__":
    for src in sys.argv[1:]:
        p = Path(src)
        md = p.read_text(encoding="utf-8")
        docx_out, pdf_out = p.with_suffix(".docx"), p.with_suffix(".pdf")
        notes = []
        for out, build in ((docx_out, build_docx), (pdf_out, build_pdf)):
            try:
                build(md, out)
                notes.append(f"{out.suffix[1:].upper()} ok")
            except PermissionError:
                notes.append(f"{out.suffix[1:].upper()} LOCKED (close it in Word/Acrobat)")
        print(f"{p.name}: " + " | ".join(notes))
