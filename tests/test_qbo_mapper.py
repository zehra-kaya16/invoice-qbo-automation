from decimal import Decimal

import pytest

from app.schemas.documents import ReceiptData
from app.services.qbo.mapper import build_qbo_create_plan


def make_invoice() -> ReceiptData:
    return ReceiptData(
        vendor="NORTHSTAR SOFTWARE LLC",
        date="2026-09-15",
        invoice_number="INV-2026-0915",
        due_date="2026-09-30",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("50.00"),
        total_amount=Decimal("550.00"),
    )


def test_bill_mapping():
    invoice = make_invoice()

    plan = build_qbo_create_plan(
        data=invoice,
        create_as="bill",
        vendor_id="vendor_123",
        account_id="account_456",
        description="Reviewed vendor invoice",
    )

    assert plan["method"] == "create_bill"

    kwargs = plan["kwargs"]

    assert kwargs["vendor_id"] == "vendor_123"
    assert kwargs["account_id"] == "account_456"
    assert kwargs["amount"] == Decimal("550.00")
    assert kwargs["date"] == "2026-09-15"
    assert kwargs["due_date"] == "2026-09-30"
    assert kwargs["description"] == "Reviewed vendor invoice"


def test_expense_mapping():
    invoice = make_invoice()

    plan = build_qbo_create_plan(
        data=invoice,
        create_as="expense",
        vendor_id="vendor_123",
        account_id="account_456",
        bank_account_id="bank_789",
    )

    assert plan["method"] == "create_expense"

    kwargs = plan["kwargs"]

    assert kwargs["vendor_id"] == "vendor_123"
    assert kwargs["account_id"] == "account_456"
    assert kwargs["amount"] == Decimal("550.00")
    assert kwargs["date"] == "2026-09-15"
    assert kwargs["bank_account_id"] == "bank_789"


def test_invalid_create_as():
    invoice = make_invoice()

    with pytest.raises(
        ValueError,
        match="create_as must be 'expense' or 'bill'",
    ):
        build_qbo_create_plan(
            data=invoice,
            create_as="invoice",
            vendor_id="vendor_123",
            account_id="account_456",
        )


def test_missing_total_amount():
    invoice = ReceiptData(
        vendor="NORTHSTAR SOFTWARE LLC",
        date="2026-09-15",
    )

    with pytest.raises(
        ValueError,
        match="total_amount is required",
    ):
        build_qbo_create_plan(
            data=invoice,
            create_as="bill",
            vendor_id="vendor_123",
            account_id="account_456",
        )


def test_missing_date():
    invoice = ReceiptData(
        vendor="NORTHSTAR SOFTWARE LLC",
        total_amount=Decimal("550.00"),
    )

    with pytest.raises(
        ValueError,
        match="date is required",
    ):
        build_qbo_create_plan(
            data=invoice,
            create_as="bill",
            vendor_id="vendor_123",
            account_id="account_456",
        )