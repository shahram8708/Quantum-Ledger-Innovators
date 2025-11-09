"""PDF utilities.

This module primarily uses a JavaScript renderer (powered by pdf.js)
to convert PDF pages into Pillow images.  If the Node.js workflow is
unavailable it falls back to a pure-Python implementation using
PyMuPDF.  For non-PDF images we simply load the file via Pillow.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

import fitz
from PIL import Image

logger = logging.getLogger(__name__)


class PDFProcessingError(RuntimeError):
    """Raised when rendering a PDF file fails."""


def _node_renderer_ready() -> tuple[bool, str | None]:
    if shutil.which("node") is None:
        return False, "Node.js runtime not found in PATH"
    package_dir = Path(__file__).resolve().parents[2] / "node_modules" / "@napi-rs" / "canvas"
    if not package_dir.exists():
        return False, "Node renderer dependencies missing; run 'npm install' in the project root"
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "render_pdf.mjs"
    if not script_path.exists():
        return False, f"JavaScript renderer script not found at {script_path}"
    return True, None


def pdf_to_images(pdf_path: str, dpi: int = 200) -> List[Image.Image]:
    """Render a PDF file into a list of Pillow RGB images.

    Preference is given to the Node.js/pdf.js pipeline if available.
    Set the environment variable ``PDF_RENDERER`` to ``python`` to
    force the PyMuPDF fallback.
    """
    renderer_pref = os.environ.get("PDF_RENDERER", "auto").lower()
    if renderer_pref in {"node", "javascript", "auto"}:
        ready, message = _node_renderer_ready()
        if ready:
            try:
                return _pdf_to_images_via_node(pdf_path, dpi)
            except PDFProcessingError as exc:
                if renderer_pref == "node":
                    raise
                logger.warning("Node-based PDF renderer failed; falling back to PyMuPDF: %s", exc)
        else:
            if renderer_pref == "node":
                raise PDFProcessingError(message or "Node renderer unavailable")
            if message:
                logger.info("Node renderer unavailable (%s); using PyMuPDF fallback", message)

    return _pdf_to_images_via_pymupdf(pdf_path, dpi)


def _pdf_to_images_via_node(pdf_path: str, dpi: int) -> List[Image.Image]:
    scale = max(dpi / 72.0, 0.5)
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "render_pdf.mjs"

    temp_dir = Path(tempfile.mkdtemp(prefix="pdf-node-"))
    try:
        cmd = [
            "node",
            str(script_path),
            "--input",
            str(Path(pdf_path).resolve()),
            "--output",
            str(temp_dir),
            "--scale",
            f"{scale:.4f}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        stdout = proc.stdout.strip()
        files: List[str] = []
        if stdout:
            try:
                payload = json.loads(stdout)
                files = [str(p) for p in payload.get("files", [])]
            except json.JSONDecodeError as exc:
                raise PDFProcessingError(f"Invalid JSON from Node renderer: {stdout}") from exc

        if not files:
            files = [str(p) for p in sorted(temp_dir.glob("page_*.png"))]

        if not files:
            raise PDFProcessingError("Node renderer produced no images")

        images: List[Image.Image] = []
        for file_path in files:
            path = Path(file_path)
            if not path.is_absolute():
                path = temp_dir / path
            if not path.exists():
                continue
            with Image.open(path) as img:
                images.append(img.convert("RGB"))

        if not images:
            raise PDFProcessingError("Node renderer images could not be loaded")
        return images
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise PDFProcessingError(f"Node renderer failed (exit {exc.returncode}): {stderr}") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _pdf_to_images_via_pymupdf(pdf_path: str, dpi: int) -> List[Image.Image]:
    """Render PDF pages using the PyMuPDF engine."""
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # pragma: no cover - defensive
        raise PDFProcessingError(f"Unable to open PDF '{pdf_path}': {exc}") from exc

    images: List[Image.Image] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    try:
        for page_index in range(doc.page_count):
            try:
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                with io.BytesIO(pix.tobytes("png")) as buffer:
                    image = Image.open(buffer)
                    images.append(image.convert("RGB"))
            except Exception as exc:  # pragma: no cover - defensive
                raise PDFProcessingError(
                    f"Failed to render page {page_index + 1} of '{pdf_path}': {exc}"
                ) from exc
    finally:
        doc.close()

    if not images:
        raise PDFProcessingError(f"No renderable pages found in '{pdf_path}'")

    return images


def open_image_file(image_path: str) -> List[Image.Image]:
    """Open an image file into a list with a single PIL Image.

    Args:
        image_path: Path to an image file (JPEG/PNG/TIFF).

    Returns:
        A list with a single PIL Image.
    """
    img = Image.open(image_path)
    return [img.convert("RGB")]