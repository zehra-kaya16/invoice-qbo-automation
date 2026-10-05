from app.services.categorization.categorizer import (
    TransactionCategorizer,
)


def test_build_category_candidates_returns_given_accounts():
    categorizer = TransactionCategorizer()

    expense_accounts = [
        {
            "id": "10",
            "name": "Office Supplies",
            "fully_qualified_name": (
                "Expenses:Office Supplies"
            ),
        },
        {
            "id": "20",
            "name": "Software & Subscriptions",
            "fully_qualified_name": (
                "Expenses:Software & Subscriptions"
            ),
        },
    ]

    candidates = (
        categorizer.build_category_candidates(
            expense_accounts
        )
    )

    assert len(candidates) == 2

    assert candidates[0] == {
        "id": "10",
        "name": "Office Supplies",
        "fully_qualified_name": (
            "Expenses:Office Supplies"
        ),
    }

    assert candidates[1] == {
        "id": "20",
        "name": "Software & Subscriptions",
        "fully_qualified_name": (
            "Expenses:Software & Subscriptions"
        ),
    }


def test_build_category_candidates_skips_invalid_accounts():
    categorizer = TransactionCategorizer()

    expense_accounts = [
        {
            "id": "10",
            "name": "Office Supplies",
            "fully_qualified_name": (
                "Expenses:Office Supplies"
            ),
        },
        {
            "id": None,
            "name": "Travel",
        },
        {
            "id": "30",
            "name": None,
        },
    ]

    candidates = (
        categorizer.build_category_candidates(
            expense_accounts
        )
    )

    assert len(candidates) == 1

    assert candidates[0]["id"] == "10"
    assert candidates[0]["name"] == "Office Supplies"


def test_build_category_candidates_uses_name_as_fallback():
    categorizer = TransactionCategorizer()

    expense_accounts = [
        {
            "id": "10",
            "name": "Office Supplies",
            "fully_qualified_name": None,
        }
    ]

    candidates = (
        categorizer.build_category_candidates(
            expense_accounts
        )
    )

    assert len(candidates) == 1

    assert (
        candidates[0]["fully_qualified_name"]
        == "Office Supplies"
    )


def test_build_category_candidates_empty_input():
    categorizer = TransactionCategorizer()

    candidates = (
        categorizer.build_category_candidates([])
    )

    assert candidates == []

def test_suggest_category_matches_account_name():
    categorizer = TransactionCategorizer()

    expense_accounts = [
        {
            "id": "10",
            "name": "Office Supplies",
            "fully_qualified_name": (
                "Expenses:Office Supplies"
            ),
        },
        {
            "id": "20",
            "name": "Software & Subscriptions",
            "fully_qualified_name": (
                "Expenses:Software & Subscriptions"
            ),
        },
    ]

    result = categorizer.suggest_category(
        transaction_description=(
            "Monthly Office Supplies purchase"
        ),
        vendor_name=None,
        expense_accounts=expense_accounts,
    )

    assert result is not None
    assert result["id"] == "10"
    assert result["name"] == "Office Supplies"


def test_suggest_category_returns_none_when_no_match():
    categorizer = TransactionCategorizer()

    expense_accounts = [
        {
            "id": "10",
            "name": "Office Supplies",
            "fully_qualified_name": (
                "Expenses:Office Supplies"
            ),
        }
    ]

    result = categorizer.suggest_category(
        transaction_description="Microsoft 365",
        vendor_name="Microsoft",
        expense_accounts=expense_accounts,
    )

    assert result is None