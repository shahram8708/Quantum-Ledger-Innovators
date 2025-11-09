# Finvela – AI-Powered Invoice Intelligence Platform

## Overview / Introduction
Finvela is a privacy-first expense intelligence platform that helps finance and compliance teams ingest, parse, and audit invoices at scale. The application combines a modern Flask web experience with local multimodal AI models, automated benchmarking, GST compliance checks, vendor drift detection, and counterfactual risk simulation. Every upload is tracked, versioned, and analysed so that organisations can surface anomalies, validate tax obligations, and act on high-risk spend in minutes instead of weeks.

Key design goals:
- Run fully on customer-controlled infrastructure with zero data egress by default.
- Pair a responsive multi-tenant web UI with API-first endpoints for automation.
- Offer explainable AI outputs, audit logs, and rate-limited authentication flows.
- Provide optional integrations (Twilio, Razorpay, S3) without hard dependencies.

## Key Features
- **Invoice ingestion pipeline**: drag-and-drop uploads, watched folders, and IMAP polling automatically enqueue files, store originals, and trigger parsing.
- **Local multimodal parsing**: Qwen2-VL based extractor normalises invoice headers and line items, with deterministic fallbacks for embeddings.
- **Risk & compliance engine**: GSTIN validation, HSN rate checks, arithmetic verification, duplicate detection, benchmarking, and explainable risk scoring.
- **Vendor intelligence**: fingerprinting, price drift tracking, and contextual chat for organisations and vendors.
- **Counterfactual simulator**: model what-if adjustments to line items and observe risk deltas before approving changes.
- **Collaboration**: organisation dashboards, role-based access control, OTP/email verification, invite flows, and a built-in team chat workspace.
- **Billing & provisioning**: Razorpay-backed seat upgrades, usage limits, and audit trails for membership changes.
- **Extensible integrations**: Celery background tasks, Redis caching, Twilio WhatsApp alerts, SMTP emails, and optional AWS S3 storage adapters.

## Live Demo
- [Product walkthrough (video)](https://drive.google.com/file/d/1N-8vBXXsYHvkGsWNUVY0BltT8KUNLnSL/view?usp=sharing)

This repository is intended for self-hosted deployments. Follow the installation and deployment guides below to spin up a local or cloud instance. When a public demo is available, add its URL here.

## Screenshots / Example Outputs
| View | Preview |
| --- | --- |
| Dashboard | ![Dashboard](docs/screenshots/dashboard.png) |
| Risk Report | ![Risk Report](docs/screenshots/risk-report.png) |
| Vendor Drift | ![Vendor Drift](docs/screenshots/vendor-drift.png) |

## Technologies & Libraries Used
- **Backend**: Python 3.11+, Flask, Flask-Login, Flask-Babel, Flask-WTF, Flask-Limiter, Flask-Caching, Flask-Talisman, Flask-Migrate/SQLAlchemy
- **Task & cache layer**: Celery, Redis, SimpleCache
- **AI/ML**: PyTorch, Transformers, Sentence-Transformers, accelerate, DuckDuckGo Search, PyMuPDF
- **Data**: SQLite/PostgreSQL via SQLAlchemy models, Watchdog for filesystem ingestion
- **Messaging & integrations**: SMTP, Twilio, Razorpay, boto3 (optional S3 storage)
- **Tooling**: Click-based Flask CLI, dotenv, Passlib (bcrypt), Requests

## System Architecture Diagram
```
                +--------------------+
                |      End Users     |
                +----------+---------+
                           |
                           v
                +----------+----------+
                | Flask Web / API     |
                | (expenseai_web, ...)|
                +----------+----------+
                           |
                 Templates ▪ Auth ▪ Admin
                           v
                +----------+----------+
                | SQLAlchemy Models   |
                | (expenseai_models)  |
                +----+-----------+----+
                     |           |
          +----------+--+   +----+-----------------+
          | Ingestion    |   | Risk & Compliance   |
          | (watchdog,   |   | (benchmark, GST,    |
          | email, CLI)  |   | vendor drift)       |
          +------+-------+   +----------+----------+
                 |                      |
                 v                      v
          +------+-------+       +------+-------+
          | Celery Tasks |       | AI Runtime   |
          | (expenseai_  |<----->| (expenseai_ai|
          | ingest)      |       |  parser/chat)|
          +------+-------+       +------+-------+
                 |                      |
                 v                      v
          +------+-------+       +------+-------+
          | Storage      |       | Audit & Logs |
          | (local/S3)   |       | (expenseai_ext|
          +--------------+       +---------------+
                           |
                           v
                 External Services (Redis, SMTP,
                    Razorpay, Twilio, Hugging Face)
```

## Folder Structure
```
.
├─ app.py                  # Dev entrypoint (mounts legacy app optionally)
├─ config.py               # Environment-specific configuration classes
├─ expenseai_ext/          # Application factory, logging, auth, security, idempotency
├─ expenseai_web/          # Web blueprint, templates, dashboards, admin flows
├─ expenseai_auth/         # Auth routes, OTP services, forms, billing integration
├─ expenseai_invoices/     # Invoice upload, parsing triggers, GST & benchmark endpoints
├─ expenseai_ai/           # Local model runtime, parser worker, embeddings, chat agent
├─ expenseai_risk/         # Risk orchestration, API routes, weighting policies
├─ expenseai_compliance/   # GST providers, HSN upload, arithmetic validation
├─ expenseai_benchmark/    # Price baseline calculation and storage
├─ expenseai_vendor/       # Vendor fingerprint directory and drift APIs
├─ expenseai_counterfactual/# Counterfactual modelling service and APIs
├─ expenseai_ingest/       # Folder/email ingestion watchers, Celery tasks, storage adapters
├─ expenseai_cli/          # Click commands exposed via `flask manage`
├─ expenseai_models/       # SQLAlchemy models for invoices, audits, chat, OTP, etc.
├─ docs/screenshots/       # Marketing and documentation assets
├─ instance/               # Runtime storage (uploads, thumbnails, SQLite, configs)
├─ translations/           # Flask-Babel localisation files
├─ storage/                # Optional static storage bucket mocks
├─ app/                    # Legacy Flask app (can be mounted at /legacy)
└─ wsgi.py                 # Production WSGI entrypoint
```

## Requirements
- Python 3.11+ (64-bit). CPU-only inference is supported; GPU acceleration requires a compatible CUDA stack.
- Redis 6+ for Celery broker/result backend (development can use the in-memory SimpleCache but workers expect Redis).
- Node is not required; templates use server-side rendering.
- Optional system dependencies:
  - `libmagic` or Windows alternatives for MIME detection (filetype falls back gracefully).
  - Build tools for PyTorch and watchdog on Windows.
  - `pymupdf` wheels for PDF parsing (handled via `pip`).

## Installation Steps
1. **Clone & create a virtual environment**
   ```powershell
   git clone https://github.com/shahram8708/Quantum-Ledger-Innovators Quantum-Ledger-Innovators
   cd Quantum-Ledger-Innovators
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. **Install Python dependencies**
   ```powershell
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. **Prepare runtime directories**
   ```powershell
   mkdir -Force instance\uploads
   mkdir -Force instance\thumbnails
   mkdir -Force instance\chat_uploads
   ```
4. **Create a `.env` file (see Environment Configuration)** and set database, Redis, email, and model parameters.
5. **Initialise the database** (uses Flask-Migrate; creates migrations directory on first run):
   ```powershell
   flask --app expenseai_ext:create_app db init   # first time only
   flask --app expenseai_ext:create_app db upgrade
   ```
6. **Optionally seed an admin user**:
   ```powershell
   flask --app expenseai_ext:create_app manage create-admin
   ```

## Environment Configuration
Create an `.env` file in the project root or rely on OS environment variables. Notable settings:

| Variable | Description | Default |
| --- | --- | --- |
| `FLASK_ENV` | `development` or `production`; selects `DevConfig`/`ProdConfig`. | `development` |
| `DATABASE_URL` | SQLAlchemy connection string (e.g. `sqlite:///instance/finvela.db`, `postgresql://user:pass@host/db`). | SQLite file |
| `SECRET_KEY` | Flask session secret; replace in production. | `please-change-me` |
| `REDIS_URL` | Broker/backend for Celery (`redis://localhost:6379/0`). | `redis://localhost:6379/0` |
| `CELERY_TASK_ALWAYS_EAGER` | Run Celery tasks inline (useful for local dev). | `false` |
| `VISION_MODEL_NAME` | Hugging Face vision-language model (tested with `Qwen/Qwen2-VL-2B-Instruct`). | Listed in `BaseConfig` |
| `EMBEDDING_MODEL_NAME` | Sentence embeddings model (`sentence-transformers/all-MiniLM-L6-v2`). | Listed |
| `EMBEDDING_DISABLE_REMOTE` | Force deterministic fallback embeddings (set `true` for air-gapped mode). | `false` |
| `AUTO_PARSE_ON_UPLOAD` | Queue parsing after every successful upload. | `true` |
| `MAX_UPLOAD_MB` | Maximum upload size. | `10` |
| `INGEST_WATCH_PATHS` | Colon-separated absolute paths for folder ingestion watchers. | _empty_ |
| `INGEST_EMAIL_*` | IMAP ingestion credentials (host, user, pass, SSL, folder). | _empty_ |
| `STORAGE_BACKEND` | `local` or `s3`; supply `S3_BUCKET`, `S3_REGION`, `AWS_*` for S3. | `local` |
| `SMTP_HOST/PORT/USER/PASS` | SMTP credentials for OTP and notifications. | Gmail defaults |
| `EMAIL_FROM` | Sender email for transactional messages. | empty |
| `ADMIN_EMAIL` | Kicks off contact form routing. | `EMAIL_FROM` |
| `RAZORPAY_KEY_ID`/`SECRET` | Enable billing checkout. | empty |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_NUMBER` | Required for WhatsApp channel. | empty |
| `ALLOW_SELF_REGISTRATION` | Allow admins to sign up without invites. | `true` |
| `BABEL_SUPPORTED_LOCALES` | Supported locale codes (comma-separated). | `en,hi` |
| `GLOBAL_RATE_LIMIT` | Application-wide rate limit (e.g. `500/minute`). | `500/minute` |

The parser downloads model weights on first use; set `HF_HOME` or `TRANSFORMERS_CACHE` if you need a custom cache path. To use private Hugging Face models, export `HUGGINGFACE_HUB_TOKEN`.

## Login / User Authentication Guide
Authentication requires verified email and valid credentials. Configure at least one admin via the CLI command above, then document the credentials privately.

For local testing we ship two sample accounts you can use immediately after running migrations:

- **Organization Admin (full access)** – use the first email/password pair below to experience the admin flows, seat management, and billing setup.
- **Team Member (standard seat)** – use the second pair to validate member permissions, invoice uploads, chat, etc.

```
EMAIL_1=shahram8708@gmail.com
PASSWORD_1=shahram8708@

EMAIL_2=shahrampravesh4@gmail.com
PASSWORD_2=shahrampravesh4@
```

- Admins approve new members and manage billing.
- Members can be invited via the team management screen.
- OTP verification is sent via email; configure SMTP settings (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`) for production.

## How to Run
### Development workflow
1. Activate virtual environment and export environment variables (`$env:FLASK_ENV = "development"`).
2. Start Redis (`redis-server` in a separate terminal) or use Docker (`docker run --name finvela-redis -p 6379:6379 redis:7`).
3. Launch the Flask app (with background services and legacy mount):
   ```powershell
   flask --app expenseai_ext:create_app run --debug
   # or
   python app.py
   ```
4. Start the Celery worker (parsing, ingestion, mail tasks):
   ```powershell
   celery -A expenseai.celery_app:celery worker --loglevel=info
   ```
5. Optional: run ingestion watcher manually if `INGEST_WATCH_PATHS` is configured (it also starts automatically inside the app):
   ```powershell
   python -m expenseai_ingest.watcher
   ```
6. To parse invoices synchronously from the CLI:
   ```powershell
   flask --app expenseai_ext:create_app manage parse-invoice --id 123
   ```

### Production runtime
- Use `wsgi.py` with Gunicorn/Waitress/uvicorn workers:
  ```powershell
  gunicorn --bind 0.0.0.0:8000 wsgi:application
  # Windows-friendly option
  waitress-serve --port=8000 wsgi:application
  ```
- Run Celery workers (and optionally separate queues) in supervised processes.
- Configure a process manager (systemd, Supervisor, Windows Service) for:
  - Flask/Gunicorn application
  - Celery worker(s)
  - Celery beat (if you add scheduled tasks)
  - Redis server (managed service recommended)
- Set `FLASK_ENV=production` and supply secure secrets.

## Deployment Guide
### Local bare-metal / VM
1. Follow installation steps, ensuring Redis and PostgreSQL/SQLite paths are accessible.
2. Configure systemd/Windows services for the web process and Celery.
3. Point your reverse proxy (Nginx/IIS/Traefik) at the WSGI port. Enforce HTTPS and websocket passthrough if using SSE features later.

### Docker (sample)
No official container is committed yet; the following snippet can bootstrap one:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV FLASK_ENV=production \ 
    PYTHONUNBUFFERED=1 \ 
    WAITRESS_THREADS=4
RUN mkdir -p instance/uploads instance/thumbnails instance/chat_uploads
CMD ["waitress-serve", "--port=8000", "wsgi:application"]
```
Build and run:
```powershell
docker build -t finvela .
docker run -d --name finvela \ 
  -e DATABASE_URL=sqlite:////app/instance/finvela.db \ 
  -e REDIS_URL=redis://host.docker.internal:6379/0 \ 
  -p 8000:8000 \ 
  finvela
```
Run Celery in a sidecar container:
```powershell
docker run -d --name finvela-worker \ 
  --env-file .env \ 
  --link finvela \ 
  finvela \ 
  celery -A expenseai.celery_app:celery worker --loglevel=info
```
Mount the `instance/` directory as a volume to persist uploads.

## API Endpoints + Examples
All JSON endpoints require authentication (cookie session). Provide `Idempotency-Key` on write operations to guarantee safe retries.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/auth/login` | Form-based login (returns session cookie). |
| `POST` | `/auth/otp/<purpose>` | Request/resend OTP (email only). |
| `GET` | `/invoices/` | Render invoice grid (HTML) with JSON support via `Accept: application/json`. |
| `POST` | `/invoices/upload` | Multipart upload; kicks off parsing and analysis. |
| `POST` | `/invoices/<id>/parse` | Force synchronous parsing and analysis. |
| `GET` | `/invoices/<id>/extracted` | JSON payload of parsed fields & line items. |
| `POST` | `/invoices/<id>/gst/<subject>/verify` | GST validation for `vendor` or `company`. |
| `GET` | `/invoices/<id>/price-benchmarks` | Retrieve recent market benchmarks per line. |
| `POST` | `/organization/chat/send_message` | JSON direct messages inside an organisation. |
| `POST` | `/organization/<id>/counterfactual/<invoice_id>` | Compute what-if changes to an invoice. |
| `POST` | `/invoices/<id>/risk/full-analysis` | Launch combined risk/compliance benchmarking. |
| `GET` | `/vendor/<gst>/profile` | Cached vendor fingerprint profile. |
| `GET` | `/admin/ingest/ping` | Admin health for ingestion services. |

Example: upload an invoice via curl
```bash
curl -X POST http://localhost:8000/invoices/upload \ 
  -H "Cookie: expenseai_session=<session_cookie>" \ 
  -F "file=@/path/to/invoice.pdf"
```

Counterfactual evaluation
```bash
curl -X POST http://localhost:8000/invoices/123/counterfactual \ 
  -H "Content-Type: application/json" \ 
  -H "Cookie: expenseai_session=<session_cookie>" \ 
  -d '{
        "line_changes": [
          {"line_no": 1, "unit_price": "1200.00", "gst_rate": "18"}
        ]
      }'
```

## Commands / Scripts
| Command | Description |
| --- | --- |
| `flask --app expenseai_ext:create_app manage init-db` | Run database migrations (upgrade shortcut). |
| `flask --app expenseai_ext:create_app manage create-admin` | Create an admin/org pair. |
| `flask --app expenseai_ext:create_app manage list-users` | List registered users and roles. |
| `flask --app expenseai_ext:create_app manage parse-invoice --id <invoice_id>` | Parse invoice synchronously. |
| `flask --app expenseai_ext:create_app manage risk-run --id <invoice_id>` | Run risk pipeline synchronously. |
| `flask --app expenseai_ext:create_app manage backfill-history --days 365` | Populate benchmarking baselines. |
| `celery -A expenseai.celery_app:celery worker` | Start task worker. |
| `python -m expenseai_ingest.watcher` | Run ingestion watcher manually (optional). |

## Data Flow Explanation
1. **Ingestion**: Users upload invoices or drop files into configured watch folders/IMAP mailboxes. Ingestion tasks store originals, create `Invoice` rows, and enqueue parsing.
2. **Parsing**: The parser worker loads cached Hugging Face weights, validates JSON output, and persists extracted fields (`ExtractedField`, `LineItem`).
3. **Post-parse orchestration**: Risk orchestrator launches benchmarking, compliance checks, and vendor drift analysis (via Celery or inline if background disabled).
4. **Risk & compliance**: Contributors (market outliers, arithmetic, GST, duplicates, bandit policy) are aggregated into composite scores stored in `RiskScore`.
5. **User experience**: Dashboards display recent invoices, risk alerts, and vendor insights; admins manage members, billing, and invites.
6. **Feedback loop**: Audit logs capture every action, enabling counterfactual simulations and rich notifications. Cached embeddings and vendor profiles accelerate subsequent analyses.

## Error Handling & Observability
- Centralised exception handling (`expenseai_ext.errors`) renders JSON or friendly HTML, adding request IDs and retry hints.
- Structured logging with JSON output captures latency, user IDs, and route metadata; slow requests (>1s) are flagged automatically.
- Rate limiting via Flask-Limiter protects auth endpoints and promotes fair usage.
- Idempotent POST endpoints accept `Idempotency-Key` headers and persist responses for safe retries.
- Audit logs (`expenseai_models.audit.AuditLog`) record security-sensitive events (OTP issuance, login failures, risk runs).
- Feature flags (`FF_VENDOR_DRIFT_ALERTS`, `FF_WHATSAPP`, etc.) allow gradual rollouts.

## Troubleshooting
- **Model downloads stall**: ensure outbound HTTPS access to Hugging Face or set up an offline mirror with pre-downloaded weights.
- **`torch` install fails on Windows**: install the official CUDA/cuDNN wheels or use the CPU-only variant (`pip install torch --index-url https://download.pytorch.org/whl/cpu`).
- **Redis connection errors**: verify `REDIS_URL`, allowlist firewall rules, or set `CELERY_TASK_ALWAYS_EAGER=true` for lightweight local testing.
- **Ingestion watcher not triggering**: confirm `INGEST_WATCH_PATHS` paths exist and the process has read permissions; on Windows run PowerShell as Administrator for network shares.
- **SMTP email not sending**: set `MAIL_SUPPRESS_SEND=false`, verify TLS/SSL flags, and check provider-specific app passwords.
- **GST provider timeouts**: fallback to `GST_PROVIDER=none` or the test fixture for offline validation.

## FAQ
**Do I need a GPU?** – No. The parser falls back to CPU inference; expect slower throughput. For production throughput, use CUDA-compatible GPUs and set `VISION_MODEL_DEVICE=cuda`.

**Where are uploaded files stored?** – Under `instance/uploads` with thumbnails in `instance/thumbnails`. Configure S3 storage by setting `STORAGE_BACKEND=s3` and providing bucket credentials.

**How do I disable automatic parsing?** – Set `AUTO_PARSE_ON_UPLOAD=false`. Users can trigger parsing manually from the invoice detail page or via `POST /invoices/<id>/parse`.

**Can I integrate a different LLM?** – Yes. Update `VISION_MODEL_NAME` and ensure the model supports multimodal prompts. Custom adapters can be added to `expenseai_ai/model_client.py`.

**What about WhatsApp conversations?** – Enable `FF_WHATSAPP=true` and configure Twilio credentials. The chat service adapts prompts to WhatsApp formatting rules.

**Is there multi-language support?** – Flask-Babel reads translations from `translations/`. Add or update `.po` files and run `flask babel compile` after changes.

## Contributing Guidelines
1. Fork the repository and create a feature branch (`git checkout -b feature/<topic>`).
2. Keep changes focused; update or add tests where feasible (pytest scaffolding recommended).
3. Run linting/formatting (e.g. `ruff`, `black`) if you add them to the toolchain.
4. Document new environment variables, CLI commands, or migrations in this README.
5. Submit a descriptive pull request summarising behaviour changes and testing notes.

## Credits / Authors
- **Shah Ram** – Full Stack Developer
- **Nisarg Parmar** – AI/ML Developer
- **Milan Gohil** – Backend Developer
- **Mahir Sanghavi** – Frontend Developer
