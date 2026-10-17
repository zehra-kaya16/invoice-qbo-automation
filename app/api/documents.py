from urllib.parse import quote
from starlette.concurrency import run_in_threadpool
from app.services.persistence.check_sources import check_image_bytes, check_image_file, check_image_filename, check_image_content_type, CheckSourceError
from app.core.auth import company_key as authorized_company_key, current_principal
from datetime import datetime, timezone
import uuid
import copy
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings

from app.services.persistence.repository import (
    DocumentConflictError,
    DocumentRepository,
)

from app.schemas.documents import RECONCILIATION_TYPES
from app.schemas.documents import (
    BankMatchReviewRequest,
    BankStatementData,
    CategoryReviewRequest,
    CheckData,
    DocumentType,
    DocumentUploadResponse,
    ExtractionResponse,
    ProcessingStatus,
    PushToQBOResponse,
    ReceiptData,
)
from app.services.categorization.categorizer import (
    TransactionCategorizer,
)
from app.services.matching.matcher import (
    BankFeedMatcher,
)
from app.services.qbo.currency_guard import check_document_currency
from app.services.qbo.mapper import build_qbo_create_plan
from app.services.qbo.push_guard import (
    QBOPushGuardError,
    ensure_invoice_can_start_push,
    initialize_qbo_push_state,
    mark_invoice_push_started,
    mark_invoice_push_succeeded,
    mark_invoice_push_uncertain,
)
from app.services.validation.invoice_validator import validate_invoice

router = APIRouter()

document_repository = DocumentRepository()


def _load_document(document_id: str, *, load_sources: bool = True) -> dict:
    try:
        document = document_repository.get(document_id) if load_sources else document_repository.get(document_id, load_sources=False)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Document database is unavailable") from exc
    if document is None:
        raise HTTPException(404, "Document not found")
    return document


def _save_document(document: dict) -> None:
    try:
        document_repository.save(document)
    except DocumentConflictError as exc:
        raise HTTPException(
            409, "Document changed; reload before trying again"
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Document database is unavailable") from exc


def _ensure_workflow_editable(document: dict) -> None:
    if document.get("status") in {ProcessingStatus.CLASSIFYING, ProcessingStatus.EXTRACTING} and document.get("extraction_claim"):
        raise HTTPException(409, "Extraction is already claimed; wait or recover the interrupted job with the server stopped")
    protected = document.get("qbo_push_state") in {
        "in_progress", "succeeded", "uncertain"
    }
    protected = protected or document.get("status") == ProcessingStatus.PUSHED
    protected = protected or any(
        item.get("qbo_category_push_status") in {"pushing", "pushed", "uncertain"}
        for item in document.get("category_suggestions", [])
    )
    protected = protected or any(
        item.get("attachment_id")
        or item.get("attachment_push_status") in {"uploading", "uncertain"}
        for item in document.get("check_match_results", [])
    )
    if protected:
        raise HTTPException(
            409, "Workflow cannot be changed after a QBO write has started"
        )

def _ensure_document_can_be_extracted(document: dict) -> None:
    if (
        document.get("status") == ProcessingStatus.PUSHED
        or document.get("qbo_push_state")
        in {"in_progress", "succeeded", "uncertain"}
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Document cannot be extracted while its QBO push "
                "is in progress, completed, or uncertain."
            ),
        )
    _ensure_workflow_editable(document)

def _ensure_bank_realm(document: dict, client, expected_realm_id=None) -> None:
    realm = getattr(client, "realm_id", None)
    stored = document.get("qbo_match_realm_id")
    if (expected_realm_id is not None and expected_realm_id != realm) or (stored and stored != realm):
        raise HTTPException(409, "QBO company differs from bank matching; rematch before writing")


@router.get("/{document_id}/bank-workflow")
def get_bank_workflow(document_id: str):
    """Read a detached bank review snapshot; never call QBO or save it."""
    document = _load_document(document_id, load_sources=False)
    if document.get("document_type") not in RECONCILIATION_TYPES:
        raise HTTPException(400, "Bank workflow requires a bank statement")
    locked = False
    try:
        _ensure_workflow_editable(document)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        locked = True
    snapshot = copy.deepcopy(document)
    blockers = []
    categories = []
    checks = []
    try:
        _prepare_reviewed_bank_statement_matches(snapshot)
        _validate_bank_statement_vendors_for_push(snapshot)
        _validate_bank_statement_check_results(snapshot)
        categories = _prepare_reviewed_bank_statement_categories(snapshot)
        checks = [item for item in snapshot.get("check_match_results", []) if item.get("matched")]
    except HTTPException as exc:
        blockers.append(exc.detail)
    return {
        "id": document_id, "revision": document.get("_revision"),
        "realm_id": document.get("qbo_match_realm_id"),
        "bank_account": document.get("qbo_match_bank_account"),
        "status": document.get("status"), "locked": locked,
        "matches": document.get("qbo_match_results"),
        "categories": document.get("category_suggestions"),
        "checks": [{key: value for key, value in item.items() if key != "image_path"}
                   for item in snapshot.get("check_match_results", [])],
        "push_ready": not blockers and not locked and bool(document.get("qbo_match_realm_id")),
        "blockers": blockers,
        "plan": {"category_updates": categories, "check_attachments": [
            {key: value for key, value in item.items() if key != "image_path"} for item in checks]},
    }


class DocumentStatus(BaseModel):
    qbo_doc_number: Optional[str] = None
    qbo_doc_number_needs_review: bool = False
    batch_id: Optional[str] = None
    id: str
    filename: str
    status: ProcessingStatus
    document_type: Optional[DocumentType] = None
    storage_key: Optional[str] = None
    error: Optional[str] = None

    qbo_push_state: Optional[str] = None
    qbo_transaction_id: Optional[str] = None
    qbo_transaction_type: Optional[str] = None
    qbo_push_error: Optional[str] = None
    qbo_attachment_id: Optional[str] = None
    qbo_attachment_error: Optional[str] = None

class PushToQBORequest(BaseModel):
    expected_revision: Optional[int] = None
    expected_realm_id: Optional[str] = None
    create_as: str = "bill"

    vendor_id: Optional[str] = None
    account_id: Optional[str] = None

    description: Optional[str] = None
    bank_account_id: Optional[str] = None
    attach_documents: bool = True

class MatchToQBORequest(BaseModel):
    expected_realm_id: Optional[str] = None
    bank_account_id: str


class ReconciliationEditRequest(BaseModel):
    expected_revision: int
    check_data: Optional[CheckData] = None
    statement_data: Optional[BankStatementData] = None


@router.patch("/{document_id}/reconciliation-data")
def edit_reconciliation_data(document_id: str, request: ReconciliationEditRequest):
    document = _load_document(document_id)
    if document.get("document_type") not in {DocumentType.CHECK, DocumentType.CREDIT_CARD_STATEMENT}:
        raise HTTPException(400, "This editor requires a check or credit-card statement")
    if document.get("_revision") != request.expected_revision:
        raise HTTPException(409, "Document changed; reload before editing")
    _ensure_workflow_editable(document)
    if document.get("status") not in {ProcessingStatus.EXTRACTED, ProcessingStatus.MATCHED, ProcessingStatus.FAILED}:
        raise HTTPException(409, "Wait for extraction before editing")
    from app.services.extraction.reconciliation import prepare_standalone_check, validate_reconciliation
    if document.get("document_type") == DocumentType.CHECK:
        if request.check_data is None or request.statement_data is not None:
            raise HTTPException(422, "Supply check fields")
        try:
            document["extracted_data"] = prepare_standalone_check(document, request.check_data)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    else:
        if request.statement_data is None or request.check_data is not None:
            raise HTTPException(422, "Supply credit-card statement fields")
        document["extracted_data"] = request.statement_data.model_dump()
        validate_reconciliation(document)
    for key in ("qbo_match_results", "check_match_results", "category_suggestions", "qbo_match_realm_id", "qbo_match_bank_account"):
        document.pop(key, None)
    document.update(status=ProcessingStatus.EXTRACTED, error=None, approved=False)
    _save_document(document)
    return _build_extraction_response(document)

def _qbo_attachment_entity_type(
    transaction_type: str,
) -> Optional[str]:

    mapping = {
        "purchase": "Purchase",
        "deposit": "Deposit",
        "transfer": "Transfer",
        "bill_payment": "BillPayment",
        "payment": "Payment",
    }

    return mapping.get(
        transaction_type
    )

@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    company_id: Optional[str] = None,
    auto_process: bool = True,
    background_tasks: BackgroundTasks = None,
):
        company_id = authorized_company_key(company_id)
        allowed_types = [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/heic",
    ]

        content_type = file.content_type or "application/octet-stream"

        if content_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File type '{content_type}' not supported. "
                    "Use PDF, PNG, JPG, or HEIC."
                ),
            )

        document_id = f"doc_{uuid.uuid4().hex[:12]}"

        content = await file.read(settings.max_upload_bytes + 1)
        if not content:
            raise HTTPException(400, "Uploaded file is empty")
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "Uploaded file exceeds MAX_UPLOAD_BYTES")

        document = {
            "id": document_id,
            "filename": file.filename or "unknown",
            "content_type": content_type,
            "content": content,
            "company_id": company_id,
            "status": ProcessingStatus.UPLOADED,
            "document_type": None,
            "extracted_data": None,
            "validation": None,
            "approved": False,
            "qbo_push_state": "not_started",
            "qbo_transaction_id": None,
            "qbo_transaction_type": None,
            "qbo_push_error": None,
        }
        await run_in_threadpool(_save_document, document)
        if auto_process and background_tasks:
            background_tasks.add_task(
                _process_document_owned,
                document_id,
                current_principal.get(),
            )

        return DocumentUploadResponse(
            id=document_id,
            filename=file.filename or "unknown",
            status=ProcessingStatus.UPLOADED,
            message=(
                "Document uploaded successfully. Processing started."
                if auto_process
                else
                "Document uploaded. Call /extract to process."
            ),
        )

class DocumentListItem(BaseModel):
    id: str
    filename: str
    status: ProcessingStatus
    document_type: Optional[DocumentType] = None
    error: Optional[str] = None


class DocumentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DocumentListItem]


# Static collection route must precede /{document_id}.
@router.get("", response_model=DocumentListResponse)
async def list_documents(
    company_id: Optional[str] = Query(None, min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    try:
        return document_repository.list_summaries(authorized_company_key(company_id), limit, offset)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Document database is unavailable") from exc


@router.get("/{document_id}/source")
async def get_document_source(document_id: str):
    document = await run_in_threadpool(_load_document, document_id)
    content = document.get("content")
    if not isinstance(content, bytes) or not content:
        raise HTTPException(404, "Document source is unavailable")
    # Only supported document media types may render inline.
    media_type = document.get("content_type")
    allowed = {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/heic"}
    if media_type not in allowed:
        media_type = "application/octet-stream"
    return Response(content=content, media_type=media_type, headers={
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline" if media_type in allowed else "attachment",
    })


@router.get("/{document_id}/source/preview")
def get_document_preview(document_id: str):
    from app.services.extraction.heic import heic_to_png, is_heic, HEICImageError
    document = _load_document(document_id)
    content = document.get("content")
    if not isinstance(content, bytes) or not content:
        raise HTTPException(404, "Document source is unavailable")
    if document.get("content_type") != "image/heic" and not is_heic(content):
        raise HTTPException(415, "Converted preview is available only for HEIC images")
    try:
        png = heic_to_png(content)
    except HEICImageError as exc:
        raise HTTPException(422, str(exc)) from None
    return Response(content=png, media_type="image/png", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


@router.get("/{document_id}/checks/{check_index}/source")
def get_check_source(document_id: str, check_index: int):
    document = _load_document(document_id)
    checks = (document.get("extracted_data") or {}).get("check_images", [])
    if document.get("document_type") not in RECONCILIATION_TYPES or check_index < 0 or check_index >= len(checks):
        raise HTTPException(404, "Check source was not found")
    try:
        content = check_image_bytes(document, checks[check_index].get("image_path"))
    except CheckSourceError as exc:
        raise HTTPException(409, str(exc)) from None
    filename=check_image_filename(document, checks[check_index].get("image_path"))
    return Response(content=content, media_type=check_image_content_type(document, checks[check_index].get("image_path")), headers={"Cache-Control":"no-store", "Content-Disposition":"inline; filename*=UTF-8\'\'" + quote(filename, safe="")})


@router.get("/{document_id}", response_model=DocumentStatus)
async def get_document(document_id: str):
    document = _load_document(document_id, load_sources=False)

    return DocumentStatus(
        qbo_doc_number=document.get("qbo_doc_number"),
        qbo_doc_number_needs_review=document.get("qbo_doc_number_needs_review", False),
        batch_id=document.get("batch_id"),
        id=document["id"],
        filename=document["filename"],
        status=document["status"],
        document_type=document.get("document_type"),
        storage_key=document.get("storage_key"),
        error=document.get("error"),
        qbo_push_state=document.get("qbo_push_state"),
        qbo_transaction_id=document.get("qbo_transaction_id"),
        qbo_transaction_type=document.get("qbo_transaction_type"),
        qbo_push_error=document.get("qbo_push_error"),
        qbo_attachment_id=document.get("qbo_attachment_id"),
        qbo_attachment_error=document.get("qbo_attachment_error"),
    )

@router.post(
    "/{document_id}/extract",
    response_model=ExtractionResponse,
)
async def extract_document(document_id: str):

    document = _load_document(document_id)

    _ensure_document_can_be_extracted(document)

    if document["status"] == ProcessingStatus.EXTRACTED:
        return _build_extraction_response(document)

    await process_document(document_id)

    document = _load_document(document_id)

    return _build_extraction_response(document)

async def _process_document_owned(document_id, principal):
    # Background task ownership does not depend on dependency cleanup timing.
    token = current_principal.set(principal)
    try:
        await process_document(document_id)
    finally:
        current_principal.reset(token)


async def process_document(document_id: str):
    await run_in_threadpool(_process_document_sync, document_id)


def _process_document_sync(document_id: str, expected_revision: int | None = None):

    document = _load_document(document_id)

    if expected_revision is not None and document.get("_revision") != expected_revision:
        raise HTTPException(409, "Document changed after extraction was queued")

    _ensure_document_can_be_extracted(document)

    extraction_clock = time.perf_counter()
    try:
        document["extraction_claim"] = uuid.uuid4().hex
        document["extraction_started_at"] = datetime.now(timezone.utc).isoformat()
        document["extracted_data"] = None
        for key in ("check_image_sources", "qbo_match_results", "check_match_results", "category_suggestions", "qbo_match_realm_id", "qbo_match_bank_account"):
            document.pop(key, None)
        document["status"] = ProcessingStatus.CLASSIFYING
        document["error"] = None
        document["approved"] = False
        document["validation"] = None
        _save_document(document)

        # Settings reads .env; os.environ alone misses .env-only keys.
        openai_key = os.getenv("OPENAI_API_KEY") or settings.openai_api_key
        anthropic_key = os.getenv("ANTHROPIC_API_KEY") or settings.anthropic_api_key
        provider = settings.ai_provider or ("openai" if openai_key else "anthropic")
        api_key = openai_key if provider == "openai" else anthropic_key

        if not api_key:
            document["status"] = (
                ProcessingStatus.FAILED
            )

            document["error"] = (
                "No AI API key configured" if not openai_key and not anthropic_key else f"No AI API key configured for {provider}"
            )

            return


        from app.services.extraction.extractor import (
            DocumentExtractor,
        )

        extractor = DocumentExtractor(
            api_key=api_key,
            provider=provider,
        )

        content = document["content"]
        content_type = document.get("content_type")

        if content_type == "application/pdf":
            page_images = extractor.pdf_to_page_images(
                content
            )

            if not page_images:
                raise ValueError(
                    "PDF contains no readable pages."
                )
        else:
            if content_type == "image/heic":
                from app.services.extraction.heic import heic_to_png
                content = heic_to_png(content)
            page_images = [content]

        document_type = extractor.classify_document(
            page_images[0]
        )

        document["document_type"] = document_type
        document["status"] = ProcessingStatus.EXTRACTING

        if document_type in (
            DocumentType.RECEIPT,
            DocumentType.INVOICE,
        ):
            extracted = extractor.extract_receipt(
                page_images[0] if len(page_images) == 1 else page_images
            )

            document["validation"] = validate_invoice(extracted)

            document["extracted_data"] = (
                extracted.model_dump()
            )

        elif document_type == DocumentType.BANK_STATEMENT:
            extracted = extractor.extract_bank_statement(
                page_images=page_images,
                document_id=document_id,
            )

            document["extracted_data"] = (
                extracted.model_dump()
            )

        elif document_type == DocumentType.CREDIT_CARD_STATEMENT:
            from app.services.extraction.reconciliation import extract_credit_card_statement
            document["extracted_data"] = extract_credit_card_statement(extractor, page_images).model_dump()
        elif document_type == DocumentType.CHECK:
            from app.services.extraction.reconciliation import prepare_standalone_check
            extracted = extractor.extract_check(page_images[0] if len(page_images) == 1 else page_images)
            document["extracted_data"] = extracted.model_dump()
            if extracted.direction is not None:
                document["extracted_data"] = prepare_standalone_check(document, extracted)

        else:
            raise ValueError("Unsupported document type; extraction was not completed")

        document["status"] = ProcessingStatus.EXTRACTED

    except HTTPException:
        raise
    except Exception as exc:
        document["status"] = ProcessingStatus.FAILED
        document["error"] = str(exc)
    finally:
        document.pop("extraction_claim", None)
        document["extraction_duration_ms"] = (time.perf_counter() - extraction_clock) * 1000
        _save_document(document)

def _build_extraction_response(
    document: dict,
) -> ExtractionResponse:

    response = ExtractionResponse(
        id=document["id"],
        document_type=document.get("document_type") or DocumentType.UNKNOWN,
        status=document["status"],
        validation=document.get("validation"),
        approved=document.get("approved", False),
    )

    extracted = document.get("extracted_data")

    if not extracted:
        return response

    if document.get("document_type") in (
        DocumentType.RECEIPT,
        DocumentType.INVOICE,
    ):
        response.receipt_data = ReceiptData(
            **extracted
        )

    elif (
        document.get("document_type")
        in RECONCILIATION_TYPES
    ):
        response.bank_statement_data = BankStatementData(**extracted)
        if document.get("document_type") == DocumentType.CHECK:
            response.check_data = CheckData(**extracted)
        elif document.get("document_type") == DocumentType.CREDIT_CARD_STATEMENT:
            response.credit_card_statement_data = response.bank_statement_data

    return response


@router.get(
    "/{document_id}/extracted",
    response_model=ExtractionResponse,
)
async def get_extracted_data(document_id: str):

    document = _load_document(document_id, load_sources=False)

    if document["status"] not in [
        ProcessingStatus.EXTRACTED,
        ProcessingStatus.MATCHED,
        ProcessingStatus.PUSHED,
    ] and not (document.get("status") == ProcessingStatus.FAILED and document.get("document_type") in {DocumentType.CHECK, DocumentType.CREDIT_CARD_STATEMENT}):
        raise HTTPException(
            status_code=400,
            detail=(
                "Document not yet extracted. "
                f"Current status: {document['status']}"
            ),
        )

    return _build_extraction_response(document)

@router.patch("/{document_id}/extracted", response_model=ExtractionResponse)
async def update_extracted_data(
    document_id: str,
    receipt_data: ReceiptData,
):
    """
    Update reviewed invoice or receipt fields and re-run validation.
    """

    doc = _load_document(document_id)

    if doc.get("document_type") not in (DocumentType.INVOICE, DocumentType.RECEIPT):
        raise HTTPException(
            status_code=400,
            detail="Review editing is currently available only for invoices and receipts",
        )

    if doc.get("status") != ProcessingStatus.EXTRACTED:
        raise HTTPException(
            status_code=400,
            detail=(
                "Document must be extracted before it can be reviewed. "
                f"Current status: {doc.get('status')}"
            ),
        )

    if not doc.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail="Document has no extracted data to review",
        )

    # Only fields actually sent by the user are treated as updates.
    _ensure_workflow_editable(doc)

    updates = receipt_data.model_dump(
        exclude_unset=True,
    )

    # Preserve existing extracted fields that the user did not edit.
    current_data = doc["extracted_data"]

    merged_data = {
        **current_data,
        **updates,
    }

    # Re-validate the merged structure through the existing Pydantic model.
    updated_receipt = ReceiptData(
        **merged_data
    )

    # Store the corrected data.
    doc["extracted_data"] = updated_receipt.model_dump()

    # Re-run deterministic invoice validation.
    doc["validation"] = validate_invoice(
        updated_receipt
    )

    doc["approved"] = False
    _save_document(doc)

    return _build_extraction_response(doc)

@router.post("/{document_id}/approve", response_model=ExtractionResponse)
async def approve_document(document_id: str):
    """
    Approve a reviewed and valid invoice or receipt.
    """

    doc = _load_document(document_id)

    if doc.get("document_type") not in (DocumentType.INVOICE, DocumentType.RECEIPT):
        raise HTTPException(
            status_code=400,
            detail="Approval is currently available only for invoices and receipts",
        )

    if doc.get("status") != ProcessingStatus.EXTRACTED:
        raise HTTPException(
            status_code=400,
            detail=(
                "Document must be extracted before approval. "
                f"Current status: {doc.get('status')}"
            ),
        )

    if not doc.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail="Document has no extracted data to approve",
        )

    _ensure_workflow_editable(doc)

    validation = doc.get("validation")

    if validation is None:
        raise HTTPException(
            status_code=400,
            detail="Invoice/receipt has not been validated",
        )

    if not validation.get("is_valid", False):
        raise HTTPException(
            status_code=400,
            detail="Invoice/receipt cannot be approved because validation failed",
        )

    doc["approved"] = True

    _save_document(doc)

    return _build_extraction_response(doc)

def _validate_bank_statement_check_results(document: dict) -> list[dict]:
    results = document.get("check_match_results")
    if results is None:
        raise HTTPException(
            400, "Check matching has not been completed. Run match-to-qbo first."
        )
    if not isinstance(results, list) or any(
        not isinstance(item, dict) for item in results
    ):
        raise HTTPException(400, "Invalid check match results")

    extracted = document.get("extracted_data") or {}
    images = extracted.get("check_images")
    if not isinstance(images, list) or any(
        not isinstance(image, dict) for image in images
    ):
        raise HTTPException(400, "Extracted check image data is missing or invalid")
    if len(results) != len(images):
        raise HTTPException(
            400, "Check match results do not cover all extracted check images"
        )

    # match-to-qbo creates one result per image with its original index/path.
    if any(type(item.get("check_index")) is not int for item in results):
        raise HTTPException(400, "Invalid check match indexes")
    by_index = {item["check_index"]: item for item in results}
    if len(by_index) != len(results):
        raise HTTPException(400, "Duplicate check match indexes")
    for index, image in enumerate(images):
        result = by_index.get(index)
        if (
            result is None
            or not image.get("image_path")
            or result.get("image_path") != image["image_path"]
        ):
            raise HTTPException(400, "Check image and match result do not agree")
    return results


def _push_bank_statement_checks(
    document_id: str,
    document: dict,
    client,
    persist=None,
) -> PushToQBOResponse:

    check_results = _validate_bank_statement_check_results(document)

    eligible_checks = [
        item
        for item in check_results
        if item.get("matched")
    ]

    # check_match_results exists, but after review
    # there may be no eligible checks left to upload.
    # This is a valid no-op, for example when the
    # check-related transaction was rejected.
    if not eligible_checks:
        document["status"] = ProcessingStatus.PUSHED

        if persist is not None:
            persist(document)

        return PushToQBOResponse(
            id=document_id,
            success=True,
            transactions_pushed=0,
            attachments_uploaded=0,
            vendors_created=0,
            errors=[],
        )

    attachments_uploaded = 0
    errors = []

    for item in eligible_checks:

        # Already uploaded:
        # do not create a duplicate attachment.
        if item.get("attachment_id"):
            continue

        if item.get("attachment_push_status") in {"uploading", "uncertain"}:
            raise HTTPException(
                409, "Previous check attachment outcome needs manual verification"
            )

        image_path = item.get(
            "image_path"
        )

        transaction_id = item.get(
            "qbo_transaction_id"
        )

        transaction_type = item.get(
            "qbo_transaction_type"
        )

        entity_type = (
            _qbo_attachment_entity_type(
                transaction_type
            )
        )

        if not image_path:
            message = (
                "Check image path is missing"
            )

            item["attachment_error"] = (
                message
            )

            errors.append(message)
            continue

        path = Path(
            image_path
        )

        if not transaction_id:
            message = (
                "Matched QBO transaction ID "
                "is missing"
            )

            item["attachment_error"] = (
                message
            )

            errors.append(message)
            continue

        if entity_type is None:
            message = (
                "Unsupported QBO transaction "
                f"type for attachment: "
                f"{transaction_type}"
            )

            item["attachment_error"] = (
                message
            )

            errors.append(message)
            continue

        item["attachment_marker"] = item.get("attachment_marker") or "receipt-ai:" + uuid.uuid4().hex + ":check"
        item["attachment_started_at"] = datetime.now(timezone.utc).isoformat()
        item["attachment_push_status"] = "uploading"
        if persist is not None:
            persist(document)

        try:
            with check_image_file(document, image_path) as source_path:
                result = client.upload_attachment(
                    file_path=source_path,
                    file_name=check_image_filename(document, image_path),
                    content_type=check_image_content_type(document, image_path),
                    entity_type=entity_type,
                    entity_id=str(transaction_id),
                    recovery_marker=item["attachment_marker"],
                )

            if not result.get("id"):
                raise RuntimeError("QBO attachment returned no ID")

            item["attachment_push_status"] = "uploaded"
            item["attachment_id"] = (
                result["id"]
            )

            item["attachment_error"] = None

            if persist is not None:
                persist(document)

            attachments_uploaded += 1

        except Exception as exc:
            item["attachment_push_status"] = "uncertain"
            message = (
                f"{path.name}: {exc}"
            )

            item["attachment_error"] = (
                message
            )

            if persist is not None:
                persist(document)

            errors.append(
                message
            )

    document[
        "check_match_results"
    ] = check_results

    all_uploaded = all(
        item.get("attachment_id")
        for item in eligible_checks
    )

    if all_uploaded:
        document["status"] = (
            ProcessingStatus.PUSHED
        )

    if persist is not None:
        persist(document)

    return PushToQBOResponse(
        id=document_id,
        success=(
            len(errors) == 0
        ),
        transactions_pushed=0,
        attachments_uploaded=(
            attachments_uploaded
        ),
        vendors_created=0,
        errors=errors,
    )

def _validate_bank_statement_vendors_for_push(
    document: dict,
) -> None:
    check_match_results = document.get(
        "check_match_results",
        [],
    )

    unresolved_vendors = []

    for check_match in check_match_results:
        vendor_status = check_match.get(
            "vendor_status",
            "not_checked",
        )

        if vendor_status in (
            "matched",
            "no_vendor_name",
            "skipped_rejected",
        ):
            continue

        if vendor_status in (
            "missing",
            "not_checked",
        ):
            unresolved_vendors.append(
                {
                    "statement_transaction_index": (
                        check_match.get(
                            "statement_transaction_index"
                        )
                    ),
                    "vendor_name": (
                        check_match.get(
                            "vendor_name"
                        )
                    ),
                    "vendor_status": vendor_status,
                }
            )
            continue

        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid vendor resolution "
                "status"
            ),
        )

    if unresolved_vendors:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Vendor resolution must be "
                    "completed before push"
                ),
                "unresolved_vendors": (
                    unresolved_vendors
                ),
            },
        )

def _prepare_reviewed_bank_statement_categories(
    document: dict,
) -> list[dict]:
    category_suggestions = document.get(
        "category_suggestions"
    )

    if category_suggestions is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Category suggestions have not "
                "been generated before push"
            ),
        )

    pending_categories = [
        item
        for item in category_suggestions
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    ]

    if pending_categories:
        pending_indexes = [
            item.get("transaction_index")
            for item in pending_categories
        ]

        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "All category suggestions "
                    "must be reviewed before push"
                ),
                "pending_transaction_indexes": (
                    pending_indexes
                ),
            },
        )

    category_updates = []

    for item in category_suggestions:
        review_status = item.get(
            "review_status"
        )

        if review_status == "rejected":
            continue

        if review_status not in (
            "approved",
            "manual_override",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid category review status"
                ),
            )

        qbo_transaction_type = item.get(
            "qbo_transaction_type"
        )

        if qbo_transaction_type != "purchase":
            raise HTTPException(
                status_code=400,
                detail=(
                    "Category write currently "
                    "supports purchase "
                    "transactions only"
                ),
            )

        qbo_transaction_id = item.get(
            "qbo_transaction_id"
        )

        if not qbo_transaction_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed category has no "
                    "QBO transaction ID"
                ),
            )

        reviewed_category = item.get(
            "reviewed_category"
        )

        if not reviewed_category:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed category is missing"
                ),
            )

        account_id = reviewed_category.get(
            "id"
        )

        if not account_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed category has no "
                    "QBO account ID"
                ),
            )

        category_updates.append(
            {
                "transaction_index": item.get(
                    "transaction_index"
                ),
                "qbo_transaction_id": str(
                    qbo_transaction_id
                ),
                "qbo_transaction_type": (
                    qbo_transaction_type
                ),
                "account_id": str(
                    account_id
                ),
                "category_name": (
                    reviewed_category.get(
                        "name"
                    )
                ),
            }
        )

    return category_updates

def _push_bank_statement_categories(
    document: dict,
    client,
    persist=None,
) -> dict:
    category_updates = (
        _prepare_reviewed_bank_statement_categories(
            document
        )
    )

    category_suggestions = document.get(
        "category_suggestions",
        [],
    )

    updated_count = 0
    skipped_count = 0
    errors = []
    results = []

    for item in category_suggestions:
        if item.get("review_status") != "rejected":
            continue

        skipped_count += 1

        results.append(
            {
                "transaction_index": item.get(
                    "transaction_index"
                ),
                "qbo_transaction_id": item.get(
                    "qbo_transaction_id"
                ),
                "qbo_account_id": None,
                "status": "skipped_rejected",
            }
        )

    for update in category_updates:
        transaction_index = update.get(
            "transaction_index"
        )

        selected_category = next(
            (
                item
                for item in category_suggestions
                if item.get("transaction_index")
                == transaction_index
            ),
            None,
        )

        if selected_category is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Category review state was "
                    "not found during QBO push"
                ),
            )

        desired_account_id = str(
            update["account_id"]
        )

        current_push_status = (
            selected_category.get(
                "qbo_category_push_status",
                "not_pushed",
            )
        )

        pushed_account_id = (
            selected_category.get(
                "qbo_category_account_id"
            )
        )

        # --------------------------------------
        # IDEMPOTENCY:
        # The same reviewed category has already
        # been written successfully to QBO.
        # --------------------------------------
        if (
            current_push_status == "pushed"
            and pushed_account_id is not None
            and str(pushed_account_id)
            == desired_account_id
        ):
            skipped_count += 1

            results.append(
                {
                    "transaction_index": (
                        transaction_index
                    ),
                    "qbo_transaction_id": (
                        update[
                            "qbo_transaction_id"
                        ]
                    ),
                    "qbo_account_id": (
                        desired_account_id
                    ),
                    "status": "skipped",
                    "reason": (
                        "Category has already "
                        "been pushed to QBO"
                    ),
                }
            )

            continue

        # A previous operation may have stopped
        # after entering the write phase.
        if current_push_status in (
            "pushing",
            "uncertain",
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Category QBO update is "
                        "already in progress or "
                        "its previous result is "
                        "uncertain"
                    ),
                    "transaction_index": (
                        transaction_index
                    ),
                },
            )

        selected_category["qbo_category_started_at"] = datetime.now(timezone.utc).isoformat()
        selected_category[
            "qbo_category_push_status"
        ] = "pushing"

        selected_category[
            "qbo_category_account_id"
        ] = desired_account_id

        selected_category[
            "qbo_category_push_result"
        ] = None

        selected_category[
            "qbo_category_push_error"
        ] = None

        if persist is not None:
            persist(document)

        try:
            result = (
                client.update_purchase_category(
                    purchase_id=(
                        update[
                            "qbo_transaction_id"
                        ]
                    ),
                    account_id=(
                        desired_account_id
                    ),
                )
            )

            selected_category[
                "qbo_category_push_status"
            ] = "pushed"

            selected_category[
                "qbo_category_push_result"
            ] = result

            selected_category[
                "qbo_category_push_error"
            ] = None

            if persist is not None:
                persist(document)

            updated_count += 1

            results.append(
                {
                    "transaction_index": (
                        transaction_index
                    ),
                    "qbo_transaction_id": (
                        update[
                            "qbo_transaction_id"
                        ]
                    ),
                    "qbo_account_id": (
                        desired_account_id
                    ),
                    "status": "updated",
                    "result": result,
                }
            )

        except Exception as exc:
            selected_category[
                "qbo_category_push_status"
            ] = "uncertain"

            selected_category[
                "qbo_category_push_result"
            ] = None

            selected_category[
                "qbo_category_push_error"
            ] = str(exc)

            if persist is not None:
                persist(document)

            errors.append(
                {
                    "transaction_index": (
                        transaction_index
                    ),
                    "qbo_transaction_id": (
                        update[
                            "qbo_transaction_id"
                        ]
                    ),
                    "error": str(exc),
                }
            )

            results.append(
                {
                    "transaction_index": (
                        transaction_index
                    ),
                    "qbo_transaction_id": (
                        update[
                            "qbo_transaction_id"
                        ]
                    ),
                    "qbo_account_id": (
                        desired_account_id
                    ),
                    "status": "failed",
                    "error": str(exc),
                }
            )

    document[
        "category_suggestions"
    ] = category_suggestions

    document[
        "category_push_results"
    ] = results

    if persist is not None:
        persist(document)

    return {
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "errors": errors,
        "updates": results,
    }

def _resolve_bank_statement_vendors(
    document: dict,
    client,
) -> None:

    check_match_results = document.get(
        "check_match_results",
        [],
    )

    for check_match in check_match_results:

        transaction_index = check_match.get(
            "statement_transaction_index"
        )

        statement_match = next(
            (
                item
                for item in document.get(
                    "qbo_match_results",
                    [],
                )
                if item.get(
                    "transaction_index"
                )
                == transaction_index
            ),
            None,
        )

        if (
            statement_match
            and statement_match.get(
                "review_status"
            )
            == "rejected"
        ):
            check_match[
                "vendor_status"
            ] = "skipped_rejected"

            check_match[
                "vendor_match"
            ] = None

            continue

        vendor_name = check_match.get(
            "vendor_name"
        )

        if not vendor_name:
            check_match[
                "vendor_status"
            ] = "no_vendor_name"

            check_match[
                "vendor_match"
            ] = None

            continue

        vendor_match = (
            client.find_vendor_by_name(
                vendor_name
            )
        )

        if vendor_match:
            check_match[
                "vendor_status"
            ] = "matched"

            check_match[
                "vendor_match"
            ] = vendor_match

        else:
            check_match[
                "vendor_status"
            ] = "missing"

            check_match[
                "vendor_match"
            ] = None

    document[
        "check_match_results"
    ] = check_match_results

def _prepare_reviewed_bank_statement_matches(
    document: dict,
) -> None:
    match_results = document.get(
        "qbo_match_results"
    )

    if not match_results:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has not been "
                "matched to QBO"
            ),
        )

    pending_results = [
        item
        for item in match_results
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    ]

    if pending_results:
        pending_indexes = [
            item.get("transaction_index")
            for item in pending_results
        ]

        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "All bank statement matches "
                    "must be reviewed before push"
                ),
                "pending_transaction_indexes": (
                    pending_indexes
                ),
            },
        )

    check_match_results = document.get("check_match_results")
    if check_match_results is None:
        raise HTTPException(
            400, "Check matching has not been completed. Run match-to-qbo first."
        )
    elif not isinstance(check_match_results, list) or any(
        not isinstance(item, dict) for item in check_match_results
    ):
        raise HTTPException(400, "Invalid check match results")

    for check_match in check_match_results:
        transaction_index = check_match.get(
            "statement_transaction_index"
        )

        statement_match = next(
            (
                item
                for item in match_results
                if item.get(
                    "transaction_index"
                )
                == transaction_index
            ),
            None,
        )

        if statement_match is None:
            continue

        review_status = statement_match.get(
            "review_status"
        )

        if review_status == "rejected":
            check_match["matched"] = False
            check_match["qbo_transaction_id"] = None
            check_match[
                "qbo_transaction_type"
            ] = None
            continue

        if review_status not in (
            "approved",
            "manual_override",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid bank statement "
                    "review status"
                ),
            )

        reviewed_match = statement_match.get(
            "reviewed_match"
        )

        if not reviewed_match:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed match is missing "
                    "for an approved transaction"
                ),
            )

        reviewed_transaction = (
            reviewed_match.get(
                "transaction"
            )
        )

        if not reviewed_transaction:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed QBO transaction "
                    "is missing"
                ),
            )

        qbo_transaction_id = (
            reviewed_transaction.get("id")
        )

        qbo_transaction_type = (
            reviewed_transaction.get("type")
        )

        if (
            not qbo_transaction_id
            or not qbo_transaction_type
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed QBO transaction "
                    "does not contain id/type"
                ),
            )

        check_match["matched"] = True
        check_match[
            "qbo_transaction_id"
        ] = str(qbo_transaction_id)

        check_match[
            "qbo_transaction_type"
        ] = qbo_transaction_type

    document["check_match_results"] = check_match_results

@router.post(
    "/{document_id}/push-to-qbo",
    response_model=PushToQBOResponse,
)

async def push_to_qbo(
    document_id: str,
    request: PushToQBORequest,
):
    document = _load_document(document_id)
    document_type = document.get("document_type")
    if request.expected_revision is not None and request.expected_revision != document.get("_revision"):
        raise HTTPException(409, "Document changed; reload and preview before pushing")

    if document_type in RECONCILIATION_TYPES:
        if document.get("status") not in {ProcessingStatus.EXTRACTED, ProcessingStatus.MATCHED, ProcessingStatus.PUSHED}:
            raise HTTPException(409, "Extract the bank statement successfully before pushing")
        has_check_images = bool(
            (document.get("extracted_data") or {}).get("check_images")
        )
        if has_check_images and not request.attach_documents:
            raise HTTPException(
                status_code=400,
                detail=(
                    "attach_documents must be true when the "
                    "bank statement contains check images"
                ),
            )

        from app.api.qbo import (
            _get_connected_qbo_client,
        )

        client = _get_connected_qbo_client()
        _ensure_bank_realm(document, client, request.expected_realm_id)
        if request.expected_revision is not None and request.expected_revision != document.get("_revision"):
            raise HTTPException(409, "Bank review changed; reload and preview before pushing")


        # 1. Match review gate
        _prepare_reviewed_bank_statement_matches(
            document
        )

        # 2. Vendor gate
        _validate_bank_statement_vendors_for_push(
            document
        )

        # An empty list is valid only when extraction detected no checks.
        # Missing or incomplete check results must fail before category writes.
        _validate_bank_statement_check_results(document)
        _save_document(document)

        # Confirm all pending check sources before any category write.
        for check in document.get("check_match_results", []):
            if check.get("matched") and not check.get("attachment_id"):
                try:
                    check_image_bytes(document, check.get("image_path"))
                except CheckSourceError as exc:
                    raise HTTPException(409, str(exc)) from None

        # 3. Category review + QBO category write
        category_push_result = (
            _push_bank_statement_categories(
                document=document,
                client=client,
                persist=_save_document,
            )
        )

        # Fail closed:
        # category write başarısız / belirsiz ise
        # check attachment aşamasına geçme.
        if category_push_result["errors"]:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": (
                        "One or more category updates "
                        "could not be confirmed in QBO. "
                        "Check QuickBooks before retrying."
                    ),
                    "category_errors": (
                        category_push_result["errors"]
                    ),
                },
            )

        # 4. Category write tamamlandıktan sonra
        # check attachment push.
        check_push_result = _push_bank_statement_checks(
            document_id=document_id,
            document=document,
            client=client,
            persist=_save_document,
        )

        return PushToQBOResponse(
            id=check_push_result.id,
            success=check_push_result.success,
            transactions_pushed=(
                check_push_result.transactions_pushed
            ),
            attachments_uploaded=(
                check_push_result.attachments_uploaded
            ),
            vendors_created=(
                check_push_result.vendors_created
            ),
            categories_updated=(
                category_push_result["updated_count"]
            ),
            categories_skipped=(
                category_push_result["skipped_count"]
            ),
            category_updates=(
                category_push_result["updates"]
            ),
            errors=check_push_result.errors,
        )

    if document_type not in (DocumentType.INVOICE, DocumentType.RECEIPT):
        raise HTTPException(
            status_code=400,
            detail=(
                "This QBO push flow supports "
                "invoices, receipts and reviewed "
                "bank statements"
            ),
        )

    if not document.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail="Invoice/receipt has no extracted data",
        )

    if not document.get("approved"):
        raise HTTPException(
            status_code=400,
            detail="Invoice/receipt must be approved before it can be pushed to QBO",
        )

    # Push state alanlarını garanti altına al.
    initialize_qbo_push_state(document)

    # Daha önce push edilmiş / belirsiz durumda / devam eden
    # bir invoice tekrar gönderilemez.
    try:
        ensure_invoice_can_start_push(document)
    except QBOPushGuardError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    if document.get("status") != ProcessingStatus.EXTRACTED:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invoice/receipt must be in extracted state before QBO push. "
                f"Current status: {document.get('status')}"
            ),
        )

    # Circular import oluşturmamak için lokal import.
    from app.api.qbo import _get_connected_qbo_client

    client = _get_connected_qbo_client()
    if request.expected_realm_id is not None and request.expected_realm_id != client.realm_id:
        raise HTTPException(409, "QBO company changed; reload mapping options before continuing")


    # ---------------------------------------------------------
    # 1. Gerçek QBO Vendor ID doğrulaması
    # ---------------------------------------------------------

    if not request.vendor_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "vendor_id is required "
                "for invoice/receipt QBO push"
            ),
        )

    if not request.account_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "account_id is required "
                "for invoice/receipt QBO push"
            ),
        )

    vendors = client.get_vendors()

    vendor = next(
        (
            item
            for item in vendors
            if str(item.get("id")) == str(request.vendor_id)
        ),
        None,
    )

    if vendor is None:
        raise HTTPException(
            status_code=400,
            detail="Selected vendor_id does not exist in QBO",
        )

    # ---------------------------------------------------------
    # 2. Gerçek Expense Account ID doğrulaması
    # ---------------------------------------------------------

    expense_accounts = client.get_expense_accounts()

    account = next(
        (
            item
            for item in expense_accounts
            if str(item.get("id")) == str(request.account_id)
        ),
        None,
    )

    if account is None:
        raise HTTPException(
            status_code=400,
            detail="Selected account_id is not a valid QBO expense account",
        )

    # ---------------------------------------------------------
    # 3. Expense seçildiyse verilen Bank Account ID'yi doğrula
    # ---------------------------------------------------------

    bank_account = None
    if request.create_as == "expense" and not request.bank_account_id:
        raise HTTPException(400, "bank_account_id is required for expense creation")
    if request.create_as == "expense" and request.bank_account_id:
        bank_accounts = client.get_bank_accounts()

        bank_account = next(
            (
                item
                for item in bank_accounts
                if str(item.get("id"))
                == str(request.bank_account_id)
            ),
            None,
        )

        if bank_account is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Selected bank_account_id is not "
                    "a valid QBO bank account"
                ),
            )

    # ---------------------------------------------------------
    # 4. Extracted invoice -> ReceiptData
    # ---------------------------------------------------------

    try:
        invoice_data = ReceiptData(
            **document["extracted_data"]
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid extracted invoice/receipt data: {exc}",
        ) from exc

    # ---------------------------------------------------------
    # 5. Aynı mapper ile plan oluştur
    # ---------------------------------------------------------

    check_document_currency(client, invoice_data, vendor, account, bank_account)

    try:
        plan = build_qbo_create_plan(
            data=invoice_data,
            create_as=request.create_as,
            vendor_id=request.vendor_id,
            account_id=request.account_id,
            description=request.description,
            bank_account_id=request.bank_account_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    attachments_uploaded = 0
    response_errors = []

    if request.attach_documents:
        from app.services.persistence.attachment_sources import prepare_invoice_attachment
        prepare_invoice_attachment(document)

    # Bundan önce QBO'ya hiçbir write yapılmadı.
    # Şimdi external write başlamak üzere.
    document["qbo_push_context"] = {"realm_id": getattr(client, "realm_id", None),
                                    "plan": plan, "currency": invoice_data.currency,
                                    "marker": "receipt-ai:" + uuid.uuid4().hex}
    document["qbo_push_started_at"] = datetime.now(timezone.utc).isoformat()
    mark_invoice_push_started(document)
    _save_document(document)

    try:
        # ---------------------------------------------
        # 6. GERÇEK QBO WRITE
        # ---------------------------------------------

        if plan["method"] == "create_bill":
            result = client.create_bill(
                **plan["kwargs"], recovery_marker=document["qbo_push_context"]["marker"]
            )

        elif plan["method"] == "create_expense":
            result = client.create_expense(
                **plan["kwargs"], recovery_marker=document["qbo_push_context"]["marker"]
            )

        else:
            raise ValueError(
                f"Unsupported QBO method: {plan['method']}"
            )

        transaction_id = str(
            result.get("id") or ""
        ).strip()

        transaction_type = str(
            result.get("type") or request.create_as
        ).strip().lower()

        # QBO request dönmüş olsa bile transaction ID yoksa
        # "başarılı" kabul etmiyoruz.
        if not transaction_id:
            raise RuntimeError(
                "QBO write returned no transaction ID"
            )

        # ---------------------------------------------
        # 7. Başarılı QBO write'ı kaydet
        # ---------------------------------------------

        if "doc_number" in result:
            document["qbo_doc_number"] = result["doc_number"]
        mark_invoice_push_succeeded(
            document=document,
            transaction_id=transaction_id,
            transaction_type=transaction_type,
        )
        _save_document(document)

    except Exception as exc:
        # External write başladıktan sonra oluşan hata için
        # otomatik retry güvenli değildir.
        mark_invoice_push_uncertain(
            document=document,
            error=str(exc),
        )
        _save_document(document)

        raise HTTPException(
            status_code=502,
            detail=(
                "QBO push outcome is uncertain. "
                "Do not retry automatically. "
                "Check QuickBooks Online before trying again."
            ),
        ) from exc

    expected_number = plan["kwargs"].get("doc_number")
    if expected_number is not None and "doc_number" in result and result["doc_number"] != expected_number:
        # The financial ID is already persisted. Never recreate this transaction.
        document["qbo_doc_number_needs_review"] = True
        document["qbo_push_error"] = "QBO document number differs from the approved plan; correct and verify the existing transaction"
        _save_document(document)
        raise HTTPException(502, document["qbo_push_error"])

    if request.attach_documents:
        temp_path = None

        try:
            from app.services.persistence.attachment_sources import invoice_attachment
            filename, content_type, file_content = invoice_attachment(document)

            suffix = Path(filename).suffix

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=suffix,
            ) as temp_file:
                temp_file.write(file_content)
                temp_path = temp_file.name

            if transaction_type == "bill":
                entity_type = "Bill"

            elif transaction_type == "expense":
                entity_type = "Purchase"

            else:
                raise ValueError(
                    "Unsupported QBO transaction type "
                    f"for attachment: {transaction_type}"
                )

            document["qbo_attachment_marker"] = document["qbo_push_context"]["marker"] + ":source"
            document["qbo_attachment_started_at"] = datetime.now(timezone.utc).isoformat()
            document["qbo_attachment_push_status"] = "uploading"
            _save_document(document)

            attachment_result = client.upload_attachment(
                file_path=temp_path,
                file_name=filename,
                content_type=content_type,
                entity_type=entity_type,
                entity_id=transaction_id,
                recovery_marker=document["qbo_attachment_marker"],
            )

            if not attachment_result.get("id"):
                raise RuntimeError("QBO attachment returned no ID")
            document["qbo_attachment_push_status"] = "uploaded"
            document["qbo_attachment_id"] = (
                attachment_result["id"]
            )

            document["qbo_attachment_error"] = None

            _save_document(document)
            attachments_uploaded = 1

        except Exception as exc:
            if document.get("qbo_attachment_push_status") == "uploading":
                document["qbo_attachment_push_status"] = "uncertain"
            else:
                document["qbo_attachment_push_status"] = "failed_before_write"
            document["qbo_attachment_error"] = str(exc)
            _save_document(document)

            response_errors.append(
                "Transaction was created successfully, "
                f"but attachment upload failed: {exc}"
            )

        finally:
            if (
                temp_path
                and os.path.exists(temp_path)
            ):
                os.remove(temp_path)


    return PushToQBOResponse(
    id=document_id,
    success=not response_errors,
    transactions_pushed=1,
    attachments_uploaded=attachments_uploaded,
    vendors_created=0,
    errors=response_errors,
)

@router.post(
    "/{document_id}/match-to-qbo"
)
async def match_document_to_qbo(
    document_id: str,
    request: MatchToQBORequest,
):
    document = _load_document(document_id)

    if document.get("status") == ProcessingStatus.PUSHED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Document already pushed to QBO; "
                "rematching is not allowed."
            ),
        )

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO matching in this endpoint "
                "requires a bank statement"
            ),
        )

    _ensure_workflow_editable(document)

    extracted_data = document.get(
        "extracted_data"
    )

    if not extracted_data:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has no "
                "extracted data"
            ),
        )

    from app.services.extraction.reconciliation import validate_reconciliation
    validate_reconciliation(document)

    try:
        statement = BankStatementData(
            **extracted_data
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid bank statement "
                f"extraction: {exc}"
            ),
        ) from exc

    if (
        not statement.statement_period_start
        or not statement.statement_period_end
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement period start "
                "and end dates are required "
                "for QBO matching"
            ),
        )

    # Lazy import avoids the same API-module
    # circular-import problem we handled earlier.
    from app.api.qbo import (
        _get_connected_qbo_client,
    )

    client = (
        _get_connected_qbo_client()
    )
    if request.expected_realm_id is not None and request.expected_realm_id != getattr(client, "realm_id", None):
        raise HTTPException(409, "QBO company changed; reload bank accounts")
    accounts = client.get_accounts(
        account_type="CreditCard" if document.get("document_type") == DocumentType.CREDIT_CARD_STATEMENT else "Bank"
    )

    selected_bank_account = next(
        (
            account
            for account in accounts
            if str(account["id"])
            == str(request.bank_account_id)
        ),
        None,
    )

    if selected_bank_account is None or (document.get("document_type") == DocumentType.CREDIT_CARD_STATEMENT
            and selected_bank_account.get("type") != "CreditCard"):
        raise HTTPException(
            status_code=400,
            detail=(
                "bank_account_id must reference "
                "an existing QBO account of the document's required type"
            ),
        )

    try:
        qbo_transactions = (
            client.get_matching_transactions(
                start_date=(
                    statement.statement_period_start
                ),
                end_date=(
                    statement.statement_period_end
                ),
                bank_account_id=request.bank_account_id,
                **({"account_type": "CreditCard"} if document.get("document_type") == DocumentType.CREDIT_CARD_STATEMENT else {}),
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to read QBO "
                f"transactions: {exc}"
            ),
        ) from exc

    if document.get("document_type") == DocumentType.CHECK:
        number = str(extracted_data.get("check_number") or "").strip()
        qbo_transactions = [t for t in qbo_transactions
            if t.get("type") in ({"deposit", "payment"} if extracted_data.get("direction") == "incoming" else {"purchase", "bill_payment"})
            and str(t.get("check_number") or "").strip() == number]

    matcher = BankFeedMatcher()

    match_results = (
        matcher.match_statement(
            transactions=(
                statement.transactions
            ),
            qbo_transactions=(
                qbo_transactions
            ),
        )
    )

    for match_result in match_results:
        match_result["review_status"] = "pending"
        match_result["reviewed_match"] = None

    previous_check_results = {
        item.get("image_path"): item
        for item in (
            document.get(
                "check_match_results"
            )
            or []
        )
        if item.get("image_path")
    }

    check_match_results = []

    for check_index, check_image in enumerate(
        statement.check_images
    ):
        check_match = (
            matcher.match_check_image_to_statement(
                check_image=check_image,
                **({"allow_incoming": True} if document.get("document_type") == DocumentType.CHECK and extracted_data.get("direction") == "incoming" else {}),
                transactions=(
                    statement.transactions
                ),
            )
        )

        statement_transaction_index = None
        statement_match_score = None

        qbo_transaction_id = None
        qbo_transaction_type = None
        check_image.matched_transaction_id = None

        if check_match["matched"]:
            best_statement_match = (
                check_match["best_match"]
            )

            statement_transaction_index = (
                best_statement_match[
                    "statement_transaction_index"
                ]
            )

            statement_match_score = (
                best_statement_match["score"]
            )

            qbo_match = next(
                (
                    item
                    for item in match_results
                    if (
                        item[
                            "transaction_index"
                        ]
                        == statement_transaction_index
                        and item["matched"]
                    )
                ),
                None,
            )

            if (
                qbo_match
                and qbo_match.get(
                    "best_match"
                )
            ):
                qbo_transaction = (
                    qbo_match[
                        "best_match"
                    ][
                        "transaction"
                    ]
                )

                qbo_transaction_id = str(
                    qbo_transaction["id"]
                )

                qbo_transaction_type = (
                    qbo_transaction["type"]
                )

                check_image.matched_transaction_id = (
                    qbo_transaction_id
                )

        previous = (
            previous_check_results.get(
                check_image.image_path
            )
            or {}
        )

        same_qbo_target = (
            str(
                previous.get(
                    "qbo_transaction_id"
                )
                or ""
            )
            == str(
                qbo_transaction_id
                or ""
            )
            and previous.get(
                "qbo_transaction_type"
            )
            == qbo_transaction_type
        )

        attachment_id = (
            previous.get(
                "attachment_id"
            )
            if same_qbo_target
            else None
        )

        attachment_error = (
            previous.get(
                "attachment_error"
            )
            if same_qbo_target
            else None
        )

        check_match_results.append({
            "check_index": check_index,
            "image_path": (
                check_image.image_path
            ),
            "check_number": (
                check_image.check_number
            ),
            "statement_transaction_index": (
                statement_transaction_index
            ),
            "statement_match_score": (
                statement_match_score
            ),
            "qbo_transaction_id": (
                qbo_transaction_id
            ),
            "qbo_transaction_type": (
                qbo_transaction_type
            ),
            "matched": (
                qbo_transaction_id
                is not None
            ),
            "attachment_id": (
                attachment_id
            ),
            "attachment_error": (
                attachment_error
            ),
            "vendor_name": check_image.payee,
            "vendor_match": None,
            "vendor_status": "not_checked",
        })

    document["extracted_data"] = {**extracted_data, **statement.model_dump()}

    document[
        "check_match_results"
    ] = check_match_results

    matched_count = sum(
        1
        for item in match_results
        if item["matched"]
    )

    unmatched_count = (
        len(match_results)
        - matched_count
    )

    document.pop("category_suggestions", None)
    document["qbo_match_realm_id"] = getattr(client, "realm_id", None)
    document["qbo_match_bank_account"] = {"id": str(selected_bank_account["id"]), "name": selected_bank_account["name"]}

    # Store result for review.
    # No QBO write happens here.
    document[
        "qbo_match_results"
    ] = match_results

    document["status"] = (
        ProcessingStatus.MATCHED
    )

    _save_document(document)

    return {
        "id": document_id,
        "transactions_extracted": len(
            statement.transactions
        ),
        "qbo_candidates_loaded": len(
            qbo_transactions
        ),
        "matched_count": matched_count,
        "unmatched_count": (
            unmatched_count
        ),
        "matches": match_results,
        "qbo_bank_account": {
            "id": str(
                selected_bank_account["id"]
            ),
            "name": selected_bank_account["name"],
        },
        "qbo_candidate_types": {
            transaction_type: sum(
                1
                for txn in qbo_transactions
                if txn["type"] == transaction_type
            )
            for transaction_type in sorted(
                {
                    txn["type"]
                    for txn in qbo_transactions
                }
            )
        },
        "checks_detected": len(
            statement.check_images
        ),

        "checks_linked_to_qbo": sum(
            1
            for item in check_match_results
            if item["matched"]
        ),

        "check_matches": (
            check_match_results
        ),
    }

@router.post(
    "/{document_id}/resolve-vendors"
)
async def resolve_bank_statement_vendors(
    document_id: str,
):
    document = _load_document(document_id)

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Vendor resolution is only "
                "available for bank statements"
            ),
        )

    _ensure_workflow_editable(document)

    match_results = document.get(
        "qbo_match_results"
    )

    if not match_results:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has not been "
                "matched to QBO"
            ),
        )

    pending = [
        item
        for item in match_results
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    ]

    if pending:
        raise HTTPException(
            status_code=400,
            detail=(
                "All bank statement matches "
                "must be reviewed first"
            ),
        )

    from app.api.qbo import (
        _get_connected_qbo_client,
    )

    client = (
        _get_connected_qbo_client()
    )
    _ensure_bank_realm(document, client)

    _resolve_bank_statement_vendors(
        document=document,
        client=client,
    )

    check_match_results = document.get(
        "check_match_results",
        [],
    )

    matched_count = sum(
        1
        for item in check_match_results
        if item.get("vendor_status")
        == "matched"
    )

    missing_count = sum(
        1
        for item in check_match_results
        if item.get("vendor_status")
        == "missing"
    )

    no_vendor_name_count = sum(
        1
        for item in check_match_results
        if item.get("vendor_status")
        == "no_vendor_name"
    )

    _save_document(document)

    return {
        "id": document_id,
        "vendor_matched_count": (
            matched_count
        ),
        "vendor_missing_count": (
            missing_count
        ),
        "vendor_no_name_count": (
            no_vendor_name_count
        ),
        "check_matches": (
            check_match_results
        ),
    }

@router.post(
    "/{document_id}/suggest-categories"
)
async def suggest_bank_statement_categories(
    document_id: str,
):
    document = _load_document(document_id)

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Category suggestions are only "
                "available for bank statements"
            ),
        )

    _ensure_workflow_editable(document)

    match_results = document.get(
        "qbo_match_results"
    )

    if not match_results:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has not been "
                "matched to QBO"
            ),
        )

    pending_matches = [
        item
        for item in match_results
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    ]

    if pending_matches:
        raise HTTPException(
            status_code=400,
            detail=(
                "All bank statement matches "
                "must be reviewed first"
            ),
        )

    check_match_results = document.get(
        "check_match_results",
        [],
    )

    unresolved_vendors = [
        item
        for item in check_match_results
        if item.get(
            "vendor_status",
            "not_checked",
        )
        in (
            "not_checked",
            "missing",
        )
    ]

    if unresolved_vendors:
        raise HTTPException(
            status_code=400,
            detail=(
                "Vendor resolution must be "
                "completed before category "
                "suggestion"
            ),
        )

    from app.api.qbo import (
        _get_connected_qbo_client,
    )

    client = _get_connected_qbo_client()
    _ensure_bank_realm(document, client)

    expense_accounts = (
        client.get_expense_accounts()
    )

    extracted_data = document.get(
        "extracted_data"
    )

    if not extracted_data:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has no "
                "extracted data"
            ),
        )

    statement = BankStatementData(
        **extracted_data
    )

    categorizer = TransactionCategorizer()

    suggestions = []

    for transaction_index, transaction in enumerate(
        statement.transactions
    ):
        reviewed_result = next(
            (
                item
                for item in match_results
                if item.get("transaction_index")
                == transaction_index
            ),
            None,
        )

        # There should normally be a match-review
        # record for every extracted transaction.
        if reviewed_result is None:
            continue

        review_status = reviewed_result.get(
            "review_status"
        )

        # A rejected QBO match must never be used
        # for category suggestion.
        if review_status == "rejected":
            transaction.category_suggestion = None
            continue

        if review_status not in (
            "approved",
            "manual_override",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid bank statement "
                    "review status for category "
                    "suggestion"
                ),
            )

        reviewed_match = reviewed_result.get(
            "reviewed_match"
        )

        if not reviewed_match:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed QBO match is missing "
                    "for category suggestion"
                ),
            )

        reviewed_qbo_transaction = (
            reviewed_match.get("transaction")
        )

        if not reviewed_qbo_transaction:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Reviewed QBO transaction is "
                    "missing for category suggestion"
                ),
            )

        qbo_transaction_type = (
            reviewed_qbo_transaction.get("type")
        )

        # Expense-category suggestion currently
        # applies only to QBO Purchase transactions.
        if qbo_transaction_type != "purchase":
            transaction.category_suggestion = None
            continue

        category_match = (
            categorizer.suggest_category(
                transaction_description=(
                    transaction.description
                ),
                vendor_name=(
                    transaction.vendor_suggestion
                ),
                expense_accounts=(
                    expense_accounts
                ),
            )
        )

        transaction.category_suggestion = (
            category_match["name"]
            if category_match
            else None
        )

        suggestions.append(
            {
                "transaction_index": (
                    transaction_index
                ),
                "description": (
                    transaction.description
                ),
                "vendor_suggestion": (
                    transaction.vendor_suggestion
                ),
                "qbo_transaction_id": str(
                    reviewed_qbo_transaction["id"]
                ),
                "qbo_transaction_type": (
                    qbo_transaction_type
                ),
                "category_suggestion": (
                    category_match["name"]
                    if category_match
                    else None
                ),
                "category_match": (
                    category_match
                ),
                "review_status": "pending",
                "reviewed_category": None,
                "qbo_category_push_status": (
                    "not_pushed"
                ),
                "qbo_category_account_id": None,
                "qbo_category_push_result": None,
                "qbo_category_push_error": None,
            }
        )

    document["extracted_data"] = {**extracted_data, **statement.model_dump()}

    document[
        "category_suggestions"
    ] = suggestions

    _save_document(document)

    return {
        "id": document_id,
        "transactions_processed": len(
            suggestions
        ),
        "suggested_count": sum(
            1
            for item in suggestions
            if item[
                "category_suggestion"
            ]
            is not None
        ),
        "suggestions": suggestions,
    }

@router.patch(
    "/{document_id}/category-review/{transaction_index}"
)
async def review_bank_statement_category(
    document_id: str,
    transaction_index: int,
    review: CategoryReviewRequest,
):
    document = _load_document(document_id)

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Category review is only "
                "available for bank statements"
            ),
        )

    category_suggestions = document.get(
        "category_suggestions"
    )

    if category_suggestions is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Category suggestions have not "
                "been generated yet"
            ),
        )

    selected_category = next(
        (
            item
            for item in category_suggestions
            if item.get("transaction_index")
            == transaction_index
        ),
        None,
    )

    if selected_category is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Category suggestion for this "
                "transaction was not found"
            ),
        )

    # Defensive check:
    # category review currently supports only
    # QBO Purchase transactions.
    if (
        selected_category.get(
            "qbo_transaction_type"
        )
        != "purchase"
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Category review currently "
                "supports purchase "
                "transactions only"
            ),
        )

    if selected_category.get("qbo_category_push_status") == "pushed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Category review cannot be changed "
                "after it has been pushed to QBO"
            ),
        )
    
    _ensure_workflow_editable(document)

    if review.decision == "approve":
        category_match = (
            selected_category.get(
                "category_match"
            )
        )

        if not category_match:
            raise HTTPException(
                status_code=400,
                detail=(
                    "There is no suggested "
                    "category to approve"
                ),
            )

        selected_category[
            "review_status"
        ] = "approved"

        selected_category[
            "reviewed_category"
        ] = category_match

    elif review.decision == "reject":
        selected_category[
            "review_status"
        ] = "rejected"

        selected_category[
            "reviewed_category"
        ] = None

    elif review.decision == "manual_override":
        if not review.qbo_account_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "qbo_account_id is required "
                    "for manual_override"
                ),
            )

        from app.api.qbo import (
            _get_connected_qbo_client,
        )

        client = (
            _get_connected_qbo_client()
        )
        _ensure_bank_realm(document, client)

        expense_accounts = (
            client.get_expense_accounts()
        )

        selected_account = next(
            (
                account
                for account in expense_accounts
                if str(
                    account.get("id")
                )
                == str(
                    review.qbo_account_id
                )
            ),
            None,
        )

        if selected_account is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Selected QBO expense "
                    "account does not exist"
                ),
            )

        selected_category[
            "review_status"
        ] = "manual_override"

        selected_category[
            "reviewed_category"
        ] = {
            "id": str(
                selected_account["id"]
            ),
            "name": (
                selected_account["name"]
            ),
            "fully_qualified_name": (
                selected_account.get(
                    "fully_qualified_name"
                )
                or selected_account["name"]
            ),
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid category review "
                "decision"
            ),
        )

    extracted_data = document.get(
        "extracted_data"
    )

    if not extracted_data:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has no "
                "extracted data"
            ),
        )

    statement = BankStatementData(
        **extracted_data
    )

    if (
        transaction_index < 0
        or transaction_index
        >= len(statement.transactions)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid transaction index"
            ),
        )

    transaction = (
        statement.transactions[
            transaction_index
        ]
    )

    reviewed_category = (
        selected_category.get(
            "reviewed_category"
        )
    )

    if reviewed_category:
        transaction.category_suggestion = (
            reviewed_category["name"]
        )
    else:
        transaction.category_suggestion = (
            None
        )

    document["extracted_data"] = {**extracted_data, **statement.model_dump()}

    document[
        "category_suggestions"
    ] = category_suggestions

    _save_document(document)

    return {
        "id": document_id,
        "transaction_index": (
            transaction_index
        ),
        "review_status": (
            selected_category[
                "review_status"
            ]
        ),
        "reviewed_category": (
            selected_category.get(
                "reviewed_category"
            )
        ),
    }

@router.get(
    "/{document_id}/category-review"
)
async def get_bank_statement_category_review(
    document_id: str,
):
    document = _load_document(document_id)

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Category review is only "
                "available for bank statements"
            ),
        )

    category_suggestions = document.get(
        "category_suggestions"
    )

    if category_suggestions is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Category suggestions have not "
                "been generated yet"
            ),
        )

    pending_count = sum(
        1
        for item in category_suggestions
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    )

    approved_count = sum(
        1
        for item in category_suggestions
        if item.get("review_status")
        == "approved"
    )

    rejected_count = sum(
        1
        for item in category_suggestions
        if item.get("review_status")
        == "rejected"
    )

    manual_override_count = sum(
        1
        for item in category_suggestions
        if item.get("review_status")
        == "manual_override"
    )

    return {
        "id": document_id,
        "total_categories": len(
            category_suggestions
        ),
        "pending_count": pending_count,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "manual_override_count": (
            manual_override_count
        ),
        "categories": category_suggestions,
    }

@router.patch(
    "/{document_id}/match-review/{transaction_index}"
)
async def review_bank_statement_match(
    document_id: str,
    transaction_index: int,
    review: BankMatchReviewRequest,
):
    document = _load_document(document_id)

    if (
        document.get("document_type")
        not in RECONCILIATION_TYPES
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Match review is only available "
                "for bank statements"
            ),
        )

    match_results = document.get(
        "qbo_match_results"
    )

    if match_results is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Document has not been matched "
                "to QBO yet"
            ),
        )

    selected_match = next(
        (
            item
            for item in match_results
            if item.get("transaction_index")
            == transaction_index
        ),
        None,
    )

    if selected_match is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Transaction match result "
                "not found"
            ),
        )

    if document.get("status") == ProcessingStatus.PUSHED:
        raise HTTPException(
            status_code=409,
            detail=(
                "Match review cannot be changed "
                "after the document has been pushed to QBO"
            ),
        )

    _ensure_workflow_editable(document)

    if review.decision == "approve":
        best_match = selected_match.get(
            "best_match"
        )

        if (
            not selected_match.get("matched")
            or not best_match
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "There is no suggested "
                    "QBO match to approve"
                ),
            )

        selected_match[
            "review_status"
        ] = "approved"

        selected_match[
            "reviewed_match"
        ] = best_match

    elif review.decision == "reject":
        selected_match[
            "review_status"
        ] = "rejected"

        selected_match[
            "reviewed_match"
        ] = None

    elif review.decision == "manual_override":
        if (
            not review.qbo_transaction_id
            or not review.qbo_transaction_type
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "qbo_transaction_id and "
                    "qbo_transaction_type are "
                    "required for manual_override"
                ),
            )

        candidates = selected_match.get(
            "candidates",
            [],
        )

        override_match = next(
            (
                candidate
                for candidate in candidates
                if (
                    str(
                        candidate.get(
                            "transaction",
                            {},
                        ).get("id")
                    )
                    == str(
                        review.qbo_transaction_id
                    )
                    and candidate.get(
                        "transaction",
                        {},
                    ).get("type")
                    == review.qbo_transaction_type
                )
            ),
            None,
        )

        if override_match is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Selected QBO transaction "
                    "is not one of the available "
                    "match candidates"
                ),
            )

        selected_match[
            "review_status"
        ] = "manual_override"

        selected_match[
            "reviewed_match"
        ] = override_match

    # A changed match invalidates every downstream category review and vendor lookup.
    document.pop("category_suggestions", None)
    for item in document.get("check_match_results", []):
        item["vendor_status"] = "not_checked"
        item["vendor_match"] = None

    _save_document(document)

    return {
        "id": document_id,
        "transaction_index": (
            transaction_index
        ),
        "review_status": (
            selected_match[
                "review_status"
            ]
        ),
        "reviewed_match": (
            selected_match.get(
                "reviewed_match"
            )
        ),
    }

@router.get(
    "/{document_id}/match-review"
)
async def get_bank_statement_match_review(
    document_id: str,
):
    document = _load_document(document_id)

    match_results = document.get(
        "qbo_match_results"
    )

    if match_results is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Document has not been matched "
                "to QBO yet"
            ),
        )

    pending_count = sum(
        1
        for item in match_results
        if item.get(
            "review_status",
            "pending",
        )
        == "pending"
    )

    approved_count = sum(
        1
        for item in match_results
        if item.get("review_status")
        == "approved"
    )

    rejected_count = sum(
        1
        for item in match_results
        if item.get("review_status")
        == "rejected"
    )

    manual_override_count = sum(
        1
        for item in match_results
        if item.get("review_status")
        == "manual_override"
    )

    return {
        "id": document_id,
        "total_matches": len(
            match_results
        ),
        "pending_count": pending_count,
        "approved_count": approved_count,
        "rejected_count": rejected_count,
        "manual_override_count": (
            manual_override_count
        ),
        "matches": match_results,
    }
