from pathlib import Path

import pytest

from app.schemas.documents import DocumentType, ReceiptData, ProcessingStatus
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import FakeQBO, client, restart_request, seed_invoice


class ReceiptQBO(FakeQBO):
    def find_vendor_by_name(self, name):
        return self.get_vendors()[0]

    def get_bank_accounts(self):
        return [{"id": "1", "name": "Checking"}]

    def create_expense(self, **kwargs):
        assert DocumentRepository().get(self.document_id)["qbo_push_state"] == "in_progress"
        assert kwargs["bank_account_id"] == "1"
        self.writes += 1
        if self.fail:
            raise TimeoutError("provider timeout")
        return {"id": "147", "type": "expense"}

    def upload_attachment(self, **kwargs):
        persisted = DocumentRepository().get(self.document_id)
        assert persisted["qbo_push_state"] == "succeeded"
        assert persisted["qbo_attachment_push_status"] == "uploading"
        assert kwargs["entity_id"] == persisted["qbo_transaction_id"]
        assert Path(kwargs["file_path"]).read_bytes() == persisted["content"]
        self.attachments += 1
        if self.fail_attachment:
            raise TimeoutError("attachment timeout")
        return {"id": "receipt_attachment"}


def seed_receipt(monkeypatch, **options):
    document = seed_invoice()
    document["document_type"] = DocumentType.RECEIPT
    document["approved"] = False
    DocumentRepository().save(document)
    fake = ReceiptQBO(document["id"], **options)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    return document["id"], fake


def request_body(create_as="expense", attach=True):
    return {"create_as": create_as, "vendor_id": "30", "account_id": "10", "bank_account_id": "1", "attach_documents": attach}


def test_receipt_extraction_edit_and_approval(monkeypatch):
    class Extractor:
        def __init__(self, **kwargs):
            pass

        def classify_document(self, image):
            return DocumentType.RECEIPT

        def extract_receipt(self, image):
            return ReceiptData(vendor="Books by Bessie", date="2026-09-15", subtotal="500", tax_amount="50", total_amount="550", currency="USD")

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr("app.services.extraction.extractor.DocumentExtractor", Extractor)
    upload = client.post("/api/documents/upload", params={"auto_process": False}, files={"file": ("receipt.png", b"image", "image/png")})
    prefix = f"/api/documents/{upload.json()['id']}"
    response = client.post(prefix + "/extract")
    assert response.status_code == 200, response.text
    assert response.json()["document_type"] == "receipt"
    assert response.json()["validation"]["is_valid"] is True
    assert client.post(prefix + "/approve").status_code == 200
    assert client.patch(prefix + "/extracted", json={"total_amount": "600"}).status_code == 200
    document = DocumentRepository().get(upload.json()["id"])
    assert document["approved"] is False
    assert document["validation"]["is_valid"] is False
    assert client.post(prefix + "/approve").status_code == 400
    assert client.patch(prefix + "/extracted", json={"total_amount": "550"}).status_code == 200
    assert client.post(prefix + "/approve").status_code == 200


@pytest.mark.parametrize("create_as", ["expense", "bill"])
def test_receipt_options_plan_push_attachment_and_restart(monkeypatch, isolated_document_database, create_as):
    document_id, fake = seed_receipt(monkeypatch)
    prefix = f"/api/documents/{document_id}"
    assert client.post(prefix + "/approve").status_code == 200
    assert client.get(f"/api/qbo/invoice-options/{document_id}").status_code == 200
    plan = client.post(f"/api/qbo/invoice-plan/{document_id}", json=request_body(create_as))
    assert plan.status_code == 200, plan.text
    assert plan.json()["plan"]["method"] == "create_" + create_as
    assert fake.writes == 0
    response = client.post(prefix + "/push-to-qbo", json=request_body(create_as))
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert response.json()["transactions_pushed"] == 1
    assert response.json()["attachments_uploaded"] == 1
    assert fake.writes == 1
    assert fake.attachments == 1
    state = DocumentRepository().get(document_id)
    assert state["document_type"] == DocumentType.RECEIPT
    assert state["status"] == ProcessingStatus.PUSHED
    assert state["qbo_push_state"] == "succeeded"
    restarted = restart_request(isolated_document_database, "GET", prefix)
    assert restarted["body"]["qbo_push_state"] == "succeeded"
    restarted_retry = restart_request(isolated_document_database, "POST", prefix + "/push-to-qbo", request_body(create_as))
    assert restarted_retry["status"] == 409
    assert client.post(prefix + "/push-to-qbo", json=request_body(create_as)).status_code == 409
    assert client.post(prefix + "/extract").status_code == 409
    assert client.patch(prefix + "/extracted", json={"total_amount": "600"}).status_code in (400, 409)
    assert fake.writes == 1


@pytest.mark.parametrize("failure", ["transaction", "attachment"])
def test_receipt_provider_failure_does_not_duplicate(monkeypatch, failure):
    document_id, fake = seed_receipt(monkeypatch, fail=failure == "transaction", fail_attachment=failure == "attachment")
    prefix = f"/api/documents/{document_id}"
    assert client.post(prefix + "/approve").status_code == 200
    response = client.post(prefix + "/push-to-qbo", json=request_body())
    assert response.status_code == (502 if failure == "transaction" else 200)
    state = DocumentRepository().get(document_id)
    assert state["qbo_push_state"] == ("uncertain" if failure == "transaction" else "succeeded")
    if failure == "attachment":
        assert state["qbo_attachment_push_status"] == "uncertain"
        assert response.json()["errors"]
    assert client.post(prefix + "/push-to-qbo", json=request_body()).status_code == 409
    assert fake.writes == 1


@pytest.mark.parametrize("endpoint", ["options", "plan", "push"])
def test_receipt_requires_approval(monkeypatch, endpoint):
    document_id, fake = seed_receipt(monkeypatch)
    if endpoint == "options":
        response = client.get(f"/api/qbo/invoice-options/{document_id}")
    elif endpoint == "plan":
        response = client.post(f"/api/qbo/invoice-plan/{document_id}", json=request_body())
    else:
        response = client.post(f"/api/documents/{document_id}/push-to-qbo", json=request_body())
    assert response.status_code == (409 if endpoint == "plan" else 400)
    assert fake.writes == 0


@pytest.mark.parametrize("field,value", [("vendor_id", "bad"), ("account_id", "bad"), ("bank_account_id", "bad"), ("create_as", "bad")])
def test_receipt_invalid_mapping_never_writes(monkeypatch, field, value):
    document_id, fake = seed_receipt(monkeypatch)
    assert client.post(f"/api/documents/{document_id}/approve").status_code == 200
    body = request_body()
    body[field] = value
    assert client.post(f"/api/qbo/invoice-plan/{document_id}", json=body).status_code == 400
    assert client.post(f"/api/documents/{document_id}/push-to-qbo", json=body).status_code == 400
    assert fake.writes == 0
