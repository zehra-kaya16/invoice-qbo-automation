from datetime import date as Date
from decimal import Decimal
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


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

    @field_validator("unit_price", "amount", mode="before")
    @classmethod
    def clean_currency_values(cls, value):
        if isinstance(value, str):
            value = (
                value.strip()
                .replace("$", "")
                .replace("€", "")
                .replace("£", "")
                .replace("₺", "")
                .replace(",", "")
            )

        return value

class ReceiptData(BaseModel):
    vendor: Optional[str] = None
    vendor_address: Optional[str] = None
    date: Optional[Date] = None
    invoice_number: Optional[str] = None
    due_date: Optional[Date] = None
    total_amount: Optional[Decimal] = None
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    tip_amount: Optional[Decimal] = None

    currency: Optional[str] = None
    payment_method: Optional[str] = None

    line_items: List[LineItem] = []

    category_suggestion: Optional[str] = None

    confidence: float = 0.0
    raw_text: Optional[str] = None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value):
        if value is None:
            return None

        return str(value).strip().upper()

    @field_validator(
        "total_amount",
        "subtotal",
        "tax_amount",
        "tip_amount",
        mode="before",
    )
    @classmethod
    def clean_currency_values(cls, value):
        if isinstance(value, str):
            value = (
                value.strip()
                .replace("$", "")
                .replace("€", "")
                .replace("£", "")
                .replace("₺", "")
                .replace(",", "")
            )

        return value

class InvoiceValidationResult(BaseModel):
    is_valid: bool
    errors: List[str] = []
    warnings: List[str] = []
    checks_performed: List[str] = []
    checks_skipped: List[str] = []
    line_items_total: Optional[Decimal] = None
    expected_total: Optional[Decimal] = None

class CheckData(BaseModel):
    check_number: Optional[str] = None
    payee: Optional[str] = None
    amount: Optional[Decimal] = None
    date: Optional[Date] = None
    memo: Optional[str] = None
    bank_name: Optional[str] = None
    routing_number: Optional[str] = None
    account_number_last4: Optional[str] = None
    confidence: float = 0.0
    image_path: Optional[str] = None

class BankTransaction(BaseModel):
    date: Date
    description: str
    amount: Decimal
    transaction_type: str
    check_number: Optional[str] = None
    running_balance: Optional[Decimal] = None
    vendor_suggestion: Optional[str] = None
    category_suggestion: Optional[str] = None
    check_image: Optional[CheckData] = None

class CheckImage(BaseModel):
    image_path: str

    check_number: Optional[str] = None
    payee: Optional[str] = None
    amount: Optional[Decimal] = None
    date: Optional[Date] = None
    memo: Optional[str] = None

    matched_transaction_id: Optional[str] = None

class BankStatementData(BaseModel):
    bank_name: Optional[str] = None
    account_number_last4: Optional[str] = None
    account_type: Optional[str] = None
    statement_period_start: Optional[Date] = None
    statement_period_end: Optional[Date] = None
    beginning_balance: Optional[Decimal] = None
    ending_balance: Optional[Decimal] = None
    total_deposits: Optional[Decimal] = None
    total_withdrawals: Optional[Decimal] = None
    transactions: List[BankTransaction] = Field(
        default_factory=list
    )
    check_images: List[CheckImage] = Field(
        default_factory=list
    )
    confidence: float = 0.0
    raw_text: Optional[str] = None
    @field_validator(
        "beginning_balance",
        "ending_balance",
        "total_deposits",
        "total_withdrawals",
        mode="before",
    )
    @classmethod
    def clean_bank_currency_values(cls, value):
        if value is None or value == "":
            return None

        if isinstance(value, str):
            value = (
                value.strip()
                .replace("$", "")
                .replace("€", "")
                .replace("£", "")
                .replace("₺", "")
                .replace(",", "")
            )

        return value

class BankMatchReviewRequest(BaseModel):
    decision: Literal[
        "approve",
        "reject",
        "manual_override",
    ]

    qbo_transaction_id: Optional[str] = None
    qbo_transaction_type: Optional[str] = None

class CategoryReviewRequest(BaseModel):
    decision: Literal[
        "approve",
        "reject",
        "manual_override",
    ]

    qbo_account_id: Optional[str] = None

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
    validation: Optional[InvoiceValidationResult] = None
    approved: bool = False

class PushToQBOResponse(BaseModel):
    id: str
    success: bool
    transactions_pushed: int
    attachments_uploaded: int
    vendors_created: int
    errors: List[str]
    categories_updated: int = 0
    categories_skipped: int = 0
    category_updates: list[dict] = Field(default_factory=list)