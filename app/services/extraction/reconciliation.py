"""Adapt document evidence to the existing reviewed reconciliation workflow."""
import base64
import hashlib
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from app.schemas.documents import BankStatementData, BankTransaction, CheckData, CheckImage


def prepare_standalone_check(document, check: CheckData):
    if check.direction not in {'outgoing', 'incoming'}:
        raise ValueError("Select whether the check is outgoing or incoming before matching")
    if not check.date or check.amount is None or not check.amount.is_finite() or check.amount <= 0:
        raise ValueError("Check date and a positive amount are required; correct the extracted check fields")
    if not check.check_number or not check.check_number.strip():
        raise ValueError("Check number is required; correct the extracted check fields")
    from app.services.persistence.attachment_sources import prepare_invoice_attachment, invoice_attachment
    prepare_invoice_attachment(document)
    filename, content_type, content = invoice_attachment(document)
    key = 'standalone:' + document['id']
    document['check_image_sources'] = {key: {
        'filename': filename, 'content_type': content_type,
        'content_base64': base64.b64encode(content).decode('ascii'),
        'sha256': hashlib.sha256(content).hexdigest(),
    }}
    # Date window permits a written check to clear later. The user must review
    # every candidate; this synthetic transaction never creates an expense.
    statement = BankStatementData(
        bank_name=check.bank_name, account_number_last4=check.account_number_last4,
        account_type='checking', statement_period_start=check.date,
        statement_period_end=check.date + timedelta(days=90),
        transactions=[BankTransaction(date=check.date, description=check.memo or check.payee or 'Check',
            amount=check.amount if check.direction == 'incoming' else -check.amount, transaction_type='check', check_number=check.check_number,
            vendor_suggestion=check.payee)],
        check_images=[CheckImage(image_path=key, **check.model_dump(exclude={'image_path', 'bank_name', 'direction',
            'routing_number', 'account_number_last4', 'confidence', *({'payee'} if check.direction == 'incoming' else set())}))],
        confidence=check.confidence,
    )
    return {**check.model_dump(), **statement.model_dump()}


def validate_reconciliation(document):
    try:
        data = BankStatementData(**(document.get('extracted_data') or {}))
        if not data.statement_period_start or not data.statement_period_end or data.statement_period_start > data.statement_period_end:
            raise ValueError('A valid statement period is required')
        if not data.transactions:
            raise ValueError('No transactions were extracted; review the source document')
        if any(not t.amount.is_finite() or t.amount == 0 for t in data.transactions):
            raise ValueError('Every transaction must have a finite, nonzero amount')
        if document.get('document_type') == 'credit_card_statement':
            if data.check_images:
                raise ValueError('Credit-card statements cannot contain check attachments')
            if any(not data.statement_period_start <= t.date <= data.statement_period_end for t in data.transactions):
                raise ValueError('Transaction date lies outside the statement period')
        if document.get('document_type') == 'check':
            if len(data.transactions) != 1 or len(data.check_images) != 1:
                raise ValueError('Re-extract or correct the standalone check before matching')
        return data
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None


def extract_credit_card_statement(extractor, pages):
    """Read the header across pages, then every page without bank/check heuristics."""
    header = extractor._parse_json(extractor._vision_request_pages(pages, """
Extract the credit-card statement header from ALL supplied pages. Return only JSON:
{"bank_name":null,"account_number_last4":null,"statement_period_start":null,
"statement_period_end":null,"beginning_balance":null,"ending_balance":null}.
Use ISO dates, numeric monetary values without currency symbols. Unknown values are null.
Do not substitute payment due date for statement period end.
"""))
    transactions = []
    for page in pages:
        rows = extractor._parse_json(extractor._vision_request(page, """
Extract EVERY posted transaction row on this CREDIT CARD statement page.
Return ONLY a JSON array with date (YYYY-MM-DD), description, amount,
transaction_type (charge/refund/payment/fee/interest/transfer), vendor_suggestion (or null).
Normalize amounts: purchases, charges, fees, interest NEGATIVE; refunds, payments
and credits reducing the card balance POSITIVE. Use the posted transaction date.
Never include balance totals, pending transactions, payment coupons or summary subtotals.
Preserve separate identical rows: do not deduplicate legitimate transactions.
Return [] if no posted transaction rows. Never invent missing dates or amounts.
"""))
        if not isinstance(rows, list):
            raise ValueError('Credit-card extraction did not return transaction rows')
        transactions.extend(BankTransaction(**row) for row in rows)
    data = BankStatementData(**header, account_type='credit_card', transactions=transactions,
        total_deposits=sum((t.amount for t in transactions if t.amount > 0), Decimal(0)),
        total_withdrawals=sum((-t.amount for t in transactions if t.amount < 0), Decimal(0)),
        confidence=0.8)
    validate_reconciliation({'document_type':'credit_card_statement','extracted_data':data.model_dump()})
    return data
