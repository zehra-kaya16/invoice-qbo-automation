"""Sign a local attachment for this project's normalized webhook protocol."""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import mimetypes
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True, help='http://localhost:8000/api/webhooks/ID/generic')
    parser.add_argument('--file', required=True)
    parser.add_argument('--event-id', required=True, type=uuid.UUID, help='Reuse the same UUID and unchanged file for a replay')
    args = parser.parse_args()
    endpoint = urlsplit(args.endpoint)
    if endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and endpoint.hostname in {'localhost', '127.0.0.1', '::1'}):
        parser.error('Use HTTPS, or HTTP on localhost for development')
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not endpoint.path.endswith('/generic'):
        parser.error('Use the generic webhook endpoint without credentials, query or fragment')
    file = Path(args.file)
    if file.stat().st_size > 20 * 1024 * 1024:
        parser.error('File exceeds 20 MiB')
    mime = {'heic': 'image/heic'}.get(file.suffix.lower().lstrip('.')) or mimetypes.guess_type(file.name)[0]
    data = {'event': 'document.uploaded', 'data': {'attachments': [{'filename': file.name,
            'content_type': mime, 'content_base64': base64.b64encode(file.read_bytes()).decode()}]}}
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    secret = getpass.getpass('Webhook secret (hidden): ')
    stamp, event = str(int(time.time())), str(args.event_id)
    signature = hmac.new(secret.encode(), f'{stamp}.{event}.generic.'.encode() + raw, hashlib.sha256).hexdigest()
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        result = client.post(args.endpoint, content=raw, headers={'Content-Type': 'application/json',
            'X-Webhook-Timestamp': stamp, 'X-Webhook-Id': event, 'X-Signature': signature})
        if result.status_code != 202:
            parser.exit(1, f'Webhook HTTP {result.status_code}; inspect event history before retrying.\n')
        print(json.dumps(result.json(), ensure_ascii=False))


if __name__ == '__main__':
    main()
