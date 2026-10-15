import copy
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, event
from sqlalchemy.exc import OperationalError

from app.api import batch
from app.main import app
from app.models.database import Document, UploadBatch
from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import restart_request

client = TestClient(app)


@pytest.fixture(autouse=True)
def existing_company():
    from manage_users import initialize_company
    initialize_company("default", "Test company")


def upload(key=None, files=None, query="", caller=client):
    return caller.post("/api/batch/upload" + query, headers={"Idempotency-Key": key or str(uuid.uuid4())},
                       files=files or [("files", ("first.pdf", b"%PDF-one", "application/pdf")),
                                       ("files", ("second.png", b"png-two", "image/png"))])


def test_batch_upload_source_list_restart_and_live_status(isolated_document_database):
    result = upload()
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["pending"] == 2 and data["status"] == "pending" and data["total_files"] == 2
    ids = data["uploaded_documents"]
    assert client.get(f"/api/documents/{ids[0]}/source").content == b"%PDF-one"
    assert client.get("/api/documents").json()["total"] == 2
    assert client.get("/api/batch").json()["items"][0]["batch_id"] == data["batch_id"]
    restarted = restart_request(isolated_document_database, "GET", "/api/batch/status/" + data["batch_id"])
    assert restarted["body"] == data
    document = DocumentRepository().get(ids[0])
    assert document["batch_id"] == data["batch_id"]
    document.update(status="extracted", document_type="invoice", extracted_data={"total_amount": "10.00"})
    DocumentRepository().save(document)
    status = client.get("/api/batch/status/" + data["batch_id"]).json()
    assert status["pending"] == 1 and status["completed"] == 1
    document = DocumentRepository().get(ids[1]); document["status"] = "failed"; DocumentRepository().save(document)
    status = client.get("/api/batch/status/" + data["batch_id"]).json()
    assert status["status"] == "partial" and status["failed"] == 1


def test_same_key_reuses_ids_and_different_payload_is_409(isolated_document_database):
    key = str(uuid.uuid4())
    first = upload(key).json()
    assert upload(key).json() == first
    assert upload(key, [("files", ("changed.pdf", b"other", "application/pdf"))]).status_code == 409
    with isolated_document_database() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 2
        assert session.scalar(select(func.count()).select_from(UploadBatch)) == 1


def test_concurrent_same_key_creates_one_batch(isolated_document_database):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(key), range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert responses[0].json()["uploaded_documents"] == responses[1].json()["uploaded_documents"]
    with isolated_document_database() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 2


def test_partial_validation_errors_and_complete_failure():
    data = upload(files=[("files", ("ok.pdf", b"%PDF", "application/pdf")),
                         ("files", ("bad.exe", b"exe", "application/octet-stream")),
                         ("files", ("empty.pdf", b"", "application/pdf"))]).json()
    assert len(data["uploaded_documents"]) == 1 and len(data["failed_files"]) == 2
    assert data["failed"] == 2 and data["pending"] == 1
    data = upload(files=[("files", ("empty.pdf", b"", "application/pdf"))]).json()
    assert data["status"] == "failed" and data["total_files"] == 1


def test_size_bounds_and_no_rows_on_global_rejection(monkeypatch, isolated_document_database):
    monkeypatch.setattr(batch.settings, "max_upload_bytes", 4)
    assert upload().json()["failed"] == 2
    before = client.get("/api/batch").json()["total"]
    monkeypatch.setattr(batch, "MAX_BATCH_BYTES", 3)
    assert upload().status_code == 413
    assert client.get("/api/batch").json()["total"] == before
    monkeypatch.setattr(batch, "MAX_FILES", 1)
    assert upload().status_code == 400


def test_storage_failure_rolls_back_batch_and_all_documents(isolated_document_database):
    engine = isolated_document_database.kw["bind"]
    def fail(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("INSERT INTO UPLOAD_BATCHES"):
            raise OperationalError("secret-statement", {}, RuntimeError("secret-database"))
    event.listen(engine, "before_cursor_execute", fail)
    try:
        response = upload()
        assert response.status_code == 503 and "secret" not in response.text
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with isolated_document_database() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(UploadBatch)) == 0


def test_batch_reads_do_not_modify_financial_records():
    data = upload().json()
    id = data["uploaded_documents"][0]
    document = DocumentRepository().get(id)
    document.update(status="pushed", qbo_transaction_id="saved-qbo-id", qbo_push_state="succeeded")
    DocumentRepository().save(document)
    before = copy.deepcopy(DocumentRepository().get(id))
    assert client.get("/api/batch/status/" + data["batch_id"]).json()["completed"] == 1
    client.get("/api/batch")
    assert DocumentRepository().get(id) == before


@pytest.mark.parametrize("params", ["limit=0", "limit=101", "offset=-1"])
def test_invalid_batch_list_parameters(params):
    assert client.get("/api/batch?" + params).status_code == 422


def test_missing_and_invalid_upload_keys_and_scope():
    assert client.post("/api/batch/upload", files={"files": ("a.pdf", b"%PDF", "application/pdf")}).status_code == 422
    assert upload("invalid").status_code == 422
    assert upload(query="?company_id=other").status_code == 403
    assert client.get("/api/batch/status/batch_unknown").status_code == 404


def test_batch_list_pagination():
    first = upload().json();second = upload().json()
    page = client.get("/api/batch?limit=1").json()
    assert page["total"] == 2 and len(page["items"]) == 1
    other = client.get("/api/batch?limit=1&offset=1").json()
    assert other["items"][0]["batch_id"] != page["items"][0]["batch_id"]


def test_migration_upgrade_preserves_existing_document(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///" + str(tmp_path / "migration.db")
    root = Path(__file__).resolve().parents[1]
    def run(args):
        return subprocess.run([sys.executable, *args], cwd=root, env=env, text=True,
                              capture_output=True, check=True, timeout=30)
    run(["-m", "alembic", "upgrade", "100a1d0c2026"])
    run(["-c", "from app.services.persistence.repository import DocumentRepository; DocumentRepository().save({'id':'doc_old','filename':'old.pdf','company_id':'default','content':b'original','status':'pushed','qbo_transaction_id':'old-qbo','qbo_push_state':'succeeded'})"])
    run(["-m", "alembic", "upgrade", "head"])
    assert "107a1d0c2026" in run(["-m", "alembic", "current"]).stdout
    run(["-c", "from fastapi.testclient import TestClient; from app.main import app; from tests.auth_helpers import override_authenticated_api; from app.services.persistence.repository import DocumentRepository; override_authenticated_api(app); c=TestClient(app); assert c.get('/ready').status_code==200; r=c.post('/api/batch/upload',headers={'Idempotency-Key':'00000000-0000-4000-8000-000000000104'},files={'files':('new.pdf',b'%PDF','application/pdf')}); assert r.status_code==200,r.text; assert DocumentRepository().get('doc_old')['qbo_transaction_id']=='old-qbo'; assert DocumentRepository().get('doc_old')['content']==b'original'"])
    run(["-m", "alembic", "downgrade", "100a1d0c2026"])
    run(["-c", "from app.services.persistence.repository import DocumentRepository; assert DocumentRepository().get('doc_old')['qbo_transaction_id']=='old-qbo'"])


@pytest.mark.auth_live
def test_real_auth_csrf_and_company_isolation():
    from manage_users import initialize_company
    from app.services.persistence.auth_repository import AuthRepository
    password = "Batch-password-104"
    for key in ["default", "other"]:
        company = initialize_company(key, key)
        AuthRepository().create_user(company, key + "@example.test", password, key, "user")
    a = TestClient(app);b = TestClient(app)
    assert upload(caller=a).status_code == 401
    assert a.get("/api/batch").status_code == 401
    for caller, key in [(a, "default"), (b, "other")]:
        assert caller.post("/api/auth/login", json={"email": key + "@example.test", "password": password}).status_code == 200
    assert upload(caller=a).status_code == 403
    for caller in [a, b]:caller.headers["X-CSRF-Token"] = caller.get("/api/auth/me").json()["csrf_token"]
    key = str(uuid.uuid4());data = upload(key, caller=a).json()
    assert b.get("/api/batch/status/" + data["batch_id"]).status_code == 404
    assert b.get("/api/batch").json()["total"] == 0
    assert b.get("/api/documents/" + data["uploaded_documents"][0] + "/source").status_code == 404
    assert upload(query="?company_id=default", caller=b).status_code == 403
    own_b = upload(key, caller=b)
    assert own_b.status_code == 200 and own_b.json()["batch_id"] != data["batch_id"]
