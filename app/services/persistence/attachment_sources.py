"""Persist an exact QBO-compatible derivative while retaining the original source."""
import base64
import hashlib
from pathlib import Path
from fastapi import HTTPException
from app.services.extraction.heic import is_heic, heic_to_png, HEICImageError


def invoice_attachment(document):
    source = document.get('content')
    record = document.get('qbo_attachment_source')
    if record is not None:
        try:
            content = base64.b64decode(record['data_base64'], validate=True)
            valid = (record['content_type'] == 'image/png' and record['filename'].endswith('.png')
                     and content.startswith(b'\x89PNG\r\n\x1a\n')
                     and hashlib.sha256(content).hexdigest() == record['sha256']
                     and isinstance(source, bytes)
                     and hashlib.sha256(source).hexdigest() == record['source_sha256'])
            if not valid:raise ValueError()
            return record['filename'], record['content_type'], content
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(409, 'Persisted QBO attachment source is invalid; the write lock is preserved') from None
    if not isinstance(source, bytes) or not source:
        raise HTTPException(409, 'Original document source is unavailable')
    return document.get('filename') or document['id']+'.bin', document.get('content_type') or 'application/octet-stream', source


def prepare_invoice_attachment(document):
    source = document.get('content')
    heic = document.get('content_type') == 'image/heic' or is_heic(source)
    if heic and document.get('qbo_attachment_source') is None:
        try:
            converted = heic_to_png(source)
        except HEICImageError as exc:
            raise HTTPException(409, str(exc)) from None
        document['qbo_attachment_source'] = {
            'filename': Path(document.get('filename') or document['id']).stem + '.png',
            'content_type': 'image/png', 'data_base64': base64.b64encode(converted).decode('ascii'),
            'sha256': hashlib.sha256(converted).hexdigest(),
            'source_sha256': hashlib.sha256(source).hexdigest(),
        }
    filename, content_type, content = invoice_attachment(document)
    return {'file_name': filename, 'content_type': content_type, 'size': len(content),
            'converted_from': 'image/heic' if document.get('qbo_attachment_source') else None,
            'sha256': hashlib.sha256(content).hexdigest()}
