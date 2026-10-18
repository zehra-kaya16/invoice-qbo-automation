"""Deployment validation, independent of financial/extraction operations."""
import hashlib
import io
import json
import socket
import time
import uuid
from pathlib import Path, PurePosixPath

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from redis import Redis
from sqlalchemy.engine import make_url

from app.core import database
from app.core.config import settings

BEAT_KEY = 'receipt-ai:deployment:beat'


def expected_head():
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'migrations'))
    return ScriptDirectory.from_config(config).get_current_head()


def validate_runtime():
    if settings.deployment_mode != 'compose':
        return
    try:
        db = make_url(settings.database_url or '')
        redis = make_url(settings.redis_url or '')
        if db.get_backend_name() != 'postgresql' or not db.host or db.host in {'localhost','127.0.0.1','::1'} or not db.password:
            raise ValueError()
        if redis.drivername not in {'redis','rediss'} or not redis.host or redis.host in {'localhost','127.0.0.1','::1'} or not redis.password:
            raise ValueError()
        Fernet((settings.qbo_token_encryption_key or '').encode())
        storage_path = settings.local_storage_path or ''

        if not (
            Path(storage_path).is_absolute()
            or PurePosixPath(storage_path).is_absolute()
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Compose requires shared PostgreSQL/Redis URLs, credentials, an absolute storage path and the existing encryption key') from None


def connection():
    return Redis.from_url(settings.redis_url or 'redis://localhost:6379/0', socket_connect_timeout=3,
                          socket_timeout=3, decode_responses=True)


def beat_healthy(redis=None, now=None):
    try:
        value = (redis or connection()).get(BEAT_KEY)
        age = (time.time() if now is None else now) - float(value)
        return 0 <= age < 45
    except (ValueError, TypeError, OSError):
        return False


def configuration_fingerprint():
    # High-entropy credentials are hashed, never returned. A worker pointed at
    # another DB/key/storage must not pass an app-process deployment check.
    from app.services.storage.objects import location_for
    descriptor = {'settings': settings.model_dump(), 'storage_location': location_for(settings.storage_type)}
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def schema_check():
    with database.engine.connect() as conn:
        if MigrationContext.configure(conn).get_current_heads() != (expected_head(),):
            raise ValueError('Database migration head does not match this image')
    from app.api.health import readiness
    readiness()


def probe_key(nonce):
    digest = hashlib.sha256(nonce.encode()).hexdigest()
    return 'companies/00000000-0000-0000-0000-000000000114/documents/doc_deployment_probe/original/' + digest


def probe(nonce, storage_key):
    """Executed in a real Celery worker; never touches document or QBO state."""
    if not isinstance(nonce,str) or len(nonce)!=32 or any(c not in '0123456789abcdef' for c in nonce):
        raise ValueError('Invalid probe nonce')
    if storage_key != probe_key(nonce):
        raise ValueError('Invalid probe key')
    from app.services.storage.objects import backend_for
    schema_check()
    backend, _ = backend_for(settings.storage_type)
    if backend.download(storage_key, max_bytes=32) != nonce.encode():
        raise ValueError('Worker cannot read the shared storage object')
    return {'nonce':nonce,'fingerprint':configuration_fingerprint(),'schema':'ready','shared_storage':True}


def check_stack(celery=None):
    """Operator-only, with one disposable object and one diagnostic queue task."""
    validate_runtime()
    if database.engine.dialect.name != 'postgresql':
        raise ValueError('Deployment check requires PostgreSQL, not the local SQLite database')
    schema_check()
    redis = connection()
    if not redis.ping():
        raise ValueError('Redis did not respond')
    if not beat_healthy(redis):
        raise ValueError('Beat heartbeat missing or stale')
    from app.tasks import celery_app
    celery = celery or celery_app
    if not celery.control.inspect(timeout=5).ping():
        raise ValueError('No Celery worker responded')
    from app.services.storage.objects import backend_for
    backend, _ = backend_for(settings.storage_type)
    nonce = uuid.uuid4().hex
    key = probe_key(nonce)
    try:
        backend.upload(io.BytesIO(nonce.encode()), key, 'application/octet-stream')
        result = celery.send_task('receipt_ai.deployment_probe', args=[nonce,key], expires=30)
        try:
            reply = result.get(timeout=40, propagate=True)
        finally:
            result.forget()
        if reply != {'nonce':nonce,'fingerprint':configuration_fingerprint(),'schema':'ready','shared_storage':True}:
            raise ValueError('Worker configuration, database or storage differs from this operator process')
        return {'postgresql':'ready','schema':expected_head(),'redis':'ready','worker':'ready',
                'beat':'ready','shared_storage':'ready'}
    finally:
        backend.delete(key)


def worker_healthy():
    from app.tasks import celery_app
    reply=celery_app.control.inspect(destination=['worker@'+socket.gethostname()],timeout=4).ping()
    return bool(reply and all(item.get('ok')=='pong' for item in reply.values()))
