from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Company, Document
from app.schemas.documents import DocumentType, ProcessingStatus
from app.services.persistence.repository import (
    DocumentConflictError,
    DocumentRepository,
)
from app.services.qbo.push_guard import (
    QBOPushGuardError,
    ensure_invoice_can_start_push,
    mark_invoice_push_started,
    mark_invoice_push_succeeded,
    mark_invoice_push_uncertain,
)


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "workflow.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield DocumentRepository(factory), factory
    finally:
        engine.dispose()


def invoice():
    return {
        "id": "doc_persistence_test",
        "company_id": "default",
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "content": b"%PDF-1.7\x00\xffbinary",
        "status": ProcessingStatus.EXTRACTED,
        "document_type": DocumentType.INVOICE,
        "extracted_data": {
            "vendor_name": "Books by Bessie",
            "date": date(2026, 9, 15),
            "total_amount": Decimal("550.00"),
        },
        "validation": {"is_valid": True},
        "approved": True,
        "qbo_push_state": "not_started",
    }


def test_snapshot_survives_new_engine_and_repository(repository):
    repo, factory = repository
    document = invoice()
    repo.save(document)
    url = factory.kw["bind"].url
    factory.kw["bind"].dispose()

    restarted_engine = create_engine(url)
    try:
        restarted = DocumentRepository(sessionmaker(bind=restarted_engine))
        loaded = restarted.get(document["id"])
        assert loaded["id"] == document["id"]
        assert loaded["company_id"] == "default"
        assert loaded["content"] == document["content"]
        assert loaded["document_type"] == "invoice"
        assert loaded["extracted_data"]["date"] == "2026-09-15"
        assert loaded["extracted_data"]["total_amount"] == "550.00"
    finally:
        restarted_engine.dispose()


def test_nested_reviews_require_explicit_save_and_survive_reload(repository):
    repo, factory = repository
    document = invoice()
    document["category_suggestions"] = [
        {"transaction_index": 0, "review_status": "pending"}
    ]
    repo.save(document)
    loaded = repo.get(document["id"])
    loaded["category_suggestions"][0]["review_status"] = "approved"
    loaded["category_suggestions"][0]["reviewed_category"] = {"id": "10"}

    assert repo.get(document["id"])["category_suggestions"][0]["review_status"] == "pending"
    repo.save(loaded)
    restarted = DocumentRepository(factory)
    assert restarted.get(document["id"])["category_suggestions"][0] == {
        "transaction_index": 0,
        "review_status": "approved",
        "reviewed_category": {"id": "10"},
    }


def test_stale_snapshot_cannot_overwrite_newer_push_state(repository):
    repo, _ = repository
    document = invoice()
    repo.save(document)
    stale = repo.get(document["id"])
    current = repo.get(document["id"])
    mark_invoice_push_started(current)
    repo.save(current)
    stale["approved"] = False
    with pytest.raises(DocumentConflictError):
        repo.save(stale)
    persisted = repo.get(document["id"])
    assert persisted["qbo_push_state"] == "in_progress"
    assert persisted["approved"] is True


@pytest.mark.parametrize("push_state", ["in_progress", "succeeded", "uncertain"])
def test_persisted_push_state_blocks_retry(repository, push_state):
    repo, factory = repository
    document = invoice()
    repo.save(document)
    mark_invoice_push_started(document)
    repo.save(document)
    if push_state == "succeeded":
        mark_invoice_push_succeeded(document, "146", "bill")
        repo.save(document)
    elif push_state == "uncertain":
        mark_invoice_push_uncertain(document, "provider timeout")
        repo.save(document)

    restarted = DocumentRepository(factory)
    loaded = restarted.get(document["id"])
    assert loaded["qbo_push_state"] == push_state
    with pytest.raises(QBOPushGuardError):
        ensure_invoice_can_start_push(loaded)


def test_failed_serialization_preserves_last_committed_state(repository):
    repo, _ = repository
    document = invoice()
    repo.save(document)
    before = deepcopy(repo.get(document["id"]))
    document["unsupported"] = object()
    with pytest.raises(TypeError):
        repo.save(document)
    assert repo.get(document["id"]) == before


def test_public_id_maps_to_uuid_and_does_not_duplicate_company(repository):
    repo, factory = repository
    first = invoice()
    second = invoice()
    second["id"] = "doc_second"
    repo.save(first)
    repo.save(second)
    with factory() as session:
        companies = session.scalars(select(Company)).all()
        rows = session.scalars(select(Document)).all()
        assert len(companies) == 1
        assert len(rows) == 2
        assert all(row.company_id == companies[0].id for row in rows)
        assert {row.api_id for row in rows} == {first["id"], second["id"]}
        assert all(row.id != row.api_id for row in rows)


def test_new_snapshot_cannot_overwrite_existing_public_id(repository):
    repo, _ = repository
    original = invoice()
    repo.save(original)
    duplicate = invoice()
    duplicate["approved"] = False
    with pytest.raises(DocumentConflictError):
        repo.save(duplicate)
    assert repo.get(original["id"])["approved"] is True


def test_missing_document_returns_none(repository):
    repo, _ = repository
    assert repo.get("doc_missing") is None
