"""Restore holds survive process restarts. Only an offline operator can release."""
from sqlalchemy import select,update
from app.core import database
from app.models.database import RestoreCheckpoint,NotificationOutbox,utc_now

MESSAGE='Geri yükleme kontrolü tamamlanmadı; işleme ve dış gönderimler operatör kontrolüne kadar bekletiliyor.'


def status():
    with database.SessionLocal() as session:
        row=session.scalar(select(RestoreCheckpoint).where(RestoreCheckpoint.active_key=='active'))
        return {'active':bool(row),'restore_id':row.id if row else None,
                'snapshot_at':row.snapshot_at.isoformat()+'Z' if row else None}


def blocked_request(method,path):
    return (method not in {'GET','HEAD','OPTIONS'} and path.startswith(('/api/documents','/api/batch','/api/qbo','/api/webhooks'))
            or path.startswith('/api/qbo') or path=='/api/export/qbo-import')


def release(id):
    with database.SessionLocal() as session:
        changed=session.execute(update(RestoreCheckpoint).where(RestoreCheckpoint.id==id,
            RestoreCheckpoint.active_key=='active').values(active_key=None,released_at=utc_now()))
        if changed.rowcount!=1:
            raise ValueError('Active restore checkpoint not found; use status and the exact restore ID')
        # Pending at snapshot time may already have been sent afterwards.
        # Never replay those messages merely because the DB was rolled back.
        cancelled=session.execute(update(NotificationOutbox).where(NotificationOutbox.status=='pending').values(
            status='cancelled',finished_at=utc_now()))
        session.commit()
        return {'restore_id':id,'active':False,'cancelled_pending_notifications':cancelled.rowcount}
