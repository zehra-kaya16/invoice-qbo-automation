import copy
import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError

from app.api import export
from app.main import app
from app.models.database import Document, utc_now
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import restart_request

client = TestClient(app)


def seed(document_id="doc_export", company="default", **extra):
    document = {
        "id": document_id, "company_id": company, "filename": "fatura-İ.pdf",
        "content_type": "application/pdf", "content": b"%PDF-export-secret-source",
        "status": "pushed", "document_type": "invoice", "approved": True,
        "extracted_data": {"vendor": "Books by Bessie", "total_amount": "550.00",
                           "currency": "USD", "date": "2026-10-12", "invoice_number": "INV-102",
                           "line_items": [{"description": "Books", "amount": "550.00"}],
                           "raw_text": "internal-raw-text", "internal": "hidden-extra"},
        "validation": {"is_valid": True}, "qbo_push_state": "succeeded",
        "qbo_transaction_id": "qbo-102", "qbo_transaction_type": "bill",
        "qbo_attachment_id": "attach-102", "qbo_attachment_push_status": "uploaded",
        "qbo_push_context": {"access_token": "hidden-token"}, "extraction_claim": "hidden-claim",
        "check_image_sources": {"private-path": {"data": "hidden-crop"}},
    }
    document.update(extra)
    DocumentRepository().save(document)
    return document


def test_json_public_values_and_persisted_push_outcome():
    document = seed()
    response = client.get("/api/export/json")
    assert response.status_code == 200
    data = response.json()
    assert data["total_documents"] == 1
    item = data["documents"][0]
    assert item["id"] == document["id"] and item["approved"] is True
    assert item["extracted_data"]["total_amount"] == "550.00"
    assert item["extracted_data"]["line_items"] == [{"description": "Books", "amount": "550.00"}]
    assert item["qbo_transaction_id"] == "qbo-102"
    assert item["qbo_attachment_push_status"] == "uploaded"
    for forbidden in ["hidden-", "internal-raw-text", "private-path", "content\"", "raw_text", "access_token"]:
        assert forbidden not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].endswith('.json"')


def test_csv_unicode_money_and_attachment_summary():
    seed()
    response = client.get("/api/export/csv")
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0]["Filename"] == "fatura-İ.pdf"
    assert rows[0]["Amount"] == "550.00" and rows[0]["Currency"] == "USD"
    assert rows[0]["Attachment Status"] == "uploaded"
    assert response.headers["content-disposition"].endswith('.csv"')


@pytest.mark.parametrize("text", ["=1+1", "+SUM(A1:A2)", "-cmd", "@SUM(1)", "  =1+1", "\tformula", "\rformula", "\nformula"])
def test_csv_formula_cells_are_literal_but_json_preserves_text(text):
    seed(filename=text)
    response = client.get("/api/export/csv")
    row = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert row["Filename"] == "'" + text
    assert client.get("/api/export/json").json()["documents"][0]["filename"] == text


def test_bank_nested_export_has_no_server_image_paths():
    seed(document_type="bank_statement", extracted_data={
        "bank_name": "Test Bank", "ending_balance": "100.10", "currency": "invented",
        "transactions": [{"date": "2026-10-01", "amount": "-20.01", "description": "Books",
                          "check_image": {"check_number": "12", "image_path": "/private/nested.png"}}],
        "check_images": [{"check_number": "12", "image_path": "/private/check.png"}],
    })
    response = client.get("/api/export/json")
    data = response.json()["documents"][0]["extracted_data"]
    assert data["transactions"][0]["amount"] == "-20.01"
    assert data["check_images"] == [{"check_number": "12"}]
    assert "image_path" not in response.text and "invented" not in response.text


def test_filters_and_dates(isolated_document_database):
    seed("doc_invoice")
    seed("doc_receipt", document_type="receipt", status="extracted")
    seed("doc_old")
    with isolated_document_database() as db:
        db.execute(update(Document).where(Document.api_id == "doc_old").values(created_at=utc_now() - timedelta(days=40)))
        db.commit()
    assert client.get("/api/export/json").json()["total_documents"] == 2
    assert client.get("/api/export/json?days=60").json()["total_documents"] == 3
    data = client.get("/api/export/json?status=extracted&document_type=receipt").json()
    assert [item["id"] for item in data["documents"]] == ["doc_receipt"]
    assert data["filters"]["status"] == "extracted"


@pytest.mark.parametrize("params", ["days=0", "days=366", "days=1.5", "status=bogus", "document_type=bogus", "company_id="])
def test_invalid_filters_return_422(params):
    assert client.get("/api/export/json?" + params).status_code == 422


def test_empty_export_and_no_silent_truncation(monkeypatch):
    assert client.get("/api/export/json").json()["documents"] == []
    assert len(list(csv.reader(io.StringIO(client.get("/api/export/csv").content.decode("utf-8-sig"))))) == 1
    seed("doc_first")
    seed("doc_second")
    monkeypatch.setattr(export, "MAX_DOCUMENTS", 1)
    for extension in ["json", "csv"]:
        assert client.get("/api/export/" + extension).status_code == 413


def test_export_is_read_only_and_does_not_select_binary_fields(isolated_document_database, monkeypatch):
    seed()
    before = copy.deepcopy(DocumentRepository().get("doc_export"))
    statements = []
    engine = isolated_document_database.kw["bind"]
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for extension in ["csv", "json"]:
            assert client.get("/api/export/" + extension).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement, _ in statements)
    assert all("source_content" not in statement for statement, _ in statements)
    assert all("check_image_sources" not in str(parameters) for _, parameters in statements)
    assert DocumentRepository().get("doc_export") == before


def test_export_storage_failure_is_sanitized_503(monkeypatch):
    def offline():
        raise OperationalError("secret-statement", {}, RuntimeError("secret-database"))
    monkeypatch.setattr(export.database, "SessionLocal", offline)
    response = client.get("/api/export/json")
    assert response.status_code == 503 and "secret" not in response.text


def test_export_same_persisted_values_after_restart(isolated_document_database):
    seed()
    data = restart_request(isolated_document_database, "GET", "/api/export/json")["body"]
    assert data["documents"] == client.get("/api/export/json").json()["documents"]


@pytest.mark.auth_live
def test_export_real_login_tenant_scope_standard_user_and_revoked_session():
    from app.services.persistence.auth_repository import AuthRepository
    from manage_users import initialize_company
    password = "Export-password-102"
    own = initialize_company("default", "Own company")
    initialize_company("other", "Other company")
    AuthRepository().create_user(own, "export@example.test", password, "Export user", "user")
    seed()
    seed("doc_other", company="other", filename="other-secret.pdf")
    visitor = TestClient(app)
    for extension in ["csv", "json"]:
        assert visitor.get("/api/export/" + extension).status_code == 401
    assert visitor.post("/api/auth/login", json={"email": "export@example.test", "password": password}).status_code == 200
    info = visitor.get("/api/auth/me").json()
    visitor.headers["X-CSRF-Token"] = info["csrf_token"]
    for extension in ["csv", "json"]:
        response = visitor.get("/api/export/" + extension)
        assert response.status_code == 200 and "other-secret" not in response.text
        assert visitor.get("/api/export/" + extension + "?company_id=other").status_code == 403
    cookie = dict(visitor.cookies)
    assert visitor.post("/api/auth/logout").status_code == 200
    stale = TestClient(app)
    stale.cookies.update(cookie)
    assert stale.get("/api/export/json").status_code == 401
