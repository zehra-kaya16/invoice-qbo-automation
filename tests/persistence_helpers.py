"""Test-only adapter for legacy documents_db seeds; writes are explicit."""

from app.services.persistence.repository import DocumentRepository


class DocumentSeeds:
    def __getitem__(self, document_id):
        value = DocumentRepository().get(document_id)
        if value is None:
            raise KeyError(document_id)
        return value

    def __setitem__(self, document_id, document):
        if document.get("id") != document_id:
            raise ValueError("Seed ID does not match document ID")
        repo = DocumentRepository()
        if "_revision" not in document:
            existing = repo.get(document_id)
            if existing is not None:
                # Tests may intentionally reset a fixture under the same ID.
                document["_revision"] = existing["_revision"]
        repo.save(document)


documents_db = DocumentSeeds()
