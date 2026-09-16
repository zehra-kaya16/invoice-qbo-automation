from datetime import date
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel

class DocumentType(str, Enum):
    RECEIPT = "receipt"
    INVOICE = "invoice"
    BILL = "bill"
    BANK_STATEMENT = "bank_statement"
    CHECK = "check"
    CREDIT_CARD_STATEMENT = "credit_card_statement"
    UNKNOWN = "unknown"

class ProcessingStatus(str, Enum):
    UPLOADED = "uploaded"
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    MATCHING = "matching"
    MATCHED = "matched"
    PUSHED = "pushed"
    FAILED = "failed"

class LineItem(BaseModel):
    description: str
    quantity: Optional[float] = 1.0
    unit_price: Optional[Decimal] = None
    amount: Decimal
    category: Optional[str] = None

class ReceiptData(BaseModel):
    vendor: Optional[str] = None
    vendor_address: Optional[str] = None
    date: Optional[date] = None

    total_amount: Optional[Decimal] = None
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    tip_amount: Optional[Decimal] = None

    payment_method: Optional[str] = None

    line_items: List[LineItem] = []

    category_suggestion: Optional[str] = None

    confidence: float = 0.0
    raw_text: Optional[str] = None

class CheckData(BaseModel):
    check_number: Optional[str] = None
    payee: Optional[str] = None
    amount: Optional[Decimal] = None
    date: Optional[date] = None
    memo: Optional[str] = None
    bank_name: Optional[str] = None
    routing_number: Optional[str] = None
    account_number_last4: Optional[str] = None
    confidence: float = 0.0
    image_path: Optional[str] = None

class BankTransaction(BaseModel):
    date: date
    description: str
    amount: Decimal
    transaction_type: str
    check_number: Optional[str] = None
    running_balance: Optional[Decimal] = None
    vendor_suggestion: Optional[str] = None
    category_suggestion: Optional[str] = None
    check_image: Optional[CheckData] = None

class BankStatementData(BaseModel):
    bank_name: Optional[str] = None
    account_number_last4: Optional[str] = None
    account_type: Optional[str] = None
    statement_period_start: Optional[date] = None
    statement_period_end: Optional[date] = None
    beginning_balance: Optional[Decimal] = None
    ending_balance: Optional[Decimal] = None
    total_deposits: Optional[Decimal] = None
    total_withdrawals: Optional[Decimal] = None
    transactions: List[BankTransaction] = []
    check_images: List[CheckData] = []
    confidence: float = 0.0
    raw_text: Optional[str] = None

class DocumentUploadResponse(BaseModel):
    id: str
    filename: str
    status: ProcessingStatus
    document_type: Optional[DocumentType] = None
    message: str = ""

class ExtractionResponse(BaseModel):
    id: str
    document_type: DocumentType
    status: ProcessingStatus
    receipt_data: Optional[ReceiptData] = None
    bank_statement_data: Optional[BankStatementData] = None
    check_data: Optional[CheckData] = None