"""
Background tasks using Celery.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "receipt_ai",
    broker=(
        settings.redis_url
        or "redis://localhost:6379/0"
    ),
    backend=(
        settings.redis_url
        or "redis://localhost:6379/0"
    ),
)


celery_app.conf.update(
    imports=("app.tasks.extraction", "app.tasks.notifications", "app.tasks.deployment"),
    beat_schedule={"notification-outbox": {"task": "receipt_ai.process_notification", "schedule": 2.0, "options": {"expires": 10}}, "batch-extraction-queue": {"task": "receipt_ai.process_batch_extraction", "schedule": 2.0, "options": {"expires": 10}}},
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600},
    worker_prefetch_multiplier=1,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    task_ignore_result=True,
    result_expires=60,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
)


celery_app.autodiscover_tasks(
    ["app.tasks"]
)