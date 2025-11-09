"""Memos processing pipeline.

This module orchestrates the steps for processing an uploaded
Memos:

1. Convert the uploaded PDF or image to a list of images.
2. Compute a checksum and check for exact file duplicates.
3. Retrieve previously processed Memos for the dealer to
   detect duplicates by Memos number or embedding similarity.
4. Verify GSTINs via the configured adapter.
5. Call the local vision adapter to extract structured data and
   confidences.
6. Perform validations: GST rate checks, arithmetic, price
   benchmarking.
7. Generate a Markdown report and convert it to PDF.
8. Persist results in the database and update Memos status.

The core entrypoint is `process_Memos_file(Memos)` which
mutates the provided Memos model instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import logging
import re
from pathlib import Path
from typing import List, Dict, Any

from flask import current_app
from PIL import Image

from .. import db
from ..models import Memos, MemosEmbedding
from .pdf import pdf_to_images, open_image_file
from .embeddings import compute_embedding, build_faiss_index, search_similar
from ..gst_adapters import get_gst_adapter
from ..llm.vision_adapter import extract_Memos, generate_report


def _markdown_to_plaintext(markdown: str) -> str:
    lines: List[str] = []
    for raw in markdown.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            content = stripped[level:].strip()
            if level == 1:
                lines.append(content.upper())
            elif level == 2:
                lines.append(content.title())
            else:
                lines.append(content)
        elif stripped.startswith("* "):
            lines.append(f"• {stripped[2:].strip()}")
        else:
            lines.append(stripped)
    return "\n".join(lines)


def _write_pdf_report(markdown: str, pdf_path: Path | str) -> None:
    from reportlab.lib import colors  # type: ignore
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore
    from reportlab.lib.units import mm  # type: ignore
    from reportlab.platypus import (  # type: ignore
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
        ListFlowable,
        ListItem,
    )

    def apply_inline(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
        text = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", text)
        return text

    styles = getSampleStyleSheet()
    body_style = styles["BodyText"].clone("BodyTextMemos")
    body_style.fontSize = 10.5
    body_style.leading = 14
    heading_styles = {
        1: ParagraphStyle("Heading1Memos", parent=styles["Heading1"], fontSize=18, leading=22, spaceAfter=10),
        2: ParagraphStyle("Heading2Memos", parent=styles["Heading2"], fontSize=14, leading=18, spaceAfter=8),
        3: ParagraphStyle("Heading3Memos", parent=styles["Heading3"], fontSize=12, leading=16, spaceAfter=6),
    }
    bullet_style = ParagraphStyle("BulletMemos", parent=body_style, leftIndent=12, bulletIndent=0)

    document = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=25 * mm,
        bottomMargin=20 * mm,
    )

    flowables: List[Any] = []
    lines = markdown.splitlines()
    paragraph_buffer: List[str] = []
    list_items: List[str] = []
    table_lines: List[str] = []

    def add_spacer(height: float = 6) -> None:
        if flowables and not isinstance(flowables[-1], Spacer):
            flowables.append(Spacer(1, height))

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        text = " ".join(paragraph_buffer).strip()
        if text:
            flowables.append(Paragraph(apply_inline(text), body_style))
            add_spacer(6)
        paragraph_buffer = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            items = [
                ListItem(Paragraph(apply_inline(item), bullet_style), leftIndent=12)
                for item in list_items
            ]
            flowables.append(
                ListFlowable(
                    items,
                    bulletType="bullet",
                    start="bullet",
                    bulletFontName="Helvetica",
                    bulletFontSize=9,
                    leftIndent=18,
                )
            )
            add_spacer(6)
        list_items = []

    def flush_table(force: bool = False) -> None:
        nonlocal table_lines
        if not table_lines:
            return
        data: List[List[str]] = []
        for idx, raw in enumerate(table_lines):
            cleaned = raw.strip()
            if idx == 1 and set(cleaned.replace("|", "").strip()) <= {"-", ":", " "}:
                continue
            row = [cell.strip() for cell in cleaned.strip("|").split("|")]
            data.append(row)
        if data:
            table = Table(data, hAlign="LEFT", repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F5F5F5")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#333333")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("ALIGN", (0, 0), (-1, 0), "LEFT"),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            flowables.append(table)
            add_spacer(8)
        table_lines = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if table_lines and not stripped.startswith("|"):
            flush_table()

        if not stripped:
            flush_paragraph()
            flush_list()
            flush_table()
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            flush_table()
            level = len(stripped) - len(stripped.lstrip("#"))
            content = stripped[level:].strip()
            style = heading_styles.get(level, heading_styles[3])
            flowables.append(Paragraph(apply_inline(content), style))
            add_spacer(6)
            continue

        if stripped.startswith("|"):
            table_lines.append(raw_line)
            continue

        bullet_match = re.match(r"([*\-+])\s+(.*)", stripped)
        numbered_match = re.match(r"(\d+)\.\s+(.*)", stripped)
        if bullet_match:
            flush_paragraph()
            list_items.append(bullet_match.group(2).strip())
            continue
        if numbered_match:
            flush_paragraph()
            list_items.append(numbered_match.group(2).strip())
            continue

        paragraph_buffer.append(stripped)

    flush_table()
    flush_paragraph()
    flush_list()

    if not flowables:
        flowables.append(Paragraph(apply_inline(markdown), body_style))

    document.build(flowables)


def _derive_gstin_for_report(Memos: Memos, extracted: Dict[str, Any]) -> str:
    """Return a filesystem-safe GST tag for naming report files."""
    candidates = [
        extracted.get("billed_gstin"),
        extracted.get("dealer_gstin"),
        getattr(Memos.dealer, "gstin", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        clean = re.sub(r"\s+", "", str(candidate).upper())
        if not clean:
            continue
        clean = re.sub(r"[^0-9A-Z]", "-", clean)
        return clean
    return f"UNKNOWN-{Memos.id}"

logger = logging.getLogger(__name__)


def sha256_file(path: str) -> str:
    """Compute the SHA256 checksum of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):  # type: ignore
            h.update(chunk)
    return h.hexdigest()


def process_Memos_file(Memos: Memos) -> None:
    """Process an uploaded Memos through the local vision pipeline.

    This function reads the Memos file from disk, runs through
    all the processing steps and updates the database model.  It
    does not commit the session; callers should commit once
    processing succeeds.
    """
    storage_path = Memos.storage_path
    full_path = Path(storage_path)
    if not full_path.exists():
        raise FileNotFoundError(f"Memos file {full_path} not found")

    # Compute checksum and update
    checksum = sha256_file(str(full_path))
    Memos.checksum = checksum

    # Determine if it's a PDF or image
    images: List[Image.Image]
    if Memos.mime_type.lower() == "application/pdf" or full_path.suffix.lower() == ".pdf":
        images = pdf_to_images(str(full_path))
    else:
        images = open_image_file(str(full_path))

    # Load context
    dealer = Memos.dealer
    dealer_name = dealer.name
    # Gather previously processed Memos for duplicate check
    previous_Memos = Memos.query.filter(Memos.dealer_id == dealer.id, Memos.status == "processed").all()
    previous_summaries: List[Dict[str, Any]] = []
    for inv in previous_Memos:
        summary = {
            "id": inv.id,
            "Memos_number": inv.extracted_fields.get("Memos_number") if inv.extracted_fields else None,
            "amount": inv.extracted_fields.get("Memos_amount") if inv.extracted_fields else None,
            "date": inv.extracted_fields.get("Memos_date") if inv.extracted_fields else None,
        }
        previous_summaries.append(summary)

    # GST validations
    gst_adapter = get_gst_adapter(preferred=True)
    gst_statuses: Dict[str, Dict[str, str]] = {}
    if Memos.dealer.gstin:
        gst_statuses["dealer_gstin"] = {"status": gst_adapter.verify_gstin(Memos.dealer.gstin)}
    # If Memos has billed GST from previous processing, use that too
    # else, set unknown; actual number will be filled by extraction
    gst_statuses.setdefault("billed_gstin", {"status": "unknown"})

    # Build context for vision extraction
    hsn_rates = {}  # Could load from CSV; omitted for brevity
    context = {
        "dealer_name": dealer_name,
        "previous_duplicates": previous_summaries,
        "gst_statuses": gst_statuses,
        "hsn_rates": hsn_rates,
    }
    extracted, confidences = extract_Memos(images, context)

    # Record duplicate flag
    is_duplicate = extracted.get("duplicate_check", {}).get("is_duplicate", False)
    Memos.duplicate_flag = bool(is_duplicate)
    dup_of = extracted.get("duplicate_check", {}).get("duplicate_of_Memos_number")
    # Link to first matching Memos id if exists
    if is_duplicate and dup_of:
        for inv in previous_Memos:
            if inv.extracted_fields and inv.extracted_fields.get("Memos_number") == dup_of:
                Memos.duplicate_of_id = inv.id
                break

    # Validate GSTINs again with extracted numbers
    dealer_gstin = extracted.get("dealer_gstin")
    billed_gstin = extracted.get("billed_gstin")
    if dealer_gstin:
        gst_statuses["dealer_gstin"] = {"status": gst_adapter.verify_gstin(dealer_gstin)}
    if billed_gstin:
        gst_statuses["billed_gstin"] = {"status": gst_adapter.verify_gstin(billed_gstin)}
    extracted["gst_validations"] = gst_statuses
    Memos.gst_verify_status = json.dumps(gst_statuses)

    # Compute embedding and check semantic duplicates (simple example)
    text_for_embedding = json.dumps({k: extracted[k] for k in ("Memos_number", "Memos_date", "Memos_amount")})
    embedding_bytes = compute_embedding(text_for_embedding)
    # Add to DB
    Memos.embedding = MemosEmbedding(vector=embedding_bytes)

    # Compare against previous embeddings to flag duplicates by similarity (threshold)
    prev_embeddings = [inv.embedding.vector for inv in previous_Memos if inv.embedding]
    if prev_embeddings:
        try:
            index = build_faiss_index(prev_embeddings)
            idx = search_similar(index, embedding_bytes, k=1)[0]
            # For demonstration treat any neighbour as duplicate
            neighbour = previous_Memos[idx]
            if neighbour and neighbour.extracted_fields and neighbour.extracted_fields.get("Memos_number") != extracted["Memos_number"]:
                Memos.duplicate_flag = True
                Memos.duplicate_of_id = neighbour.id
        except Exception as exc:  # In case FAISS fails
            logger.warning("FAISS duplicate check failed: %s", exc)

    # Save extracted fields and confidences
    Memos.extracted_fields = extracted
    Memos.confidence_scores = confidences

    # Compute risk score (simple sum as in report)
    risk = 0
    if Memos.duplicate_flag:
        risk += 40
    # Use gst_statuses: count non verified statuses
    mismatches = [status for status in gst_statuses.values() if status.get("status") != "verified"]
    if mismatches:
        risk += 30
    arithmetic_valid = extracted.get("arithmetic_check", {}).get("valid", True)
    if not arithmetic_valid:
        risk += 20
    outliers = extracted.get("price_outliers") or []
    if outliers:
        risk += 10 * len(outliers)
    Memos.risk_score = risk

    # Generate Markdown report
    md_report = generate_report(extracted, confidences)
    # Determine report file paths
    dealer_folder = Path(Memos.dealer.folder_path)
    report_folder = dealer_folder / "reports"
    os.makedirs(report_folder, exist_ok=True)
    gst_tag = _derive_gstin_for_report(Memos, extracted)
    base_name = f"{gst_tag}_ai_response_{Memos.id}"
    md_filename = f"{base_name}.md"
    pdf_filename = f"{base_name}.pdf"
    md_path = report_folder / md_filename
    pdf_path = report_folder / pdf_filename
    # Write markdown
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    Memos.ai_md_path = str(md_path)
    try:
        _write_pdf_report(md_report, pdf_path)
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.error("PDF generation failed: %s", exc)
        try:
            import fitz  # type: ignore

            doc = fitz.open()
            page = doc.new_page()
            text = _markdown_to_plaintext(md_report)
            text_rect = fitz.Rect(40, 40, 555, 800)
            page.insert_textbox(text_rect, text, fontsize=11, fontname="helv")
            doc.save(pdf_path)
            doc.close()
        except Exception as fallback_exc:  # pragma: no cover - defensive
            logger.error("PyMuPDF fallback PDF generation failed: %s", fallback_exc)
        with open(pdf_path, "wb") as f:
            f.write(md_report.encode("utf-8"))
    Memos.ai_pdf_path = str(pdf_path)

    Memos.status = "processed"
    Memos.processed_at = db.func.now()