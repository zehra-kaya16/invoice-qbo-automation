"""
Bank-statement transaction categorization service.

This module will generate category suggestions for extracted
bank transactions using the available QuickBooks expense accounts.

No QuickBooks write operation should happen in this module.
"""

from typing import Any


class TransactionCategorizer:
    """
    Generate read-only category candidates for bank transactions.

    This service does not write anything to QuickBooks.
    """

    def build_category_candidates(
        self,
        expense_accounts: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []

        for account in expense_accounts:
            account_id = account.get("id")
            account_name = account.get("name")

            if not account_id or not account_name:
                continue

            fully_qualified_name = (
                account.get("fully_qualified_name")
                or account_name
            )

            candidates.append(
                {
                    "id": str(account_id),
                    "name": str(account_name),
                    "fully_qualified_name": str(
                        fully_qualified_name
                    ),
                }
            )

        return candidates

    def suggest_category(
        self,
        transaction_description: str,
        vendor_name: str | None,
        expense_accounts: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        candidates = self.build_category_candidates(
            expense_accounts
        )

        if not candidates:
            return None

        search_text = " ".join(
            part
            for part in [
                transaction_description,
                vendor_name or "",
            ]
            if part
        ).lower()

        if not search_text.strip():
            return None

        for candidate in candidates:
            account_name = candidate["name"].lower()

            if account_name in search_text:
                return candidate

        return None