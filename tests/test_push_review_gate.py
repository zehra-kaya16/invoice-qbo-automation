from fastapi.testclient import TestClient

from app.main import app
from app.api.documents import documents_db
from app.schemas.documents import DocumentType, ProcessingStatus


client = TestClient(app)


def seed_bank_statement_with_pending_review():
    document_id = "doc_test_pending_review"

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
                    "description": "Test transaction",
                    "transaction_type": "debit",
                }
            ]
        },
        "qbo_match_results": [
            {
                "transaction_index": 0,
                "matched": True,
                "review_status": "pending",
                "reviewed_match": None,
                "best_match": {
                    "score": 80,
                    "transaction": {
                        "id": "145",
                        "type": "purchase",
                    },
                },
            }
        ],
    }

    return document_id


def test_push_to_qbo_blocked_when_match_review_is_pending(
    monkeypatch,
):
    document_id = seed_bank_statement_with_pending_review()

    # push_to_qbo() review gate'e ulaşmadan önce
    # QBO client'ı alıyor. Testi gerçek QBO bağlantısından
    # bağımsız tutuyoruz.
    monkeypatch.setattr(
        "app.api.qbo._get_connected_qbo_client",
        lambda: object(),
    )

    response = client.post(
        f"/api/documents/{document_id}/push-to-qbo",
        json={},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "message": (
            "All bank statement matches "
            "must be reviewed before push"
        ),
        "pending_transaction_indexes": [0],
    }