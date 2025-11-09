"""WSGI entrypoint exposing the Flask application for production servers."""
from __future__ import annotations

from expenseai_ext import create_app

application = create_app(mount_legacy=True)
