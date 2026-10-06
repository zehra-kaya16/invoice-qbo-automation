from fastapi.testclient import TestClient

from app.api.documents import documents_db
from app.main import app
from app.schemas.documents import DocumentType, ProcessingStatus

client = TestClient(app)


def seed_category_review_document(document_id: str):
    category_match = {
        "id": "55",
        "name": "Office Supplies",
        "fully_qualified_name": "Office Supplies",
    }

    documents_db[document_id] = {
        "id": document_id,
        "filename": "test_bank_statement.pdf",
        "status": ProcessingStatus.MATCHED,
        "document_type": DocumentType.BANK_STATEMENT,
        "extracted_data": {
            "transactions": [
                {
                    "date": "2026-09-15",
                    "amount": -550.00,
                    "description": "Test purchase",
                    "transaction_type": "debit",
                }
            ]
        },
        "category_suggestions": [
            {
                "transaction_index": 0,
                "description": "Test purchase",
                "vendor_suggestion": "Test Vendor",
                "qbo_transaction_id": "101",
                "qbo_transaction_type": "purchase",
                "category_suggestion": "Office Supplies",
                "category_match": category_match,
                "review_status": "pending",
                "reviewed_category": None,
                "qbo_category_push_status": "not_pushed",
                "qbo_category_account_id": None,
                "qbo_category_push_result": None,
                "qbo_category_push_error": None,
            }
        ],
    }

    return category_match

def test_category_review_approve_uses_suggested_category():
    document_id = "doc_test_category_approve"

    category_match = seed_category_review_document(document_id)

    response = client.patch(
        f"/api/documents/{document_id}/category-review/0",
        json={
            "decision": "approve",
        },
    )

    assert response.status_code == 200

    category = documents_db[document_id]["category_suggestions"][0]

    assert category["review_status"] == "approved"
    assert category["reviewed_category"] == category_match

def test_category_review_reject_clears_reviewed_category():
    document_id = "doc_test_category_reject"

    category_match = seed_category_review_document(document_id)

    response = client.patch(
        f"/api/documents/{document_id}/category-review/0",
        json={
            "decision": "reject",
        },
    )

    assert response.status_code == 200

    category = documents_db[document_id]["category_suggestions"][0]

    assert category["review_status"] == "rejected"
    assert category["reviewed_category"] is None
    assert category["category_match"] == category_match

def test_category_review_manual_override_requires_account_id():
    document_id = "doc_test_category_manual_override_missing_account"

    seed_category_review_document(document_id)

    response = client.patch(
        f"/api/documents/{document_id}/category-review/0",
        json={
            "decision": "manual_override",
        },
    )

    assert response.status_code == 400

    category = documents_db[document_id]["category_suggestions"][0]

    assert category["review_status"] == "pending"
    assert category["reviewed_category"] is None