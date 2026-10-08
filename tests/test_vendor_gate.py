from fastapi.testclient import TestClient

from tests.persistence_helpers import documents_db
from app.main import app
from app.schemas.documents import DocumentType, ProcessingStatus

client = TestClient(app)


def seed_bank_statement_with_vendor_status(
    document_id: str,
    vendor_status: str,
):
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
                    "description": "Books by Bessie",
                    "transaction_type": "debit",
                }
            ]
        },
        "qbo_match_results": [
            {
                "transaction_index": 0,
                "matched": True,
                "review_status": "approved",
                "reviewed_match": {
                    "score": 90,
                    "transaction": {
                        "id": "101",
                        "type": "purchase",
                    },
                },
            }
        ],
        "check_match_results": [
            {
                "statement_transaction_index": 0,
                "vendor_name": "Books by Bessie",
                "vendor_match": None,
                "vendor_status": vendor_status,
            }
        ],
    }

    return document_id


def test_push_blocked_when_vendor_is_missing(
    monkeypatch,
):
    document_id = seed_bank_statement_with_vendor_status(
        "doc_test_vendor_missing",
        "missing",
    )

    monkeypatch.setattr(
        "app.api.qbo._get_connected_qbo_client",
        lambda: object(),
    )

    response = client.post(
        f"/api/documents/{document_id}/push-to-qbo",
        json={
            "attach_documents": True,
        },
    )

    assert response.status_code == 400

    detail = response.json()["detail"]

    assert detail["message"] == (
        "Vendor resolution must be completed before push"
    )

    assert detail["unresolved_vendors"] == [
        {
            "statement_transaction_index": 0,
            "vendor_name": "Books by Bessie",
            "vendor_status": "missing",
        }
    ]


def test_push_blocked_when_vendor_not_checked(
    monkeypatch,
):
    document_id = seed_bank_statement_with_vendor_status(
        "doc_test_vendor_not_checked",
        "not_checked",
    )

    monkeypatch.setattr(
        "app.api.qbo._get_connected_qbo_client",
        lambda: object(),
    )

    response = client.post(
        f"/api/documents/{document_id}/push-to-qbo",
        json={
            "attach_documents": True,
        },
    )

    assert response.status_code == 400

    detail = response.json()["detail"]

    assert detail["message"] == (
        "Vendor resolution must be completed before push"
    )

    assert detail["unresolved_vendors"] == [
        {
            "statement_transaction_index": 0,
            "vendor_name": "Books by Bessie",
            "vendor_status": "not_checked",
        }
    ]