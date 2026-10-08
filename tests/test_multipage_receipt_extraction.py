import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from app.schemas.documents import DocumentType
from app.services.extraction.extractor import DocumentExtractor
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import client, restart_request
from tests.test_receipt_workflow import ReceiptQBO, request_body


DATA = {
    "vendor": "Books by Bessie", "date": "2026-09-15", "currency": "USD",
    "subtotal": "550.00", "total_amount": "550.00",
    "line_items": [
        {"description": "Page 1 item", "quantity": 1, "unit_price": "200", "amount": "200"},
        {"description": "Page 2 item", "quantity": 1, "unit_price": "350", "amount": "350"},
    ],
}


def pdf_bytes():
    images = [Image.new("RGB", (100, 100), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def fake_provider(provider, text, truncated=False):
    captured = []
    extractor = DocumentExtractor.__new__(DocumentExtractor)
    extractor.provider = provider
    extractor.model = "test-model"

    def create(**kwargs):
        captured.append(kwargs)
        if provider == "openai":
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="length" if truncated else "stop",
                message=SimpleNamespace(content=text),
            )])
        return SimpleNamespace(
            stop_reason="max_tokens" if truncated else "end_turn",
            content=[SimpleNamespace(text=text)],
        )

    if provider == "openai":
        extractor.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    else:
        extractor.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return extractor, captured


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_all_pages_sent_in_order_as_one_document(provider):
    extractor, calls = fake_provider(provider, json.dumps(DATA))
    receipt = extractor.extract_receipt([b"first-page", b"second-page"])
    assert len(calls) == 1
    content = calls[0]["messages"][0]["content"]
    if provider == "openai":
        encoded = [item["image_url"]["url"].split(",", 1)[1] for item in content if item["type"] == "image_url"]
    else:
        encoded = [item["source"]["data"] for item in content if item["type"] == "image"]
    assert [base64.b64decode(page) for page in encoded] == [b"first-page", b"second-page"]
    prompt = content[0]["text"]
    assert "final document grand total" in prompt
    assert "Do not sum repeated totals" in prompt
    assert [item.description for item in receipt.line_items] == ["Page 1 item", "Page 2 item"]
    assert str(receipt.total_amount) == "550.00"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_truncated_response_is_not_accepted(provider):
    extractor, _ = fake_provider(provider, json.dumps(DATA), truncated=True)
    with pytest.raises(ValueError, match="truncated"):
        extractor.extract_receipt([b"one", b"two"])


@pytest.mark.parametrize("response", ["not JSON", "{}", "[]", '{"error":"Multiple independent documents must be uploaded separately"}'])
def test_invalid_or_bundled_document_response_rejected(response):
    extractor, _ = fake_provider("openai", response)
    with pytest.raises(ValueError):
        extractor.extract_receipt([b"one", b"two"])


@pytest.mark.parametrize("pages", [[], [b""], [b"one", "two"]])
def test_invalid_page_input_never_calls_provider(pages):
    extractor, calls = fake_provider("openai", json.dumps(DATA))
    with pytest.raises(ValueError):
        extractor.extract_receipt(pages)
    assert calls == []


@pytest.mark.parametrize("document_type", [DocumentType.INVOICE, DocumentType.RECEIPT])
def test_real_two_page_pdf_extracts_approves_and_pushes(monkeypatch, isolated_document_database, document_type):
    supplied_pages = []

    class Extractor(DocumentExtractor):
        def __init__(self, **kwargs):
            pass

        def classify_document(self, image):
            return document_type

        def _vision_request_pages(self, pages, prompt):
            assert len(pages) == 2
            supplied_pages.extend(pages)
            return json.dumps(DATA)

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("app.services.extraction.extractor.DocumentExtractor", Extractor)
    source = pdf_bytes()
    upload = client.post("/api/documents/upload", params={"auto_process": False}, files={"file": ("two_pages.pdf", source, "application/pdf")})
    document_id = upload.json()["id"]
    prefix = f"/api/documents/{document_id}"
    extraction = client.post(prefix + "/extract")
    assert extraction.status_code == 200, extraction.text
    assert extraction.json()["status"] == "extracted"
    assert extraction.json()["validation"]["is_valid"] is True
    assert len(extraction.json()["receipt_data"]["line_items"]) == 2
    assert supplied_pages[0] != supplied_pages[1]
    assert client.post(prefix + "/approve").status_code == 200
    fake = ReceiptQBO(document_id)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    pushed = client.post(prefix + "/push-to-qbo", json=request_body())
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["transactions_pushed"] == 1
    assert pushed.json()["attachments_uploaded"] == 1
    assert DocumentRepository().get(document_id)["content"] == source
    restarted = restart_request(isolated_document_database, "GET", prefix)
    assert restarted["body"]["status"] == "pushed"
    assert client.post(prefix + "/push-to-qbo", json=request_body()).status_code == 409
    assert fake.writes == 1


def test_multipage_extraction_failure_persisted_without_approval(monkeypatch):
    class Extractor(DocumentExtractor):
        def __init__(self, **kwargs):
            pass

        def classify_document(self, image):
            return DocumentType.INVOICE

        def _vision_request_pages(self, pages, prompt):
            raise ValueError("Extraction response was truncated")

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("app.services.extraction.extractor.DocumentExtractor", Extractor)
    upload = client.post("/api/documents/upload", params={"auto_process": False}, files={"file": ("two_pages.pdf", pdf_bytes(), "application/pdf")})
    document_id = upload.json()["id"]
    client.post(f"/api/documents/{document_id}/extract")
    document = DocumentRepository().get(document_id)
    assert document["status"] == "failed"
    assert "truncated" in document["error"]
    assert document["approved"] is False
    assert client.post(f"/api/documents/{document_id}/approve").status_code == 400
