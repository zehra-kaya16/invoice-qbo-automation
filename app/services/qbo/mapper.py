from typing import Optional

from app.schemas.documents import ReceiptData


def build_qbo_create_plan(
    data: ReceiptData,
    create_as: str,
    vendor_id: str,
    account_id: str,
    description: Optional[str] = None,
    bank_account_id: Optional[str] = None,
) -> dict:
    """
    Convert reviewed invoice data into the arguments expected by
    the existing QBOClient create_expense/create_bill methods.

    This function does not call QuickBooks.
    """

    if create_as not in {"expense", "bill"}:
        raise ValueError(
            "create_as must be 'expense' or 'bill'"
        )

    if not vendor_id:
        raise ValueError(
            "vendor_id is required"
        )

    if not account_id:
        raise ValueError(
            "account_id is required"
        )

    if data.total_amount is None:
        raise ValueError(
            "total_amount is required for QBO mapping"
        )

    if data.date is None:
        raise ValueError(
            "date is required for QBO mapping"
        )

    transaction_date = data.date.isoformat()

    if create_as == "expense":
        return {
            "method": "create_expense",
            "kwargs": {
                "vendor_id": vendor_id,
                "account_id": account_id,
                "amount": data.total_amount,
                "date": transaction_date,
                "description": description,
                "bank_account_id": bank_account_id,
            },
        }

    due_date = (
        data.due_date.isoformat()
        if data.due_date is not None
        else None
    )

    return {
        "method": "create_bill",
        "kwargs": {
            "vendor_id": vendor_id,
            "account_id": account_id,
            "amount": data.total_amount,
            "date": transaction_date,
            "due_date": due_date,
            "description": description,
        },
    }