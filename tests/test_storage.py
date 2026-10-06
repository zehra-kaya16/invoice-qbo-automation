import io

from app.services.storage.storage import (
    DocumentStorage,
    LocalStorage,
)


def test_local_storage_upload_download_delete(tmp_path):
    backend = LocalStorage(
        base_path=str(tmp_path)
    )

    key = "company/test/example.txt"

    uploaded_path = backend.upload(
        io.BytesIO(b"hello"),
        key=key,
        content_type="text/plain",
    )

    assert uploaded_path

    content = backend.download(key)

    assert content == b"hello"

    assert backend.delete(key) is True
    assert backend.delete(key) is False


def test_document_storage_upload_document(tmp_path):
    storage = DocumentStorage(
        backend=LocalStorage(
            base_path=str(tmp_path)
        )
    )

    result = storage.upload_document(
        file=io.BytesIO(b"invoice-data"),
        filename="invoice.pdf",
        content_type="application/pdf",
        company_id="test-company",
        document_type="invoice",
    )

    assert result["filename"] == "invoice.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["company_id"] == "test-company"
    assert result["document_type"] == "invoice"

    assert result["key"].startswith(
        "test-company/invoice/"
    )

    stored_content = storage.get_document(
        result["key"]
    )

    assert stored_content == b"invoice-data"