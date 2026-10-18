"""Normalized signed inbound documents. No external event may approve or push."""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core import database
from app.core.auth import Principal, current_principal
from app.core.config import settings
from app.models.database import Company, User, WebhookIntegration, WebhookReceipt, UploadBatch

MAX_BODY = 28 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024


def cipher():
    try:
        return Fernet((settings.qbo_token_encryption_key or '').encode())
    except (ValueError, TypeError):
        raise HTTPException(503, 'Configure QBO_TOKEN_ENCRYPTION_KEY before creating integrations') from None


def principal(session, row):
    owner = session.get(User, row.owner_user_id)
    company = session.get(Company, row.company_id)
    if not row.enabled or not owner or not owner.is_active or owner.role != 'admin' or owner.company_id != row.company_id or not company or not company.api_key:
        return None
    return Principal(owner.id, company.id, company.api_key, company.name, owner.email,
                     owner.name or '', owner.role, 'integration:' + row.id, '')


def extraction_principal(owner_hash, batch_id):
    with database.SessionLocal() as session:
        row = session.get(WebhookIntegration, owner_hash.removeprefix('integration:'))
        receipt = session.scalar(select(WebhookReceipt.id).where(WebhookReceipt.integration_id == row.id,
            WebhookReceipt.batch_id == batch_id, WebhookReceipt.status == 'accepted',
            WebhookReceipt.company_id == row.company_id)) if row else None
        return principal(session, row) if row and receipt else None


def public_integration(row):
    return {k: getattr(row, k) for k in ('id', 'name', 'enabled', 'auto_extract')}


def create(user, name, auto_extract):
    secret = secrets.token_urlsafe(32)
    encrypted = cipher().encrypt(secret.encode()).decode()
    with database.SessionLocal() as session:
        owner = session.get(User, user.user_id)
        if not owner or not owner.is_active or owner.role != 'admin' or owner.company_id != user.company_id:
            raise HTTPException(403, 'Active company administrator is required')
        row = WebhookIntegration(company_id=user.company_id, owner_user_id=user.user_id,
                                 name=name, secret=encrypted, auto_extract=auto_extract)
        session.add(row)
        session.commit()
        return dict(public_integration(row), secret=secret)


def scoped(session, id, user):
    row = session.scalar(select(WebhookIntegration).where(WebhookIntegration.id == id,
                          WebhookIntegration.company_id == user.company_id))
    if row is None:
        raise HTTPException(404, 'Integration not found')
    return row


def change(user, id, action):
    with database.SessionLocal() as session:
        row = scoped(session, id, user)
        if action == 'revoke':
            row.enabled = False
            secret = None
        else:
            if not row.enabled:
                raise HTTPException(409, 'Revoked integration cannot be rotated; create a new one')
            secret = secrets.token_urlsafe(32)
            row.secret = cipher().encrypt(secret.encode()).decode()
            # Rotate transfers responsibility to the current company admin.
            row.owner_user_id = user.user_id
        session.commit()
        return dict(public_integration(row), **({'secret': secret} if secret else {}))


def authenticate(id, kind, headers, body, now=None):
    try:
        stamp = headers.get('X-Webhook-Timestamp', '')
        event = headers.get('X-Webhook-Id', '')
        if not stamp.isdecimal() or len(stamp) > 12 or abs((now or time.time()) - int(stamp)) > 300:
            raise ValueError()
        if str(uuid.UUID(event)) != event:
            raise ValueError()
        with database.SessionLocal() as session:
            row = session.get(WebhookIntegration, id)
            user = principal(session, row) if row else None
            if user is None:
                raise ValueError()
            secret = cipher().decrypt(row.secret.encode())
            signed = f'{stamp}.{event}.{kind}.'.encode() + body
            expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, headers.get('X-Signature', '')):
                raise ValueError()
            return user, event, row.auto_extract
    except (ValueError, TypeError, InvalidToken, UnicodeError):
        raise HTTPException(401, 'Invalid or expired webhook signature') from None


class Attachment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str = Field(min_length=1, max_length=255)
    content_type: str
    content_base64: str | None = None
    url: str | None = Field(None, max_length=4096)


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attachments: list[Attachment] = Field(min_length=1, max_length=10)
    # Email metadata is accepted for adapter compatibility, but never persisted.
    from_email: str | None = Field(None, max_length=255)
    subject: str | None = Field(None, max_length=255)
    body: str | None = Field(None, max_length=10000)


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated DNS answer, with TLS validation/SNI for host.
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def download(url, limit):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        allowed = {v.strip().lower() for v in settings.webhook_download_hosts.split(',') if v.strip()}
        if parts.scheme != 'https' or not host or host not in allowed or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
            raise ValueError()
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = {a[4][0] for a in answers}
        parsed = [ipaddress.ip_address(a) for a in addresses]
        if not parsed or any(not a.is_global or a.is_multicast or a.is_reserved or a.is_unspecified for a in parsed):
            raise ValueError()
        conn = PinnedHTTPS(host, sorted(addresses)[0])
        try:
            path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            conn.request('GET', path, headers={'Host': host, 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status != 200 or response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise ValueError()
            length = response.getheader('Content-Length')
            if length and (not length.isdecimal() or int(length) > limit):
                raise ValueError()
            content = response.read(limit + 1)
            if not content or len(content) > limit:
                raise ValueError()
            return content
        finally:
            conn.close()
    except (ValueError, OSError, http.client.HTTPException):
        raise HTTPException(422, 'Attachment download rejected or unavailable') from None


def prepare(kind, raw):
    from app.api.batch import ALLOWED_TYPES
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        if kind == 'generic':
            if set(body) != {'event', 'data'} or body['event'] != 'document.uploaded':
                raise ValueError()
            body = body['data']
        elif kind == 'zapier':
            # The normalized Zapier adapter uses the same attachment schema.
            if set(body) != {'attachments'}:
                raise ValueError()
        envelope = Envelope.model_validate(body)
    except (ValueError, ValidationError):
        raise HTTPException(422, 'Use the normalized document.uploaded attachment envelope') from None
    prepared, total = [], 0
    for item in envelope.attachments:
        if item.content_type not in ALLOWED_TYPES or bool(item.url) == bool(item.content_base64):
            raise HTTPException(422, 'Each supported attachment needs exactly one URL or base64 content')
        # Browser filenames remain display-only; reject control/path characters.
        if any(ord(c) < 32 for c in item.filename) or '/' in item.filename or '\\' in item.filename:
            raise HTTPException(422, 'Use a filename without paths or control characters')
        limit = min(settings.max_upload_bytes, MAX_TOTAL - total)
        if limit <= 0:
            raise HTTPException(413, 'Webhook attachments exceed 20 MiB')
        if item.url:
            content = download(item.url, limit)
        else:
            try:
                content = base64.b64decode(item.content_base64, validate=True)
            except (ValueError, UnicodeError):
                raise HTTPException(422, 'Invalid attachment base64') from None
        if not content or len(content) > limit:
            raise HTTPException(413, 'Empty or oversized attachment')
        total += len(content)
        prepared.append((item.filename, item.content_type, content))
    return prepared


def batch_key(integration_id, event_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'receipt-ai:webhook:' + integration_id + ':' + event_id))


def response(row):
    return {k: getattr(row, k) for k in ('id', 'event_id', 'integration_id', 'kind', 'status', 'batch_id', 'run_id')}


def receive(id, kind, headers, raw):
    from app.api.batch import _persist_batch
    from app.services.persistence.batch_extraction import enqueue
    user, event, auto = authenticate(id, kind, headers, raw)
    digest = hashlib.sha256(raw).hexdigest()
    with database.SessionLocal() as session:
        row = session.scalar(select(WebhookReceipt).where(WebhookReceipt.integration_id == id, WebhookReceipt.event_id == event))
        if row:
            if row.digest != digest or row.kind != kind:
                raise HTTPException(409, 'Event ID belongs to a different payload')
            if row.status == 'receiving':
                raise HTTPException(409, 'Event is being received; inspect history before retrying')
            receipt_id, accepted = row.id, row.status == 'accepted'
            if not accepted:
                changed = session.execute(update(WebhookReceipt).where(WebhookReceipt.id == row.id,
                    WebhookReceipt.status == 'retryable').values(status='receiving'))
                if changed.rowcount != 1:
                    raise HTTPException(409, 'Event is already claimed')
                session.commit()
        else:
            row = WebhookReceipt(integration_id=id, company_id=user.company_id, event_id=event, digest=digest,
                                 kind=kind, auto_extract=auto, status='receiving')
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                raise HTTPException(409, 'Event is already claimed; retry the same signed event') from None
            receipt_id, accepted = row.id, False
    token = current_principal.set(user)
    try:
        if not accepted:
            try:
                try:
                    files = prepare(kind, raw)
                except HTTPException:
                    with database.SessionLocal() as session:
                        row = session.get(WebhookReceipt, receipt_id)
                        row.status = 'retryable'
                        session.commit()
                    raise
                # Revalidate integration after external download, before persisting.
                authenticate(id, kind, headers, raw)
                result = _persist_batch(user, user.company_key, batch_key(id, event), digest, len(files), files, [])
                with database.SessionLocal() as session:
                    row = session.get(WebhookReceipt, receipt_id)
                    row.status, row.batch_id = 'accepted', result['batch_id']
                    session.commit()
            except Exception:
                # A batch commit may already exist. Explicit offline recovery
                # reconciles it; do not reopen a possibly committed operation.
                raise
        with database.SessionLocal() as session:
            row = session.get(WebhookReceipt, receipt_id)
            batch_id, run_id, auto = row.batch_id, row.run_id, row.auto_extract
        if auto and not run_id:
            job = enqueue(batch_id, user, str(uuid.uuid5(uuid.NAMESPACE_URL, 'webhook-extraction:' + receipt_id)))
            with database.SessionLocal() as session:
                row = session.get(WebhookReceipt, receipt_id)
                row.run_id = job['run_id']
                session.commit()
        with database.SessionLocal() as session:
            return response(session.get(WebhookReceipt, receipt_id))
    finally:
        current_principal.reset(token)


def recover_receipt(id):
    """Offline only, after stopping every API/worker process."""
    with database.SessionLocal() as session:
        row = session.get(WebhookReceipt, id)
        if row is None or row.status != 'receiving':
            raise ValueError('Only an interrupted receiving event can be recovered')
        batch = session.scalar(select(UploadBatch).where(UploadBatch.company_id == row.company_id,
            UploadBatch.request_key == batch_key(row.integration_id, row.event_id)))
        if batch and batch.fingerprint != row.digest:
            raise ValueError('Batch fingerprint mismatch')
        row.status = 'accepted' if batch else 'retryable'
        row.batch_id = batch.id if batch else None
        session.commit()
        return response(row)
