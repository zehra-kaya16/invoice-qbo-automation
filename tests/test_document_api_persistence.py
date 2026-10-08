import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import documents as documents_api
from app.main import app
from app.schemas.documents import DocumentType, ProcessingStatus, ReceiptData
from app.services.persistence.repository import DocumentRepository


client = TestClient(app)


def seed_invoice():
    document = {
        "id": "doc_api_persistence",
        "company_id": "default",
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "content": b"%PDF-1.7\x00\xffbinary",
        "document_type": DocumentType.INVOICE,
        "status": ProcessingStatus.EXTRACTED,
        "extracted_data": {
            "vendor": "Books by Bessie",
            "date": "2026-09-15",
            "total_amount": "550.00",
            "currency": "USD",
        },
        "validation": {"is_valid": True},
        "approved": True,
        "qbo_push_state": "not_started",
    }
    DocumentRepository().save(document)
    return document


def restart_request(factory, method, path, payload=None):
    """Exercise the API in another OS process against the same test DB."""
    env = os.environ.copy()
    env["DATABASE_URL"] = str(factory.kw["bind"].url)
    program = """
import json, sys
from fastapi.testclient import TestClient
from app.main import app
request = json.loads(sys.stdin.read())
response = TestClient(app).request(**request)
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
    request = {"method": method, "url": path}
    if payload is not None:
        request["json"] = payload
    result = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


class FakeQBO:
    def __init__(self, document_id, fail=False, fail_attachment=False):
        self.document_id = document_id
        self.fail = fail
        self.fail_attachment = fail_attachment
        self.writes = 0
        self.attachments = 0

    def get_vendors(self):
        return [{"id": "30", "name": "Books by Bessie"}]

    def get_expense_accounts(self):
        return [{"id": "10", "name": "Dues & Subscriptions", "type": "Expense"}]

    def create_bill(self, **kwargs):
        persisted = DocumentRepository().get(self.document_id)
        assert persisted["qbo_push_state"] == "in_progress"
        self.writes += 1
        if self.fail:
            raise TimeoutError("provider timeout")
        return {"id": "146", "type": "bill"}

    def upload_attachment(self, **kwargs):
        persisted = DocumentRepository().get(self.document_id)
        assert persisted["qbo_push_state"] == "succeeded"
        assert persisted["qbo_transaction_id"] == "146"
        assert persisted["qbo_attachment_push_status"] == "uploading"
        assert Path(kwargs["file_path"]).read_bytes() == persisted["content"]
        self.attachments += 1
        if self.fail_attachment:
            raise TimeoutError("attachment timeout")
        return {"id": "attachment_1"}


def push_invoice(document_id, attach=False):
    return client.post(
        f"/api/documents/{document_id}/push-to-qbo",
        json={
            "create_as": "bill",
            "vendor_id": "30",
            "account_id": "10",
            "attach_documents": attach,
        },
    )


def test_uploaded_document_is_readable_in_another_process(isolated_document_database):
    original = b"%PDF-1.7\x00\xffbinary"
    response = client.post(
        "/api/documents/upload",
        params={"auto_process": False},
        files={"file": ("source.pdf", original, "application/pdf")},
    )
    assert response.status_code == 200
    document_id = response.json()["id"]
    assert DocumentRepository().get(document_id)["content"] == original
    restarted = restart_request(
        isolated_document_database, "GET", f"/api/documents/{document_id}"
    )
    assert restarted["status"] == 200
    assert restarted["body"]["filename"] == "source.pdf"
    assert restarted["body"]["status"] == "uploaded"


def test_extraction_and_edit_and_approval_are_persisted(monkeypatch):
    response = client.post(
        "/api/documents/upload",
        params={"auto_process": False},
        files={"file": ("invoice.png", b"fake-image", "image/png")},
    )
    document_id = response.json()["id"]

    class FakeExtractor:
        def __init__(self, **kwargs):
            pass

        def classify_document(self, image):
            return DocumentType.INVOICE

        def extract_receipt(self, image):
            return ReceiptData(
                vendor="Books by Bessie",
                date="2026-09-15",
                total_amount="550.00",
                currency="USD",
            )

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(
        "app.services.extraction.extractor.DocumentExtractor", FakeExtractor
    )
    extracted = client.post(f"/api/documents/{document_id}/extract")
    assert extracted.status_code == 200
    assert DocumentRepository().get(document_id)["status"] == "extracted"
    edited = client.patch(
        f"/api/documents/{document_id}/extracted", json={"total_amount": "600.00"}
    )
    assert edited.status_code == 200
    assert DocumentRepository().get(document_id)["extracted_data"]["total_amount"] == "600.00"
    approved = client.post(f"/api/documents/{document_id}/approve")
    assert approved.status_code == 200
    assert DocumentRepository().get(document_id)["approved"] is True


def test_qbo_options_and_plan_read_persisted_documents(monkeypatch):
    document = seed_invoice()
    fake = FakeQBO(document["id"])
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    # Options use find_vendor_by_name as well as the expense account list.
    fake.find_vendor_by_name = lambda name: {"id": "30", "name": "Books by Bessie"}
    response = client.get(f"/api/qbo/invoice-options/{document['id']}")
    assert response.status_code == 200
    response = client.post(
        f"/api/qbo/invoice-plan/{document['id']}",
        json={"create_as": "bill", "vendor_id": "30", "account_id": "10"},
    )
    assert response.status_code == 200
    assert response.json()["plan"]["method"] == "create_bill"


@pytest.mark.parametrize("fail", [False, True])
def test_invoice_push_checkpoint_survives_restart(
    monkeypatch, isolated_document_database, fail
):
    document = seed_invoice()
    fake = FakeQBO(document["id"], fail=fail)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    response = push_invoice(document["id"])
    assert response.status_code == (502 if fail else 200)
    state = DocumentRepository().get(document["id"])
    assert state["qbo_push_state"] == ("uncertain" if fail else "succeeded")
    if not fail:
        assert state["qbo_transaction_id"] == "146"
    restarted = restart_request(
        isolated_document_database,
        "POST",
        f"/api/documents/{document['id']}/push-to-qbo",
        {"create_as": "bill", "vendor_id": "30", "account_id": "10", "attach_documents": False},
    )
    # Guard runs before QBO client connection, including after process restart.
    assert restarted["status"] == 409
    assert fake.writes == 1


def test_database_failure_before_claim_prevents_qbo_write(monkeypatch):
    document = seed_invoice()
    fake = FakeQBO(document["id"])
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)

    def unavailable(document):
        raise OperationalError("save", {}, RuntimeError("database offline"))

    monkeypatch.setattr(documents_api.document_repository, "save", unavailable)
    response = push_invoice(document["id"])
    assert response.status_code == 503
    assert fake.writes == 0
    assert DocumentRepository().get(document["id"])["qbo_push_state"] == "not_started"


def test_interrupted_claim_blocks_retry_in_new_process(isolated_document_database):
    document = seed_invoice()
    document["qbo_push_state"] = "in_progress"
    DocumentRepository().save(document)
    restarted = restart_request(
        isolated_document_database,
        "POST",
        f"/api/documents/{document['id']}/push-to-qbo",
        {"create_as": "bill", "vendor_id": "30", "account_id": "10"},
    )
    assert restarted["status"] == 409
    assert "already in progress" in restarted["body"]["detail"]


def test_stale_claim_prevents_second_qbo_write(monkeypatch):
    document = seed_invoice()
    fake = FakeQBO(document["id"])
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    original_save = documents_api.document_repository.save

    def racing_save(snapshot):
        if snapshot.get("qbo_push_state") == "in_progress":
            winner = DocumentRepository().get(snapshot["id"])
            winner["qbo_push_state"] = "in_progress"
            DocumentRepository().save(winner)
        original_save(snapshot)

    monkeypatch.setattr(documents_api.document_repository, "save", racing_save)
    response = push_invoice(document["id"])
    assert response.status_code == 409
    assert fake.writes == 0
    assert DocumentRepository().get(document["id"])["qbo_push_state"] == "in_progress"


@pytest.mark.parametrize("fail_attachment", [False, True])
def test_attachment_result_does_not_lose_created_transaction(monkeypatch, fail_attachment):
    document = seed_invoice()
    fake = FakeQBO(document["id"], fail_attachment=fail_attachment)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    response = push_invoice(document["id"], attach=True)
    assert response.status_code == 200
    persisted = DocumentRepository().get(document["id"])
    assert persisted["qbo_push_state"] == "succeeded"
    assert persisted["qbo_transaction_id"] == "146"
    assert persisted["qbo_attachment_push_status"] == (
        "uncertain" if fail_attachment else "uploaded"
    )
    assert push_invoice(document["id"], attach=True).status_code == 409
    assert fake.writes == 1
    assert fake.attachments == 1


class FakeBankQBO:
    def __init__(self, document_id, fail_category=False, fail_check=False):
        self.document_id = document_id
        self.fail_category = fail_category
        self.fail_check = fail_check
        self.category_writes = 0
        self.check_uploads = 0

    def get_accounts(self, account_type=None):
        return [{"id": "1", "name": "Checking", "type": "Bank"}]

    def get_matching_transactions(self, **kwargs):
        return [{
            "id": "101", "type": "purchase", "amount": "-550.00",
            "date": "2026-09-15", "check_number": "200",
            "description": "Books by Bessie",
        }]

    def find_vendor_by_name(self, name):
        return {"id": "30", "name": "Books by Bessie"}

    def get_expense_accounts(self):
        return [{"id": "10", "name": "Dues & Subscriptions"}]

    def update_purchase_category(self, **kwargs):
        persisted = DocumentRepository().get(self.document_id)
        assert persisted["category_suggestions"][0]["qbo_category_push_status"] == "pushing"
        self.category_writes += 1
        if self.fail_category:
            raise TimeoutError("category timeout")
        return {"id": "101", "account_id": "10"}

    def upload_attachment(self, **kwargs):
        persisted = DocumentRepository().get(self.document_id)
        assert persisted["category_suggestions"][0]["qbo_category_push_status"] == "pushed"
        assert persisted["check_match_results"][0]["attachment_push_status"] == "uploading"
        self.check_uploads += 1
        if self.fail_check:
            raise TimeoutError("check upload timeout")
        return {"id": "check_attachment_1"}


def prepare_bank_workflow(tmp_path, monkeypatch, **fake_options):
    check_path = tmp_path / "check.png"
    check_path.write_bytes(b"check-image")
    document = {
        "id": "doc_bank_api_persistence",
        "filename": "bank.pdf",
        "company_id": "default",
        "content": b"bank source",
        "content_type": "application/pdf",
        "document_type": DocumentType.BANK_STATEMENT,
        "status": ProcessingStatus.EXTRACTED,
        "qbo_push_state": "not_started",
        "extracted_data": {
            "statement_period_start": "2026-09-01",
            "statement_period_end": "2026-09-30",
            "transactions": [{
                "date": "2026-09-15", "amount": "-550.00",
                "description": "Dues & Subscriptions",
                "transaction_type": "debit", "check_number": "200",
                "vendor_suggestion": "Books by Bessie",
            }],
            "check_images": [{
                "image_path": str(check_path), "check_number": "200",
                "payee": "Books by Bessie", "amount": "550.00",
                "date": "2026-09-15",
            }],
        },
    }
    repo = DocumentRepository()
    repo.save(document)
    fake = FakeBankQBO(document["id"], **fake_options)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    prefix = f"/api/documents/{document['id']}"

    response = client.post(prefix + "/match-to-qbo", json={"bank_account_id": "1"})
    assert response.status_code == 200
    assert repo.get(document["id"])["qbo_match_results"][0]["matched"] is True
    response = client.patch(prefix + "/match-review/0", json={"decision": "approve"})
    assert response.status_code == 200
    assert repo.get(document["id"])["qbo_match_results"][0]["review_status"] == "approved"
    response = client.post(prefix + "/resolve-vendors")
    assert response.status_code == 200
    assert repo.get(document["id"])["check_match_results"][0]["vendor_status"] == "matched"
    response = client.post(prefix + "/suggest-categories")
    assert response.status_code == 200
    assert repo.get(document["id"])["category_suggestions"][0]["review_status"] == "pending"
    response = client.patch(prefix + "/category-review/0", json={"decision": "approve"})
    assert response.status_code == 200
    assert repo.get(document["id"])["category_suggestions"][0]["review_status"] == "approved"
    return document["id"], fake


@pytest.mark.parametrize("failure", [None, "category", "check"])
def test_bank_workflow_and_write_checkpoints_are_persisted(
    tmp_path, monkeypatch, isolated_document_database, failure
):
    document_id, fake = prepare_bank_workflow(
        tmp_path, monkeypatch,
        fail_category=failure == "category", fail_check=failure == "check",
    )
    prefix = f"/api/documents/{document_id}"
    response = client.post(prefix + "/push-to-qbo", json={"attach_documents": True})
    assert response.status_code == (502 if failure == "category" else 200)
    persisted = DocumentRepository().get(document_id)
    category = persisted["category_suggestions"][0]
    assert category["qbo_category_push_status"] == (
        "uncertain" if failure == "category" else "pushed"
    )
    if failure != "category":
        check = persisted["check_match_results"][0]
        assert check["attachment_push_status"] == (
            "uncertain" if failure == "check" else "uploaded"
        )
    restarted = restart_request(
        isolated_document_database, "GET", prefix + "/category-review"
    )
    assert restarted["status"] == 200
    before = (fake.category_writes, fake.check_uploads)
    retry = client.post(prefix + "/push-to-qbo", json={"attach_documents": True})
    assert retry.status_code == (200 if failure is None else 409)
    assert (fake.category_writes, fake.check_uploads) == before
    # Partial/uncertain provider writes cannot be erased by rematching.
    assert client.post(prefix + "/match-to-qbo", json={"bank_account_id": "1"}).status_code == 409
    assert client.post(prefix + "/suggest-categories").status_code == 409


def test_bank_category_claim_failure_prevents_provider_write(tmp_path, monkeypatch):
    document_id, fake = prepare_bank_workflow(tmp_path, monkeypatch)
    original_save = documents_api.document_repository.save

    def unavailable_at_claim(document):
        if document["category_suggestions"][0]["qbo_category_push_status"] == "pushing":
            raise OperationalError("save", {}, RuntimeError("database offline"))
        original_save(document)

    monkeypatch.setattr(documents_api.document_repository, "save", unavailable_at_claim)
    response = client.post(
        f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": True}
    )
    assert response.status_code == 503
    assert fake.category_writes == 0
    assert fake.check_uploads == 0


def test_empty_check_scope_is_rejected_before_category_write(tmp_path, monkeypatch):
    document_id, fake = prepare_bank_workflow(tmp_path, monkeypatch)
    repo = DocumentRepository()
    document = repo.get(document_id)
    document["check_match_results"] = []
    repo.save(document)
    response = client.post(
        f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": True}
    )
    assert response.status_code == 400
    assert fake.category_writes == 0
    assert fake.check_uploads == 0
