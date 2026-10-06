from fastapi.testclient import TestClient

from app.api import documents as documents_api
from app.main import app

client = TestClient(app)


def test_upload_with_auto_process_false_does_not_process(
    monkeypatch,
):
    called = []

    async def fake_process_document(document_id):
        called.append(document_id)

    monkeypatch.setattr(
        documents_api,
        "process_document",
        fake_process_document,
    )

    response = client.post(
        "/api/documents/upload",
        params={
            "auto_process": "false",
        },
        files={
            "file": (
                "test.png",
                b"fake-image-data",
                "image/png",
            )
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "uploaded"
    assert called == []

    assert data["message"] == (
        "Document uploaded. "
        "Call /extract to process."
    )


def test_upload_with_auto_process_true_runs_processing(
    monkeypatch,
):
    called = []

    async def fake_process_document(document_id):
        called.append(document_id)

    monkeypatch.setattr(
        documents_api,
        "process_document",
        fake_process_document,
    )

    response = client.post(
        "/api/documents/upload",
        params={
            "auto_process": "true",
        },
        files={
            "file": (
                "test.png",
                b"fake-image-data",
                "image/png",
            )
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert len(called) == 1
    assert called[0] == data["id"]

    assert data["message"] == (
        "Document uploaded successfully. "
        "Processing started."
    )