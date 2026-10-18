from app.tasks import celery_app
from app.services.integrations.notifications import run_once


@celery_app.task(name="receipt_ai.process_notification")
def process_notification():
    return run_once()
