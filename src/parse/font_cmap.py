"""
Per-PDF font CMap reconstruction.

Attempts to build a glyph_id -> Unicode character map for the Albany WT J
subset embedded in each declaration PDF, so that garbled Greek text can be
decoded back to readable Unicode.

Strategy (B1 in the implementation plan):
  1. Extract the embedded TTF from the PDF using pypdf.
  2. Render each glyph to a small bitmap via PIL.
  3. OCR each bitmap individually with Tesseract (Greek language model).
  4. Cache the result keyed on the font-data SHA-256 so identical subsets
     are only processed once per session.

Graceful degradation:
  If Tesseract is not installed or the TESSERACT_CMD env var is not set,
  build_cmap() raises TesseractUnavailable.  Callers should catch this and
  fall back to region_ocr.py or accept raw garbled text tagged 'positional'.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import NamedTuple

import pypdf
from fontTools import ttLib
from fontTools.ttLib.tables import _c_m_a_p as cmap_module
from PIL import Image, ImageDraw, ImageFont

# Lazy import so the module is importable even if pytesseract/Tesseract absent
_pytesseract = None
_TESSERACT_CHECKED = False


class TesseractUnavailable(RuntimeError):
    """Raised when Tesseract binary cannot be found."""


def _get_pytesseract():
    global _pytesseract, _TESSERACT_CHECKED
    if _TESSERACT_CHECKED:
        return _pytesseract
    _TESSERACT_CHECKED = True
    try:
        import pytesseract as _pt

        cmd = os.environ.get("TESSERACT_CMD", "tesseract")
        _pt.pytesseract.tesseract_cmd = cmd
        _pt.get_tesseract_version()   # raises TesseractNotFoundError if absent
        _pytesseract = _pt
    except Exception:
        _pytesseract = None
    return _pytesseract


# In-process cache: font_sha256 -> {glyph_id: char}
_CMAP_CACHE: dict[str, dict[int, str]] = {}


class FontInfo(NamedTuple):
    font_key: str       # e.g. "/F1"
    font_data: bytes
    font_sha: str


def extract_albany_font(pdf_path: str | Path) -> FontInfo | None:
    """Return the raw TTF bytes for the Albany WT J subset on page 1, or None."""
    reader = pypdf.PdfReader(str(pdf_path))
    page = reader.pages[0]
    try:
        fonts = page["/Resources"]["/Font"]
    except (KeyError, TypeError):
        return None

    for key in fonts:
        try:
            font_obj = fonts[key].get_object()
            base = str(font_obj.get("/BaseFont", ""))
            if "Albany" not in base:
                continue
            desc = font_obj["/DescendantFonts"][0].get_object()
            fd = desc["/FontDescriptor"].get_object()
            font_data = fd["/FontFile2"].get_object().get_data()
            sha = hashlib.sha256(font_data).hexdigest()
            return FontInfo(font_key=key, font_data=font_data, font_sha=sha)
        except (KeyError, TypeError, AttributeError):
            continue
    return None


def _build_pil_font(font_data: bytes, size: int = 64) -> tuple[ImageFont.FreeTypeFont, dict[int, str]]:
    """
    Load the TTF with a synthetic identity cmap and return (pil_font, cid_to_name).
    The synthetic cmap maps codepoint N -> glyph 'glyphN' for every glyph.
    """
    tt = ttLib.TTFont(io.BytesIO(font_data))
    glyph_names = tt.getGlyphOrder()

    cid_to_name: dict[int, str] = {}
    for name in glyph_names:
        if name.startswith("glyph"):
            try:
                cid = int(name[5:])
                cid_to_name[cid] = name
            except ValueError:
                pass

    # Add synthetic cmap so PIL can address glyphs by code point
    table = cmap_module.table__c_m_a_p()
    table.tableVersion = 0
    fmt4 = cmap_module.cmap_format_4(4)
    fmt4.platEncID = 3
    fmt4.platformID = 3
    fmt4.language = 0
    fmt4.cmap = {cid: name for cid, name in cid_to_name.items()}
    table.tables = [fmt4]
    tt["cmap"] = table

    tmp = Path(tempfile.mktemp(suffix=".ttf"))
    try:
        tt.save(str(tmp))
        pil_font = ImageFont.truetype(str(tmp), size=size)
    finally:
        tmp.unlink(missing_ok=True)

    return pil_font, cid_to_name


def _ocr_glyph(img: Image.Image, pt) -> str:
    """Run single-character OCR on a glyph image. Returns empty string on failure."""
    # Upscale for better OCR accuracy at small sizes
    scaled = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    try:
        text = pt.image_to_string(
            scaled,
            lang="ell",
            config="--psm 10 --oem 3 -c tessedit_char_whitelist="
            "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρστυφχψω"
            "ΆΈΉΊΌΎΏάέήίϊΐόύϋΰώ ,.-/()",
        ).strip()
        return text[:1] if text else ""
    except Exception:
        return ""


def build_cmap(pdf_path: str | Path) -> dict[int, str]:
    """
    Build and return a {glyph_id: unicode_char} map for the given PDF.

    Raises TesseractUnavailable if Tesseract is not installed.
    Returns an empty dict if no Albany font is found (e.g. fully Latin PDFs).
    """
    pt = _get_pytesseract()
    if pt is None:
        raise TesseractUnavailable(
            "Tesseract not found. Install it and set TESSERACT_CMD, "
            "or use region_ocr as the fallback."
        )

    font_info = extract_albany_font(pdf_path)
    if font_info is None:
        return {}

    if font_info.font_sha in _CMAP_CACHE:
        return _CMAP_CACHE[font_info.font_sha]

    pil_font, cid_to_name = _build_pil_font(font_info.font_data, size=64)

    cmap: dict[int, str] = {}
    for cid in sorted(cid_to_name.keys()):
        img = Image.new("L", (80, 80), 255)
        draw = ImageDraw.Draw(img)
        try:
            draw.text((8, 4), chr(cid), font=pil_font, fill=0)
        except Exception:
            continue
        char = _ocr_glyph(img, pt)
        if char:
            cmap[cid] = char

    _CMAP_CACHE[font_info.font_sha] = cmap
    return cmap


def decode_text(raw: str, cmap: dict[int, str]) -> tuple[str, float]:
    """
    Apply a cmap to a raw (garbled) string.

    Returns (decoded_text, confidence) where confidence is the fraction of
    characters successfully decoded.
    """
    if not raw or not cmap:
        return raw, 0.0
    decoded = []
    decoded_count = 0
    for ch in raw:
        cid = ord(ch)
        if ch in (" ", "\n", "\t") or ch.isascii():
            decoded.append(ch)
            decoded_count += 1
        elif cid in cmap:
            decoded.append(cmap[cid])
            decoded_count += 1
        else:
            decoded.append(ch)   # keep original — still garbled
    confidence = decoded_count / len(raw) if raw else 0.0
    return "".join(decoded), confidence
