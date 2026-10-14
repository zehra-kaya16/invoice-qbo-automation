import copy
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth import current_principal
from app.main import app
from app.models.database import AuthSession, BatchExtractionRun, User, utc_now
from app.services.persistence.batch_extraction import enqueue, recover_run, run_once
from app.services.persistence.repository import DocumentRepository
from tests.auth_helpers import TEST_PRINCIPAL
from tests.test_batch_upload import upload

client = TestClient(app)


@pytest.fixture
def actor(isolated_document_database):
    from dataclasses import replace
    from app.core.auth import get_current_user
    from manage_users import initialize_company
    principal = replace(TEST_PRINCIPAL, user_id="a0000000-0000-0000-0000-000000000106")
    company = initialize_company("default", "Test company")
    with isolated_document_database() as session:
        session.add(User(id=principal.user_id, company_id=company, email="workflow@example.test",
                         name="Test", role="admin", is_active=True, password_hash="test-only"))
        session.add(AuthSession(token_hash=TEST_PRINCIPAL.session_hash, user_id=principal.user_id,
                              csrf_token=TEST_PRINCIPAL.csrf_token, expires_at=utc_now() + timedelta(hours=1)))
        session.commit()
    async def user():
        return principal
    app.dependency_overrides[get_current_user] = user
    return principal


@pytest.fixture
def data(actor):
    response = upload()
    assert response.status_code == 200, response.text
    return response.json()


def start(data, retry=False, key=None):
    return client.post(f"/api/batch/{data['batch_id']}/extract", headers={"Idempotency-Key": key or str(uuid.uuid4())},
                       json={"retry_failed": retry})


def fake_extraction(monkeypatch, fail_ids=(), crash_ids=(), complete_then_crash=False):
    from app.api import documents
    calls = []
    def process(id, expected_revision=None):
        calls.append(id)
        assert current_principal.get().company_key == "default"
        doc = DocumentRepository().get(id)
        assert doc["_revision"] == expected_revision
        if id in crash_ids:
            doc.update(status="classifying", extraction_claim="crash", approved=False)
            DocumentRepository().save(doc)
            raise SystemExit("simulated worker crash")
        doc.update(status="failed" if id in fail_ids else "extracted", document_type="invoice",
                   approved=False, extracted_data={"vendor": "Books by Bessie", "total_amount": "10.00"})
        DocumentRepository().save(doc)
        if complete_then_crash:
            raise SystemExit("crash after result commit")
    monkeypatch.setattr(documents, "_process_document_sync", process)
    return calls


def drain(id):
    last = None
    for _ in range(10):
        result = run_once(id)
        if result is None:
            return last
        last = result
    raise AssertionError("worker did not drain")


def test_durable_queue_runs_one_document_per_turn_and_never_approves(data, monkeypatch):
    calls = fake_extraction(monkeypatch)
    response = start(data)
    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "queued" and not calls
    first = run_once(run["run_id"])
    assert first["status"] == "queued" and first["counts"]["succeeded"] == 1
    final = run_once(run["run_id"])
    assert final["status"] == "completed" and final["counts"]["succeeded"] == 2
    assert run_once(run["run_id"]) is None and len(calls) == 2
    assert start(data, key=run["request_key"]).json()["status"] == "completed"
    assert client.get("/api/batch/status/" + data["batch_id"]).json()["completed"] == 2
    for id in data["uploaded_documents"]:
        doc = DocumentRepository().get(id)
        assert doc["approved"] is False and doc["qbo_transaction_id"] is None
    assert current_principal.get() is None


def test_enqueue_idempotency_and_active_batch_guard(data):
    key = str(uuid.uuid4())
    first = start(data, key=key)
    assert start(data, key=key).json() == first.json()
    assert start(data).status_code == 409
    assert start(data, retry=True, key=key).status_code == 409
    public = client.get("/api/batch").text
    assert "owner_session_hash" not in public and "test-session" not in public


def test_concurrent_start_creates_one_run(data):
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(data, key=key), range(2)))
    assert all(r.status_code == 202 for r in results), [r.text for r in results]
    assert results[0].json()["run_id"] == results[1].json()["run_id"]


def test_failure_continues_to_next_and_requires_explicit_retry(data, monkeypatch):
    fail = data["uploaded_documents"][0]
    calls = fake_extraction(monkeypatch, fail_ids={fail})
    run = start(data).json()
    final = drain(run["run_id"])
    assert final["status"] == "partial" and final["counts"]["failed"] == 1 and final["counts"]["succeeded"] == 1
    assert start(data).status_code == 409
    retried = start(data, retry=True).json()
    assert len(retried["items"]) == 1 and retried["items"][0]["id"] == fail
    fake_extraction(monkeypatch)
    assert drain(retried["run_id"])["status"] == "completed"


@pytest.mark.parametrize("values", [{"status":"extracted"}, {"status":"matched"}, {"status":"pushed"},
    {"qbo_push_state":"uncertain"}, {"qbo_push_state":"in_progress"}, {"qbo_transaction_id":"existing"},
    {"approved":True}, {"check_match_results":[{"attachment_id":"existing"}]}])
def test_completed_reviewed_or_financial_document_is_not_selected(data, values):
    repo = DocumentRepository()
    for id in data["uploaded_documents"]:
        doc = repo.get(id);doc.update(values);repo.save(doc)
    before = [repo.get(id) for id in data["uploaded_documents"]]
    assert start(data, retry=True).status_code == 409
    assert [repo.get(id) for id in data["uploaded_documents"]] == before


def test_revision_change_after_enqueue_skips_without_touching_review(data, monkeypatch):
    run = start(data).json()
    id = data["uploaded_documents"][0]
    repo = DocumentRepository();doc = repo.get(id);doc["filename"] = "changed.pdf";repo.save(doc)
    before = copy.deepcopy(repo.get(id))
    calls = fake_extraction(monkeypatch)
    final = drain(run["run_id"])
    assert final["counts"]["skipped"] == 1 and calls == [data["uploaded_documents"][1]]
    assert repo.get(id) == before


def test_single_document_race_uses_core_revision_guard(data, monkeypatch):
    from app.api import documents
    id = data["uploaded_documents"][0]
    doc = DocumentRepository().get(id)
    old = doc["_revision"]
    doc["approved"] = True;DocumentRepository().save(doc)
    before = copy.deepcopy(DocumentRepository().get(id))
    with pytest.raises(Exception) as result:
        documents._process_document_sync(id, expected_revision=old)
    assert result.value.status_code == 409
    assert DocumentRepository().get(id) == before


def test_revoked_owner_session_cancels_remaining_documents(data, isolated_document_database, monkeypatch):
    calls = fake_extraction(monkeypatch)
    run = start(data).json()
    run_once(run["run_id"])
    with isolated_document_database() as session:
        session.delete(session.get(AuthSession, TEST_PRINCIPAL.session_hash));session.commit()
    final = run_once(run["run_id"])
    assert final["status"] == "cancelled" and final["counts"]["not_processed"] == 1 and len(calls) == 1


def test_interrupted_worker_recovers_without_replaying_completed_docs(data, monkeypatch):
    crash = data["uploaded_documents"][1]
    run = start(data).json()
    fake_extraction(monkeypatch, crash_ids={crash})
    run_once(run["run_id"])
    with pytest.raises(SystemExit):run_once(run["run_id"])
    assert start(data).status_code == 409
    assert run_once(run["run_id"]) is None
    recovered = recover_run(run["run_id"])
    assert recovered["status"] == "interrupted" and recovered["counts"]["succeeded"] == 1
    assert DocumentRepository().get(crash)["status"] == "failed"
    assert "extraction_claim" not in DocumentRepository().get(crash)
    new = start(data, retry=True).json()
    assert [item["id"] for item in new["items"]] == [crash]
    calls = fake_extraction(monkeypatch)
    assert drain(new["run_id"])["status"] == "completed" and calls == [crash]


def test_result_commit_before_worker_crash_is_adopted(data, monkeypatch):
    run = start(data).json()
    fake_extraction(monkeypatch, complete_then_crash=True)
    with pytest.raises(SystemExit):run_once(run["run_id"])
    final = recover_run(run["run_id"])
    assert final["counts"]["succeeded"] == 1 and final["counts"]["not_processed"] == 1
    new = start(data).json()
    assert len(new["items"]) == 1


def test_celery_adapter_consumes_same_queue_in_eager_mode(data, monkeypatch):
    fake_extraction(monkeypatch)
    run = start(data).json()
    from app.tasks.extraction import process_batch_extraction
    result = process_batch_extraction.apply().get()
    assert result["run_id"] == run["run_id"]


def test_queued_job_survives_another_worker_process(data, isolated_document_database):
    run = start(data).json()
    env = os.environ.copy();env["DATABASE_URL"] = str(isolated_document_database.kw["bind"].url)
    env.pop("OPENAI_API_KEY", None);env.pop("ANTHROPIC_API_KEY", None)
    program = """
import json
from app.core.config import settings
settings.openai_api_key=None;settings.anthropic_api_key=None
from app.services.persistence.batch_extraction import run_once
result=run_once()
print(json.dumps({'id':result['run_id'],'failed':result['counts']['failed']}))
"""
    result = subprocess.run([sys.executable,"-c",program],env=env,cwd=Path(__file__).resolve().parents[1],
                            capture_output=True,text=True,check=True,timeout=30)
    assert json.loads(result.stdout) == {"id":run["run_id"],"failed":1}


@pytest.mark.auth_live
def test_real_auth_csrf_company_scope_and_foreign_batch():
    from app.services.persistence.auth_repository import AuthRepository
    from manage_users import initialize_company
    password = "Extraction-password-106"
    for key in ["default","other"]:
        company = initialize_company(key,key)
        AuthRepository().create_user(company,key+"@example.test",password,key,"user")
    a=TestClient(app);b=TestClient(app)
    assert a.post('/api/batch/unknown/extract',json={},headers={'Idempotency-Key':str(uuid.uuid4())}).status_code==401
    for caller,key in [(a,'default'),(b,'other')]:
        caller.post('/api/auth/login',json={'email':key+'@example.test','password':password})
    for caller in [a,b]:caller.headers['X-CSRF-Token']=caller.get('/api/auth/me').json()['csrf_token']
    data=upload(caller=a).json();key=str(uuid.uuid4())
    assert b.post(f"/api/batch/{data['batch_id']}/extract",json={},headers={'Idempotency-Key':key}).status_code==404
    a.headers.pop('X-CSRF-Token')
    assert a.post(f"/api/batch/{data['batch_id']}/extract",json={},headers={'Idempotency-Key':key}).status_code==403


def test_parallel_workers_do_not_process_same_document(data, monkeypatch):
    from threading import Event
    from app.api import documents
    entered, release = Event(), Event()
    calls = fake_extraction(monkeypatch)
    original = documents._process_document_sync
    def slow(id, expected_revision=None):
        entered.set()
        assert release.wait(5)
        return original(id, expected_revision=expected_revision)
    monkeypatch.setattr(documents, "_process_document_sync", slow)
    run = start(data).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_once, run["run_id"])
        assert entered.wait(5)
        second = pool.submit(run_once, run["run_id"])
        assert second.result(timeout=5) is None
        release.set()
        assert first.result(timeout=5)["counts"]["succeeded"] == 1
    assert len(calls) == 1


def test_worker_uses_real_core_claim_and_validation(actor, monkeypatch):
    from app.core.config import settings
    from app.services.extraction import extractor
    from tests.test_web_review_flow import ReviewExtractor
    monkeypatch.setattr(settings, "openai_api_key", "test-only-key")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(extractor, "DocumentExtractor", ReviewExtractor)
    data = upload(files=[("files", ("image.png", b"image", "image/png"))]).json()
    run = start(data).json()
    final = run_once(run["run_id"])
    assert final["counts"]["succeeded"] == 1
    doc = DocumentRepository().get(data["uploaded_documents"][0])
    assert doc["validation"]["is_valid"] is True and doc["approved"] is False
    assert doc["extracted_data"]["total_amount"] == "550.00" and "extraction_claim" not in doc


def test_inactive_owner_cancels_without_ai(data, isolated_document_database, monkeypatch):
    calls = fake_extraction(monkeypatch)
    run = start(data).json()
    with isolated_document_database() as session:
        user = session.get(User, "a0000000-0000-0000-0000-000000000106")
        user.is_active = False;session.commit()
    result = run_once(run["run_id"])
    assert result["status"] == "cancelled" and result["counts"]["not_processed"] == 2 and not calls


def test_recovery_refuses_financial_claim_and_preserves_document(data, monkeypatch):
    id = data["uploaded_documents"][0]
    run = start(data).json()
    fake_extraction(monkeypatch, crash_ids={id})
    with pytest.raises(SystemExit):run_once(run["run_id"])
    doc = DocumentRepository().get(id);doc["qbo_transaction_id"] = "protected";DocumentRepository().save(doc)
    before = copy.deepcopy(DocumentRepository().get(id))
    with pytest.raises(ValueError):recover_run(run["run_id"])
    assert DocumentRepository().get(id) == before
