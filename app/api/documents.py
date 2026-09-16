import os
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.schemas.documents import (
    BankStatementData,
    DocumentType,
    DocumentUploadResponse,
    ExtractionResponse,
    ProcessingStatus,
    ReceiptData,
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

            document["extracted_data"] = (
                extracted.model_dump()
            )

        elif document_type == DocumentType.BANK_STATEMENT:
            extracted = extractor.extract_bank_statement(
                page_images
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