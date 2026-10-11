import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.api import qbo as api
from app.core.config import settings
from app.main import app
from app.models.database import QBOConnection, QBOOAuthAttempt, utc_now
from app.services.persistence.qbo_repository import QBOConnectionRepository, QBOConnectionConflict


class FakeOAuthClient:
    exchanges = 0
    refreshes = 0
    fail_refresh = False
    fail_profile = False

    def __init__(self, client_id, client_secret, redirect_uri, environment):
        self.client_id, self.environment = client_id, environment
        self.qb_client = None
        self.realm_id = None

    def get_auth_url(self, state):
        return "https://example.invalid/authorize?state=" + state

    def handle_callback(self, auth_code, realm_id):
        type(self).exchanges += 1
        self.set_tokens("initial-access", "initial-refresh", realm_id, utc_now() + timedelta(hours=1))

    def set_tokens(self, access_token, refresh_token, realm_id, expires_at):
        self.access_token, self.refresh_token = access_token, refresh_token
        self.realm_id, self.token_expires_at = realm_id, expires_at
        self.qb_client = object()

    def refresh_tokens(self):
        type(self).refreshes += 1
        if self.fail_refresh:
            raise RuntimeError("provider secret must not reach HTTP response")
        self.access_token = "rotated-access"
        self.refresh_token = "rotated-refresh"
        self.token_expires_at = utc_now() + timedelta(hours=1)

    def get_company_info(self):
        if self.fail_profile:
            raise RuntimeError("profile unavailable")
        return {"company_name": "Test company", "company_id": self.realm_id}

    def get_accounts(self):
        return [{"id": "bank-1", "token_restored": self.access_token == "rotated-access"}]


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setattr(settings, "qbo_client_id", "test-client")
    monkeypatch.setattr(settings, "qbo_client_secret", "test-secret")
    monkeypatch.setattr(settings, "qbo_environment", "sandbox")
    monkeypatch.setattr(settings, "qbo_token_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(api, "QBOClient", FakeOAuthClient)
    monkeypatch.setattr(api, "qbo_client", None)
    FakeOAuthClient.exchanges = FakeOAuthClient.refreshes = 0
    FakeOAuthClient.fail_refresh = FakeOAuthClient.fail_profile = False
    return TestClient(app)


def start(client):
    response = client.get("/api/qbo/connect", follow_redirects=False)
    assert response.status_code == 307, response.text
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def callback(client, state):
    return client.get("/api/qbo/callback", params={"state": state, "code": "test-code", "realmId": "realm-1"})


def connect(client):
    response = callback(client, start(client))
    assert response.status_code == 200, response.text
    return response


def restart(factory, path, method="GET", params=None):
    env = os.environ.copy()
    env.update(DATABASE_URL=str(factory.kw["bind"].url), QBO_CLIENT_ID=settings.qbo_client_id,
        QBO_CLIENT_SECRET=settings.qbo_client_secret, QBO_ENVIRONMENT=settings.qbo_environment,
        QBO_TOKEN_ENCRYPTION_KEY=settings.qbo_token_encryption_key)
    code = '''
import json, sys
sys.path.insert(0, "tests")
from test_qbo_connection_persistence import FakeOAuthClient
from app.api import qbo
qbo.QBOClient = FakeOAuthClient
qbo.qbo_client = None
from app.main import app
from fastapi.testclient import TestClient
response = TestClient(app).request(sys.argv[1], sys.argv[2], params=json.loads(sys.argv[3]))
print(json.dumps({"status": response.status_code, "body": response.json()}))
'''
    result = subprocess.run([sys.executable, "-c", code, method, path, json.dumps(params or {})],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def expire(factory):
    with factory.begin() as session:
        session.execute(update(QBOConnection).values(token_expires_at=utc_now() - timedelta(seconds=1)))


def test_callback_encrypted_and_restart_status(oauth, isolated_document_database):
    response = connect(oauth)
    assert "initial-access" not in response.text and "initial-refresh" not in response.text
    with isolated_document_database() as session:
        row = session.scalar(select(QBOConnection))
        assert row.access_token != "initial-access" and row.refresh_token != "initial-refresh"
        assert Fernet(settings.qbo_token_encryption_key.encode()).decrypt(row.refresh_token.encode()) == b"initial-refresh"
    result = restart(isolated_document_database, "/api/qbo/status")
    assert result["status"] == 200
    assert result["body"]["connected"] is True
    assert result["body"]["company_name"] == "Test company"
    assert result["body"]["expires_at"].endswith("Z")


def test_oauth_state_survives_restart(oauth, isolated_document_database):
    state = start(oauth)
    with isolated_document_database() as session:
        assert session.scalar(select(QBOOAuthAttempt)).state_hash != state
    result = restart(isolated_document_database, "/api/qbo/callback", params={"state": state, "code": "code", "realmId": "realm-1"})
    assert result["status"] == 200
    assert oauth.get("/api/qbo/status").json()["connected"] is True


def test_oauth_state_invalid_expired_replayed(oauth, isolated_document_database):
    assert callback(oauth, "invalid").status_code == 400
    state = start(oauth)
    with isolated_document_database.begin() as session:
        session.execute(update(QBOOAuthAttempt).values(expires_at=utc_now() - timedelta(seconds=1)))
    assert callback(oauth, state).status_code == 400
    valid = start(oauth)
    assert callback(oauth, valid).status_code == 200
    assert callback(oauth, valid).status_code == 400
    assert FakeOAuthClient.exchanges == 1


def test_new_oauth_cancels_previous_attempt(oauth):
    old = start(oauth)
    new = start(oauth)
    assert callback(oauth, old).status_code == 400
    assert callback(oauth, new).status_code == 200


def test_profile_failure_does_not_lose_tokens(oauth):
    FakeOAuthClient.fail_profile = True
    assert connect(oauth).json()["company_name"] is None
    assert QBOConnectionRepository().active()["refresh_token"] == "initial-refresh"


def test_expired_access_refreshes_and_persists_rotated_tokens(oauth, isolated_document_database):
    connect(oauth)
    expire(isolated_document_database)
    response = oauth.get("/api/qbo/accounts")
    assert response.status_code == 200
    assert response.json()["accounts"][0]["token_restored"] is True
    assert QBOConnectionRepository().active()["refresh_token"] == "rotated-refresh"
    result = restart(isolated_document_database, "/api/qbo/accounts")
    assert result["status"] == 200 and result["body"]["accounts"][0]["token_restored"] is True
    assert FakeOAuthClient.refreshes == 1


def test_manual_refresh_never_returns_tokens(oauth):
    connect(oauth)
    response = oauth.post("/api/qbo/refresh")
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert "rotated-access" not in response.text and "rotated-refresh" not in response.text
    assert QBOConnectionRepository().active()["refresh_token"] == "rotated-refresh"


def test_refresh_failure_blocks_old_token_reuse(oauth, isolated_document_database):
    connect(oauth)
    expire(isolated_document_database)
    FakeOAuthClient.fail_refresh = True
    response = oauth.get("/api/qbo/accounts")
    assert response.status_code == 502 and "provider secret" not in response.text
    assert oauth.get("/api/qbo/accounts").status_code == 409
    result = restart(isolated_document_database, "/api/qbo/status")
    assert result["body"]["connected"] is False and result["body"]["reconnect_required"] is True
    assert FakeOAuthClient.refreshes == 1


def test_commit_failure_after_rotation_blocks_reuse(oauth, monkeypatch):
    connect(oauth)
    def fail(*args):
        raise OperationalError("secret SQL", {}, RuntimeError("secret"))
    monkeypatch.setattr(QBOConnectionRepository, "finish_refresh", fail)
    response = oauth.post("/api/qbo/refresh")
    assert response.status_code == 502 and "secret" not in response.text
    assert oauth.get("/api/qbo/accounts").status_code == 409
    assert FakeOAuthClient.refreshes == 1


def test_claim_commit_failure_never_calls_provider(oauth, monkeypatch):
    connect(oauth)
    def fail(*args):
        raise OperationalError("secret SQL", {}, RuntimeError("secret"))
    monkeypatch.setattr(QBOConnectionRepository, "claim_refresh", fail)
    assert oauth.post("/api/qbo/refresh").status_code == 503
    assert FakeOAuthClient.refreshes == 0


def test_refresh_claim_conflict_and_disconnect_prevent_resurrection(oauth):
    connect(oauth)
    repository = QBOConnectionRepository()
    record = repository.active()
    revision = repository.claim_refresh(record)
    with pytest.raises(QBOConnectionConflict):
        repository.claim_refresh(record)
    client = api._get_qbo_client()
    oauth.post("/api/qbo/disconnect")
    with pytest.raises(QBOConnectionConflict):
        repository.finish_refresh(record, revision, client)
    assert repository.active() is None


def test_disconnect_persists_and_cancels_pending_oauth(oauth, isolated_document_database):
    connect(oauth)
    state = start(oauth)
    assert oauth.post("/api/qbo/disconnect").status_code == 200
    assert callback(oauth, state).status_code == 400
    with isolated_document_database() as session:
        row = session.scalar(select(QBOConnection))
        assert not row.is_active and row.access_token is None and row.refresh_token is None
    result = restart(isolated_document_database, "/api/qbo/status")
    assert result["body"]["connected"] is False


def test_disconnect_during_oauth_exchange_prevents_save(oauth):
    state = start(oauth)
    repository = QBOConnectionRepository()
    repository.claim_oauth(state)
    client = api._get_qbo_client()
    client.handle_callback("code", "realm-1")
    repository.disconnect()
    with pytest.raises(QBOConnectionConflict):
        repository.save_oauth(state, client)
    assert repository.active() is None


@pytest.mark.parametrize("field,value", [("qbo_client_id", "different-client"), ("qbo_environment", "production")])
def test_connection_is_bound_to_environment_and_client(oauth, monkeypatch, field, value):
    connect(oauth)
    monkeypatch.setattr(settings, field, value)
    assert oauth.get("/api/qbo/status").json()["connected"] is False
    assert oauth.get("/api/qbo/accounts").status_code == 400


@pytest.mark.parametrize("key", [None, "invalid-key"])
def test_missing_or_invalid_key_blocks_connect(oauth, monkeypatch, key):
    monkeypatch.setattr(settings, "qbo_token_encryption_key", key)
    assert oauth.get("/api/qbo/connect", follow_redirects=False).status_code == 503
    assert FakeOAuthClient.exchanges == 0


def test_wrong_key_blocks_restoration_without_refresh(oauth, monkeypatch):
    connect(oauth)
    monkeypatch.setattr(settings, "qbo_token_encryption_key", Fernet.generate_key().decode())
    assert oauth.get("/api/qbo/accounts").status_code == 503
    assert FakeOAuthClient.refreshes == 0
    # A fresh authorization can replace the unreadable old credentials.
    connect(oauth)
    assert oauth.get("/api/qbo/status").json()["connected"] is True


def test_incomplete_refresh_response_is_not_saved(oauth, monkeypatch):
    connect(oauth)
    def incomplete(self):
        self.access_token = None
    monkeypatch.setattr(FakeOAuthClient, "refresh_tokens", incomplete)
    assert oauth.post("/api/qbo/refresh").status_code == 502
    assert oauth.get("/api/qbo/accounts").status_code == 409


def test_reconnect_recovers_uncertain_refresh(oauth):
    connect(oauth)
    FakeOAuthClient.fail_refresh = True
    assert oauth.post("/api/qbo/refresh").status_code == 502
    FakeOAuthClient.fail_refresh = False
    connect(oauth)
    assert oauth.get("/api/qbo/status").json()["connected"] is True
    assert QBOConnectionRepository().active()["token_state"] == "ready"


def test_sdk_callback_and_refresh_use_provider_expiry(monkeypatch):
    from app.services.qbo import client as module
    class FakeAuth:
        expires_in = 120
        access_token = "sdk-access"
        refresh_token = "sdk-refresh"
        def get_bearer_token(self, code, realm_id):
            self.realm_id = realm_id
        def refresh(self, refresh_token):
            self.access_token, self.refresh_token = "sdk-new-access", "sdk-new-refresh"
            self.expires_in = 240
    sdk = module.QBOClient.__new__(module.QBOClient)
    sdk.auth_client = FakeAuth()
    monkeypatch.setattr(module, "QuickBooks", lambda **kwargs: object())
    before = utc_now()
    sdk.handle_callback("code", "realm-1")
    assert before + timedelta(seconds=119) <= sdk.token_expires_at <= utc_now() + timedelta(seconds=120)
    sdk.refresh_tokens()
    assert sdk.refresh_token == "sdk-new-refresh"
    assert before + timedelta(seconds=239) <= sdk.token_expires_at <= utc_now() + timedelta(seconds=240)
    assert sdk.token_expires_at.tzinfo is None


def test_sdk_restore_sets_auth_tokens_before_sdk_initialization(monkeypatch):
    from app.services.qbo import client as module
    class FakeAuth:
        access_token = None
    sdk = module.QBOClient.__new__(module.QBOClient)
    sdk.auth_client = FakeAuth()
    def quickbooks(**kwargs):
        assert kwargs["auth_client"].access_token == "stored-access"
        assert kwargs["auth_client"].refresh_token == "stored-refresh"
        assert kwargs["company_id"] == "realm-1"
        return object()
    monkeypatch.setattr(module, "QuickBooks", quickbooks)
    expiry = utc_now() + timedelta(hours=1)
    sdk.set_tokens("stored-access", "stored-refresh", "realm-1", expiry)
    assert sdk.token_expires_at == expiry
