"""Utilities for processing memos outside of request handlers.

The helper `process_memo` allows memos to be processed by
primary key using the standard application factory without
requiring Celery, Redis, or RabbitMQ.
"""

from __future__ import annotations

import logging

from . import create_app, db
from .models import Memos
from .utils.parse_memo import process_Memos_file

logger = logging.getLogger(__name__)


def process_memo(memo_id: int) -> None:
    """Process a memo via the local vision pipeline using a fresh app context."""
    app = create_app()
    with app.app_context():
        memo = Memos.query.get(memo_id)
        if not memo:
            logger.error("Memo %s not found", memo_id)
            return
        try:
            memo.status = "processing"
            db.session.commit()
            process_Memos_file(memo)
            db.session.commit()
        except Exception as exc:  # pylint: disable=broad-except
            db.session.rollback()
            memo.status = "failed"
            db.session.add(memo)
            try:
                db.session.commit()
            except Exception:  # pragma: no cover - safeguard
                db.session.rollback()
            logger.exception("Error processing memo %s: %s", memo_id, exc)
            raise