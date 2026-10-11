import pytest
from sqlalchemy.exc import OperationalError

from app.api import documents as api
from app.schemas.documents import DocumentType, ProcessingStatus
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import client, restart_request


def seed(document_id="doc_list", company="default", **extra):
    document = {"id": document_id, "filename": "original.pdf", "company_id": company,
                "status": ProcessingStatus.UPLOADED, "content_type": "application/pdf",
                "content": b"%PDF-1.7\x00\xffsource", "document_type": None}
    document.update(extra)
    DocumentRepository().save(document)
    return document


def test_empty_collection_is_not_document_id_route():
    response = client.get("/api/documents")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "limit": 20, "offset": 0, "items": []}


def test_collection_pagination_company_and_no_binary(isolated_document_database):
    seed("doc_a")
    seed("doc_b", status=ProcessingStatus.PUSHED, document_type=DocumentType.INVOICE)
    seed("doc_other", company="other")
    result = client.get("/api/documents", params={"limit": 1, "offset": 0}).json()
    assert result["total"] == 2
    assert len(result["items"]) == 1
    other_page = client.get("/api/documents", params={"limit": 1, "offset": 1}).json()
    assert result["items"][0]["id"] != other_page["items"][0]["id"]
    assert set(result["items"][0]) == {"id", "filename", "status", "document_type", "error"}
    assert client.get("/api/documents", params={"company_id": "other"}).json()["total"] == 1
    restarted = restart_request(isolated_document_database, "GET", "/api/documents")
    assert restarted["status"] == 200
    assert restarted["body"]["total"] == 2
    assert client.get("/api/documents", params={"offset": 100}).json()["items"] == []


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"company_id": ""}])
def test_invalid_collection_parameters(params):
    assert client.get("/api/documents", params=params).status_code == 422


def test_source_returns_exact_persisted_bytes():
    document = seed()
    response = client.get("/api/documents/doc_list/source")
    assert response.status_code == 200
    assert response.content == document["content"]
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == "inline"


@pytest.mark.parametrize("content", [None, b""])
def test_missing_source_is_404(content):
    seed(content=content)
    assert client.get("/api/documents/doc_list/source").status_code == 404


def test_unknown_source_document_is_404():
    assert client.get("/api/documents/doc_missing/source").status_code == 404


def test_unsupported_source_media_type_downloads_as_binary():
    seed(content_type="text/html", content=b"<script>test</script>")
    response = client.get("/api/documents/doc_list/source")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment"


def test_collection_database_failure_is_503(monkeypatch):
    def unavailable(*args):
        raise OperationalError("select", {}, RuntimeError("offline"))
    monkeypatch.setattr(api.document_repository, "list_summaries", unavailable)
    assert client.get("/api/documents").status_code == 503
