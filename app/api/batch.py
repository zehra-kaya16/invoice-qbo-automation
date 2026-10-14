"""Atomic bulk uploads and live status from company-scoped persisted records."""
import hashlib
import json
import uuid
from pydantic import BaseModel
from typing import Annotated

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core import database
from app.core.auth import Principal, company_key, get_current_user
from app.core.config import settings
from app.models.database import Company, Document, UploadBatch

router = APIRouter()
MAX_FILES = 100
MAX_BATCH_BYTES = 100 * 1024 * 1024
ALLOWED_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/heic"}


def _status(session, batch):
    rows = session.execute(select(Document.api_id, Document.filename, Document.status,
                           Document.document_type).where(Document.company_id == batch.company_id,
                           Document.api_id.in_(batch.document_ids))).all()
    by_id = {row.api_id: row for row in rows}
    items = []
    pending = in_progress = completed = failed = 0
    for document_id in batch.document_ids:
        row = by_id.get(document_id)
        state = row.status if row else "missing"
        if state in {"extracted", "matched", "pushed"}:
            completed += 1
        elif state in {"classifying", "extracting", "matching"}:
            in_progress += 1
        elif state == "uploaded":
            pending += 1
        else:
            failed += 1
        items.append({"id": document_id, "filename": row.filename if row else None,
                      "status": state, "document_type": row.document_type if row else None})
    failed += len(batch.failed_files)
    status = ("processing" if in_progress else "pending" if pending else
              "completed" if not failed else "partial" if completed else "failed")
    from app.services.persistence.batch_extraction import latest_run
    extraction_run = latest_run(session, batch.id, batch.company_id)
    return {"extraction_run": extraction_run, "batch_id": batch.id, "total_files": batch.total_files,
            "uploaded_documents": list(batch.document_ids), "failed_files": list(batch.failed_files),
            "completed": completed, "failed": failed, "in_progress": in_progress,
            "pending": pending, "status": status, "items": items,
            "created_at": batch.created_at.isoformat()}


def _existing(session, user, key, fingerprint):
    batch = session.scalar(select(UploadBatch).where(UploadBatch.company_id == user.company_id,
                                                   UploadBatch.request_key == key))
    if batch and batch.fingerprint != fingerprint:
        raise HTTPException(409, "Upload key belongs to different files")
    return batch


@router.post("/upload")
async def batch_upload(
    files: Annotated[list[UploadFile], File()],
    request_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    company_id: str | None = Query(None, min_length=1, max_length=100),
    user: Principal = Depends(get_current_user),
):
    scope = company_key(company_id)
    if not 1 <= len(files) <= MAX_FILES:
        raise HTTPException(400, "Upload between 1 and 100 files")
    key = str(request_key)
    digest = hashlib.sha256()
    prepared = []
    errors = []
    total_bytes = 0
    for index, file in enumerate(files):
        content = await file.read(settings.max_upload_bytes + 1)
        total_bytes += len(content)
        if total_bytes > MAX_BATCH_BYTES:
            raise HTTPException(413, "Batch exceeds 100 MiB; split the upload")
        filename = file.filename or "unknown"
        media = file.content_type or "application/octet-stream"
        digest.update(json.dumps([index, filename, media, len(content)], ensure_ascii=False).encode())
        digest.update(hashlib.sha256(content).digest())
        error = ("Filename exceeds 255 characters" if len(filename) > 255 else
                 "File type is not supported" if media not in ALLOWED_TYPES else
                 "File is empty" if not content else
                 "File exceeds MAX_UPLOAD_BYTES" if len(content) > settings.max_upload_bytes else None)
        if error:
            errors.append({"index": index, "filename": filename[:255], "error": error})
        else:
            prepared.append((filename, media, content))
    return await run_in_threadpool(_persist_batch, user, scope, key, digest.hexdigest(), len(files), prepared, errors)


def _persist_batch(user, scope, key, fingerprint, total_files, prepared, errors):
    try:
        with database.SessionLocal() as session:
            existing = _existing(session, user, key, fingerprint)
            if existing:
                return _status(session, existing)
            # Same persisted document contract as individual upload; all source
            # bytes and the batch membership are committed in one transaction.
            if session.get(Company, user.company_id) is None:
                raise HTTPException(403, "Company is unavailable")
            batch_id = "batch_" + uuid.uuid4().hex
            ids = []
            for filename, media, content in prepared:
                document_id = "doc_" + uuid.uuid4().hex[:12]
                ids.append(document_id)
                state = {"id": document_id, "filename": filename, "company_id": scope,
                         "content_type": media, "status": "uploaded", "document_type": None,
                         "extracted_data": None, "validation": None, "approved": False,
                         "qbo_push_state": "not_started", "qbo_transaction_id": None,
                         "qbo_transaction_type": None, "qbo_push_error": None, "batch_id": batch_id}
                session.add(Document(api_id=document_id, company_id=user.company_id, filename=filename,
                                     content_type=media, file_size=len(content), source_content=content,
                                     status="uploaded", workflow_state=state))
            batch = UploadBatch(id=batch_id, company_id=user.company_id, request_key=key,
                                fingerprint=fingerprint, total_files=total_files, document_ids=ids,
                                failed_files=errors)
            session.add(batch)
            # Unique key arbitrates concurrent retries. No document survives a
            # rollback, including IDs allocated by the losing request.
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = _existing(session, user, key, fingerprint)
                if existing:
                    return _status(session, existing)
                raise
            return _status(session, batch)
    except SQLAlchemyError:
        raise HTTPException(503, "Batch storage unavailable; check history before retrying") from None


@router.post("/upload-zip")
async def batch_upload_zip(
    file: Annotated[UploadFile, File()],
    request_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    company_id: str | None = Query(None, min_length=1, max_length=100),
    user: Principal = Depends(get_current_user),
):
    from app.services.persistence.zip_upload import MAX_ZIP_BYTES, prepare_zip
    scope = company_key(company_id)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "Choose a ZIP file")
    content = await file.read(MAX_ZIP_BYTES + 1)
    if len(content) > MAX_ZIP_BYTES:
        raise HTTPException(413, "Compressed ZIP exceeds 20 MiB")
    # Domain-separated fingerprint prevents a normal upload from colliding
    # with the same key used for a ZIP request. Exact archive retries reuse IDs.
    fingerprint = hashlib.sha256(b"receipt-ai:zip-upload:v1\0" + content).hexdigest()
    total, prepared, errors = await run_in_threadpool(
        prepare_zip, content, MAX_FILES, settings.max_upload_bytes, MAX_BATCH_BYTES)
    return await run_in_threadpool(_persist_batch, user, scope, str(request_key), fingerprint, total, prepared, errors)


class BatchExtractionRequest(BaseModel):
    retry_failed: bool = False


@router.post("/{batch_id}/extract", status_code=202)
def start_batch_extraction(
    batch_id: str, body: BatchExtractionRequest,
    request_key: Annotated[uuid.UUID, Header(alias="Idempotency-Key")],
    user: Principal = Depends(get_current_user),
):
    from app.services.persistence.batch_extraction import enqueue
    return enqueue(batch_id, user, request_key, body.retry_failed)


@router.get("/status/{batch_id}")
def batch_status(batch_id: str, user: Principal = Depends(get_current_user)):
    try:
        with database.SessionLocal() as session:
            batch = session.scalar(select(UploadBatch).where(UploadBatch.id == batch_id,
                                                            UploadBatch.company_id == user.company_id))
            if not batch:
                raise HTTPException(404, "Batch not found")
            return _status(session, batch)
    except SQLAlchemyError:
        raise HTTPException(503, "Batch storage unavailable") from None


@router.get("")
def list_batches(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 user: Principal = Depends(get_current_user)):
    try:
        with database.SessionLocal() as session:
            batches = session.scalars(select(UploadBatch).where(UploadBatch.company_id == user.company_id)
                                      .order_by(UploadBatch.created_at.desc(), UploadBatch.id.desc())
                                      .offset(offset).limit(limit)).all()
            total = session.scalar(select(func.count()).select_from(UploadBatch).where(UploadBatch.company_id == user.company_id))
            return {"total": total, "limit": limit, "offset": offset, "items": [_status(session, b) for b in batches]}
    except SQLAlchemyError:
        raise HTTPException(503, "Batch storage unavailable") from None
