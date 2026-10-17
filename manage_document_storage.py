"""Trusted operator: audit or migrate source objects with all app processes stopped."""
import argparse
import hashlib
from sqlalchemy import select
from app.core import database
from app.core.config import settings
from app.models.database import Document
from app.services.persistence.repository import DocumentRepository
from app.services.persistence.check_sources import capture_check_sources, check_image_bytes
from app.services.persistence.attachment_sources import invoice_attachment
from app.services.storage.objects import location_for


def migrate(apply=False, document_id=None):
    repository = DocumentRepository()
    with database.SessionLocal() as session:
        query = select(Document.api_id).where(Document.api_id.is_not(None))
        if document_id:
            query = query.where(Document.api_id == document_id)
        ids = list(session.scalars(query.order_by(Document.api_id)))
    result = {'documents':len(ids), 'verified':0, 'needs_migration':0, 'migrated':0, 'failed':0, 'applied':apply}
    for id in ids:
        try:
            document = repository.get(id)
            content = document.get('content')
            if not content:
                raise ValueError('Original source is missing')
            original_hash = hashlib.sha256(content).hexdigest()
            capture_check_sources(document)
            for check in (document.get('extracted_data') or {}).get('check_images', []):
                check_image_bytes(document, check.get('image_path'))
            if document.get('qbo_attachment_source'):
                invoice_attachment(document)
            refs = [document.get('source_storage')]
            refs += [record.get('blob_ref') for record in (document.get('check_image_sources') or {}).values()]
            if document.get('qbo_attachment_source'):
                refs.append(document['qbo_attachment_source'].get('blob_ref'))
            desired_location = location_for(settings.storage_type)
            pending = any(not ref or ref['backend'] != settings.storage_type or ref['location'] != desired_location for ref in refs)
            result['needs_migration'] += int(pending)
            if pending and apply:
                repository.save(document, migrate_source=True)
                fresh = repository.get(id)
                if hashlib.sha256(fresh['content']).hexdigest() != original_hash:
                    raise ValueError('Read-back verification failed')
                result['migrated'] += 1
            result['verified'] += 1
        except Exception:
            # Never print cloud credentials, signed URLs or provider exceptions.
            print(f'{id}: source unavailable/invalid or revision changed; restore configuration/source and audit again')
            result['failed'] += 1
    if document_id and not ids:
        result['failed'] += 1
        print('Document ID was not found')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all-processes-stopped', action='store_true')
    parser.add_argument('--document-id')
    args = parser.parse_args()
    if args.apply and not args.all_processes_stopped:
        parser.error('--apply requires --all-processes-stopped; stop Uvicorn, workers and schedulers first')
    result = migrate(args.apply, args.document_id)
    print(result)
    return 2 if result['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
