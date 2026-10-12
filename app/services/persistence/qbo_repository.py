"""Encrypted OAuth storage scoped to the authenticated company and OAuth session."""
import hashlib
import uuid
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, update

from app.core import database
from app.core.auth import current_principal
from app.core.config import settings
from app.models.database import Company, QBOConnection, QBOOAuthAttempt, utc_now


class QBOStorageError(RuntimeError):
    pass


class QBOConnectionConflict(QBOStorageError):
    pass


class QBOConnectionRepository:
    def __init__(self, session_factory=None):
        self.factory = session_factory
        self.environment = settings.qbo_environment
        self.fingerprint = hashlib.sha256((settings.qbo_client_id or "").encode()).hexdigest()
        self.user = current_principal.get()
        self.company_id = self.user.company_id if self.user else str(uuid.uuid5(uuid.NAMESPACE_URL, "invoice-qbo-automation:company:default"))

    def session(self):
        return (self.factory or database.SessionLocal)()

    def cipher(self):
        try:
            return Fernet((settings.qbo_token_encryption_key or "").encode())
        except (ValueError, TypeError):
            raise QBOStorageError("Configure a valid QBO_TOKEN_ENCRYPTION_KEY") from None

    def scope(self, model):
        return (model.environment == self.environment, model.client_fingerprint == self.fingerprint)

    def oauth_scope(self):
        return (*self.scope(QBOOAuthAttempt), QBOOAuthAttempt.company_id == self.company_id)

    def oauth_binding(self):
        return (QBOOAuthAttempt.user_id == (self.user.user_id if self.user else None),
                QBOOAuthAttempt.session_hash == (self.user.session_hash if self.user else None))

    def connection_scope(self):
        return (*self.scope(QBOConnection), QBOConnection.company_id == self.company_id)

    def _encode(self, token):
        if not isinstance(token, str) or not token:
            raise QBOStorageError("QuickBooks returned an incomplete token response")
        return self.cipher().encrypt(token.encode()).decode()

    def _decode(self, token):
        try:
            return self.cipher().decrypt((token or "").encode()).decode()
        except (InvalidToken, UnicodeError):
            raise QBOStorageError("Stored QBO tokens cannot be decrypted with the configured key") from None

    @staticmethod
    def state_hash(state):
        return hashlib.sha256(state.encode()).hexdigest()

    def begin_oauth(self, state):
        self.cipher()  # Fail before redirecting to Intuit.
        with self.session() as session, session.begin():
            session.execute(delete(QBOOAuthAttempt).where(QBOOAuthAttempt.expires_at < utc_now()))
            session.execute(update(QBOOAuthAttempt).where(*self.oauth_scope(),
                *self.oauth_binding(), QBOOAuthAttempt.status.in_(["pending", "exchanging"])).values(status="cancelled"))
            if session.get(Company, self.company_id) is None:
                session.add(Company(id=self.company_id, name="default", api_key="default"))
                session.flush()
            session.add(QBOOAuthAttempt(company_id=self.company_id, user_id=self.user.user_id if self.user else None,
                session_hash=self.user.session_hash if self.user else None, state_hash=self.state_hash(state), environment=self.environment,
                client_fingerprint=self.fingerprint, status="pending", expires_at=utc_now() + timedelta(minutes=10)))

    def claim_oauth(self, state):
        self.cipher()
        with self.session() as session, session.begin():
            result = session.execute(update(QBOOAuthAttempt).where(
                QBOOAuthAttempt.state_hash == self.state_hash(state), *self.oauth_scope(), *self.oauth_binding(),
                QBOOAuthAttempt.status == "pending", QBOOAuthAttempt.expires_at > utc_now()
            ).values(status="exchanging"))
            if result.rowcount != 1:
                raise QBOConnectionConflict("Invalid, expired or already used OAuth state")

    def save_oauth(self, state, client):
        access, refresh = self._encode(client.access_token), self._encode(client.refresh_token)
        if client.token_expires_at is None or not client.realm_id:
            raise QBOStorageError("QuickBooks returned incomplete connection data")
        with self.session() as session, session.begin():
            result = session.execute(update(QBOOAuthAttempt).where(
                QBOOAuthAttempt.state_hash == self.state_hash(state), *self.oauth_scope(), *self.oauth_binding(),
                QBOOAuthAttempt.status == "exchanging"
            ).values(status="completed"))
            if result.rowcount != 1:
                raise QBOConnectionConflict("OAuth connection was cancelled; connect again")
            if session.get(Company, self.company_id) is None:
                session.add(Company(id=self.company_id, name="default", api_key="default"))
                session.flush()
            row = session.scalar(select(QBOConnection).where(QBOConnection.realm_id == client.realm_id))
            if row is not None and row.company_id != self.company_id:
                raise QBOConnectionConflict("This QBO company belongs to another local company")
            session.execute(update(QBOConnection).where(*self.connection_scope()).values(
                is_active=False, access_token=None, refresh_token=None, revision=QBOConnection.revision + 1))
            if row is None:
                row = QBOConnection(company_id=self.company_id, realm_id=client.realm_id)
                session.add(row)
            else:
                session.refresh(row)
                row.revision += 1
            row.environment, row.client_fingerprint = self.environment, self.fingerprint
            row.access_token, row.refresh_token = access, refresh
            row.token_expires_at = client.token_expires_at
            row.qbo_company_name = None
            row.is_active, row.token_state = True, "ready"

    def active(self, decrypt=True):
        with self.session() as session:
            row = session.scalar(select(QBOConnection).where(*self.connection_scope(), QBOConnection.is_active.is_(True)))
            if row is None:
                return None
            result = {"id": row.id, "revision": row.revision, "realm_id": row.realm_id,
                "company_name": row.qbo_company_name, "expires_at": row.token_expires_at, "token_state": row.token_state}
            if decrypt and row.token_state == "ready":
                result.update(access_token=self._decode(row.access_token), refresh_token=self._decode(row.refresh_token))
            return result

    def save_name(self, record, name):
        with self.session() as session, session.begin():
            session.execute(update(QBOConnection).where(*self.connection_scope(), QBOConnection.id == record["id"],
                QBOConnection.revision == record["revision"], QBOConnection.is_active.is_(True)).values(qbo_company_name=name))

    def claim_refresh(self, record):
        with self.session() as session, session.begin():
            result = session.execute(update(QBOConnection).where(*self.connection_scope(), QBOConnection.id == record["id"],
                QBOConnection.is_active.is_(True), QBOConnection.revision == record["revision"], QBOConnection.token_state == "ready"
            ).values(token_state="refreshing", revision=QBOConnection.revision + 1))
            if result.rowcount != 1:
                raise QBOConnectionConflict("QBO connection changed or refresh is already in progress")
        return record["revision"] + 1

    def finish_refresh(self, record, revision, client):
        access, refresh = self._encode(client.access_token), self._encode(client.refresh_token)
        if client.token_expires_at is None:
            raise QBOStorageError("QuickBooks returned no token expiry")
        with self.session() as session, session.begin():
            result = session.execute(update(QBOConnection).where(*self.connection_scope(), QBOConnection.id == record["id"],
                QBOConnection.is_active.is_(True), QBOConnection.revision == revision, QBOConnection.token_state == "refreshing"
            ).values(access_token=access, refresh_token=refresh, token_expires_at=client.token_expires_at,
                token_state="ready", revision=QBOConnection.revision + 1))
            if result.rowcount != 1:
                raise QBOConnectionConflict("QBO connection changed during refresh; connect again")

    def fail_refresh(self, record, revision):
        with self.session() as session, session.begin():
            session.execute(update(QBOConnection).where(*self.connection_scope(), QBOConnection.id == record["id"],
                QBOConnection.revision == revision, QBOConnection.token_state == "refreshing").values(token_state="uncertain"))

    def disconnect(self):
        with self.session() as session, session.begin():
            session.execute(update(QBOOAuthAttempt).where(*self.oauth_scope()).values(status="cancelled"))
            session.execute(update(QBOConnection).where(*self.connection_scope()).values(is_active=False,
                access_token=None, refresh_token=None, token_expires_at=None, token_state="ready", revision=QBOConnection.revision + 1))
