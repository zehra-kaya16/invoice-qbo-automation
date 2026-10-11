import pytest

from app.schemas.documents import DocumentType, ReceiptData
from app.core.config import settings
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import client, restart_request


class ReviewExtractor:
    document_type = DocumentType.INVOICE
    fail = False

    def __init__(self, **kwargs):
        pass

    def classify_document(self, image):
        if self.fail:
            raise ValueError("First extraction failed")
        return self.document_type

    def extract_receipt(self, image):
        return ReceiptData(vendor="Books by Bessie", date="2026-09-15", invoice_number="INV-96",
            currency="USD", subtotal="550.00", total_amount="550.00", confidence=0.92,
            raw_text="Source text retained", line_items=[
                {"description": "Service", "quantity": 1, "unit_price": "550.00", "amount": "550.00", "category": "Software"}])


@pytest.fixture
def review_extractor(monkeypatch):
    ReviewExtractor.fail = False
    ReviewExtractor.document_type = DocumentType.INVOICE
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr("app.services.extraction.extractor.DocumentExtractor", ReviewExtractor)
    return ReviewExtractor


def upload():
    response = client.post("/api/documents/upload?auto_process=false",
        files={"file": ("review.png", b"test-image", "image/png")})
    assert response.status_code == 200
    return response.json()["id"]


def test_successful_retry_removes_old_extraction_error(review_extractor):
    document_id = upload()
    path = f"/api/documents/{document_id}"
    review_extractor.fail = True
    assert client.post(path + "/extract").json()["status"] == "failed"
    assert client.get(path).json()["error"] == "First extraction failed"
    review_extractor.fail = False
    response = client.post(path + "/extract")
    assert response.status_code == 200 and response.json()["status"] == "extracted"
    assert client.get(path).json()["error"] is None
    assert response.json()["validation"]["is_valid"] is True
    assert response.json()["approved"] is False


@pytest.mark.parametrize("document_type", [DocumentType.INVOICE, DocumentType.RECEIPT])
def test_sparse_ui_edit_preserves_lines_and_review_survives_restart(review_extractor, isolated_document_database, document_type):
    review_extractor.document_type = document_type
    document_id = upload()
    path = f"/api/documents/{document_id}"
    original = client.post(path + "/extract").json()["receipt_data"]
    assert client.post(path + "/approve").json()["approved"] is True
    edited = client.patch(path + "/extracted", json={"vendor": "Reviewed vendor"})
    assert edited.status_code == 200
    assert edited.json()["approved"] is False
    receipt = edited.json()["receipt_data"]
    for key in ("line_items", "raw_text", "confidence", "currency", "invoice_number", "total_amount"):
        assert receipt[key] == original[key]
    assert client.post(path + "/approve").json()["approved"] is True
    restored = restart_request(isolated_document_database, "GET", path + "/extracted")
    assert restored["body"]["approved"] is True
    assert restored["body"]["receipt_data"]["vendor"] == "Reviewed vendor"


def test_retry_after_missing_key_can_succeed(review_extractor, monkeypatch):
    document_id = upload()
    path = f"/api/documents/{document_id}"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.post(path + "/extract").json()["status"] == "failed"
    assert client.get(path).json()["error"] == "No AI API key configured"
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    assert client.post(path + "/extract").json()["status"] == "extracted"
    assert DocumentRepository().get(document_id)["error"] is None


def test_extraction_uses_key_loaded_from_dotenv_settings(review_extractor, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "test-settings-key")
    document_id = upload()
    response = client.post(f"/api/documents/{document_id}/extract")
    assert response.json()["status"] == "extracted"
