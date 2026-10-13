"""Trusted local operator: preserve legacy cropped checks before delivery."""
import argparse
from sqlalchemy import select
from app.core import database
from app.models.database import Document
from app.services.persistence.repository import DocumentRepository
from app.services.persistence.check_sources import capture_check_sources, check_image_bytes, CheckSourceError


def preserve(apply=False):
    repository = DocumentRepository()
    with database.SessionLocal() as session:
        ids = list(session.scalars(select(Document.api_id).where(Document.api_id.is_not(None))))
    total = captured = missing = 0
    for id in ids:
        document = repository.get(id)
        checks = (document.get('extracted_data') or {}).get('check_images', [])
        if document.get('document_type') != 'bank_statement' or not checks:
            continue
        total += len(checks)
        added = capture_check_sources(document)
        for check in checks:
            try:
                check_image_bytes(document, check.get('image_path'))
            except CheckSourceError:
                missing += 1
                print(f'{id}: original check source needs restoration')
        if apply and added:
            repository.save(document)
        captured += added
    return {'checks': total, 'new_copies': captured, 'missing_or_invalid': missing, 'applied': apply}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = preserve(args.apply)
    print(result)
    return 2 if result['missing_or_invalid'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
