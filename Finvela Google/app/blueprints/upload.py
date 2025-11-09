"""Blueprint for dealer Memos upload.

Dealers upload Memoss via a simple HTML form.  On submission the
file is stored in a dealer‑specific folder and a database record
created.  The Memos is then processed immediately via the
Gemini pipeline; no external task queue is required.  Dealers only
see a confirmation message; they cannot view processing results
through this interface.
"""

from __future__ import annotations

import os
import uuid
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from werkzeug.utils import secure_filename

from .. import db
from ..models import Dealer, Memos
from ..utils.parse_memo import process_Memos_file
from ..utils.pdf import PDFProcessingError


bp = Blueprint("upload", __name__)


@bp.before_app_request
def _ensure_secret_key() -> None:
    if not current_app.secret_key:
        fallback = secrets.token_hex(16)
        current_app.config.setdefault("SECRET_KEY", fallback)
        current_app.secret_key = fallback


@bp.route("/", methods=["GET"])
def index() -> str:
    return render_template("upload.html")


@bp.route("/upload", methods=["GET", "POST"])
def upload() -> str:
    if request.method == "POST":
        dealer_name = request.form.get("dealer_name")
        gstin = request.form.get("gstin") or None
        phone = request.form.get("phone") or None
        file = request.files.get("file")
        if not dealer_name or not file:
            flash("Dealername and file are required", "danger")
            return redirect(url_for("upload.upload"))

        # Find or create dealer
        dealer = Dealer.query.filter_by(name=dealer_name).first()
        if not dealer:
            # Create dealer folder
            storage_root = current_app.config["STORAGE_ROOT"]
            dealer_id = uuid.uuid4().hex
            folder_path = os.path.join(storage_root, "dealers", dealer_id)
            os.makedirs(os.path.join(folder_path, "uploads"), exist_ok=True)
            os.makedirs(os.path.join(folder_path, "reports"), exist_ok=True)
            dealer = Dealer(name=dealer_name, gstin=gstin, phone=phone, folder_path=folder_path)
            db.session.add(dealer)
            db.session.commit()
        else:
            # Ensure dealer folder exists
            os.makedirs(os.path.join(dealer.folder_path, "uploads"), exist_ok=True)
            os.makedirs(os.path.join(dealer.folder_path, "reports"), exist_ok=True)

        # Save file
        orig_filename = secure_filename(file.filename)
        # Generate a random UUID for storage
        file_uuid = uuid.uuid4().hex
        ext = os.path.splitext(orig_filename)[1]
        filename = f"raw_{file_uuid}{ext}"
        upload_path = os.path.join(dealer.folder_path, "uploads", filename)
        file.save(upload_path)

        mime_type = file.mimetype or "application/octet-stream"
        # Create memo record
        memo_record = Memos(
            dealer_id=dealer.id,
            original_filename=orig_filename,
            mime_type=mime_type,
            storage_path=upload_path,
            checksum="",
            status="queued",
        )
        db.session.add(memo_record)
        db.session.commit()

        memo_record.status = "processing"
        try:
            process_Memos_file(memo_record)
            db.session.commit()
            flash("Memos processed successfully", "success")
        except PDFProcessingError as pdf_exc:
            db.session.rollback()
            refreshed = Memos.query.get(memo_record.id)
            if refreshed:
                refreshed.status = "failed"
                db.session.add(refreshed)
                db.session.commit()
            message = (
                "PDF rendering failed. Ensure the Memos is a valid PDF and that the PyMuPDF library is installed. "
                f"Details: {pdf_exc}"
            )
            flash(message, "danger")
            return redirect(url_for("upload.index"))
        except Exception as processing_exc:  # pylint: disable=broad-except
            db.session.rollback()
            refreshed = Memos.query.get(memo_record.id)
            if refreshed:
                refreshed.status = "failed"
                db.session.add(refreshed)
                try:
                    db.session.commit()
                except Exception:  # pragma: no cover - safeguard
                    db.session.rollback()
            current_app.logger.exception("Memos processing failed: %s", processing_exc)
            flash("Memos upload failed due to processing error", "danger")
            return redirect(url_for("upload.index"))

        return redirect(url_for("upload.index"))
    return render_template("upload.html")