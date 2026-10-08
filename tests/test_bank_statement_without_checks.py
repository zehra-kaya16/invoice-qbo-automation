import pytest

from app.schemas.documents import ProcessingStatus
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import (
    FakeBankQBO, client, prepare_bank_workflow, restart_request,
)


class NoCheckQBO(FakeBankQBO):
    def upload_attachment(self, **kwargs):
        raise AssertionError("A statement without checks must not upload a check")

    def find_vendor_by_name(self, name):
        raise AssertionError("No check vendor should be resolved")


def prepare_no_check_workflow(tmp_path, monkeypatch, fail=False):
    document_id, _ = prepare_bank_workflow(tmp_path, monkeypatch)
    repo = DocumentRepository()
    document = repo.get(document_id)
    document["extracted_data"]["check_images"] = []
    document["extracted_data"]["transactions"][0]["check_number"] = None
    repo.save(document)
    fake = NoCheckQBO(document_id, fail_category=fail)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    prefix = f"/api/documents/{document_id}"
    assert client.post(prefix + "/match-to-qbo", json={"bank_account_id": "1"}).status_code == 200
    assert repo.get(document_id)["check_match_results"] == []
    assert client.patch(prefix + "/match-review/0", json={"decision": "approve"}).status_code == 200
    assert client.post(prefix + "/resolve-vendors").status_code == 200
    assert client.post(prefix + "/suggest-categories").status_code == 200
    assert client.patch(prefix + "/category-review/0", json={"decision": "approve"}).status_code == 200
    return document_id, fake


@pytest.mark.parametrize("attach", [False, True])
def test_no_checks_pushes_categories_and_survives_restart(
    tmp_path, monkeypatch, isolated_document_database, attach
):
    document_id, fake = prepare_no_check_workflow(tmp_path, monkeypatch)
    prefix = f"/api/documents/{document_id}"
    response = client.post(prefix + "/push-to-qbo", json={"attach_documents": attach})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["success"] is True
    assert result["categories_updated"] == 1
    assert result["transactions_pushed"] == 0
    assert result["attachments_uploaded"] == 0
    assert result["vendors_created"] == 0
    assert DocumentRepository().get(document_id)["status"] == ProcessingStatus.PUSHED
    restarted = restart_request(isolated_document_database, "GET", prefix + "/category-review")
    assert restarted["status"] == 200
    retry = client.post(prefix + "/push-to-qbo", json={"attach_documents": attach})
    assert retry.status_code == 200
    assert retry.json()["categories_updated"] == 0
    assert fake.category_writes == 1


@pytest.mark.parametrize("invalid", ["absent", "null", "object", "missing_images"])
def test_missing_check_scope_fails_before_category_write(tmp_path, monkeypatch, invalid):
    document_id, fake = prepare_no_check_workflow(tmp_path, monkeypatch)
    repo = DocumentRepository()
    document = repo.get(document_id)
    if invalid == "absent":
        document.pop("check_match_results")
    elif invalid == "null":
        document["check_match_results"] = None
    elif invalid == "object":
        document["check_match_results"] = {}
    else:
        document["extracted_data"].pop("check_images")
    repo.save(document)
    response = client.post(f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": False})
    assert response.status_code == 400, response.text
    assert fake.category_writes == 0
    assert repo.get(document_id)["status"] != ProcessingStatus.PUSHED


def test_no_checks_uncertain_category_does_not_complete_or_repeat(tmp_path, monkeypatch):
    document_id, fake = prepare_no_check_workflow(tmp_path, monkeypatch, fail=True)
    path = f"/api/documents/{document_id}/push-to-qbo"
    assert client.post(path, json={"attach_documents": False}).status_code == 502
    document = DocumentRepository().get(document_id)
    assert document["status"] != ProcessingStatus.PUSHED
    assert document["category_suggestions"][0]["qbo_category_push_status"] == "uncertain"
    assert client.post(path, json={"attach_documents": False}).status_code == 409
    assert fake.category_writes == 1


@pytest.mark.parametrize("gate", ["match", "category"])
def test_no_checks_still_requires_reviews(tmp_path, monkeypatch, gate):
    document_id, fake = prepare_no_check_workflow(tmp_path, monkeypatch)
    repo = DocumentRepository()
    document = repo.get(document_id)
    key = "qbo_match_results" if gate == "match" else "category_suggestions"
    document[key][0]["review_status"] = "pending"
    repo.save(document)
    response = client.post(f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": False})
    assert response.status_code in (400, 409), response.text
    assert fake.category_writes == 0


def test_rejected_category_without_checks_completes_without_writing(tmp_path, monkeypatch):
    document_id, fake = prepare_no_check_workflow(tmp_path, monkeypatch)
    prefix = f"/api/documents/{document_id}"
    assert client.patch(prefix + "/category-review/0", json={"decision": "reject"}).status_code == 200
    response = client.post(prefix + "/push-to-qbo", json={"attach_documents": False})
    assert response.status_code == 200, response.text
    assert response.json()["categories_updated"] == 0
    assert response.json()["success"] is True
    assert fake.category_writes == 0
    assert DocumentRepository().get(document_id)["status"] == ProcessingStatus.PUSHED


def test_detected_checks_require_attachment_flag(tmp_path, monkeypatch):
    document_id, fake = prepare_bank_workflow(tmp_path, monkeypatch)
    response = client.post(f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": False})
    assert response.status_code == 400
    assert fake.category_writes == 0
    assert fake.check_uploads == 0


@pytest.mark.parametrize("invalid", ["item", "index", "path", "extra_result"])
def test_corrupt_detected_check_scope_fails_before_writing(tmp_path, monkeypatch, invalid):
    document_id, fake = prepare_bank_workflow(tmp_path, monkeypatch)
    repo = DocumentRepository()
    document = repo.get(document_id)
    if invalid == "item":
        document["check_match_results"] = [None]
    elif invalid == "index":
        document["check_match_results"][0]["check_index"] = []
    elif invalid == "path":
        document["check_match_results"][0]["image_path"] = "another-check.png"
    else:
        document["check_match_results"].append(dict(document["check_match_results"][0]))
    repo.save(document)
    response = client.post(f"/api/documents/{document_id}/push-to-qbo", json={"attach_documents": True})
    assert response.status_code == 400, response.text
    assert fake.category_writes == 0
    assert fake.check_uploads == 0
