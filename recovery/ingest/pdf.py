"""PDF text extraction, OCR fallback, and chunking — stateless, DB-agnostic.

Two halves:
  - extract_pages(): pull the existing text layer per page; for pages with no
    usable text, optionally OCR a rendered image of the page via a vision LLM.
  - chunk_text(): mixed-bag chunking — structural split with a paragraph
    fallback, fragment merging, and a hard size cap.
"""
from __future__ import annotations

import base64
import os
import re

import fitz  # PyMuPDF

# --- Tunable knobs -----------------------------------------------------------
# Pages with fewer than this many extracted chars are treated as scanned/empty
# and routed to OCR (if enabled). Raise if text PDFs are wrongly hitting OCR.
MIN_TEXT_CHARS_PER_PAGE = 40
# OCR render resolution. Higher = sharper but bigger payloads; 200 is plenty.
OCR_DPI = 200
# Chunk sizing (characters). MAX caps a single chunk; MIN merges tiny fragments
# into the previous chunk so we don't index one-line scraps.
MAX_CHUNK_CHARS = 1500
MIN_CHUNK_CHARS = 200
# -----------------------------------------------------------------------------


class OcrUnavailable(RuntimeError):
    """Raised when OCR is needed but ANTHROPIC_API_KEY is missing."""


def ocr_page(img_bytes: bytes, model: str) -> str:
    """OCR a single rendered PDF page via a vision LLM (Haiku by default).

    Lazy-imports anthropic and reads ANTHROPIC_API_KEY so the app runs fine
    without the key for text-only PDFs. Raises OcrUnavailable if the key is
    missing, letting the caller skip the page gracefully.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise OcrUnavailable("ANTHROPIC_API_KEY not set; cannot OCR scanned pages.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(img_bytes).decode("ascii")
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": (
                    "Transcribe all text in this page image exactly, preserving "
                    "headings and reading order. Output only the transcribed text, "
                    "no commentary. If the page is blank, output nothing."
                )},
            ],
        }],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def extract_pages(
    pdf_bytes: bytes,
    *,
    ocr_enabled: bool = True,
    ocr_model: str = "claude-haiku-4-5",
) -> tuple[list[tuple[int, str]], bool]:
    """Extract text per page; OCR-fallback pages with no usable text layer.

    Returns (pages, used_ocr) where pages is [(page_number_1indexed, text)] for
    pages that yielded text, and used_ocr is True if any page went through OCR.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[tuple[int, str]] = []
    used_ocr = False
    try:
        for i, page in enumerate(doc, start=1):
            text = (page.get_text() or "").strip()
            if len(text) < MIN_TEXT_CHARS_PER_PAGE and ocr_enabled:
                try:
                    pix = page.get_pixmap(dpi=OCR_DPI)
                    ocr_text = ocr_page(pix.tobytes("png"), ocr_model).strip()
                    if ocr_text:
                        text = ocr_text
                        used_ocr = True
                except OcrUnavailable:
                    # No key — leave whatever (little) text we had; caller decides.
                    pass
                except Exception:
                    # OCR failed for this page; skip it rather than fail the upload.
                    pass
            if text:
                pages.append((i, text))
    finally:
        doc.close()
    return pages, used_ocr


def _hard_split(chunk: str) -> list[str]:
    """Split an oversize chunk on sentence boundaries, packing up to the cap."""
    if len(chunk) <= MAX_CHUNK_CHARS:
        return [chunk]
    sentences = re.split(r"(?<=[.!?])\s+", chunk)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > MAX_CHUNK_CHARS:
            out.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur.strip():
        out.append(cur.strip())
    return out


def chunk_text(text: str) -> list[str]:
    """Mixed-bag chunking: structural split, paragraph fallback, merge + cap."""
    text = text.strip()
    if not text:
        return []

    # Primary: split before markdown headers or "N. " numbered sections.
    parts = re.split(r"\n(?=#{1,3} |\d+\.\s)", text)
    parts = [p.strip() for p in parts if p.strip()]

    # Fallback: if structural split found nothing (one big blob), use paragraphs.
    if len(parts) <= 1:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not parts:
        parts = [text]

    # Merge fragments under MIN into the previous chunk; hard-split oversize.
    merged: list[str] = []
    for p in parts:
        if merged and len(merged[-1]) < MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]}\n\n{p}"
        else:
            merged.append(p)

    chunks: list[str] = []
    for m in merged:
        chunks.extend(_hard_split(m))
    return [c for c in chunks if c.strip()]
