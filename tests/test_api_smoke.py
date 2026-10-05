from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200

def test_nonexistent_document_returns_404():
    response = client.get(
        "/api/documents/doc_does_not_exist"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"