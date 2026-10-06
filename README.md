# Invoice QBO Automation

**AI-powered invoice and bank statement processing with QuickBooks Online integration.**

Not just receipts — bank statements, checks, invoices, bills. Extract everything. Match to QBO bank feed. Attach source documents automatically.

## 🚀 Features

### Document Processing
- 📷 **Multi-format support:** PDF, PNG, JPG, HEIC
- 🏦 **Bank statement processing:** Extract ALL transactions
- 💳 **Check image extraction:** Snip checks from bank statements
- 🧾 **Receipt/Invoice OCR:** Vendor, amount, date, line items

### QBO Integration
- 🔄 **Bank feed matching:** Auto-match extracted transactions
- 📎 **Document attachment:** Attach source docs to transactions
- 👤 **Vendor management:** Auto-create or match vendors
- 📊 **Smart categorization:** Based on vendor history

### Why This Exists

| Feature | Dext | Hubdoc | Receipt AI |
|---------|------|--------|------------|
| Receipt capture | ✅ | ✅ | ✅ |
| Bank statement processing | ❌ | ❌ | ✅ |
| Check image extraction | ❌ | ❌ | ✅ |
| Bank feed matching | ❌ | ❌ | ✅ |
| Doc attachment to txns | ❌ | ❌ | ✅ |
| Price | $31-62/mo | $20/mo | **$10/mo** |

## 📦 Installation

```bash
# Clone
git clone https://github.com/zehra-kaya16/invoice-qbo-automation.git
cd invoice-qbo-automation

# Setup virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
make install
# or: pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run
make run
# or: python -m uvicorn app.main:app --reload
```

## 🛠️ CLI Usage

```bash
# Classify a document
python cli.py classify receipt.jpg

# Extract data from a receipt
python cli.py extract receipt.jpg

# Extract from bank statement
python cli.py extract statement.pdf --type bank_statement

# Extract as JSON (for scripting)
python cli.py extract receipt.jpg --json

# Run the API server
python cli.py server --port 8000 --reload
```

## 🐳 Docker

```bash
# Build
make docker-build
# or: docker build -t receipt-ai .

# Run with docker-compose (includes PostgreSQL)
docker compose up --build

# Run standalone
docker run -p 8000:8000 --env-file .env receipt-ai
```

## 📡 API Endpoints

### Documents

- `POST /api/documents/upload` - Upload a document for processing
- `GET /api/documents/{id}` - Get document status
- `POST /api/documents/{id}/extract` - Trigger AI extraction
- `GET /api/documents/{id}/extracted` - Get extracted data
- `PATCH /api/documents/{id}/extracted` - Review and update extracted invoice data
- `POST /api/documents/{id}/approve` - Approve a reviewed invoice

### Bank Statement Workflow

- `POST /api/documents/{id}/match-to-qbo` - Match extracted bank-statement transactions to QBO
- `GET /api/documents/{id}/match-review` - Get bank-statement match review state
- `PATCH /api/documents/{id}/match-review/{transaction_index}` - Approve, reject, or manually override a QBO transaction match
- `POST /api/documents/{id}/resolve-vendors` - Resolve check payees against QBO vendors
- `POST /api/documents/{id}/suggest-categories` - Generate category suggestions for matched purchase transactions
- `GET /api/documents/{id}/category-review` - Get category review state
- `PATCH /api/documents/{id}/category-review/{transaction_index}` - Approve, reject, or manually override a category
- `POST /api/documents/{id}/push-to-qbo` - Push reviewed invoice or bank-statement results to QuickBooks Online

### QuickBooks

- `GET /api/qbo/connect` - Start OAuth flow
- `GET /api/qbo/callback` - OAuth callback
- `GET /api/qbo/status` - Get QBO connection status
- `GET /api/qbo/accounts` - Get chart of accounts
- `GET /api/qbo/vendors` - Get vendor list
- `POST /api/qbo/vendors` - Create a QBO vendor

### Health

- `GET /` - App information
- `GET /health` - Health check

## 🏗️ Project Structure

```
receipt-ai/
├── app/
│   ├── main.py              # FastAPI application
│   ├── api/                 # API endpoints
│   │   ├── documents.py     # Document processing
│   │   ├── qbo.py          # QuickBooks integration
│   │   └── health.py       # Health checks
│   ├── core/
│   │   └── config.py       # Configuration
│   ├── schemas/
│   │   └── documents.py    # Data models
│   └── services/
│       ├── extraction/      # AI document extraction
│       │   └── extractor.py
│       ├── matching/        # Bank feed matching
│       │   └── matcher.py
│       ├── qbo/            # QuickBooks API client
│       │   └── client.py
│       └── storage/        # File storage
│           └── storage.py
├── tests/                  # Test suite
├── cli.py                  # Command-line interface
├── Dockerfile             # Container build
├── docker-compose.yml     # Local dev environment
├── Makefile              # Common commands
├── PRODUCT-SPEC.md       # Full product specification
├── requirements.txt
└── .env.example
```

## 🧪 Testing

```bash
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ -v --cov=app
```

## ⚙️ Configuration

Key environment variables (see `.env.example`):

```bash
# AI (required)
OPENAI_API_KEY=sk-...      # or ANTHROPIC_API_KEY

# QuickBooks (for QBO integration)
QBO_CLIENT_ID=...
QBO_CLIENT_SECRET=...
QBO_REDIRECT_URI=http://localhost:8000/api/qbo/callback

# Storage (optional, defaults to local)
STORAGE_TYPE=local         # local, s3, or r2
S3_BUCKET=my-bucket
```

## 🗺️ Roadmap

### Completed

- [x] Document upload API
- [x] PDF and image extraction
- [x] AI-based document classification
- [x] Invoice and receipt extraction
- [x] Bank statement extraction
- [x] Check image detection and extraction
- [x] QBO OAuth integration
- [x] QBO vendor and account retrieval
- [x] Invoice review and approval workflow
- [x] Invoice push to QBO as Bill or Expense
- [x] Bank statement transaction matching
- [x] Match review workflow
- [x] Vendor resolution workflow
- [x] Manual QBO vendor creation
- [x] Category suggestion workflow
- [x] Category review and manual override
- [x] QBO purchase category update
- [x] Check attachment upload to QBO
- [x] Push-state and duplicate-write guards
- [x] Automated pytest regression tests
- [x] Local/S3/R2 storage abstraction
- [x] SQLAlchemy database models
- [x] Alembic migration setup
- [x] Docker Compose environment
- [x] Celery worker and Redis integration

### Planned / Future Improvements

- [ ] Replace remaining in-memory document state with persistent database-backed storage
- [ ] Persist QBO OAuth connection state in the database
- [ ] Add production-ready background processing for the full document workflow
- [ ] Improve transaction categorization using vendor/history learning
- [ ] Add broader integration and end-to-end test coverage
- [ ] Add production deployment configuration and monitoring

## 📄 License

MIT

---

**Built by [Turtle-tools](https://github.com/Turtle-tools)** 🐢

*Slow and steady wins the race.*
