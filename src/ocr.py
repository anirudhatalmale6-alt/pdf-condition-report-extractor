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

IMPORTANT (Windows/PyInstaller): the app ships as a *windowed* (no-console)
executable. In that mode a child process launched with the default handles can
fail with "the handle is invalid", which is exactly why an earlier build read
scanned PDFs as empty. We therefore invoke tesseract.exe ourselves with fully
specified, safe handles (stdin=DEVNULL, stdout/stderr=PIPE, CREATE_NO_WINDOW)
instead of relying on any library's default subprocess behaviour.
"""

import os
import sys
import shutil
import logging
import tempfile
import subprocess

logger = logging.getLogger(__name__)

# Render scanned pages at ~300 DPI (a 4.17x zoom on the 72 DPI PDF grid). High
# enough for Tesseract to read small typed comments; not so high it is slow.
_OCR_ZOOM = 4.17

# Windows: don't pop a console window for each tesseract call.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_tesseract_cmd = None       # resolved path to the tesseract binary, or None
_tesseract_ready = None     # tri-state: None = not probed, True/False = probed
_status = "not probed"      # human-readable diagnostic surfaced in the JSON


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


def _resolve_cmd():
    """Locate a usable tesseract binary: bundled first, then system PATH."""
    base = _bundle_dir()
    bundled_dir = os.path.join(base, "tesseract")
    for name in ("tesseract.exe", "tesseract"):
        cand = os.path.join(bundled_dir, name)
        if os.path.isfile(cand):
            # Point tesseract at the bundled language data.
            if os.path.isdir(os.path.join(bundled_dir, "tessdata")):
                os.environ["TESSDATA_PREFIX"] = bundled_dir
            return cand
    # Fall back to whatever is on PATH (local development / system install).
    found = shutil.which("tesseract")
    return found


def _run(args, **kw):
    """Run a tesseract command with handles that are always valid, even in a
    windowed (no-console) PyInstaller build."""
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )


def _configure():
    """Resolve + verify the tesseract binary once. Caches the result and a
    human-readable status string for diagnostics."""
    global _tesseract_cmd, _tesseract_ready, _status
    if _tesseract_ready is not None:
        return _tesseract_ready

    cmd = _resolve_cmd()
    if not cmd:
        _tesseract_ready = False
        _status = "tesseract binary not found (bundle missing and none on PATH)"
        logger.warning(_status)
        return False

    try:
        proc = _run([cmd, "--version"])
        ver = (proc.stdout or b"").decode("utf-8", "replace").splitlines()
        ver = ver[0].strip() if ver else "unknown"
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            _tesseract_ready = False
            _status = "tesseract failed to start (rc=%s): %s" % (
                proc.returncode, err or "no output")
            logger.warning(_status)
            return False
        _tesseract_cmd = cmd
        _tesseract_ready = True
        _status = "ready (%s at %s)" % (ver, cmd)
        return True
    except Exception as e:
        _tesseract_ready = False
        _status = "tesseract could not be launched: %s" % e
        logger.warning(_status)
        return False


def is_available():
    return _configure()


def status():
    """Human-readable OCR engine status, safe to surface in the output JSON."""
    _configure()
    return _status


def ocr_page_text(fitz_page, zoom=_OCR_ZOOM):
    """Return the OCR'd text for one PyMuPDF page, or "" if OCR is unavailable.

    The page is rasterised to a greyscale PNG and passed to tesseract, which we
    invoke directly (see module docstring) so it works in the windowed build.
    Any failure degrades gracefully to an empty string.
    """
    if not _configure():
        return ""
    tmp_path = None
    try:
        import fitz

        pix = fitz_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                   colorspace=fitz.csGRAY)
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="orbas_ocr_")
        os.close(fd)
        pix.save(tmp_path)
        # "stdout" tells tesseract to write the recognised text to stdout.
        # --psm 3 (fully automatic page segmentation) reads these multi-column
        # forms - the two-column header row and the condition grid - more
        # reliably than a single-block assumption.
        proc = _run([_tesseract_cmd, tmp_path, "stdout", "--psm", "3"])
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
            logger.warning("tesseract rc=%s on page %s: %s",
                           proc.returncode,
                           getattr(fitz_page, "number", "?"), err)
            return ""
        return (proc.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        logger.warning("OCR failed on page %s: %s",
                       getattr(fitz_page, "number", "?"), e)
        return ""
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
