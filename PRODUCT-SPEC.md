# Receipt AI - Product Specification

## Vision

**Not just a receipt scanner - a complete document-to-ledger automation system.**

Upload ANY financial document → AI extracts everything → Matches & pushes to QBO

---

## Document Types Supported

### 1. 🧾 Receipts & Invoices
- Purchase receipts
- Vendor invoices/bills
- Credit card receipts
- Digital receipts (email forwards)

**Extract:** Vendor, amount, date, line items, tax, category

### 2. 🏦 Bank Statements (THE BIG ONE)
- Full bank statement PDFs
- Individual transaction extraction
- Check image extraction from statements
- Deposit detail extraction

**Extract per transaction:**
- Date
- Amount
- Check number (if applicable)
- Payee/description
- Running balance

**Special feature:** Snip individual checks from bank statement images

### 3. 📝 Checks (Standalone or from Statements)
- Scanned check images
- Check images embedded in bank statements
- Deposit slips with check details

**Extract:**
- Check number
- Payee name
- Amount
- Date
- Memo line
- Bank/routing (if visible)

### 4. 💳 Credit Card Statements
- Monthly statements
- Individual transaction extraction

---

## Core Features

### Document Processing Pipeline

```
Upload Document
      ↓
Classify Document Type (AI)
      ↓
Extract Data (AI - type-specific)
      ↓
Match to QBO Bank Feed
      ↓
Create/Match Vendor
      ↓
Attach Source Document
      ↓
Categorize Transaction
      ↓
Push to QBO (or queue for review)
```

### Bank Statement → QBO Matching (KEY FEATURE)

**The Problem:**
- Bank feeds in QBO show transactions but no source docs
- Accountants manually match statements to transactions
- Check images are buried in bank PDFs
- No way to auto-attach supporting docs

**Our Solution:**

1. **Upload bank statement PDF**
2. **AI extracts ALL transactions** from statement
3. **AI extracts check images** (snips them from the PDF)
4. **Match each transaction** to QBO bank feed by:
   - Amount (exact match)
   - Date (within range)
   - Check number (for checks)
5. **Auto-attach source document** to matched transaction
6. **Create vendor** if payee doesn't exist
7. **Suggest category** based on vendor/history

### Check Image Extraction

For bank statements with check images:

1. Detect check image regions in PDF
2. Extract/snip each check as separate image
3. OCR the check to get:
   - Payee name
   - Amount
   - Check number
   - Memo
4. Match to corresponding debit in bank feed
5. Attach check image to transaction
6. Create vendor record if needed

---

## QBO Integration Points

### Read from QBO:
- Bank feed transactions (unmatched)
- Chart of accounts
- Vendor list
- Existing transactions

### Write to QBO:
- Match bank feed transactions
- Create expenses
- Create bills
- Create vendors
- Attach documents to transactions
- Categorize transactions

### Matching Logic:

```python
def match_to_bank_feed(extracted_txn, qbo_bank_feed):
    """
    Match extracted transaction to QBO bank feed
    """
    candidates = []
    
    for qbo_txn in qbo_bank_feed:
        score = 0
        
        # Amount match (required)
        if abs(extracted_txn.amount - qbo_txn.amount) < 0.01:
            score += 50
        else:
            continue  # Must match amount
        
        # Date match (within 3 days)
        date_diff = abs(extracted_txn.date - qbo_txn.date).days
        if date_diff == 0:
            score += 30
        elif date_diff <= 3:
            score += 20
        elif date_diff <= 7:
            score += 10
        
        # Check number match (if applicable)
        if extracted_txn.check_number and qbo_txn.check_number:
            if extracted_txn.check_number == qbo_txn.check_number:
                score += 40
        
        # Vendor name similarity
        if extracted_txn.vendor:
            similarity = fuzzy_match(extracted_txn.vendor, qbo_txn.description)
            score += similarity * 20
        
        candidates.append((qbo_txn, score))
    
    # Return best match if score > threshold
    candidates.sort(key=lambda x: x[1], reverse=True)
    if candidates and candidates[0][1] >= 70:
        return candidates[0][0]
    
    return None
```

---

## User Workflow

### Workflow 1: Monthly Bank Statement Processing

1. Download bank statement PDF (with check images)
2. Upload to Receipt AI
3. AI extracts all transactions + check images
4. Review extracted data (edit if needed)
5. Click "Match to QBO"
6. AI matches transactions to bank feed
7. Review matches (approve/reject)
8. Click "Push to QBO"
9. All transactions matched, docs attached, vendors created ✅

### Workflow 2: Receipt/Bill Upload

1. Upload receipt/bill image
2. AI extracts vendor, amount, date, category
3. Review/edit extracted data
4. Select: Create as Expense or Bill
5. Push to QBO
6. Done ✅

### Workflow 3: Email Forwarding

1. Forward receipt email to receipts@[user].receiptai.com
2. AI processes automatically
3. Notification: "New receipt processed - Review?"
4. One-click approve or edit

---

## Technical Architecture

### AI Extraction Service

```python
class DocumentExtractor:
    """Multi-modal AI extraction"""
    
    def classify(self, document) -> DocumentType:
        """Classify document type"""
        # Use vision model to classify
        pass
    
    def extract_receipt(self, document) -> ReceiptData:
        """Extract receipt/invoice data"""
        pass
    
    def extract_bank_statement(self, document) -> BankStatementData:
        """Extract all transactions from bank statement"""
        # Returns list of transactions + check images
        pass
    
    def extract_check(self, image) -> CheckData:
        """Extract check details from image"""
        pass
    
    def snip_checks_from_statement(self, pdf) -> List[CheckImage]:
        """Find and extract check images from bank statement PDF"""
        # Use vision to locate check regions
        # Extract as separate images
        pass
```

### Data Models

```python
class ExtractedTransaction:
    date: date
    amount: Decimal
    vendor: str
    description: str
    check_number: Optional[str]
    category_suggestion: str
    source_document: str  # S3 path
    confidence: float

class BankStatementExtraction:
    statement_date: date
    account_number: str
    beginning_balance: Decimal
    ending_balance: Decimal
    transactions: List[ExtractedTransaction]
    check_images: List[CheckImage]

class CheckImage:
    image_path: str  # S3 path
    check_number: str
    payee: str
    amount: Decimal
    date: date
    memo: Optional[str]
    matched_transaction_id: Optional[str]
```

---

## MVP Scope (v0.1)

### Must Have:
- [ ] Upload PDF/image documents
- [ ] AI document classification
- [ ] Receipt/invoice extraction
- [ ] Basic bank statement extraction (transactions)
- [ ] QBO OAuth connection
- [ ] Push expenses to QBO
- [ ] Simple web UI

### v0.2 (Next):
- [ ] Check image extraction from bank statements
- [ ] Bank feed matching
- [ ] Attach documents to QBO transactions
- [ ] Vendor creation/matching

### v0.3 (Later):
- [ ] Email forwarding inbox
- [ ] Bulk processing
- [ ] Auto-categorization learning
- [ ] Multi-company support

---

## Competitive Advantage

| Feature | Dext | Hubdoc | Receipt AI |
|---------|------|--------|------------|
| Receipt capture | ✅ | ✅ | ✅ |
| Bank statement processing | ❌ | ❌ | ✅ |
| Check image extraction | ❌ | ❌ | ✅ |
| Bank feed matching | ❌ | ❌ | ✅ |
| Doc attachment to txns | ❌ | ❌ | ✅ |
| Price | $31-62/mo | $20/mo | $10/mo |

**We're not competing on receipts - we're solving the FULL document problem.**

---

## Pricing Strategy

### Starter: $10/month
- 100 documents/month
- 1 QBO company
- Receipt & invoice processing
- Basic bank statement processing

### Pro: $25/month
- 500 documents/month
- 3 QBO companies
- Check image extraction
- Bank feed matching
- Priority support

### Firm: $15/user/month
- Unlimited documents
- Unlimited companies
- All features
- API access
- White-label option

---

*Last updated: 2026-01-28*
