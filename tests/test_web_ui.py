from fastapi.testclient import TestClient

from app.api import documents as api
from app.main import app

client = TestClient(app)


def test_home_page_serves_real_ui_and_local_assets():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert 'href="/static/app.css"' in response.text
    assert 'src="/static/app.js"' in response.text
    for path, media in [("/static/app.css", "text/css"), ("/static/app.js", "javascript")]:
        asset = client.get(path)
        assert asset.status_code == 200
        assert media in asset.headers["content-type"]


def test_static_mount_does_not_expose_python_files():
    for path in ["/static/main.py", "/static/%2e%2e/core/config.py", "/static/%2e%2e/%2e%2e/.env"]:
        assert client.get(path).status_code == 404


def test_ui_upload_list_source_contract_is_persistent(isolated_document_database, monkeypatch):
    def unexpected_processing(*args):
        raise AssertionError("UI upload must not start AI extraction")
    monkeypatch.setattr(api, "process_document", unexpected_processing)
    content = b"%PDF-1.4\nsource-for-ui\n%%EOF"
    upload = client.post("/api/documents/upload?company_id=default&auto_process=false",
                         files={"file": ("ui-invoice.pdf", content, "application/pdf")})
    assert upload.status_code == 200
    document_id = upload.json()["id"]
    response = client.get("/api/documents?company_id=default&limit=20&offset=0")
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == document_id
    assert response.json()["items"][0]["status"] == "uploaded"
    source = client.get(f"/api/documents/{document_id}/source")
    assert source.status_code == 200 and source.content == content
    # Another OS process uses the same database, with no browser memory.
    from tests.test_document_api_persistence import restart_request
    assert restart_request(isolated_document_database, "GET", "/api/documents")["body"]["items"][0]["id"] == document_id
