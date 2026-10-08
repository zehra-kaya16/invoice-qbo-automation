from fastapi.testclient import TestClient

from tests.persistence_helpers import documents_db
from app.main import app
from app.schemas.documents import DocumentType, ProcessingStatus

client = TestClient(app)


def seed_matched_bank_statement(document_id: str):
    best_match = {
        "score": 90,
        "transaction": {
            "id": "101",
            "type": "purchase",
        },
    }

    alternate_match = {
        "score": 80,
        "transaction": {
            "id": "202",
            "type": "purchase",
        },
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
                    "description": "Test transaction",
                    "transaction_type": "debit",
                }
            ]
        },
        "qbo_match_results": [
            {
                "transaction_index": 0,
                "matched": True,
                "best_match": best_match,
                "candidates": [
                    best_match,
                    alternate_match,
                ],
                "review_status": "pending",
                "reviewed_match": None,
            }
        ],
    }

    return best_match, alternate_match


def test_match_review_approve_uses_best_match():
    document_id = "doc_test_match_approve"

    best_match, _ = seed_matched_bank_statement(
        document_id
    )

    response = client.patch(
        f"/api/documents/{document_id}/match-review/0",
        json={
            "decision": "approve",
        },
    )

    assert response.status_code == 200

    match_result = (
        documents_db[document_id]["qbo_match_results"][0]
    )

    assert match_result["review_status"] == "approved"
    assert match_result["reviewed_match"] == best_match


def test_match_review_reject_clears_reviewed_match():
    document_id = "doc_test_match_reject"

    best_match, _ = seed_matched_bank_statement(
        document_id
    )

    response = client.patch(
        f"/api/documents/{document_id}/match-review/0",
        json={
            "decision": "reject",
        },
    )

    assert response.status_code == 200

    match_result = (
        documents_db[document_id]["qbo_match_results"][0]
    )

    assert match_result["review_status"] == "rejected"
    assert match_result["reviewed_match"] is None
    assert match_result["best_match"] == best_match


def test_match_review_manual_override_uses_selected_candidate():
    document_id = "doc_test_match_manual_override"

    best_match, alternate_match = (
        seed_matched_bank_statement(document_id)
    )

    response = client.patch(
        f"/api/documents/{document_id}/match-review/0",
        json={
            "decision": "manual_override",
            "qbo_transaction_id": "202",
            "qbo_transaction_type": "purchase",
        },
    )

    assert response.status_code == 200

    match_result = (
        documents_db[document_id]["qbo_match_results"][0]
    )

    assert (
        match_result["review_status"]
        == "manual_override"
    )
    assert (
        match_result["reviewed_match"]
        == alternate_match
    )
    assert match_result["best_match"] == best_match