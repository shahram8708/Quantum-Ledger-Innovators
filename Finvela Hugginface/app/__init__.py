"""Application factory and configuration setup.

This module creates the Flask application, initialises extensions
such as the SQLAlchemy database and login manager, and registers
blueprints.  It reads configuration from environment variables and
falls back to sensible defaults for local development.

The application factory pattern allows the app to be created for
different contexts (web server, CLI, tests).
"""

import os
from pathlib import Path

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from sqlalchemy.engine.url import make_url

# Load environment variables from a .env file if present
load_dotenv()

db = SQLAlchemy()
migrate = Migrate()


def create_app(test_config: dict | None = None) -> Flask:
    """Application factory for creating a Flask app instance."""
    app = Flask(__name__, instance_relative_config=True)

    # Ensure the instance folder exists for SQLite databases and other stateful files
    os.makedirs(app.instance_path, exist_ok=True)

    default_sqlite_uri = f"sqlite:///{(Path(app.instance_path) / 'finvela.db').as_posix()}"

    # Default configuration
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        secret_key = "finvelate-memo-app-dev-key"
    app.config["SECRET_KEY"] = secret_key
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", os.environ.get("POSTGRES_URL") or default_sqlite_uri)
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("STORAGE_ROOT", os.environ.get("STORAGE_ROOT", "./storage"))

    # Ensure Flask sees the configured secret key for session handling
    app.secret_key = app.config["SECRET_KEY"]
    if app.secret_key == "finvelate-memo-app-dev-key":
        app.logger.warning("Using default development SECRET_KEY; override it in production deployments.")
    app.config.setdefault("VISION_MODEL_NAME", os.environ.get("VISION_MODEL_NAME", "Qwen/Qwen2-VL-2B-Instruct"))
    app.config.setdefault("VISION_MODEL_DEVICE", os.environ.get("VISION_MODEL_DEVICE", "auto"))
    app.config.setdefault("EMBEDDING_MODEL_NAME", os.environ.get("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"))
    app.config.setdefault("EMBEDDING_DEVICE", os.environ.get("EMBEDDING_DEVICE", "auto"))
    app.config.setdefault("SEARCH_PROVIDER", os.environ.get("SEARCH_PROVIDER", "duckduckgo"))
    app.config.setdefault("SEARCH_MAX_RESULTS", int(os.environ.get("SEARCH_MAX_RESULTS", "6")))
    app.config.setdefault("GST_API_URL", os.environ.get("GST_API_URL"))
    app.config.setdefault("GST_API_KEY", os.environ.get("GST_API_KEY"))
    app.config.setdefault("TWILIO_SID", "")
    app.config.setdefault("TWILIO_AUTH_TOKEN", "")
    app.config.setdefault("TWILIO_WHATSAPP_NUMBER", "")
    app.config.setdefault("PRICE_BENCHMARK_PATH", os.environ.get("PRICE_BENCHMARK_PATH", "./data/price_benchmarks.csv"))
    app.config.setdefault("ADMIN_BOOTSTRAP_TOKEN", os.environ.get("ADMIN_BOOTSTRAP_TOKEN"))

    # Apply test overrides
    if test_config:
        app.config.update(test_config)

    # Ensure storage directories exist
    os.makedirs(app.config["STORAGE_ROOT"], exist_ok=True)

    # Initialise extensions
    db.init_app(app)
    migrate.init_app(app, db)
    # Register blueprints
    from .blueprints.upload import bp as upload_bp
    from .blueprints.admin import bp as admin_bp
    app.register_blueprint(upload_bp)
    app.register_blueprint(admin_bp)

    _ensure_database_created(app)

    return app


def _ensure_database_created(app: Flask) -> None:
    """Create the configured database and tables if they do not yet exist."""
    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not database_uri:
        return

    url = make_url(database_uri)

    if url.drivername.startswith("sqlite"):
        database_path = url.database or ""
        if database_path not in {":memory:", ":memory"}:
            db_path = Path(database_path)
            if not db_path.is_absolute():
                db_path = Path(app.instance_path) / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db.create_all()
        if url.drivername.startswith("sqlite"):
            _ensure_sqlite_backfills(app)
        app.logger.info("Database initialised at %s", database_uri)


def _ensure_sqlite_backfills(app: Flask) -> None:
    """Apply lightweight schema backfills for existing SQLite databases."""

    engine = db.engine
    inspector = inspect(engine)
    table_lookup = {name.lower(): name for name in inspector.get_table_names()}
    memo_table = table_lookup.get("memo")
    if not memo_table:
        return

    expected_columns = {
        "dealer_id": "INTEGER",
        "original_filename": "TEXT",
        "mime_type": "TEXT",
        "storage_path": "TEXT",
        "checksum": "TEXT",
        "status": "TEXT DEFAULT 'queued'",
        "extracted_fields": "TEXT",
        "confidence_scores": "TEXT",
        "ai_md_path": "TEXT",
        "ai_pdf_path": "TEXT",
        "duplicate_flag": "INTEGER DEFAULT 0",
        "duplicate_of_id": "INTEGER",
        "gst_verify_status": "TEXT",
        "risk_score": "NUMERIC",
        "anomaly_summary": "TEXT",
        "created_at": "DATETIME",
        "processed_at": "DATETIME",
    }

    column_names = {column["name"].lower() for column in inspector.get_columns(memo_table)}
    missing_columns = [(name, ddl) for name, ddl in expected_columns.items() if name.lower() not in column_names]

    if not missing_columns:
        return

    with engine.begin() as connection:
        for column_name, ddl in missing_columns:
            statement = f'ALTER TABLE "{memo_table}" ADD COLUMN {column_name} {ddl}'
            connection.execute(text(statement))
            column_names.add(column_name.lower())

    added = ", ".join(name for name, _ in missing_columns)
    app.logger.info("Added missing columns to %s: %s", memo_table, added)