"""Celery adapter for the same durable queue consumed by the local worker."""
from app.tasks import celery_app
from app.services.persistence.batch_extraction import run_once


@celery_app.task(name="receipt_ai.process_batch_extraction", ignore_result=True)
def process_batch_extraction():
    # One document per task; interrupted claims are never automatically replayed.
    result = run_once()
    return {"run_id": result["run_id"], "status": result["status"]} if result else None
