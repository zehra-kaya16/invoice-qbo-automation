from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from app.core import database
from app.models.database import AuthSession, Company, Document, QBOOAuthAttempt, QBOConnection, User, UploadBatch, BatchExtractionRun

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "healthy"}


@router.get('/ready')
def readiness():
    try:
        with database.SessionLocal() as session:
            # Verify the real schema, including the latest tenant/session columns.
            queries = [select(BatchExtractionRun.id, BatchExtractionRun.status, BatchExtractionRun.revision, BatchExtractionRun.owner_session_hash, BatchExtractionRun.claim_token, BatchExtractionRun.items), select(UploadBatch.id, UploadBatch.company_id, UploadBatch.request_key, UploadBatch.fingerprint, UploadBatch.document_ids, UploadBatch.failed_files, UploadBatch.total_files, UploadBatch.created_at), select(Company.id, Company.api_key),
                       select(User.id, User.company_id, User.role, User.is_active),
                       select(QBOConnection.id, QBOConnection.token_state, QBOConnection.revision),
                       select(Document.id, Document.api_id, Document.revision),
                       select(AuthSession.token_hash, AuthSession.csrf_token),
                       select(QBOOAuthAttempt.company_id, QBOOAuthAttempt.user_id, QBOOAuthAttempt.session_hash)]
            for query in queries:
                session.execute(query.limit(1)).first()
    except SQLAlchemyError:
        raise HTTPException(503, 'Database schema is not ready; run migrations') from None
    return {'status': 'ready'}
