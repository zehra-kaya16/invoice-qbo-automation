import pytest

from app.api.documents import _push_bank_statement_categories


class FakeQBOClient:
    def __init__(self):
        self.update_purchase_category_calls = []

    def update_purchase_category(
        self,
        purchase_id: str,
        account_id: str,
    ):
        self.update_purchase_category_calls.append(
            {
                "purchase_id": purchase_id,
                "account_id": account_id,
            }
        )

        return {
            "id": str(purchase_id),
            "type": "purchase",
            "account_id": str(account_id),
        }


def build_category_document(
    review_status: str,
    *,
    push_status: str = "not_pushed",
    pushed_account_id=None,
):
    return {
        "category_suggestions": [
            {
                "transaction_index": 0,
                "qbo_transaction_id": "101",
                "qbo_transaction_type": "purchase",
                "category_suggestion": "Office Supplies",
                "category_match": {
                    "id": "55",
                    "name": "Office Supplies",
                },
                "review_status": review_status,
                "reviewed_category": (
                    None
                    if review_status == "rejected"
                    else {
                        "id": "55",
                        "name": "Office Supplies",
                    }
                ),
                "qbo_category_push_status": push_status,
                "qbo_category_account_id": pushed_account_id,
                "qbo_category_push_result": None,
                "qbo_category_push_error": None,
            }
        ]
    }


@pytest.mark.parametrize(
    "review_status",
    [
        "approved",
        "manual_override",
    ],
)
def test_reviewed_category_is_written_to_qbo(review_status):
    document = build_category_document(
        review_status
    )

    client = FakeQBOClient()

    result = _push_bank_statement_categories(
        document=document,
        client=client,
    )

    assert client.update_purchase_category_calls == [
        {
            "purchase_id": "101",
            "account_id": "55",
        }
    ]

    category = document["category_suggestions"][0]

    assert category["qbo_category_push_status"] == "pushed"
    assert category["qbo_category_account_id"] == "55"
    assert category["qbo_category_push_error"] is None

    assert category["qbo_category_push_result"] == {
        "id": "101",
        "type": "purchase",
        "account_id": "55",
    }

    assert result["updated_count"] == 1
    assert result["skipped_count"] == 0
    assert result["errors"] == []

    assert result["updates"][0]["status"] == "updated"
    assert result["updates"][0]["transaction_index"] == 0
    assert result["updates"][0]["qbo_transaction_id"] == "101"
    assert result["updates"][0]["qbo_account_id"] == "55"


def test_rejected_category_is_skipped_without_qbo_write():
    document = build_category_document(
        "rejected"
    )

    client = FakeQBOClient()

    result = _push_bank_statement_categories(
        document=document,
        client=client,
    )

    assert client.update_purchase_category_calls == []

    assert result["updated_count"] == 0
    assert result["skipped_count"] == 1
    assert result["errors"] == []

    assert result["updates"] == [
        {
            "transaction_index": 0,
            "qbo_transaction_id": "101",
            "qbo_account_id": None,
            "status": "skipped_rejected",
        }
    ]


def test_already_pushed_category_is_idempotently_skipped():
    document = build_category_document(
        "approved",
        push_status="pushed",
        pushed_account_id="55",
    )

    client = FakeQBOClient()

    result = _push_bank_statement_categories(
        document=document,
        client=client,
    )

    assert client.update_purchase_category_calls == []

    category = document["category_suggestions"][0]

    assert category["qbo_category_push_status"] == "pushed"
    assert category["qbo_category_account_id"] == "55"

    assert result["updated_count"] == 0
    assert result["skipped_count"] == 1
    assert result["errors"] == []

    assert result["updates"] == [
        {
            "transaction_index": 0,
            "qbo_transaction_id": "101",
            "qbo_account_id": "55",
            "status": "skipped",
            "reason": (
                "Category has already "
                "been pushed to QBO"
            ),
        }
    ]