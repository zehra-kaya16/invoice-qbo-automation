from app.tasks import celery_app
from app.services.deployment import probe


@celery_app.task(name='receipt_ai.deployment_probe', ignore_result=False, soft_time_limit=25, time_limit=30)
def deployment_probe(nonce, storage_key):
    return probe(nonce, storage_key)
