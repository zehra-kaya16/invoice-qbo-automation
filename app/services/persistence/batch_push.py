"""Explicit, sequential bulk push built on the existing single-document workflow.

No Celery replay, blanket mappings, or automatic retry of external writes.
"""
import asyncio
import copy
import json
import uuid
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from app.core import database
from app.core.auth import current_principal
from app.models.database import BatchPushRun, Document, UploadBatch, utc_now
from app.services.persistence.auth_repository import AuthRepository
from app.services.persistence.repository import DocumentRepository, _json_default


@contextmanager
def storage():
    try:
        with database.SessionLocal() as session:
            yield session
    except (IntegrityError, StaleDataError):
        raise HTTPException(409, "Batch changed or another push is active; refresh its status") from None
    except SQLAlchemyError:
        raise HTTPException(503, "Batch push storage is unavailable; read status before continuing") from None


def batch_row(session, batch_id, user):
    row = session.scalar(select(UploadBatch).where(UploadBatch.id == batch_id,
                                                 UploadBatch.company_id == user.company_id))
    if row is None:
        raise HTTPException(404, "Batch not found")
    return row


def active_run(session, batch):
    return session.scalar(select(BatchPushRun).where(BatchPushRun.batch_id == batch.id,
                         BatchPushRun.company_id == batch.company_id, BatchPushRun.active_key == "active"))


def public_run(row):
    return {"run_id": row.id, "batch_id": row.batch_id, "request_key": row.request_key,
            "status": row.status, "items": copy.deepcopy(row.items),
            "created_at": row.created_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


def latest_run(session, batch):
    row = session.scalar(select(BatchPushRun).where(BatchPushRun.batch_id == batch.id,
                         BatchPushRun.company_id == batch.company_id)
                         .order_by(BatchPushRun.created_at.desc(), BatchPushRun.id.desc()).limit(1))
    return public_run(row) if row else None


def public_plans(session, batch):
    # Status lists must not hydrate source_content or bank check-source JSON.
    revisions = dict(session.execute(select(Document.api_id, Document.revision).where(
        Document.company_id == batch.company_id, Document.api_id.in_(batch.document_ids))).all())
    return [dict(copy.deepcopy(plan), stale=revisions.get(id) != plan["document_revision"])
            for id, plan in (batch.qbo_plans or {}).items()]


def load_document(id, expected_revision, batch_id=None):
    from app.api.documents import _load_document
    doc = _load_document(id)
    if doc.get("_revision") != expected_revision:
        raise HTTPException(409, f"{id}: document changed; review and save a new plan")
    if batch_id and doc.get("batch_id") != batch_id:
        raise HTTPException(404, "Document does not belong to this batch")
    if doc.get("status") not in {"extracted", "matched"}:
        raise HTTPException(409, f"{id}: not ready for push; extract/review it or use recovery for incomplete results")
    return doc


def preview(id, body, batch_id):
    from app.api import documents, qbo
    from app.schemas.documents import DocumentType
    from app.services.persistence.check_sources import check_image_bytes, CheckSourceError
    doc = load_document(id, body["expected_revision"], batch_id)
    if doc.get("document_type") == DocumentType.BANK_STATEMENT:
        if body["create_as"] != "bank_statement":
            raise HTTPException(400, "A bank statement requires its reviewed bank workflow plan")
        snapshot = documents.get_bank_workflow(id)
        if not snapshot["push_ready"]:
            raise HTTPException(409, {"message": "Complete bank review before saving its plan", "blockers": snapshot["blockers"]})
        client = qbo._get_connected_qbo_client()
        documents._ensure_bank_realm(doc, client, body["expected_realm_id"])
        for item in doc.get("check_match_results", []):
            if item.get("matched") and not item.get("attachment_id"):
                try:
                    check_image_bytes(doc, item.get("image_path"))
                except CheckSourceError as exc:
                    raise HTTPException(409, str(exc)) from None
        result = {"realm_id": snapshot["realm_id"], "document_id": id,
                  "create_as": "bank_statement", "bank_account": snapshot["bank_account"],
                  "plan": snapshot["plan"]}
    else:
        if not doc.get("content"):
            raise HTTPException(409, "Original document source is missing; no QBO write was started")
        if not (doc.get("validation") or {}).get("is_valid"):
            raise HTTPException(409, "Document validation must be valid before batch push")
        request = qbo.QBOPlanRequest(**{k: body.get(k) for k in qbo.QBOPlanRequest.model_fields})
        result = asyncio.run(qbo.build_invoice_qbo_plan(id, request)).model_dump(mode="json")
        if result.get("attachment") is None:
            result.pop("attachment", None)  # preserve saved step-107 plans for other source formats
    load_document(id, body["expected_revision"], batch_id)  # remote reads may take time
    return json.loads(json.dumps(result, default=_json_default))


def save_plan(batch_id, id, body, user):
    with storage() as session:
        batch = batch_row(session, batch_id, user)
        if id not in batch.document_ids:
            raise HTTPException(404, "Document does not belong to this batch")
        if active_run(session, batch):
            raise HTTPException(409, "A push is active; saved plans are locked")
        result = preview(id, body, batch_id)
        plan = {"plan_id": "plan_" + uuid.uuid4().hex, "document_id": id,
                "document_revision": body["expected_revision"], "realm_id": body["expected_realm_id"],
                "body": copy.deepcopy(body), "preview": result, "saved_at": utc_now().isoformat()}
        plans = copy.deepcopy(batch.qbo_plans or {}); plans[id] = plan; batch.qbo_plans = plans
        session.commit()  # version check fences concurrent saves and push-start
        return dict(plan, stale=False)


def selection_key(selection):
    return sorted(selection, key=lambda item: item["document_id"])


def find_previous(session, user, key, batch_id, selection):
    prior = session.scalar(select(BatchPushRun).where(BatchPushRun.company_id == user.company_id,
                                                    BatchPushRun.request_key == key))
    if prior and (prior.batch_id != batch_id or prior.selection != selection):
        raise HTTPException(409, "Push request key belongs to a different batch or selection")
    return prior


def prepare(batch_id, selection, user, key):
    selection = selection_key(selection)
    if len({item["document_id"] for item in selection}) != len(selection):
        raise HTTPException(400, "Select each document only once")
    with storage() as session:
        batch = batch_row(session, batch_id, user)
        previous = find_previous(session, user, key, batch_id, selection)
        if previous:
            return public_run(previous), None
        if active_run(session, batch):
            raise HTTPException(409, "A batch push is active; inspect its run before continuing")
        plans = []
        # Check EVERY selected plan before starting ANY external write.
        for item in selection:
            plan = (batch.qbo_plans or {}).get(item["document_id"])
            if not plan or plan["plan_id"] != item["plan_id"] or item["document_id"] not in batch.document_ids:
                raise HTTPException(409, "A saved plan changed; refresh the batch and select again")
            result = preview(item["document_id"], plan["body"], batch_id)
            if result != plan["preview"]:
                raise HTTPException(409, f"{item['document_id']}: QBO mapping changed; save and review a new plan")
            plans.append(copy.deepcopy(plan))
        row = BatchPushRun(id="push_" + uuid.uuid4().hex, batch_id=batch_id, company_id=user.company_id,
                           request_key=key, selection=selection, active_key="active", status="running",
                           items=[{"id": p["document_id"], "status": "queued", "message": None,
                                   "document_revision": p["document_revision"], "transaction_id": None,
                                   "transaction_type": None, "attachment_id": None} for p in plans])
        session.add(row)
        batch.revision += 1  # a concurrent plan save must lose its CAS, or this start must lose
        session.commit()
        return public_run(row), plans


def read_run(batch_id, id, user):
    with storage() as session:
        batch_row(session, batch_id, user)
        row = session.scalar(select(BatchPushRun).where(BatchPushRun.id == id, BatchPushRun.batch_id == batch_id,
                                                       BatchPushRun.company_id == user.company_id))
        if not row:
            raise HTTPException(404, "Push run not found")
        return public_run(row)


def checkpoint(id, user, index=None, values=None, finish=None):
    with storage() as session:
        row = session.scalar(select(BatchPushRun).where(BatchPushRun.id == id,
                                                       BatchPushRun.company_id == user.company_id))
        if not row or row.active_key != "active":
            raise HTTPException(409, "Push run no longer active")
        items = copy.deepcopy(row.items)
        if index is not None:
            items[index].update(values)
        if finish:
            for item in items:
                if item["status"] == "queued":
                    item.update(status="not_started", message="Stopped before this document; no write was attempted")
            row.status = finish; row.active_key = None; row.finished_at = utc_now()
        row.items = items
        session.commit()
        return public_run(row)


def document_result(id):
    doc = DocumentRepository().get(id)
    if not doc:
        return {}, False
    fields = {"transaction_id": doc.get("qbo_transaction_id"),
              "transaction_type": doc.get("qbo_transaction_type"), "doc_number": doc.get("qbo_doc_number"), "attachment_id": doc.get("qbo_attachment_id")}
    if doc.get("document_type") == "bank_statement":
        fields["bank_results"] = {
            "categories": [{k: c.get(k) for k in ("qbo_transaction_id", "qbo_category_push_status", "qbo_category_account_id")}
                           for c in doc.get("category_suggestions") or []],
            "checks": [{k: c.get(k) for k in ("qbo_transaction_id", "qbo_transaction_type", "attachment_id", "attachment_push_status")}
                       for c in doc.get("check_match_results") or []]}
        done = doc.get("status") == "pushed"
    else:
        done = doc.get("status") == "pushed" and doc.get("qbo_push_state") == "succeeded" and bool(doc.get("qbo_attachment_id")) and not doc.get("qbo_doc_number_needs_review")
    return fields, done


def push(batch_id, selection, user, key):
    from app.api.documents import push_to_qbo, PushToQBORequest
    result, plans = prepare(batch_id, selection, user, key)
    if plans is None:
        return result  # replay only returns persisted status, including an interrupted run
    id = result["run_id"]
    for index, plan in enumerate(plans):
        actor = AuthRepository().principal(user.session_hash)
        if actor is None or actor.user_id != user.user_id or actor.company_id != user.company_id:
            return checkpoint(id, user, finish="cancelled")
        token = current_principal.set(actor)
        try:
            # Also recheck immediately before each document, after preceding writes.
            fresh = preview(plan["document_id"], plan["body"], batch_id)
            if fresh != plan["preview"]:
                raise HTTPException(409, "QBO plan changed during batch push")
            checkpoint(id, user, index, {"status": "sending"})
            response = asyncio.run(push_to_qbo(plan["document_id"], PushToQBORequest(**plan["body"])))
            fields, done = document_result(plan["document_id"])
            if not response.success or not done:
                fields.update(status="needs_review", message="QBO result or attachment needs recovery; no automatic retry")
                checkpoint(id, user, index, fields)
                return checkpoint(id, user, finish="needs_review")
            fields.update(status="succeeded", message=None)
            checkpoint(id, user, index, fields)
        except Exception as exc:
            # A failed response is not proof that the provider did not write.
            fields, done = document_result(plan["document_id"])
            doc = DocumentRepository().get(plan["document_id"])
            started = bool(doc and (doc.get("qbo_push_state") in {"in_progress", "uncertain", "succeeded"}
                                   or any(c.get("qbo_category_push_status") in {"in_progress", "uncertain", "succeeded", "pushed"}
                                          for c in doc.get("category_suggestions") or [])
                                   or any(c.get("attachment_push_status") in {"uploading", "uncertain", "uploaded"}
                                          for c in doc.get("check_match_results") or [])))
            status = "needs_review" if started else "blocked"
            fields.update(status=status, message=str(exc.detail) if isinstance(exc, HTTPException) else "Push failed; inspect document recovery")
            checkpoint(id, user, index, fields)
            return checkpoint(id, user, finish=status)
        finally:
            current_principal.reset(token)
    return checkpoint(id, user, finish="completed")


def recover_run(id):
    """Operator-only, all API/worker processes stopped. Never calls QBO."""
    with storage() as session:
        row = session.get(BatchPushRun, id)
        if row is None:
            raise ValueError("Push run not found")
        if row.active_key is None:
            return public_run(row)
        items = copy.deepcopy(row.items)
        old = current_principal.set(None)  # trusted operator spans tenants, not an HTTP endpoint
        try:
            for item in items:
                if item["status"] == "queued":
                    item.update(status="not_started", message="Server stopped before this document")
                elif item["status"] == "sending":
                    doc = DocumentRepository().get(item["id"])
                    owner = session.scalar(select(Document.company_id).where(Document.api_id == item["id"]))
                    if not doc or owner != row.company_id:
                        raise ValueError("Document missing or company ownership changed")
                    fields, done = document_result(item["id"])
                    untouched = doc.get("_revision") == item["document_revision"]
                    item.update(fields, status="succeeded" if done else "not_started" if untouched else "needs_review",
                                message=None if done else "Inspect persisted document result before continuing")
        finally:
            current_principal.reset(old)
        row.items = items; row.status = "interrupted"; row.active_key = None; row.finished_at = utc_now()
        session.commit()
        return public_run(row)
