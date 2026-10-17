"""Read-only company metrics, with an explicit admin-only event reset."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core import database
from app.core.auth import Principal, company_key, get_current_user, require_admin
from app.models.database import AnalyticsEvent as Event, Document

router = APIRouter()


@contextmanager
def storage():
    try:
        with database.SessionLocal() as session:
            yield session
    except SQLAlchemyError:
        raise HTTPException(503, 'Analytics storage is unavailable') from None


def filters(user, hours, now):
    return (Event.company_id == user.company_id,
            Event.created_at >= (now - timedelta(hours=hours)).replace(tzinfo=None),
            Event.created_at <= now.replace(tzinfo=None))


def counters(session, scope):
    return dict(session.execute(select(Event.name, func.count()).where(*scope).group_by(Event.name)).all())


def numeric_stats(session, scope, name):
    event_name = 'document.uploaded' if name == 'document.size_bytes' else 'extraction.succeeded'
    row = session.execute(select(func.count(Event.value), func.sum(Event.value), func.avg(Event.value),
        func.min(Event.value), func.max(Event.value)).where(*scope, Event.name == event_name, Event.value.is_not(None))).one()
    return {'count':row[0], 'sum':row[1] or 0, 'avg':row[2], 'min':row[3], 'max':row[4]}


def timers(session, scope):
    family = case(
        (Event.name.in_(['extraction.succeeded','extraction.failed']), 'extraction.duration'),
        (Event.name.in_(['qbo.transaction.succeeded','qbo.transaction.uncertain']), 'qbo.transaction.duration'),
        (Event.name == 'api.request', 'api.request.duration'), else_=None)
    ranked = select(family.label('family'), Event.duration_ms.label('duration'),
        func.row_number().over(partition_by=family, order_by=Event.duration_ms).label('rank'),
        func.count().over(partition_by=family).label('total')).where(
            *scope, Event.duration_ms.is_not(None), family.is_not(None)).subquery()
    values = session.execute(select(ranked.c.family, func.count(), func.avg(ranked.c.duration),
        func.min(ranked.c.duration), func.max(ranked.c.duration),
        func.min(case((ranked.c.rank * 2 >= ranked.c.total, ranked.c.duration))),
        func.min(case((ranked.c.rank * 100 >= ranked.c.total * 95, ranked.c.duration))))
        .group_by(ranked.c.family)).all()
    return {r[0]:{'count':r[1], 'avg_ms':r[2], 'min_ms':r[3], 'max_ms':r[4], 'p50_ms':r[5], 'p95_ms':r[6]} for r in values}


def inventory(session, user):
    # SQL aggregates only: never hydrate source BLOBs or workflow JSON documents.
    scope = (Document.company_id == user.company_id, Document.api_id.is_not(None))
    by_status = dict(session.execute(select(Document.status, func.count()).where(*scope).group_by(Document.status)).all())
    by_type = dict(session.execute(select(func.coalesce(Document.document_type, 'unclassified'), func.count()).where(*scope).group_by(Document.document_type)).all())
    qbo = dict(session.execute(select(Document.workflow_state['qbo_push_state'].as_string(), func.count())
        .where(*scope).group_by(Document.workflow_state['qbo_push_state'].as_string())).all())
    return {'total':sum(by_status.values()), 'by_status':by_status, 'by_type':by_type,
        'confirmed_financial_documents':qbo.get('succeeded',0), 'uncertain_financial_documents':qbo.get('uncertain',0)}


@router.get('/summary')
def summary(hours: int = Query(24, ge=1, le=168), company_id: str | None = None, user: Principal = Depends(get_current_user)):
    company_key(company_id)
    now = datetime.now(timezone.utc)
    with storage() as session:
        scope = filters(user, hours, now)
        counts = counters(session, scope)
        methods = dict(session.execute(select(Event.request_method, func.count()).where(*scope, Event.name=='api.request').group_by(Event.request_method)).all())
        statuses = {str(k):v for k,v in session.execute(select(Event.status_code, func.count()).where(*scope, Event.name=='api.request').group_by(Event.status_code)).all()}
        succeeded, failed = counts.get('extraction.succeeded',0), counts.get('extraction.failed',0)
        data = {'period_hours':hours, 'period_start':(now-timedelta(hours=hours)).isoformat(), 'period_end':now.isoformat(),
            'inventory_scope':'all_time_current_state', 'history_scope':'events_recorded_since_step_112',
            'inventory':inventory(session,user), 'counters':counts,
            'extraction':{'succeeded':succeeded,'failed':failed,'success_rate':succeeded/(succeeded+failed) if succeeded+failed else None,
                'reported_confidence':numeric_stats(session,scope,'extraction.confidence')},
            'api':{'total_requests':counts.get('api.request',0),'by_method':methods,'by_status':statuses},
            'timers':timers(session,scope)}
    return {'status':'success','data':data}


@router.get('/counters')
def get_counters(hours: int = Query(24, ge=1, le=168), company_id: str | None = None, user: Principal = Depends(get_current_user)):
    company_key(company_id)
    with storage() as session:
        return {'status':'success','data':counters(session,filters(user,hours,datetime.now(timezone.utc)))}


@router.get('/timers')
def get_timers(hours: int = Query(24, ge=1, le=168), company_id: str | None = None, user: Principal = Depends(get_current_user)):
    company_key(company_id)
    with storage() as session:
        return {'status':'success','data':timers(session,filters(user,hours,datetime.now(timezone.utc)))}


@router.get('/stats')
def get_stats(metric: Literal['document.size_bytes','extraction.confidence'] | None = None,
    hours: int = Query(24, ge=1, le=168), company_id: str | None = None, user: Principal = Depends(get_current_user)):
    company_key(company_id)
    with storage() as session:
        scope = filters(user,hours,datetime.now(timezone.utc))
        data = numeric_stats(session,scope,metric) if metric else {'counters':counters(session,scope),
            'timers':timers(session,scope), 'metrics':{n:numeric_stats(session,scope,n) for n in ['document.size_bytes','extraction.confidence']}}
        return {'status':'success','metric':metric,'data':data}


@router.post('/reset')
def reset(company_id: str | None = None, user: Principal = Depends(require_admin)):
    company_key(company_id)
    with storage() as session:
        session.execute(delete(Event).where(Event.company_id==user.company_id))
        session.commit()
    return {'status':'success','message':'This company analytics events were cleared; documents and QBO results were preserved'}
