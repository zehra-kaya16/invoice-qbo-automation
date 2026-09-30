from typing import Optional


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    return " ".join(
        value.lower().strip().split()
    )


def find_expense_account_candidates(
    category_suggestion: Optional[str],
    expense_accounts: list[dict],
) -> list[dict]:
    """
    Return conservative text-based account candidates.

    This function does not select an account automatically.
    It only compares the extracted category suggestion with
    real QBO Expense account names.

    No QBO API call is made here.
    """

    normalized_category = _normalize_text(
        category_suggestion
    )

    if not normalized_category:
        return []

    matches: list[dict] = []

    for account in expense_accounts:
        name = _normalize_text(
            account.get("name")
        )

        fully_qualified_name = _normalize_text(
            account.get("fully_qualified_name")
        )

        name_matches = (
            normalized_category == name
            or normalized_category in name
            or (
                name
                and name in normalized_category
            )
        )

        fully_qualified_name_matches = (
            normalized_category == fully_qualified_name
            or normalized_category in fully_qualified_name
            or (
                fully_qualified_name
                and fully_qualified_name
                in normalized_category
            )
        )

        if (
            name_matches
            or fully_qualified_name_matches
        ):
            matches.append(account)

    return matches