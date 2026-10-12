"""Use real authentication: no dependency overrides in this module."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.auth import COOKIE_NAME, current_principal, hash_password, verify_password
from app.core.config import settings
from app.main import app
from app.models.database import AuthSession, Company, User, utc_now
from app.services.persistence.auth_repository import AuthRepository, company_uuid
from app.services.persistence.repository import DocumentRepository
from manage_users import initialize_company
from tests.test_qbo_connection_persistence import FakeOAuthClient

pytestmark = pytest.mark.auth_live
PASSWORD = "Strong-test-password-100"


@pytest.fixture
def tenants(monkeypatch):
    a = initialize_company("default", "First Company")
    b = initialize_company("beta", "Second Company")
    repo = AuthRepository()
    admin_a = repo.create_user(a, "admin-a@example.test", PASSWORD, "Admin A", "admin")
    admin_b = repo.create_user(b, "admin-b@example.test", PASSWORD, "Admin B", "admin")
    user_a = repo.create_user(a, "user-a@example.test", PASSWORD, "User A", "user")
    for key in ["default", "beta"]:
        DocumentRepository().save({"id": "doc_" + key, "company_id": key, "filename": key + ".pdf",
                                   "content_type": "application/pdf", "content": b"%PDF-tenant-source",
                                   "status": "uploaded", "document_type": None, "qbo_push_state": "not_started"})
    monkeypatch.setattr(settings, "qbo_client_id", "test-client")
    monkeypatch.setattr(settings, "qbo_client_secret", "test-secret")
    monkeypatch.setattr(settings, "qbo_token_encryption_key", Fernet.generate_key().decode())
    from app.api import qbo
    class ScopedFake(FakeOAuthClient):
        def get_accounts(self):
            return [{"realm": self.realm_id}]
    monkeypatch.setattr(qbo, "QBOClient", ScopedFake)
    return {"a": admin_a, "b": admin_b, "user": user_a}


def signed_in(email, password=PASSWORD):
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    info = client.get("/api/auth/me").json()
    client.headers["X-CSRF-Token"] = info["csrf_token"]
    return client


def authorize(client, realm):
    response = client.post("/api/qbo/connect")
    assert response.status_code == 200, response.text
    state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
    response = client.get("/api/qbo/callback", params={"state": state, "code": "code", "realmId": realm})
    assert response.status_code == 200, response.text
    return state


PROTECTED = [
    ("GET", "/api/documents", None), ("GET", "/api/documents/doc_default/source", None),
    ("POST", "/api/documents/doc_default/extract", None),
    ("GET", "/api/documents/doc_default/recovery", None),
    ("GET", "/api/qbo/status", None), ("GET", "/api/qbo/accounts", None),
    ("POST", "/api/qbo/connect", None), ("POST", "/api/qbo/disconnect", None),
    ("GET", "/api/auth/users", None), ("POST", "/api/auth/logout", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_anonymous_requests_are_blocked(method, path, body):
    assert TestClient(app).request(method, path, json=body).status_code == 401


CROSS_DOCUMENT = [
    ("GET", "", None), ("GET", "/source", None), ("GET", "/extracted", None),
    ("GET", "/match-review", None), ("GET", "/category-review", None),
    ("GET", "/bank-workflow", None), ("GET", "/recovery", None),
    ("POST", "/extract", None), ("POST", "/approve", None),
    ("POST", "/push-to-qbo", {}), ("POST", "/match-to-qbo", {"bank_account_id": "1"}),
    ("POST", "/resolve-vendors", None), ("POST", "/suggest-categories", None),
    ("PATCH", "/extracted", {"vendor": "Changed"}),
    ("PATCH", "/match-review/0", {"decision": "approve"}),
    ("PATCH", "/category-review/0", {"decision": "approve"}),
    ("POST", "/recovery", {"key": "transaction", "operation": "verify", "expected_revision": 1, "expected_realm_id": "realm-a"}),
]


@pytest.mark.parametrize("method,suffix,body", CROSS_DOCUMENT)
def test_other_company_document_is_not_read_or_modified(tenants, method, suffix, body):
    client = signed_in(tenants["b"]["email"])
    before = copy.deepcopy(DocumentRepository().get("doc_default"))
    response = client.request(method, "/api/documents/doc_default" + suffix, json=body)
    assert response.status_code == 404, response.text
    assert DocumentRepository().get("doc_default") == before


def test_company_scope_cannot_be_supplied_by_query_or_upload(tenants):
    client = signed_in(tenants["b"]["email"])
    assert client.get("/api/documents?company_id=default").status_code == 403
    files = {"file": ("new.pdf", b"%PDF", "application/pdf")}
    assert client.post("/api/documents/upload?company_id=default&auto_process=false", files=files).status_code == 403
    response = client.post("/api/documents/upload?auto_process=false", files=files)
    assert response.status_code == 200
    doc = DocumentRepository().get(response.json()["id"])
    assert doc["company_id"] == "beta"
    assert "doc_default" not in [item["id"] for item in client.get("/api/documents").json()["items"]]


@pytest.mark.parametrize("path,body", [("/api/documents/doc_default/approve", None), ("/api/qbo/connect", None),
                                      ("/api/auth/logout", None), ("/api/auth/users", {"email": "new@example.test", "name": "New", "password": PASSWORD})])
def test_csrf_is_required_before_mutations(tenants, path, body):
    client = signed_in(tenants["a"]["email"])
    del client.headers["X-CSRF-Token"]
    assert client.post(path, json=body).status_code == 403


def test_login_origin_and_protected_origin_are_checked(tenants):
    anonymous = TestClient(app)
    assert anonymous.post("/api/auth/login", headers={"Origin": "https://evil.example"}, json={"email": tenants["a"]["email"], "password": PASSWORD}).status_code == 403
    client = signed_in(tenants["a"]["email"])
    assert client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200


def test_logout_and_expiry_revoke_cookie(tenants, isolated_document_database):
    client = signed_in(tenants["a"]["email"])
    cookie = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set(COOKIE_NAME, cookie)
    assert client.get("/api/documents").status_code == 401
    client = signed_in(tenants["a"]["email"])
    with isolated_document_database.begin() as session:
        session.execute(update(AuthSession).values(expires_at=utc_now() - timedelta(seconds=1)))
    assert client.get("/api/auth/me").status_code == 401


def test_cookie_is_http_only_and_database_does_not_store_raw_token(tenants, isolated_document_database):
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"email": tenants["a"]["email"], "password": PASSWORD})
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    cookie = client.cookies.get(COOKIE_NAME)
    with isolated_document_database() as session:
        row = session.scalar(select(AuthSession))
        user = session.get(User, tenants["a"]["id"])
        assert row.token_hash != cookie and PASSWORD not in user.password_hash
        assert verify_password(PASSWORD, user.password_hash)
    assert COOKIE_NAME not in client.get("/api/auth/me").text


def test_user_role_can_use_documents_but_cannot_manage_users_or_qbo_connection(tenants):
    client = signed_in(tenants["user"]["email"])
    assert client.get("/api/documents/doc_default/source").status_code == 200
    assert client.get("/api/auth/users").status_code == 403
    for path in ["/api/qbo/connect", "/api/qbo/disconnect", "/api/qbo/refresh"]:
        assert client.post(path).status_code == 403
    assert client.post("/api/auth/users", json={"email": "new@example.test", "name": "New", "password": PASSWORD}).status_code == 403


def test_user_administration_is_scoped_and_revokes_sessions(tenants):
    admin = signed_in(tenants["a"]["email"])
    other = signed_in(tenants["user"]["email"])
    response = admin.post("/api/auth/users", json={"email": "new@example.test", "name": "New", "password": PASSWORD, "company_id": "beta"})
    assert response.status_code == 200
    assert {u["email"] for u in admin.get("/api/auth/users").json()["items"]} == {tenants["a"]["email"], tenants["user"]["email"], "new@example.test"}
    assert admin.patch("/api/auth/users/" + tenants["b"]["id"], json={"is_active": False}).status_code == 404
    assert admin.patch("/api/auth/users/" + tenants["a"]["id"], json={"is_active": False}).status_code == 409
    assert admin.patch("/api/auth/users/" + tenants["user"]["id"], json={"is_active": False}).status_code == 200
    assert other.get("/api/documents").status_code == 401
    assert TestClient(app).post("/api/auth/login", json={"email": tenants["user"]["email"], "password": PASSWORD}).status_code == 401


def test_password_change_revokes_all_sessions(tenants):
    first = signed_in(tenants["a"]["email"])
    second = signed_in(tenants["a"]["email"])
    assert first.post("/api/auth/password", json={"old_password": "wrong", "password": PASSWORD + "new"}).status_code == 403
    assert first.post("/api/auth/password", json={"old_password": PASSWORD, "password": PASSWORD + "new"}).status_code == 200
    assert second.get("/api/auth/me").status_code == 401
    assert TestClient(app).post("/api/auth/login", json={"email": tenants["a"]["email"], "password": PASSWORD}).status_code == 401
    assert signed_in(tenants["a"]["email"], PASSWORD + "new").get("/api/documents").status_code == 200


def test_invalid_logins_are_throttled_and_do_not_issue_sessions(tenants):
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": tenants["a"]["email"], "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": tenants["a"]["email"], "password": PASSWORD}).status_code == 429
    assert COOKIE_NAME not in client.cookies


def test_oauth_state_is_bound_to_company_and_browser_session(tenants):
    a = signed_in(tenants["a"]["email"])
    a2 = signed_in(tenants["a"]["email"])
    b = signed_in(tenants["b"]["email"])
    state = parse_qs(urlparse(a.post("/api/qbo/connect").json()["authorization_url"]).query)["state"][0]
    params = {"state": state, "code": "code", "realmId": "realm-a"}
    assert b.get("/api/qbo/callback", params=params).status_code == 400
    assert a2.get("/api/qbo/callback", params=params).status_code == 400
    assert a.get("/api/qbo/callback", params=params).status_code == 200
    assert a.get("/api/qbo/callback", params=params).status_code == 400
    assert b.get("/api/qbo/status").json()["connected"] is False


def test_qbo_connection_and_sdk_are_isolated_across_simultaneous_requests(tenants):
    a = signed_in(tenants["a"]["email"])
    b = signed_in(tenants["b"]["email"])
    authorize(a, "realm-a")
    authorize(b, "realm-b")
    pairs = [(a, "realm-a"), (b, "realm-b")] * 5
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda pair: (pair[0].get("/api/qbo/accounts").json(), pair[1]), pairs))
    for value, realm in values:
        assert value["accounts"] == [{"realm": realm}]
    assert a.post("/api/qbo/disconnect").status_code == 200
    assert a.get("/api/qbo/status").json()["connected"] is False
    assert b.get("/api/qbo/status").json()["company_id"] == "realm-b"


def test_existing_qbo_company_cannot_be_taken_by_another_company(tenants):
    a = signed_in(tenants["a"]["email"])
    b = signed_in(tenants["b"]["email"])
    authorize(a, "realm-a")
    state = parse_qs(urlparse(b.post("/api/qbo/connect").json()["authorization_url"]).query)["state"][0]
    assert b.get("/api/qbo/callback", params={"state": state, "code": "code", "realmId": "realm-a"}).status_code == 409
    assert b.get("/api/qbo/status").json()["connected"] is False
    assert a.get("/api/qbo/status").json()["company_id"] == "realm-a"


def test_other_company_qbo_document_options_are_blocked_before_sdk(tenants, monkeypatch):
    client = signed_in(tenants["b"]["email"])
    from app.api import qbo
    monkeypatch.setattr(qbo, "_get_connected_qbo_client", lambda: (_ for _ in ()).throw(AssertionError("must not call QBO")))
    assert client.get("/api/qbo/invoice-options/doc_default").status_code == 404
    assert client.post("/api/qbo/invoice-plan/doc_default", json={"create_as": "bill", "vendor_id": "30", "account_id": "10"}).status_code == 404


def test_company_context_does_not_escape_a_request(tenants):
    assert current_principal.get() is None
    a = signed_in(tenants["a"]["email"])
    a.get("/api/documents")
    assert current_principal.get() is None
    assert DocumentRepository().get("doc_beta") is not None


def test_real_session_and_company_scope_survive_process_restart(tenants, isolated_document_database):
    import json, os, subprocess, sys
    from pathlib import Path
    client = signed_in(tenants["b"]["email"])
    cookie = client.cookies.get(COOKIE_NAME)
    env = os.environ.copy()
    env['DATABASE_URL'] = str(isolated_document_database.kw['bind'].url)
    program = '''
import json,sys
from fastapi.testclient import TestClient
from app.main import app
cookie=json.loads(sys.stdin.read())
client=TestClient(app,headers={"Cookie":cookie})
response=client.get("/api/documents")
print(json.dumps({"status":response.status_code,"body":response.json()}))
'''
    def restart():
        result = subprocess.run([sys.executable, '-c', program],input=json.dumps(COOKIE_NAME+'='+cookie),
            text=True,capture_output=True,check=True,env=env,cwd=Path(__file__).resolve().parents[1],timeout=30)
        return json.loads(result.stdout)
    result=restart()
    assert result['status']==200 and [v['id'] for v in result['body']['items']]==['doc_beta']
    client.post('/api/auth/logout')
    assert restart()['status']==401


def test_migration_keeps_old_documents_and_default_qbo_company(tmp_path):
    import json, os, subprocess, sys
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from app.services.persistence.auth_repository import company_uuid
    path=tmp_path/'old.db';env=os.environ.copy();env['DATABASE_URL']='sqlite:///'+str(path)
    root=Path(__file__).resolve().parents[1]
    def migrate(target):
        subprocess.run([sys.executable,'-m','alembic','upgrade',target],env=env,cwd=root,capture_output=True,check=True,timeout=30)
    migrate('94a1d0c20260')
    company=company_uuid('default').replace('-','')
    engine=create_engine(env['DATABASE_URL'])
    with engine.begin() as session:
        session.execute(text("INSERT INTO companies (id,name) VALUES (:id,'default')"),{'id':company})
        session.execute(text("INSERT INTO documents (id,company_id,filename,api_id,workflow_state,revision) VALUES (:id,:company,'old.pdf','doc_old',:state,7)"),
                        {'id':'00000000000000000000000000000100','company':company,'state':json.dumps({'company_id':'default','status':'pushed','qbo_transaction_id':'701'})})
        session.execute(text("INSERT INTO qbo_connections (id,company_id,realm_id,is_active) VALUES (:id,:company,'realm-old',1)"),{'id':'00000000000000000000000000000200','company':company})
    migrate('head')
    with engine.connect() as session:
        assert session.execute(text('SELECT api_key FROM companies')).scalar()=='default'
        row=session.execute(text('SELECT api_id,revision,workflow_state FROM documents')).one()
        assert row.api_id=='doc_old' and row.revision==7 and json.loads(row.workflow_state)['qbo_transaction_id']=='701'
        assert session.execute(text('SELECT company_id FROM qbo_connections')).scalar()==company
    engine.dispose()


def test_secure_cookie_works_over_https(tenants, monkeypatch):
    monkeypatch.setattr(settings, 'auth_cookie_secure', True)
    client=TestClient(app,base_url='https://testserver')
    response=client.post('/api/auth/login',json={'email':tenants['a']['email'],'password':PASSWORD})
    assert response.status_code==200 and 'Secure' in response.headers['set-cookie']
    assert client.get('/api/auth/me').status_code==200


def test_concurrent_admin_role_changes_retain_an_active_admin(tenants, isolated_document_database):
    a=signed_in(tenants['a']['email'])
    assert a.patch('/api/auth/users/'+tenants['user']['id'],json={'role':'admin'}).status_code==200
    b=signed_in(tenants['user']['email'])
    pairs=[(a,tenants['user']['id']),(b,tenants['a']['id'])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda pair:pair[0].patch('/api/auth/users/'+pair[1],json={'role':'user'}),pairs))
    assert sum(r.status_code==200 for r in responses)==1
    with isolated_document_database() as session:
        admins=list(session.scalars(select(User).where(User.company_id==company_uuid('default'),User.role=='admin',User.is_active.is_(True))))
        assert len(admins)==1
