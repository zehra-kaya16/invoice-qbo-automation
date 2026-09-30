from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.services.matching.matcher import (
    BankFeedMatcher,
)


def test_check_matches_debit_by_amount_and_date():
    matcher = BankFeedMatcher()

    check = SimpleNamespace(
        amount=Decimal("550.00"),
        date=date(2026, 9, 15),
        check_number=None,
        payee=None,
    )

    transaction = SimpleNamespace(
        amount=Decimal("-550.00"),
        date=date(2026, 9, 15),
        check_number=None,
        vendor_suggestion=None,
        description="Test",
    )

    result = (
        matcher.match_check_image_to_statement(
            check_image=check,
            transactions=[transaction],
        )
    )

    assert result["matched"] is True

    assert (
        result["best_match"][
            "statement_transaction_index"
        ]
        == 0
    )

    assert (
        result["best_match"]["score"]
        >= 70
    )


def test_check_does_not_match_deposit():
    matcher = BankFeedMatcher()

    check = SimpleNamespace(
        amount=Decimal("550.00"),
        date=date(2026, 9, 15),
        check_number="1001",
        payee="Test Vendor",
    )

    transaction = SimpleNamespace(
        amount=Decimal("550.00"),
        date=date(2026, 9, 15),
        check_number="1001",
        vendor_suggestion="Test Vendor",
        description="Test Vendor",
    )

    result = (
        matcher.match_check_image_to_statement(
            check_image=check,
            transactions=[transaction],
        )
    )

    assert result["matched"] is False


def test_check_requires_amount_match():
    matcher = BankFeedMatcher()

    check = SimpleNamespace(
        amount=Decimal("550.00"),
        date=date(2026, 9, 15),
        check_number="1001",
        payee="Test Vendor",
    )

    transaction = SimpleNamespace(
        amount=Decimal("-549.00"),
        date=date(2026, 9, 15),
        check_number="1001",
        vendor_suggestion="Test Vendor",
        description="Test Vendor",
    )

    result = (
        matcher.match_check_image_to_statement(
            check_image=check,
            transactions=[transaction],
        )
    )

    assert result["matched"] is False