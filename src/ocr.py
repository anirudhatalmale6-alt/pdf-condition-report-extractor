"""Optical Character Recognition for scanned (image-only) condition reports.

Some real-world reports are not digital PDFs but scans/photos of a printed,
filled-in form - every page is a flat image with no text layer at all. For
those, PyMuPDF's get_text() returns nothing, so the normal text parsing has
nothing to work with. This module renders each page to an image and runs
Tesseract OCR to recover a text layer, which the extractor then parses exactly
as it would a digital PDF.

Tesseract is bundled with the Windows build (see build.yml / orbas.spec) so the
app stays fully self-contained and works offline. During local development the
system-installed tesseract is used instead.
"""

import os
import sys
import logging

logger = logging.getLogger(__name__)

# Render scanned pages at ~300 DPI (a 4.17x zoom on the 72 DPI PDF grid). High
# enough for Tesseract to read small typed comments; not so high it is slow.
_OCR_ZOOM = 4.17

_tesseract_ready = None  # tri-state: None = not probed, True/False = probed


def _bundle_dir():
    """Directory the app runs from (PyInstaller onedir) or the source tree."""
    if getattr(sys, "frozen", False):
        # onedir build: resources sit next to the executable / in _internal.
        exe_dir = os.path.dirname(sys.executable)
        for cand in (exe_dir, os.path.join(exe_dir, "_internal"),
                     getattr(sys, "_MEIPASS", exe_dir)):
            if os.path.isdir(os.path.join(cand, "tesseract")):
                return cand
        return exe_dir
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _configure_tesseract():
    """Point pytesseract at the bundled tesseract.exe + tessdata when frozen.

    Returns True if an OCR engine is usable, False otherwise. Result is cached.
    """
    global _tesseract_ready
    if _tesseract_ready is not None:
        return _tesseract_ready

    try:
        import pytesseract  # noqa: F401
    except Exception as e:
        logger.warning("pytesseract not importable: %s", e)
        _tesseract_ready = False
        return False

    import pytesseract as pt

    # Prefer a tesseract bundled alongside the app (self-contained Windows exe).
    base = _bundle_dir()
    bundled_dir = os.path.join(base, "tesseract")
    exe_names = ("tesseract.exe", "tesseract")
    for name in exe_names:
        cand = os.path.join(bundled_dir, name)
        if os.path.isfile(cand):
            pt.pytesseract.tesseract_cmd = cand
            # Tesseract expects TESSDATA_PREFIX to be the *parent* of tessdata.
            # tesseract.exe also auto-resolves a sibling tessdata folder, so
            # this simply makes that explicit and version-independent.
            if os.path.isdir(os.path.join(bundled_dir, "tessdata")):
                os.environ["TESSDATA_PREFIX"] = bundled_dir
            break

    # Verify whichever binary we ended up with actually runs.
    try:
        pt.get_tesseract_version()
        _tesseract_ready = True
    except Exception as e:
        logger.warning("Tesseract not available: %s", e)
        _tesseract_ready = False
    return _tesseract_ready


def is_available():
    return _configure_tesseract()


def ocr_page_text(fitz_page, zoom=_OCR_ZOOM):
    """Return the OCR'd text for one PyMuPDF page, or "" if OCR is unavailable.

    The page is rasterised to a greyscale image and passed to Tesseract. Any
    failure (engine missing, render error) degrades gracefully to an empty
    string so a scanned page simply yields no text rather than crashing.
    """
    if not _configure_tesseract():
        return ""
    try:
        import fitz
        import pytesseract
        from PIL import Image
        from io import BytesIO

        pix = fitz_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                   colorspace=fitz.csGRAY)
        img = Image.open(BytesIO(pix.tobytes("png")))
        # --psm 3 (fully automatic page segmentation) reads these multi-column
        # forms - the two-column header row and the condition grid - more
        # reliably than a single-block assumption.
        text = pytesseract.image_to_string(img, config="--psm 3")
        return text or ""
    except Exception as e:
        logger.warning("OCR failed on page %s: %s",
                       getattr(fitz_page, "number", "?"), e)
        return ""
