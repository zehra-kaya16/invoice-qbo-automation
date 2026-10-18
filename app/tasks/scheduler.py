"""One beat service publishes its liveness without storing business state."""
import logging
import time
from celery.beat import PersistentScheduler
from app.services.deployment import BEAT_KEY, connection


class DeploymentScheduler(PersistentScheduler):
    def tick(self, *args, **kwargs):
        interval = super().tick(*args, **kwargs)
        try:
            connection().set(BEAT_KEY, str(time.time()), ex=45)
        except Exception:
            logging.getLogger(__name__).warning('Beat liveness could not be stored')
        return min(interval, 10)
