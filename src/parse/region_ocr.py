"""
Region-level OCR fallback (Strategy B2).

When per-glyph CMap reconstruction (font_cmap.py) fails or Tesseract is
unavailable for glyph-level OCR, this module renders a cropped page region
at high resolution and runs full Tesseract OCR on the image.

Useful for cells with mixed Greek/Latin content or where the CMap glyph OCR
produced low-confidence results.

Graceful degradation: ocr_region() returns None when Tesseract is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pdfplumber

# Lazy Tesseract import
_pytesseract = None
_TESSERACT_CHECKED = False


def _get_pytesseract():
    global _pytesseract, _TESSERACT_CHECKED
    if _TESSERACT_CHECKED:
        return _pytesseract
    _TESSERACT_CHECKED = True
    try:
        import pytesseract as _pt

        cmd = os.environ.get("TESSERACT_CMD", "tesseract")
        _pt.pytesseract.tesseract_cmd = cmd
        _pt.get_tesseract_version()
        _pytesseract = _pt
    except Exception:
        _pytesseract = None
    return _pytesseract


def tesseract_available() -> bool:
    return _get_pytesseract() is not None


def ocr_region(
    page: "pdfplumber.page.Page",
    bbox: tuple[float, float, float, float],
    lang: str = "ell+eng",
    resolution: int = 300,
) -> str | None:
    """
    Render a bounding-box region of a pdfplumber page and OCR it.

    Parameters
    ----------
    page : pdfplumber Page object
    bbox : (x0, top, x1, bottom) in page coordinates
    lang : Tesseract language string
    resolution : DPI for rendering (300 is good for small text)

    Returns
    -------
    Decoded string, or None if Tesseract is unavailable or rendering fails.
    """
    pt = _get_pytesseract()
    if pt is None:
        return None

    try:
        cropped = page.crop(bbox)
        img = cropped.to_image(resolution=resolution).original
        text = pt.image_to_string(
            img,
            lang=lang,
            config="--psm 6 --oem 3",
        ).strip()
        return text or None
    except Exception:
        return None


def ocr_cell(
    page: "pdfplumber.page.Page",
    cell_chars: list[dict],
    lang: str = "ell+eng",
    resolution: int = 300,
    padding: float = 2.0,
) -> str | None:
    """
    OCR the page region spanned by a list of pdfplumber char objects.

    Parameters
    ----------
    cell_chars : list of char dicts from page.chars (each has x0,top,x1,bottom)
    """
    if not cell_chars:
        return None
    x0 = min(c["x0"] for c in cell_chars) - padding
    top = min(c["top"] for c in cell_chars) - padding
    x1 = max(c["x1"] for c in cell_chars) + padding
    bottom = max(c["bottom"] for c in cell_chars) + padding
    return ocr_region(page, (x0, top, x1, bottom), lang=lang, resolution=resolution)
