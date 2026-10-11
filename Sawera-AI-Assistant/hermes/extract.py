"""Document text extraction ladder for files fetched back out of S3.

Turns raw file bytes into plain text the agent can read in-conversation:

* DOCX               -> python-docx (paragraphs + table cells)
* XLSX / XLSM        -> openpyxl (every sheet, row cells joined)
* PPTX               -> python-pptx (slide text + tables)
* PDF / image/*      -> NuExtract OCR (handles scanned docs and phone photos)
* text/json/csv/...  -> decode UTF-8 directly

OCR is not optional: most tenant documents are phone photos, so a text-layer
reader alone would fail on real traffic. NuExtract and python-docx both run
independent of the global agent-model switch, so extraction works whether the
active chat model is Claude or OpenAI.
"""
from __future__ import annotations

import logging

log = logging.getLogger("hermes.extract")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Text-like MIME hints we can decode without OCR (mirrors ingest._extract_text).
_TEXT_HINTS = ("text/", "json", "csv", "xml", "markdown", "yaml", "html")

class ExtractionError(RuntimeError):
    """Raised with a message meant to be relayed to the user."""


def extract_text(filename: str, raw: bytes, mime: str) -> str:
    """Extract plain text from a file's bytes. Raises ExtractionError on an
    unsupported type or when the underlying engine fails."""
    mime = (mime or "").lower().split(";")[0].strip()
    name = (filename or "").lower()

    if mime == DOCX_MIME or name.endswith(".docx"):
        return _extract_docx(raw)

    if mime == XLSX_MIME or name.endswith((".xlsx", ".xlsm")):
        return _extract_xlsx(raw)

    if mime == PPTX_MIME or name.endswith(".pptx"):
        return _extract_pptx(raw)

    # HEIC/HEIF has no decoder in-repo and NuExtract doesn't reliably handle it;
    # reject with a clear message rather than a confusing upstream error. (WhatsApp
    # normally delivers JPEG, so this is rare in practice.)
    if mime in ("image/heic", "image/heif") or name.endswith((".heic", ".heif")):
        raise ExtractionError(
            f"'{filename}' is a HEIC/HEIF image, which can't be read yet. Ask the "
            "sender to share it as a JPEG or PDF instead."
        )

    if mime == "application/pdf" or name.endswith(".pdf") or mime.startswith("image/"):
        return _extract_pdf_or_image(filename, raw, mime)

    if any(h in mime for h in _TEXT_HINTS) or mime == "":
        try:
            decoded = raw.decode("utf-8")
            if "\x00" not in decoded:  # reject binary that happens to decode
                return decoded
        except UnicodeDecodeError:
            pass
        raise ExtractionError(
            f"Couldn't read '{filename}' as text, and its type ({mime or 'unknown'}) "
            "isn't a supported document format."
        )

    # HEIC and other formats NuExtract/python-docx can't handle. There is no
    # HEIC decoder in-repo; WhatsApp normally delivers JPEG, so this is rare.
    raise ExtractionError(
        f"Unsupported document type for '{filename}' ({mime}). Supported: PDF, "
        "JPEG/PNG images, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and "
        "text/CSV/JSON files."
    )


def _extract_docx(raw: bytes) -> str:
    import io

    try:
        from docx import Document  # python-docx
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExtractionError("DOCX support requires the python-docx package.") from exc

    try:
        doc = Document(io.BytesIO(raw))
    except Exception as exc:
        raise ExtractionError(f"Couldn't open the DOCX file: {exc}") from exc

    parts: list[str] = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    if not text:
        raise ExtractionError("The DOCX file contained no readable text.")
    return text


def _extract_xlsx(raw: bytes) -> str:
    """Every sheet of an .xlsx/.xlsm workbook as text — one line per non-empty
    row, cells joined by ' | ', prefixed by a '# Sheet: <name>' header. Uses
    computed cell values (data_only) so formulas come through as their result."""
    import io

    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExtractionError("Excel (.xlsx) support requires the openpyxl package.") from exc

    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractionError(f"Couldn't open the Excel file: {exc}") from exc

    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"# Sheet: {ws.title}")
                parts.extend(rows)
    finally:
        wb.close()

    text = "\n".join(parts).strip()
    if not text:
        raise ExtractionError("The Excel file contained no readable cells.")
    return text


def _extract_pptx(raw: bytes) -> str:
    """Text of every slide in a .pptx — shape text frames and table cells,
    grouped under a '# Slide N' header."""
    import io

    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ExtractionError("PowerPoint (.pptx) support requires the python-pptx package.") from exc

    try:
        prs = Presentation(io.BytesIO(raw))
    except Exception as exc:
        raise ExtractionError(f"Couldn't open the PowerPoint file: {exc}") from exc

    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        slide_lines.append(line)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        slide_lines.append(" | ".join(cells))
        if slide_lines:
            parts.append(f"# Slide {i}")
            parts.extend(slide_lines)

    text = "\n".join(parts).strip()
    if not text:
        raise ExtractionError("The PowerPoint file contained no readable text.")
    return text


def _extract_pdf_or_image(filename: str, raw: bytes, mime: str) -> str:
    """Read a PDF or image, preferring NuExtract but falling back to Claude
    vision whenever NuExtract can't deliver — billing exhausted, outage, DNS,
    or an empty result. OCR is not optional on real traffic (mostly phone
    photos), so a single-vendor failure must not take reading down."""
    try:
        text = _extract_via_nuextract(filename, raw, mime)
        if text.strip():
            return text
        log.info("NuExtract returned no text for %s; trying Claude vision", filename)
    except ExtractionError as exc:
        log.warning("NuExtract failed for %s (%s); trying Claude vision", filename, exc)

    try:
        return _extract_via_claude(filename, raw, mime)
    except ExtractionError as claude_exc:
        raise ExtractionError(
            f"Couldn't read '{filename}': NuExtract was unavailable and the Claude "
            f"vision fallback also failed ({claude_exc})."
        ) from claude_exc


def _extract_via_nuextract(filename: str, raw: bytes, mime: str) -> str:
    # Lazy import: the NuExtract transport lives in bills, which pulls in the DB
    # layer — no need to load that just to import this module.
    from . import bills

    # Content extraction returns verbatim Markdown — exactly the transcription
    # this OCR path wants, so use it directly instead of a structured schema.
    try:
        text = bills.nuextract_content(filename, raw, mime).strip()
    except RuntimeError as exc:
        raise ExtractionError(str(exc)) from exc

    if text:
        return text
    raise ExtractionError(f"NuExtract returned no readable text for '{filename}'.")


# Image media types Anthropic's vision API accepts (HEIC is not among them and is
# rejected upstream in extract_text). jpg is normalized to jpeg.
_CLAUDE_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# One prompt for both PDFs and images. For text-bearing files it behaves as a
# verbatim OCR; for photos/scenes with no meaningful text it describes the image
# instead of failing — so a scenery photo comes back as "a landscape photo",
# not an error. Only a truly blank/corrupt file yields the BLANK_IMAGE sentinel.
_VISION_PROMPT = (
    "Read this file.\n"
    "1. Transcribe ALL text exactly as written, preserving line breaks, numbers, "
    "dates, and table row order. Do not summarize, translate, or omit any text.\n"
    "2. If the file has little or no text (e.g. a photograph, scene, logo, or "
    "object), then instead briefly describe what it shows in 1-3 sentences.\n"
    "Reply with only the transcription or the description — no preamble. Reply "
    "with exactly BLANK_IMAGE only if the file is entirely empty or too corrupt "
    "to interpret at all."
)

_anthropic_client = None


def _anthropic():
    """Lazy Anthropic client for the vision fallback. Built here (not imported
    from hermes.agent) to avoid an agent -> tools -> extract import cycle."""
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic  # lazy

        from .config import Config

        _anthropic_client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    return _anthropic_client


def _extract_via_claude(filename: str, raw: bytes, mime: str) -> str:
    """Read a PDF/image with a vision model — the fallback OCR path when
    NuExtract is unavailable.

    Runs on NVIDIA NIM, not Anthropic: OCR is background work triggered by
    uploads and sync jobs, and operator policy keeps background spend off the
    metered Anthropic/OpenAI accounts (see models.background_model). The name is
    kept because several modules import it.

    PDFs are the one thing this path loses by moving off Claude — the
    OpenAI-shaped vision API takes images only. Callers already handle
    ExtractionError, and PDFs have a real text-extraction path upstream; this
    fallback only ever mattered for scanned ones.
    """
    import base64

    from . import models
    from .config import Config

    media_type = "image/jpeg" if mime == "image/jpg" else (mime or "").lower()
    if media_type == "application/pdf" or filename.lower().endswith(".pdf"):
        raise ExtractionError(
            f"'{filename}' is a scanned PDF and NuExtract is unavailable. The "
            "vision fallback reads images only — convert the page to PNG/JPEG, "
            "or restore NuExtract."
        )
    if media_type not in _CLAUDE_IMAGE_MIMES:
        raise ExtractionError(f"Vision fallback can't read '{filename}' ({mime}).")

    model_id = models.background_model(preferred=Config.OCR_FALLBACK_MODEL, vision=True)
    b64 = base64.standard_b64encode(raw).decode("utf-8")
    try:
        from .llm_json import _openai_compatible

        resp = _openai_compatible("nvidia", model_id).chat.completions.create(
            model=model_id,
            max_tokens=8000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                ],
            }],
        )
        text = (resp.choices[0].message.content or "").strip()
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface a readable error
        raise ExtractionError(f"Vision request failed ({model_id}): {exc}") from exc

    # Only a genuinely blank/corrupt file fails now — a text-less photo comes back
    # as a description, not an error.
    if not text or text == "BLANK_IMAGE":
        raise ExtractionError(f"'{filename}' appears to be blank or unreadable.")
    log.info("Read %s via %s vision fallback (%d chars)", filename, model_id, len(text))
    return text
