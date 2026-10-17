"""Durable cropped check bytes in the document workflow snapshot."""
import base64
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath


class CheckSourceError(ValueError):
    pass


def capture_check_sources(document):
    if document.get('document_type') != 'bank_statement' or document.get('status') in {'classifying','extracting'}:
        return 0
    sources = document.setdefault('check_image_sources', {})
    captured = 0
    for check in (document.get('extracted_data') or {}).get('check_images', []):
        key = check.get('image_path')
        if not key or key in sources:
            continue
        path = Path(key)
        if not path.is_file():
            continue
        content = path.read_bytes()
        if not content:
            continue
        sources[key] = {'content_base64': base64.b64encode(content).decode('ascii'),
                        'sha256': hashlib.sha256(content).hexdigest(), 'filename': path.name}
        captured += 1
    return captured


def check_image_bytes(document, image_path):
    if not image_path:
        raise CheckSourceError('Check source was not recorded')
    source = (document.get('check_image_sources') or {}).get(image_path)
    if source is not None:
        try:
            content = base64.b64decode(source['content_base64'], validate=True)
            if not content or hashlib.sha256(content).hexdigest() != source['sha256']:
                raise ValueError('Source checksum mismatch')
            return content
        except (KeyError, ValueError, TypeError):
            raise CheckSourceError('Persisted check source is invalid; restore its backup') from None
    path = Path(image_path)
    if not path.is_file():
        raise CheckSourceError('Check source is unavailable; restore its original file')
    content = path.read_bytes()
    if not content:
        raise CheckSourceError('Check source is empty')
    return content


def check_image_content_type(document, image_path):
    return ((document.get('check_image_sources') or {}).get(image_path) or {}).get('content_type', 'image/png')


def check_image_filename(document, image_path):
    source = (document.get('check_image_sources') or {}).get(image_path) or {}
    return source.get('filename') or PureWindowsPath(image_path).name


@contextmanager
def check_image_file(document, image_path):
    content = check_image_bytes(document, image_path)
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(check_image_filename(document, image_path)).suffix or '.bin') as file:
            file.write(content)
            path = file.name
        yield path
    finally:
        if path:
            Path(path).unlink(missing_ok=True)
