"""Durable extraction queue: one CAS claim and one document per worker turn."""
import copy
import uuid

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core import database
from app.core.auth import current_principal
from app.models.database import BatchExtractionRun, Document, UploadBatch, utc_now
from app.services.persistence.auth_repository import AuthRepository
from app.services.persistence.repository import DocumentRepository

TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


def public_run(row):
    counts = {state: sum(item["status"] == state for item in row.items)
              for state in ("queued", "running", "succeeded", "failed", "skipped", "not_processed")}
    return {"run_id": row.id, "batch_id": row.batch_id, "request_key": row.request_key,
            "status": row.status, "retry_failed": row.retry_failed, "counts": counts,
            "items": [{k: item.get(k) for k in ("id", "status", "message")} for item in row.items],
            "created_at": row.created_at.isoformat(),
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


def latest_run(session, batch_id, company_id):
    row = session.scalar(select(BatchExtractionRun).where(BatchExtractionRun.batch_id == batch_id,
                          BatchExtractionRun.company_id == company_id)
                         .order_by(BatchExtractionRun.created_at.desc(), BatchExtractionRun.id.desc()).limit(1))
    return public_run(row) if row else None


def eligible(document, retry_failed):
    from app.api.documents import _ensure_document_can_be_extracted
    if document.get("status") not in ({"uploaded", "failed"} if retry_failed else {"uploaded"}):
        return False
    if document.get("approved") or document.get("qbo_transaction_id"):
        return False
    try:
        _ensure_document_can_be_extracted(document)
    except HTTPException:
        return False
    return True


def enqueue(batch_id, user, request_key, retry_failed=False):
    key = str(request_key)
    try:
        with database.SessionLocal() as session:
            batch = session.scalar(select(UploadBatch).where(UploadBatch.id == batch_id,
                                                            UploadBatch.company_id == user.company_id))
            if batch is None:
                raise HTTPException(404, "Batch not found")
            previous = session.scalar(select(BatchExtractionRun).where(
                BatchExtractionRun.company_id == user.company_id, BatchExtractionRun.request_key == key))
            if previous:
                if previous.batch_id != batch_id or previous.retry_failed != retry_failed:
                    raise HTTPException(409, "Extraction key belongs to a different request")
                return public_run(previous)
            active = session.scalar(select(BatchExtractionRun).where(
                BatchExtractionRun.batch_id == batch_id, BatchExtractionRun.active_key == "active"))
            if active:
                # The first request may commit between the nonce lookup above
                # and this active-run lookup. Replaying that same request is
                # still idempotent, rather than an unrelated active-job conflict.
                if active.request_key == key and active.retry_failed == retry_failed:
                    return public_run(active)
                raise HTTPException(409, "Batch extraction is already queued or running")
            # Queue selection must not load source/crop bytes from completed
            # bank documents just to determine whether they are eligible.
            fields = ("status", "approved", "qbo_push_state", "qbo_transaction_id",
                      "extraction_claim", "category_suggestions", "check_match_results")
            rows = session.execute(select(Document.api_id, Document.revision,
                *((Document.workflow_state[field].as_boolean() if field == "approved"
                   else Document.workflow_state[field]).label(field) for field in fields)).where(
                Document.company_id == user.company_id, Document.api_id.in_(batch.document_ids),
                Document.status.in_(["uploaded", "failed"] if retry_failed else ["uploaded"]))).all()
            candidates = {row.api_id: row for row in rows if eligible(
                {field: row._mapping[field] for field in fields if row._mapping[field] is not None}, retry_failed)}
            items = [{"id": id, "revision": candidates[id].revision, "status": "queued", "message": None}
                     for id in batch.document_ids if id in candidates]
            if not items:
                raise HTTPException(409, "No eligible pending documents; failed documents require explicit retry")
            run = BatchExtractionRun(id="run_" + uuid.uuid4().hex, batch_id=batch_id, company_id=user.company_id,
                owner_user_id=user.user_id, owner_session_hash=user.session_hash, request_key=key,
                retry_failed=retry_failed, active_key="active", status="queued", items=items)
            session.add(run)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                previous = session.scalar(select(BatchExtractionRun).where(
                    BatchExtractionRun.company_id == user.company_id, BatchExtractionRun.request_key == key))
                if previous and previous.batch_id == batch_id and previous.retry_failed == retry_failed:
                    return public_run(previous)
                raise HTTPException(409, "Batch extraction was claimed by another request") from None
            return public_run(run)
    except SQLAlchemyError:
        raise HTTPException(503, "Extraction queue storage is unavailable; refresh before retrying") from None


def _claim(run_id=None):
    with database.SessionLocal() as session:
        query = select(BatchExtractionRun).where(BatchExtractionRun.status == "queued")
        if run_id:
            query = query.where(BatchExtractionRun.id == run_id)
        row = session.scalar(query.order_by(BatchExtractionRun.created_at, BatchExtractionRun.id).limit(1))
        if row is None:
            return None
        id, version = row.id, row.revision
        claim = uuid.uuid4().hex
        changed = session.execute(update(BatchExtractionRun).where(BatchExtractionRun.id == id,
            BatchExtractionRun.status == "queued", BatchExtractionRun.revision == version).values(
            status="running", claim_token=claim, started_at=row.started_at or utc_now(), revision=version + 1))
        session.commit()
        return (id, claim) if changed.rowcount == 1 else None


def _owned_run(session, id, claim):
    row = session.scalar(select(BatchExtractionRun).where(BatchExtractionRun.id == id,
                          BatchExtractionRun.status == "running", BatchExtractionRun.claim_token == claim))
    if row is None:
        raise RuntimeError("Batch extraction claim was lost")
    return row


def _finish(id, claim, index=None, status=None, message=None, cancelled=False):
    with database.SessionLocal() as session:
        row = _owned_run(session, id, claim)
        items = copy.deepcopy(row.items)
        if index is not None:
            items[index].update(status=status, message=message)
        if cancelled:
            for item in items:
                if item["status"] in {"queued", "running"}:
                    item.update(status="not_processed", message="Owner session expired or was revoked")
        row.items = items
        if cancelled:
            row.status = "cancelled"
        elif any(item["status"] == "queued" for item in items):
            row.status = "queued"
        else:
            successes = sum(item["status"] == "succeeded" for item in items)
            failures = sum(item["status"] == "failed" for item in items)
            row.status = "partial" if successes and failures else "failed" if failures else "completed"
        row.claim_token = None
        if row.status in TERMINAL:
            row.active_key = None
            row.finished_at = utc_now()
        session.commit()
        return public_run(row)


def run_once(run_id=None):
    """Trusted worker entry point. It performs at most one document extraction."""
    from app.services.deployment.restore_guard import status as restore_status
    if restore_status()['active']:
        return None
    owned = _claim(run_id)
    if owned is None:
        return None
    id, claim = owned
    with database.SessionLocal() as session:
        row = _owned_run(session, id, claim)
        owner_hash, owner_id, company_id = row.owner_session_hash, row.owner_user_id, row.company_id
        items = copy.deepcopy(row.items)
        batch_id = row.batch_id
    if owner_hash.startswith('integration:'):
        from app.services.integrations.webhooks import extraction_principal
        principal = extraction_principal(owner_hash, batch_id)
    else:
        principal = AuthRepository().principal(owner_hash)
    if principal is None or principal.user_id != owner_id or principal.company_id != company_id:
        return _finish(id, claim, cancelled=True)
    index = next((n for n, item in enumerate(items) if item["status"] == "queued"), None)
    if index is None:
        return _finish(id, claim)
    item = items[index]
    token = current_principal.set(principal)
    try:
        repository = DocumentRepository()
        document = repository.get(item["id"])
        if document is None or document.get("_revision") != item["revision"] or not eligible(document, True):
            return _finish(id, claim, index, "skipped", "Document changed, is protected, or is already processed")
        with database.SessionLocal() as session:
            row = _owned_run(session, id, claim)
            updated = copy.deepcopy(row.items)
            updated[index]["status"] = "running"
            row.items = updated
            session.commit()
        from app.api.documents import _process_document_sync
        try:
            _process_document_sync(item["id"], expected_revision=item["revision"])
        except HTTPException as exc:
            if exc.status_code == 409:
                return _finish(id, claim, index, "skipped", "Document changed or another extraction claimed it")
            raise  # Storage failures preserve the running claim for explicit recovery.
        document = repository.get(item["id"])
        if document and document.get("status") in {"extracted", "matched", "pushed"}:
            return _finish(id, claim, index, "succeeded")
        return _finish(id, claim, index, "failed", "Extraction failed; inspect the document before retrying")
    finally:
        current_principal.reset(token)


def recover_run(run_id):
    """Trusted operator only: all servers and workers must already be stopped."""
    with database.SessionLocal() as session:
        row = session.get(BatchExtractionRun, run_id)
        if row is None:
            raise ValueError("Extraction run was not found")
        if row.status == "interrupted":
            return public_run(row)
        if row.status != "running":
            raise ValueError("Only interrupted running jobs can be recovered")
        items = copy.deepcopy(row.items)
        for item in items:
            if item["status"] == "running":
                owned = session.scalar(select(Document.api_id).where(Document.api_id == item["id"],
                                                                      Document.company_id == row.company_id))
                if not owned:
                    raise ValueError("Run document ownership could not be verified")
                document = DocumentRepository().get(item["id"])
                if document.get("qbo_transaction_id") and document.get("status") in {"classifying", "extracting"}:
                    raise ValueError("Financial document is protected")
                if document.get("status") in {"classifying", "extracting"}:
                    from manage_extraction import recover_interrupted
                    recover_interrupted(item["id"])
                    item.update(status="failed", message="Interrupted extraction recovered; explicit retry required")
                elif document.get("status") in {"extracted", "matched", "pushed"}:
                    item.update(status="succeeded", message="Existing completed result preserved")
                else:
                    item.update(status="failed", message="Inspect document before retrying")
            elif item["status"] == "queued":
                item.update(status="not_processed", message="Run was interrupted; start a new request")
        row.items = items
        row.status = "interrupted"
        row.active_key = None
        row.claim_token = None
        row.finished_at = utc_now()
        session.commit()
        return public_run(row)
