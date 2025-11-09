"""Admin blueprint providing dashboard and login functionality.

Only users with credentials can access the admin dashboard.  The
dashboard lists Memos with filtering options and links to
download raw uploads, Markdown reports and PDF reports.  A simple
login form is provided using Flask‑Login.
"""

from __future__ import annotations

import os
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app, jsonify
from sqlalchemy.orm import joinedload

from ..models import Memos, Dealer
from ..utils.duplicate_detector import run_manual_duplicate_checks


bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
def dashboard() -> str:
    # Filters
    dealer_id = request.args.get("dealer_id", type=int)
    duplicate = request.args.get("duplicate")
    gst_status = request.args.get("gst_status")
    query = Memos.query
    if dealer_id:
        query = query.filter_by(dealer_id=dealer_id)
    if duplicate == "yes":
        query = query.filter_by(duplicate_flag=True)
    elif duplicate == "no":
        query = query.filter_by(duplicate_flag=False)
    if gst_status:
        query = query.filter(Memos.gst_verify_status.contains(gst_status))
    memo_rows = query.order_by(Memos.created_at.desc()).limit(50).all()
    dealers = Dealer.query.all()
    return render_template("dashboard.html", Memos=memo_rows, dealers=dealers)


@bp.route("/Memos/<int:Memos_id>")
def Memos_detail(Memos_id: int) -> str:
    memo = Memos.query.get_or_404(Memos_id)
    return render_template("Memos_detail.html", Memos=memo)


@bp.route("/Memos/<int:Memos_id>/duplicate-check/manual")
def manual_duplicate_check(Memos_id: int):
    memo = Memos.query.options(joinedload(Memos.dealer)).get_or_404(Memos_id)
    try:
        result = run_manual_duplicate_checks(memo)
    except Exception:
        current_app.logger.exception("Manual duplicate check failed for Memos %s", Memos_id)
        return jsonify({"status": "error", "message": "Duplicate analysis failed"}), 500
    for check in result.get("checks", []):
        for match in check.get("matches", []):
            match["detail_url"] = url_for("admin.Memos_detail", Memos_id=match["Memos_id"])
    return jsonify(result)


@bp.route("/download/<path:path>")
def download_file(path: str):
    # Serve a file from disk for download if it resides under storage
    abs_path = os.path.abspath(path)
    storage_root = os.path.abspath(os.environ.get("STORAGE_ROOT", "./storage"))
    if not abs_path.startswith(storage_root):
        flash("Invalid file path", "danger")
        return redirect(url_for("admin.dashboard"))
    inline = request.args.get("inline") in {"1", "true", "True"}
    return send_file(abs_path, as_attachment=not inline)

