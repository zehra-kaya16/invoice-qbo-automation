"""Private, immutable source objects referenced by durable workflow snapshots."""
import base64
import hashlib
import io
import json
import re

from fastapi import HTTPException
from app.core.config import settings


class SourceStorageError(HTTPException):
    def __init__(self):
        super().__init__(503, "Document storage is unavailable, misconfigured, or failed integrity verification; restore the recorded source before retrying")


def location_for(kind):
    if kind == 'local':
        # A stable ID lets a complete object tree move from Windows to a Docker
        # volume or restored backup without rewriting every document reference.
        descriptor = {'store': settings.local_storage_id}
    elif kind == 's3':
        if not settings.s3_bucket:
            raise ValueError('S3_BUCKET is required')
        descriptor = {'bucket': settings.s3_bucket, 'region': settings.s3_region, 'endpoint': settings.s3_endpoint or None}
    elif kind == 'r2':
        if not settings.r2_bucket or not settings.r2_account_id:
            raise ValueError('R2_BUCKET and R2_ACCOUNT_ID are required')
        descriptor = {'bucket': settings.r2_bucket, 'account': settings.r2_account_id}
    else:
        raise ValueError('Invalid storage backend')
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def backend_for(kind):
    from app.services.storage.storage import LocalStorage, S3Storage, R2Storage
    location = location_for(kind)
    if kind == 'local':
        backend = LocalStorage(settings.local_storage_path)
    elif kind == 's3':
        backend = S3Storage(settings.s3_bucket, region=settings.s3_region, access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key, endpoint_url=settings.s3_endpoint or None)
    else:
        if not settings.r2_access_key or not settings.r2_secret_key:
            raise ValueError('R2 S3 API credentials are required')
        backend = R2Storage(settings.r2_bucket, account_id=settings.r2_account_id,
            access_key=settings.r2_access_key, secret_key=settings.r2_secret_key)
    return backend, location


def _prefix(company_uuid, document_id, role):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,120}', str(document_id)) or role not in {'original', 'check', 'attachment'}:
        raise ValueError('Invalid object scope')
    if not re.fullmatch(r'[a-fA-F0-9-]{36}', str(company_uuid)):
        raise ValueError('Invalid company scope')
    return f'companies/{company_uuid}/documents/{document_id}/{role}/'


def validate_ref(ref, company_uuid, document_id, role):
    digest = ref['sha256']
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Invalid digest')
    if ref['key'] != _prefix(company_uuid, document_id, role) + digest:
        raise ValueError('Object belongs to another scope')
    if type(ref['size']) is not int or ref['size'] <= 0:
        raise ValueError('Invalid size')
    if ref['location'] != location_for(ref['backend']):
        raise ValueError('Restore the original storage location/configuration')


def read_object(ref, company_uuid, document_id, role):
    try:
        validate_ref(ref, company_uuid, document_id, role)
        backend, _ = backend_for(ref['backend'])
        content = backend.download(ref['key'], max_bytes=ref['size'])
        if len(content) != ref['size'] or hashlib.sha256(content).hexdigest() != ref['sha256']:
            raise ValueError('Object integrity check failed')
        return content
    except SourceStorageError:
        raise
    except Exception as exc:
        raise SourceStorageError() from exc


def write_object(content, content_type, company_uuid, document_id, role, existing=None, rebind=False):
    try:
        digest = hashlib.sha256(content).hexdigest()
        if existing and not rebind:
            validate_ref(existing, company_uuid, document_id, role)
            if existing['sha256'] == digest and existing['size'] == len(content):
                return existing
        backend, location = backend_for(settings.storage_type)
        ref = {'backend': settings.storage_type, 'location': location,
            'key': _prefix(company_uuid, document_id, role) + digest, 'size': len(content), 'sha256': digest}
        if not content:
            raise ValueError('Empty source')
        backend.upload(io.BytesIO(content), ref['key'], content_type)
        # A DB pointer must never commit before the uploaded bytes are readable.
        downloaded = backend.download(ref['key'], max_bytes=ref['size'])
        if downloaded != content:
            raise ValueError('Uploaded bytes did not verify')
        return ref
    except SourceStorageError:
        raise
    except Exception as exc:
        raise SourceStorageError() from exc


def externalize(state, content, company_uuid, *, original=True, rebind=False):
    """Mutate only a detached, JSON-serialized snapshot before its DB commit."""
    if original and content:
        state['source_storage'] = write_object(content, state.get('content_type') or 'application/octet-stream',
            company_uuid, state['id'], 'original', state.get('source_storage'), rebind)
        state['storage_key'] = state['source_storage']['key']
    elif content is None:
        state.pop('source_storage', None)
        state.pop('storage_key', None)
    records = [(entry, 'content_base64', 'check') for entry in (state.get('check_image_sources') or {}).values()]
    if state.get('qbo_attachment_source'):
        records.append((state['qbo_attachment_source'], 'data_base64', 'attachment'))
    for record, field, role in records:
        if field not in record:
            continue
        try:
            data = base64.b64decode(record[field], validate=True)
            # Preserve invalid legacy metadata for existing workflow validators;
            # it must never be replaced by an apparently valid external object.
            if not data or hashlib.sha256(data).hexdigest() != record.get('sha256'):
                record.pop('blob_ref', None)
                continue
        except (ValueError, TypeError):
            record.pop('blob_ref', None)
            continue
        record['blob_ref'] = write_object(data, record.get('content_type') or 'image/png',
            company_uuid, state['id'], role, record.get('blob_ref'), rebind)
        record.pop(field)
    return None if state.get('source_storage') else content


def hydrate(state, legacy_content, company_uuid):
    content = read_object(state['source_storage'], company_uuid, state['id'], 'original') if state.get('source_storage') else legacy_content
    records = [(entry, 'content_base64', 'check') for entry in (state.get('check_image_sources') or {}).values()]
    if state.get('qbo_attachment_source'):
        records.append((state['qbo_attachment_source'], 'data_base64', 'attachment'))
    for record, field, role in records:
        if record.get('blob_ref'):
            record[field] = base64.b64encode(read_object(record['blob_ref'], company_uuid, state['id'], role)).decode('ascii')
    return content


def update_caller_refs(document, state):
    for field in ('source_storage', 'storage_key'):
        if field in state:
            document[field] = state[field]
        else:
            document.pop(field, None)
    for key, record in (state.get('check_image_sources') or {}).items():
        if record.get('blob_ref'):
            document['check_image_sources'][key]['blob_ref'] = record['blob_ref']
    record = state.get('qbo_attachment_source') or {}
    if record.get('blob_ref'):
        document['qbo_attachment_source']['blob_ref'] = record['blob_ref']
