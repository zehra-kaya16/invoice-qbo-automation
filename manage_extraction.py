"""Trusted operator: recover an interrupted extraction with all servers stopped."""
import argparse
from app.api.documents import _ensure_workflow_editable
from app.services.persistence.repository import DocumentRepository


def recover_interrupted(document_id):
    repository=DocumentRepository()
    document=repository.get(document_id)
    if document is None:
        raise ValueError('Document was not found')
    if document.get('status') not in {'classifying','extracting'}:
        raise ValueError('Document does not have an interrupted extraction')
    document.pop('extraction_claim',None)
    # Financial write guards still apply. Never reset any QBO checkpoints.
    _ensure_workflow_editable(document)
    document.update(status='failed',error='Interrupted extraction recovered; verify the source and extract again',approved=False)
    repository.save(document)


def main():
    parser=argparse.ArgumentParser(description='Stop every Uvicorn/worker process before using this command')
    parser.add_argument('--document-id',required=True)
    args=parser.parse_args()
    try:
        recover_interrupted(args.document_id)
    except Exception:
        parser.exit(1,'Recovery refused; verify document ID, interrupted status and QBO write checkpoints\n')
    print('Extraction unlocked. Start the server and extract the original source again.')


if __name__=='__main__':
    main()
