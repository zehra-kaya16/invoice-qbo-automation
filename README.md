# Invoice QBO Automation

FastAPI application for reviewing invoices, receipts and bank statements before writing to QuickBooks Online.

## Implemented workflow

- Upload PDF/PNG/JPEG documents and retain the original source in the database.
- Extract one invoice/receipt across multiple PDF pages; edit and validate totals; require human approval.
- Map the approved total to one QBO Bill or Expense and attach the source. Expense requires a bank account.
- Match statement transactions to existing QBO records; review matches, vendors and Purchase categories before writing.
- Extract check images, retain checksum-verified copies in the document snapshot, and attach them to the approved existing QBO transaction.
- Block repeat writes on the same document ID. An attachment error leaves the existing financial transaction ID intact and returns `success=false` with errors.
- Reconcile uncertain results against the original company, write identity and approved plan. Never recreate an uncertain Bill/Expense or automatically repeat an uncertain attachment upload.
- Use persistent browser sessions, company-scoped data and QBO credentials, administrator-managed users and CSRF-protected writes.

Images labelled JPEG are sent to the AI provider with the JPEG media type. HEIC is accepted by the legacy upload interface but does not yet have a verified decoding and complete QBO attachment workflow; convert it to PNG/JPEG for the supported workflow.

The source PDF/image, review decisions, check bytes and write checkpoints are persisted in the database. Database and QBO encryption-key backups are both needed for restoration. Cropped check files are also written under `uploads/checks/{document_id}`.

## Local setup — Windows PowerShell

Use Python 3.12 or newer:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set actual values in `.env`:

```dotenv
DATABASE_URL=sqlite:///./receipt_ai.db
AI_PROVIDER=openai
AI_MODEL=gpt-4o
OPENAI_API_KEY=your-real-key
QBO_CLIENT_ID=your-real-client-id
QBO_CLIENT_SECRET=your-real-client-secret
QBO_REDIRECT_URI=http://localhost:8000/api/qbo/callback
QBO_ENVIRONMENT=sandbox
AUTH_COOKIE_SECURE=false
AUTH_SESSION_HOURS=12
MAX_UPLOAD_BYTES=20971520
MAX_PDF_PAGES=50
```

Generate `QBO_TOKEN_ENCRYPTION_KEY` once, put the output in `.env`, and preserve that same key:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -m alembic upgrade head
python manage_users.py create --company default --company-name "Invoice QBO" --email "admin@example.com" --name "Administrator" --role admin
python -m uvicorn app.main:app --reload
```

Choose your own email/password. Open http://localhost:8000 and sign in. Create team members in **Hesap ve şirket kullanıcıları**. A user belongs to one local company; users of that company share its documents.

For Anthropic, use `AI_PROVIDER=anthropic`, `ANTHROPIC_API_KEY` and an available vision model via `AI_MODEL`. Without an explicit provider the existing key-based fallback chooses OpenAI first. Defaults are `gpt-4o` for OpenAI and `claude-sonnet-4-6` for Anthropic. Explicit `AI_MODEL` applies to the selected provider; use a provider-compatible model.

## Shared LAN development

Run one backend/database on the host computer:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Other computers use `http://HOST_LAN_IP:8000` and accounts created on that backend. Separate databases on each developer's machine do not share documents or users. With the development localhost callback, perform QBO authorization from the host browser using `http://localhost:8000`; the callback must return to the same signed-in browser origin/session. Other signed-in company users can use the saved QBO connection.

For HTTPS deployment, configure the real callback origin in both `.env` and Intuit, and set `AUTH_COOKIE_SECURE=true`. The Compose file is a development configuration.

## Recovery and source preservation

Before moving an older installation, stop the backend and preserve legacy check bytes:

```powershell
python manage_check_sources.py
python manage_check_sources.py --apply
```

The first command reports; the second persists available copies. Resolve any `missing_or_invalid` entries from original file backups. This does not perform QBO writes or alter existing QBO IDs.

If a process stopped during extraction, stop **all** backend/worker processes before unlocking that specific document:

```powershell
python manage_extraction.py --document-id doc_REPLACE_WITH_ACTUAL_ID
```

Restart the backend and extract the original source again. Financial-write checkpoints cannot be reset by this command. The claim prevents simultaneous extraction of the same document; a retry discards old extraction/review data before collecting a new result.

## API essentials

| Operation | Endpoint |
| --- | --- |
| Sign in / current session / sign out | POST `/api/auth/login`, GET `/api/auth/me`, POST `/api/auth/logout` |
| Upload and list | POST `/api/documents/upload`, GET `/api/documents` |
| Source and cropped check source | GET `/api/documents/{id}/source`, GET `/api/documents/{id}/checks/{index}/source` |
| Extract / edit / approve | POST `/{id}/extract`, PATCH `/{id}/extracted`, POST `/{id}/approve` under `/api/documents` |
| QBO authorization | POST `/api/qbo/connect` → authorization URL; GET `/api/qbo/callback` |
| Statement review | `/api/documents/{id}/bank-workflow`, match/vendor/category endpoints |
| Plan / push / recovery | POST `/api/qbo/invoice-plan/{id}`, POST `/api/documents/{id}/push-to-qbo`, GET/POST `/api/documents/{id}/recovery` |
| Process health / database readiness | GET `/health`, GET `/ready` |

Protected endpoints require the session cookie. Mutations also require the `X-CSRF-Token` returned by `/api/auth/me`; the web UI supplies it automatically. `/docs` shows that header for manual Swagger requests. A different company's document returns 404; company-scope manipulation or missing administrator permission returns 403; an absent/expired session returns 401.

## Docker development

```powershell
docker compose up --build -d
docker compose exec app python manage_users.py create --company default --company-name "Invoice QBO" --email "admin@example.com" --name "Administrator" --role admin
```

The app waits for PostgreSQL health and applies Alembic migrations before Uvicorn starts. Named volumes retain PostgreSQL data and check-file cache. Compose uses PostgreSQL, not the host's SQLite database; it does not migrate SQLite users/data into PostgreSQL. `/ready` checks database schema access; it does not attest live AI/QBO availability.

Celery configuration exists, but the main workflow is not dispatched to a durable Celery task. The worker scaffold is optional under the `worker-tools` profile. Extraction currently runs in the app's thread pool; automatic upload processing uses in-process FastAPI background tasks. Interrupted extraction requires explicit recovery.

## Validation and delivery status

```powershell
python -m pytest tests/test_release_flow.py -q
python -m pytest tests -q
```

Step 101 validation: 325 tests and 89 browser checks with temporary SQLite and mocked AI/QBO. The crop snapshot, independent-process restart, deleted-file recovery, tenant isolation, JPEG media type, input limits, duplicate guards and extraction responsiveness were checked. Docker configuration and entrypoint syntax were inspected; a Docker image/PostgreSQL runtime was not executed in the validation environment.

These checks do not replace real sandbox validation. The missing upstream modules, current limitations and next work are documented in `ADIM101-KAPSAM-DENETIMI.md`. Full upstream parity and production readiness are not claimed.
