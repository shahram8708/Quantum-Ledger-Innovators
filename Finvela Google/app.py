"""Application entrypoint for the Finvela Flask project."""
from __future__ import annotations

from expenseai_ext import create_app

app = create_app(mount_legacy=True)


if __name__ == "__main__":
    # Running via `python app.py` is handy during prototyping, but production
    # deployments should rely on wsgi.py or a dedicated WSGI/ASGI server.
    app.run(use_reloader=False, host="0.0.0.0", port=8000)
