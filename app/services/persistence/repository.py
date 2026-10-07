"""Store document workflow snapshots with optimistic concurrency control.

Call save explicitly after changing nested review or push data. A returned
dictionary is detached: modifying it alone does not update the database.
"""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.core import database
from app.models.database import Company, Document


class DocumentConflictError(RuntimeError):
    """The snapshot is stale or its public ID already exists."""


def _json_default(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported workflow value: {type(value).__name__}")


class DocumentRepository:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    def _session(self):
        factory = self.session_factory or database.SessionLocal
        return factory()

    def get(self, document_id: str) -> dict | None:
        with self._session() as session:
            row = session.scalar(
                select(Document).where(Document.api_id == document_id)
            )
            if row is None:
                return None
            if row.workflow_state is None:
                raise RuntimeError("Document has no persisted workflow state")

            # Return an independent snapshot, not an ORM mutable JSON object.
            document = json.loads(json.dumps(row.workflow_state))
            document["id"] = row.api_id
            document["content"] = row.source_content
            document["_revision"] = row.revision
            return document

    def save(self, document: dict) -> None:
        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Document id is required")
        if not document.get("filename"):
            raise ValueError("Document filename is required")

        company_key = document.get("company_id", "default")
        if not isinstance(company_key, str) or not company_key:
            raise ValueError("Document company_id must be a nonempty string")

        content = document.get("content")
        if content is not None and not isinstance(content, bytes):
            raise TypeError("Document content must be bytes")

        expected_revision = document.get("_revision")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("Invalid document revision")

        state = {
            key: value
            for key, value in document.items()
            if key not in {"content", "_revision"}
        }
        # Round-trip detaches every nested list/dict and serializes actual
        # enum/date/Decimal values produced by the extraction models.
        state = json.loads(json.dumps(state, default=_json_default))
        state["company_id"] = company_key

        with self._session() as session:
            try:
                row = session.scalar(
                    select(Document).where(Document.api_id == document_id)
                )

                if expected_revision is None:
                    if row is not None:
                        raise DocumentConflictError(
                            "Document already exists; reload before saving"
                        )

                    # Legacy API keys (including 'default') are represented
                    # by valid, deterministic Company UUIDs in the ORM.
                    company_uuid = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"invoice-qbo-automation:company:{company_key}",
                        )
                    )
                    company = session.get(Company, company_uuid)
                    if company is None:
                        company = Company(id=company_uuid, name=company_key)
                        session.add(company)
                        session.flush()

                    row = Document(
                        api_id=document_id,
                        company_id=company_uuid,
                        filename=state["filename"],
                    )
                    session.add(row)
                else:
                    if row is None or row.revision != expected_revision:
                        raise DocumentConflictError(
                            "Document changed; reload before saving"
                        )
                    if row.workflow_state.get("company_id") != company_key:
                        raise ValueError("Document company_id cannot be changed")

                row.filename = state["filename"]
                row.content_type = state.get("content_type")
                row.file_size = len(content) if content is not None else None
                row.storage_key = state.get("storage_key")
                row.document_type = state.get("document_type")
                row.status = state.get("status", "uploaded")
                row.error_message = state.get("error")
                row.source_content = content
                row.workflow_state = state

                session.flush()
                revision = row.revision
                session.commit()
            except (IntegrityError, StaleDataError) as exc:
                session.rollback()
                raise DocumentConflictError(
                    "Concurrent document/company write; reload before saving"
                ) from exc
            except Exception:
                session.rollback()
                raise

        # Advance the caller's version only after the commit succeeded.
        document["_revision"] = revision
