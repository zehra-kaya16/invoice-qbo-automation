import io
import stat
import struct
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import batch
from app.main import app
from app.models.database import Document, UploadBatch
from app.services.persistence import zip_upload
from tests.test_document_api_persistence import restart_request

client = TestClient(app)


@pytest.fixture(autouse=True)
def existing_company():
    from manage_users import initialize_company
    initialize_company("default", "Test company")


def archive(entries=None, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as z:
        for name, content in (entries if entries is not None else [("invoices/first.pdf", b"%PDF-1.4\nfirst"),
                                                                       ("second.pdf", b"%PDF-1.4\nsecond")]):
            z.writestr(name, content)
    return output.getvalue()


def upload(content=None, key=None, name="batch.zip", caller=client, query=""):
    return caller.post("/api/batch/upload-zip" + query, headers={"Idempotency-Key": key or str(uuid.uuid4())},
                       files={"file": (name, archive() if content is None else content, "application/zip")})


def assert_no_rows(factory):
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UploadBatch)) == 0
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_zip_members_persist_source_status_and_restart(isolated_document_database):
    response = upload()
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_files"] == 2 and data["pending"] == 2
    assert data["items"][0]["filename"] == "invoices/first.pdf"
    assert client.get("/api/documents/" + data["uploaded_documents"][0] + "/source").content == b"%PDF-1.4\nfirst"
    assert restart_request(isolated_document_database, "GET", "/api/batch/status/" + data["batch_id"])["body"] == data


def test_same_zip_key_reuses_ids_and_payload_or_endpoint_change_is_409():
    key = str(uuid.uuid4())
    first = upload(key=key).json()
    assert upload(key=key).json() == first
    assert upload(archive([("new.pdf", b"%PDF-1.4 changed")]), key=key).status_code == 409
    normal = client.post("/api/batch/upload", headers={"Idempotency-Key": key},
                         files={"files": ("first.pdf", b"%PDF-1.4\nfirst", "application/pdf")})
    assert normal.status_code == 409
    assert client.get("/api/documents").json()["total"] == 2


def test_concurrent_zip_retries_are_atomic():
    key = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: upload(key=key), range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert responses[0].json()["uploaded_documents"] == responses[1].json()["uploaded_documents"]
    assert client.get("/api/batch").json()["total"] == 1


def test_visible_errors_and_hidden_metadata_are_reported_correctly():
    data = upload(archive([("folder/ok.PDF", b"%PDF-1.4 ok"), ("folder/readme.txt", b"text"),
                           ("empty.pdf", b""), ("fake.pdf", b"not-pdf"), ("__MACOSX/._ok.pdf", b"meta"),
                           (".hidden/secret.pdf", b"%PDF-1.4 hidden"), ("folder/", b"")])).json()
    assert data["total_files"] == 4 and data["pending"] == 1 and data["failed"] == 3
    assert len(data["failed_files"]) == 3
    assert data["items"][0]["filename"] == "folder/ok.PDF"


@pytest.mark.parametrize("path", ["../evil.pdf", "folder/../../evil.pdf", "/absolute.pdf", "C:\\evil.pdf",
                                  "\\\\host\\evil.pdf", "file.pdf:stream", "folder/./evil.pdf", "bad\x00.pdf"])
def test_unsafe_paths_reject_entire_archive(path, isolated_document_database):
    payload = archive([("good.pdf", b"%PDF-1.4 good"), (path.replace("\x00", "X"), b"%PDF-1.4 evil")])
    # ZipInfo's writer strips NULs; inject one into both on-disk name fields.
    if "\x00" in path:
        payload = payload.replace(b"badX.pdf", b"bad\x00.pdf")
    assert upload(payload).status_code == 400
    assert_no_rows(isolated_document_database)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK])
def test_zip_links_and_special_files_are_rejected(mode, isolated_document_database):
    info = zipfile.ZipInfo("link.pdf");info.create_system = 3;info.external_attr = (mode | 0o777) << 16
    assert upload(archive([(info, b"%PDF-1.4")])).status_code == 400
    assert_no_rows(isolated_document_database)


def test_duplicate_case_normalized_names_are_rejected(isolated_document_database):
    assert upload(archive([("folder/a.pdf", b"%PDF-1.4"), ("folder\\A.pdf", b"%PDF-1.4")])).status_code == 400
    assert_no_rows(isolated_document_database)


@pytest.mark.parametrize("content", [b"", b"not a zip", b"PK\x03\x04garbage", archive()[:-10]])
def test_malformed_zip_is_rejected_without_writes(content, isolated_document_database):
    assert upload(content).status_code == 400
    assert_no_rows(isolated_document_database)


def test_crc_damage_and_encryption_flags_reject_without_writes(isolated_document_database):
    raw = bytearray(archive(compression=zipfile.ZIP_STORED))
    pos = raw.index(b"%PDF-");raw[pos] ^= 1
    assert upload(bytes(raw)).status_code == 400
    raw = bytearray(archive());central = raw.index(b"PK\x01\x02")
    struct.pack_into("<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | 1)
    assert upload(bytes(raw)).status_code == 400
    assert_no_rows(isolated_document_database)


def test_archive_expansion_ratio_metadata_and_count_bounds(monkeypatch, isolated_document_database):
    monkeypatch.setattr(zip_upload, "MAX_ZIP_BYTES", 10)
    assert upload().status_code == 413
    monkeypatch.setattr(zip_upload, "MAX_ZIP_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(batch, "MAX_BATCH_BYTES", 10)
    assert upload().status_code == 413
    monkeypatch.setattr(batch, "MAX_BATCH_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(batch, "MAX_FILES", 1)
    assert upload().status_code == 413
    monkeypatch.setattr(batch, "MAX_FILES", 100)
    monkeypatch.setattr(zip_upload, "MAX_ARCHIVE_ENTRIES", 1)
    assert upload().status_code == 413
    monkeypatch.setattr(zip_upload, "MAX_ARCHIVE_ENTRIES", 1000)
    assert upload(archive([("bomb.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024))])).status_code == 413
    assert_no_rows(isolated_document_database)


def test_empty_hidden_only_bzip_and_bad_filename():
    assert upload(archive([])).status_code == 400
    assert upload(archive([(".DS_Store", b"meta")])).status_code == 400
    assert upload(archive(compression=zipfile.ZIP_BZIP2)).status_code == 400
    assert upload(name="data.txt").status_code == 400


def test_member_file_size_is_partial_error(monkeypatch):
    monkeypatch.setattr(batch.settings, "max_upload_bytes", 10)
    data = upload(archive([("ok.pdf", b"%PDF-1"), ("big.pdf", b"%PDF-1.4 too large")])).json()
    assert data["pending"] == 1 and data["failed"] == 1


@pytest.mark.auth_live
def test_zip_real_auth_csrf_and_tenant_scope():
    from manage_users import initialize_company
    from app.services.persistence.auth_repository import AuthRepository
    password = "Zip-password-105"
    for key in ["default", "other"]:
        company = initialize_company(key, key)
        AuthRepository().create_user(company, key + "@example.test", password, key, "user")
    a=TestClient(app);b=TestClient(app)
    assert upload(caller=a).status_code == 401
    for caller, key in [(a, "default"), (b, "other")]:
        assert caller.post("/api/auth/login", json={"email": key + "@example.test", "password": password}).status_code == 200
    assert upload(caller=a).status_code == 403
    for caller in [a,b]:caller.headers["X-CSRF-Token"] = caller.get("/api/auth/me").json()["csrf_token"]
    data = upload(caller=a).json()
    assert b.get("/api/batch/status/" + data["batch_id"]).status_code == 404
    assert b.get("/api/documents/" + data["uploaded_documents"][0] + "/source").status_code == 404
    assert upload(caller=b, query="?company_id=default").status_code == 403
