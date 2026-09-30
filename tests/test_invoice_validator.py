from decimal import Decimal

from app.schemas.documents import LineItem, ReceiptData
from app.services.validation.invoice_validator import (
    validate_invoice,
)


def test_valid_invoice():
    invoice = ReceiptData(
        vendor="Northstar Software LLC",
        date="2026-09-15",
        invoice_number="INV-2026-0915",
        due_date="2026-09-30",
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("50.00"),
        total_amount=Decimal("550.00"),
        line_items=[
            LineItem(
                description="Invoice workflow automation setup",
                quantity=1,
                unit_price=Decimal("350.00"),
                amount=Decimal("350.00"),
            ),
            LineItem(
                description="PDF extraction and validation module",
                quantity=1,
                unit_price=Decimal("150.00"),
                amount=Decimal("150.00"),
            ),
        ],
    )

    result = validate_invoice(invoice)

    assert result["is_valid"] is True
    assert result["errors"] == []
    assert result["line_items_total"] == Decimal("500.00")
    assert result["expected_total"] == Decimal("550.00")


def test_invalid_line_item_amount():
    invoice = ReceiptData(
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("50.00"),
        total_amount=Decimal("550.00"),
        line_items=[
            LineItem(
                description="Test item",
                quantity=2,
                unit_price=Decimal("100.00"),
                amount=Decimal("150.00"),
            ),
        ],
    )

    result = validate_invoice(invoice)

    assert result["is_valid"] is False
    assert len(result["errors"]) > 0


def test_invalid_total():
    invoice = ReceiptData(
        subtotal=Decimal("500.00"),
        tax_amount=Decimal("50.00"),
        total_amount=Decimal("525.00"),
        line_items=[
            LineItem(
                description="Test item",
                quantity=1,
                unit_price=Decimal("500.00"),
                amount=Decimal("500.00"),
            ),
        ],
    )

    result = validate_invoice(invoice)

    assert result["is_valid"] is False
    assert len(result["errors"]) > 0


def test_missing_subtotal_does_not_crash():
    invoice = ReceiptData(
        total_amount=Decimal("550.00"),
        line_items=[
            LineItem(
                description="Test item",
                quantity=1,
                unit_price=Decimal("550.00"),
                amount=Decimal("550.00"),
            ),
        ],
    )

    result = validate_invoice(invoice)

    assert result["is_valid"] is True
    assert "subtotal_tax_tip_vs_total" in result["checks_skipped"]