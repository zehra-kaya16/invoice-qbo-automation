from fastapi.testclient import TestClient

from app.api.documents import documents_db
from app.main import app
from app.schemas.documents import DocumentType, ProcessingStatus

client = TestClient(app)

def seed_pushed_bank_statement():
    document_id = "doc_test_pushed"

    documents_db[document_id] = {
        "id": document_id,
        "filename": "test_bank_statement.pdf",
        "status": ProcessingStatus.PUSHED,
        "document_type": DocumentType.BANK_STATEMENT,
        "extracted_data": {
            "transactions": [
                {
                    "date": "2026-09-15",
                    "description": "Test purchase",
                    "amount": "-550.00",
                    "transaction_type": "debit",
                    "check_number": None,
                    "running_balance": None,
                    "vendor_suggestion": "Books by Bessie",
                    "category_suggestion": "Dues & Subscriptions",
                    "check_image": None,
                }
            ]
        },
        "qbo_match_results": [
            {
                "transaction_index": 0,
                "review_status": "approved",
            }
        ],
        "category_suggestions": [
            {
                "transaction_index": 0,
                "qbo_transaction_type": "purchase",
                "review_status": "approved",
                "reviewed_category": {
                    "id": "1",
                    "name": "Test Expense",
                    "fully_qualified_name": "Test Expense",
                },
                "qbo_category_push_status": "pushed",
            }
        ],
    }

    return document_id

def test_match_review_blocked_after_document_pushed():
    document_id = seed_pushed_bank_statement()

    response = client.patch(
        f"/api/documents/{document_id}/match-review/0",
        json={
            "decision": "reject"
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Match review cannot be changed "
        "after the document has been pushed to QBO"
    )

def test_category_review_blocked_after_category_pushed():
    document_id = seed_pushed_bank_statement()

    response = client.patch(
        f"/api/documents/{document_id}/category-review/0",
        json={
            "decision": "reject"
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Category review cannot be changed "
        "after it has been pushed to QBO"
    )

def test_match_to_qbo_blocked_after_document_pushed():
    document_id = seed_pushed_bank_statement()

    response = client.post(
        f"/api/documents/{document_id}/match-to-qbo",
        json={
            "bank_account_id": "36"
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Document already pushed to QBO; "
        "rematching is not allowed."
    )

def test_extraction_blocked_after_document_pushed(monkeypatch):
    from copy import deepcopy
    from app.api import documents as documents_api

    document_id = seed_pushed_bank_statement()
    before = deepcopy(documents_db[document_id])

    async def unexpected_processing(document_id):
        raise AssertionError("Extraction must not start")

    monkeypatch.setattr(
        documents_api,
        "process_document",
        unexpected_processing,
    )

    response = client.post(
        f"/api/documents/{document_id}/extract"
    )

    assert response.status_code == 409
    assert documents_db[document_id] == before


def test_extraction_blocked_for_protected_qbo_push_states(monkeypatch):
    from copy import deepcopy
    from app.api import documents as documents_api

    async def unexpected_processing(document_id):
        raise AssertionError("Extraction must not start")

    monkeypatch.setattr(
        documents_api,
        "process_document",
        unexpected_processing,
    )

    for push_state in ("in_progress", "succeeded", "uncertain"):
        document_id = seed_pushed_bank_statement()
        document = documents_db[document_id]
        document["status"] = ProcessingStatus.EXTRACTED
        document["qbo_push_state"] = push_state

        before = deepcopy(document)

        response = client.post(
            f"/api/documents/{document_id}/extract"
        )

        assert response.status_code == 409, push_state
        assert documents_db[document_id] == before, push_state


def test_direct_processing_blocked_after_document_pushed():
    import asyncio
    from copy import deepcopy

    import pytest
    from fastapi import HTTPException
    from app.api.documents import process_document

    document_id = seed_pushed_bank_statement()
    before = deepcopy(documents_db[document_id])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(process_document(document_id))

    assert exc.value.status_code == 409
    assert documents_db[document_id] == before