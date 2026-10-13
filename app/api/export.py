"""Read-only, company-scoped reports from persisted workflow fields."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core import database
from app.core.auth import Principal, company_key, get_current_user
from app.models.database import Document
from app.schemas.documents import (
    BankStatementData, BankTransaction, CheckData, CheckImage, DocumentType,
    InvoiceValidationResult, LineItem, ProcessingStatus, ReceiptData,
)

router = APIRouter()
MAX_DOCUMENTS = 1000
CSV_COLUMNS = [
    "ID", "Filename", "Upload Date", "Status", "Document Type", "Vendor",
    "Amount", "Currency", "Date", "Category", "Approved", "QBO ID",
    "QBO Type", "QBO Status", "Attachment ID", "Attachment Status",
]


def _project(data, model, nested=None):
    """Keep actual extraction fields, without server paths or raw OCR text."""
    if not isinstance(data, dict):
        return None
    result = {key: value for key, value in data.items()
              if key in model.model_fields and key not in {"image_path", "raw_text"}}
    for key, (child_model, many, children) in (nested or {}).items():
        if key not in result:
            continue
        value = result[key]
        if many:
            result[key] = [_project(item, child_model, children)
                           for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        else:
            result[key] = _project(value, child_model, children)
    return result


def _extracted(data, kind):
    if kind in {"receipt", "invoice", "bill"}:
        return _project(data, ReceiptData, {"line_items": (LineItem, True, None)})
    if kind == "bank_statement":
        return _project(data, BankStatementData, {
            "transactions": (BankTransaction, True, {"check_image": (CheckData, False, None)}),
            "check_images": (CheckImage, True, None),
        })
    if kind == "check":
        return _project(data, CheckData)
    return None


def _timestamp(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()


def report(
    company_id: str | None = Query(None, min_length=1, max_length=100),
    status: ProcessingStatus | None = None,
    document_type: DocumentType | None = None,
    days: int = Query(30, ge=1, le=365),
    user: Principal = Depends(get_current_user),
):
    scope = company_key(company_id)
    now = datetime.now(timezone.utc)
    filters = [Document.company_id == user.company_id, Document.api_id.is_not(None),
               Document.created_at >= (now - timedelta(days=days)).replace(tzinfo=None)]
    if status is not None:
        filters.append(Document.status == status.value)
    if document_type is not None:
        filters.append(Document.document_type == document_type.value)
    # Project individual JSON fields: source bytes, cropped images, tokens and
    # internal push contexts never enter the result or the Python query rows.
    fields = ["extracted_data", "validation", "approved", "qbo_transaction_id",
              "qbo_transaction_type", "qbo_push_state", "qbo_attachment_id",
              "qbo_attachment_push_status"]
    query = select(Document.api_id, Document.filename, Document.content_type,
                   Document.created_at, Document.processed_at, Document.status,
                   Document.document_type,
                   *((Document.workflow_state[field].as_boolean() if field == "approved"
                      else Document.workflow_state[field]).label(field) for field in fields))
    try:
        with database.SessionLocal() as session:
            rows = session.execute(query.where(*filters).order_by(
                Document.created_at.desc(), Document.api_id.desc()).limit(MAX_DOCUMENTS + 1)).mappings().all()
    except SQLAlchemyError:
        raise HTTPException(503, "Export storage is unavailable") from None
    if len(rows) > MAX_DOCUMENTS:
        raise HTTPException(413, "Export exceeds 1000 documents; narrow days, status or document_type")
    items = []
    for row in rows:
        items.append({
            "id": row["api_id"], "filename": row["filename"], "content_type": row["content_type"],
            "created_at": _timestamp(row["created_at"]), "processed_at": _timestamp(row["processed_at"]),
            "status": row["status"], "document_type": row["document_type"],
            "approved": row["approved"] is True,
            "extracted_data": _extracted(row["extracted_data"], row["document_type"]),
            "validation": _project(row["validation"], InvoiceValidationResult),
            "qbo_transaction_id": row["qbo_transaction_id"],
            "qbo_transaction_type": row["qbo_transaction_type"],
            "qbo_push_state": row["qbo_push_state"],
            "qbo_attachment_id": row["qbo_attachment_id"],
            "qbo_attachment_push_status": row["qbo_attachment_push_status"],
        })
    return {"exported_at": now.isoformat(), "total_documents": len(items),
            "filters": {"company_id": scope, "status": status, "document_type": document_type, "days": days},
            "documents": items}


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    # Quoting CSV is insufficient to prevent spreadsheet formula execution.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _headers(extension):
    filename = f"receipt-ai-export-{datetime.now(timezone.utc):%Y%m%d}.{extension}"
    return {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"}


@router.get("/json")
def export_json(data: dict = Depends(report)):
    return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                    headers=_headers("json"))


@router.get("/csv")
def export_csv(data: dict = Depends(report)):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for document in data["documents"]:
        extracted = document["extracted_data"] or {}
        values = [document["id"], document["filename"], document["created_at"], document["status"],
                  document["document_type"], extracted.get("vendor"), extracted.get("total_amount"),
                  extracted.get("currency"), extracted.get("date"), extracted.get("category_suggestion"),
                  document["approved"], document["qbo_transaction_id"], document["qbo_transaction_type"],
                  document["qbo_push_state"], document["qbo_attachment_id"], document["qbo_attachment_push_status"]]
        writer.writerow([_csv_cell(value) for value in values])
    # UTF-8 BOM preserves Turkish characters in spreadsheet applications.
    return Response(("\ufeff" + output.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers=_headers("csv"))
