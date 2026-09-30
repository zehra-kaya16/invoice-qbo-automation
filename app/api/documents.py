import os
import tempfile
from pathlib import Path
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel
from app.services.validation.invoice_validator import validate_invoice

from app.services.qbo.mapper import build_qbo_create_plan

from app.schemas.documents import (
    BankStatementData,
    DocumentType,
    DocumentUploadResponse,
    ExtractionResponse,
    ProcessingStatus,
    ReceiptData,
    PushToQBOResponse,
    BankMatchReviewRequest
)
from app.services.qbo.push_guard import (
    QBOPushGuardError,
    ensure_invoice_can_start_push,
    initialize_qbo_push_state,
    mark_invoice_push_started,
    mark_invoice_push_succeeded,
    mark_invoice_push_uncertain,
)

from app.services.matching.matcher import (
    BankFeedMatcher,
)

router = APIRouter()

documents_db = {} # Geçici belge veri tabanı

class DocumentStatus(BaseModel):
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
    create_as: str = "bill"

    vendor_id: Optional[str] = None
    account_id: Optional[str] = None

    description: Optional[str] = None
    bank_account_id: Optional[str] = None
    attach_documents: bool = True

class MatchToQBORequest(BaseModel):
    bank_account_id: str

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
    company_id: str = "default",
    auto_process: bool = True,
    background_tasks: BackgroundTasks = None,
):
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

        content = await file.read()

        documents_db[document_id] = {
        "id": document_id,
        "filename": file.filename,
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

        return DocumentUploadResponse(
        id=document_id,
        filename=file.filename or "unknown",
        status=ProcessingStatus.UPLOADED,
        message=(
            "Document uploaded. "
            "Automatic processing will be added in the extraction stage."
        ),
    )

@router.get("/{document_id}", response_model=DocumentStatus)
async def get_document(document_id: str):
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]

    return DocumentStatus(
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

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]

    if document["status"] == ProcessingStatus.EXTRACTED:
        return _build_extraction_response(document)

    await process_document(document_id)

    document = documents_db[document_id]

    return _build_extraction_response(document)

async def process_document(document_id: str):

    if document_id not in documents_db:
        return

    document = documents_db[document_id]

    try:
        document["status"] = ProcessingStatus.CLASSIFYING

        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            document["status"] = ProcessingStatus.FAILED
            document["error"] = (
                "No AI API key configured"
            )
            return

        from app.services.extraction.extractor import (
            DocumentExtractor,
        )

        extractor = DocumentExtractor(
            api_key=api_key,
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
            if len(page_images) > 1:
                raise ValueError(
                    "Multi-page receipt/invoice PDF extraction "
                    "is not implemented yet."
                )

            extracted = extractor.extract_receipt(
                page_images[0]
            )

            if document_type == DocumentType.INVOICE:
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

        elif document_type == DocumentType.CHECK:
            extracted = extractor.extract_check(
                page_images[0]
            )

            document["extracted_data"] = (
                extracted.model_dump()
            )

        else:
            document["extracted_data"] = {
                "raw_type": str(document_type)
            }

        document["status"] = ProcessingStatus.EXTRACTED

    except Exception as exc:
        document["status"] = ProcessingStatus.FAILED
        document["error"] = str(exc)

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
        == DocumentType.BANK_STATEMENT
    ):
        response.bank_statement_data = (
            BankStatementData(
                **extracted
            )
        )

    return response


@router.get(
    "/{document_id}/extracted",
    response_model=ExtractionResponse,
)
async def get_extracted_data(document_id: str):

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]

    if document["status"] not in [
        ProcessingStatus.EXTRACTED,
        ProcessingStatus.MATCHED,
        ProcessingStatus.PUSHED,
    ]:
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
    Update reviewed invoice fields and re-run validation.
    """

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    doc = documents_db[document_id]

    if doc.get("document_type") != DocumentType.INVOICE:
        raise HTTPException(
            status_code=400,
            detail="Review editing is currently available only for invoices",
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
    updates = receipt_data.model_dump(
        exclude_unset=True,
    )
    print("PATCH fields_set:", receipt_data.model_fields_set)
    print("PATCH updates:", updates)

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
    return _build_extraction_response(doc)

@router.post("/{document_id}/approve", response_model=ExtractionResponse)
async def approve_document(document_id: str):
    """
    Approve a reviewed and valid invoice.
    """

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    doc = documents_db[document_id]

    if doc.get("document_type") != DocumentType.INVOICE:
        raise HTTPException(
            status_code=400,
            detail="Approval is currently available only for invoices",
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

    validation = doc.get("validation")

    if validation is None:
        raise HTTPException(
            status_code=400,
            detail="Invoice has not been validated",
        )

    if not validation.get("is_valid", False):
        raise HTTPException(
            status_code=400,
            detail="Invoice cannot be approved because validation failed",
        )

    doc["approved"] = True

    return _build_extraction_response(doc)

def _push_bank_statement_checks(
    document_id: str,
    document: dict,
    client,
) -> PushToQBOResponse:

    check_results = (
        document.get(
            "check_match_results"
        )
        or []
    )

    if not check_results:
        raise HTTPException(
            status_code=400,
            detail=(
                "Bank statement has no "
                "check match results. "
                "Run match-to-qbo first."
            ),
        )

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

        if not path.exists():
            message = (
                "Check image file does not "
                f"exist: {image_path}"
            )

            item["attachment_error"] = (
                message
            )

            errors.append(message)
            continue

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

        try:
            result = client.upload_attachment(
                file_path=str(path),
                file_name=path.name,
                content_type="image/png",
                entity_type=entity_type,
                entity_id=str(
                    transaction_id
                ),
            )

            item["attachment_id"] = (
                result["id"]
            )

            item["attachment_error"] = None

            attachments_uploaded += 1

        except Exception as exc:
            message = (
                f"{path.name}: {exc}"
            )

            item["attachment_error"] = (
                message
            )

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

    document[
        "check_match_results"
    ] = check_match_results

@router.post(
    "/{document_id}/push-to-qbo",
    response_model=PushToQBOResponse,
)

async def push_to_qbo(
    document_id: str,
    request: PushToQBORequest,
):
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]
    document_type = document.get("document_type")

    if document_type == DocumentType.BANK_STATEMENT:
        if not request.attach_documents:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Bank statement QBO push "
                    "currently performs matched "
                    "check attachments, so "
                    "attach_documents must be true"
                ),
            )

        from app.api.qbo import _get_connected_qbo_client

        client = _get_connected_qbo_client()

        _prepare_reviewed_bank_statement_matches(
            document
        )

        _validate_bank_statement_vendors_for_push(
            document
        )

        return _push_bank_statement_checks(
            document_id=document_id,
            document=document,
            client=client,
        )

    if document_type != DocumentType.INVOICE:
        raise HTTPException(
            status_code=400,
            detail=(
                "This QBO push flow supports "
                "invoices and matched "
                "bank-statement checks"
            ),
        )

    if not document.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail="Invoice has no extracted data",
        )

    if not document.get("approved"):
        raise HTTPException(
            status_code=400,
            detail="Invoice must be approved before it can be pushed to QBO",
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
                "Invoice must be in extracted state before QBO push. "
                f"Current status: {document.get('status')}"
            ),
        )

    # Circular import oluşturmamak için lokal import.
    from app.api.qbo import _get_connected_qbo_client

    client = _get_connected_qbo_client()

    # ---------------------------------------------------------
    # 1. Gerçek QBO Vendor ID doğrulaması
    # ---------------------------------------------------------

    if not request.vendor_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "vendor_id is required "
                "for invoice QBO push"
            ),
        )

    if not request.account_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "account_id is required "
                "for invoice QBO push"
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
            detail=f"Invalid extracted invoice data: {exc}",
        ) from exc

    # ---------------------------------------------------------
    # 5. Aynı mapper ile plan oluştur
    # ---------------------------------------------------------

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

    # Bundan önce QBO'ya hiçbir write yapılmadı.
    # Şimdi external write başlamak üzere.
    mark_invoice_push_started(document)

    try:
        # ---------------------------------------------
        # 6. GERÇEK QBO WRITE
        # ---------------------------------------------

        if plan["method"] == "create_bill":
            result = client.create_bill(
                **plan["kwargs"]
            )

        elif plan["method"] == "create_expense":
            result = client.create_expense(
                **plan["kwargs"]
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

        mark_invoice_push_succeeded(
            document=document,
            transaction_id=transaction_id,
            transaction_type=transaction_type,
        )
        if request.attach_documents:
            temp_path = None

            try:
                filename = (
                    document.get("filename")
                    or f"{document_id}.bin"
                )

                content_type = (
                    document.get("content_type")
                    or "application/octet-stream"
                )

                file_content = document.get("content")

                if not file_content:
                    raise ValueError(
                        "Original document content is missing"
                    )

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

                attachment_result = client.upload_attachment(
                    file_path=temp_path,
                    file_name=filename,
                    content_type=content_type,
                    entity_type=entity_type,
                    entity_id=transaction_id,
                )

                document["qbo_attachment_id"] = (
                    attachment_result["id"]
                )

                document["qbo_attachment_error"] = None

                attachments_uploaded = 1

            except Exception as exc:
                document["qbo_attachment_error"] = str(exc)

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

    except Exception as exc:
        # External write başladıktan sonra oluşan hata için
        # otomatik retry güvenli değildir.
        mark_invoice_push_uncertain(
            document=document,
            error=str(exc),
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "QBO push outcome is uncertain. "
                "Do not retry automatically. "
                "Check QuickBooks Online before trying again."
            ),
        ) from exc

    return PushToQBOResponse(
    id=document_id,
    success=True,
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
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[
        document_id
    ]

    if (
        document.get("document_type")
        != DocumentType.BANK_STATEMENT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO matching in this endpoint "
                "requires a bank statement"
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
    accounts = client.get_accounts(
        account_type="Bank"
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

    if selected_bank_account is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "bank_account_id must reference "
                "an existing QBO Bank account"
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
                bank_account_id=(
                    request.bank_account_id
                ),
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

    document["extracted_data"] = (
        statement.model_dump()
    )

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

    # Store result for review.
    # No QBO write happens here.
    document[
        "qbo_match_results"
    ] = match_results

    document["status"] = (
        ProcessingStatus.MATCHED
    )

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
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[
        document_id
    ]

    if (
        document.get("document_type")
        != DocumentType.BANK_STATEMENT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Vendor resolution is only "
                "available for bank statements"
            ),
        )

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

@router.patch(
    "/{document_id}/match-review/{transaction_index}"
)
async def review_bank_statement_match(
    document_id: str,
    transaction_index: int,
    review: BankMatchReviewRequest,
):
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]

    if (
        document.get("document_type")
        != DocumentType.BANK_STATEMENT
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
    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    document = documents_db[document_id]

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