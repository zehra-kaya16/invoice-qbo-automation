from app.services.qbo.matcher import (
    find_expense_account_candidates,
)


def make_accounts():
    return [
        {
            "id": "1",
            "name": "Software & Subscriptions",
            "type": "Expense",
            "sub_type": "DuesSubscriptions",
            "fully_qualified_name": (
                "Software & Subscriptions"
            ),
        },
        {
            "id": "2",
            "name": "Office Supplies",
            "type": "Expense",
            "sub_type": "OfficeGeneralAdministrativeExpenses",
            "fully_qualified_name": (
                "Office Supplies"
            ),
        },
        {
            "id": "3",
            "name": "Travel",
            "type": "Expense",
            "sub_type": "Travel",
            "fully_qualified_name": "Travel",
        },
    ]


def test_exact_category_match():
    accounts = make_accounts()

    result = find_expense_account_candidates(
        "Office Supplies",
        accounts,
    )

    assert len(result) == 1
    assert result[0]["id"] == "2"


def test_partial_category_match():
    accounts = make_accounts()

    result = find_expense_account_candidates(
        "Software",
        accounts,
    )

    assert len(result) == 1
    assert result[0]["id"] == "1"


def test_case_insensitive_match():
    accounts = make_accounts()

    result = find_expense_account_candidates(
        "software & subscriptions",
        accounts,
    )

    assert len(result) == 1
    assert result[0]["id"] == "1"


def test_no_category_returns_empty_list():
    accounts = make_accounts()

    result = find_expense_account_candidates(
        None,
        accounts,
    )

    assert result == []


def test_unrelated_category_returns_empty_list():
    accounts = make_accounts()

    result = find_expense_account_candidates(
        "Advertising",
        accounts,
    )

    assert result == []