"""Authenticated management and a separately HMAC-authenticated ingress."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core import database
from app.core.auth import Principal, get_current_user, require_admin
from app.core.config import settings
from app.models.database import NotificationPreference, NotificationOutbox, User, WebhookIntegration, WebhookReceipt, utc_now
from app.services.integrations import webhooks

router = APIRouter()
ingress = APIRouter()


class IntegrationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    auto_extract: bool = False


class Preference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.get('/webhooks')
def list_integrations(user: Principal = Depends(require_admin)):
    with database.SessionLocal() as session:
        return {'items': [webhooks.public_integration(row) for row in session.scalars(select(WebhookIntegration)
            .where(WebhookIntegration.company_id == user.company_id).order_by(WebhookIntegration.created_at.desc()).limit(100))]}


@router.post('/webhooks', status_code=201)
def create_integration(body: IntegrationCreate, user: Principal = Depends(require_admin)):
    return webhooks.create(user, body.name, body.auto_extract)


@router.post('/webhooks/{id}/{action}')
def change_integration(id: str, action: Literal['rotate', 'revoke'], user: Principal = Depends(require_admin)):
    return webhooks.change(user, id, action)


@router.get('/webhooks/{id}/events')
def webhook_events(id: str, user: Principal = Depends(require_admin)):
    with database.SessionLocal() as session:
        webhooks.scoped(session, id, user)
        return {'items': [webhooks.response(row) for row in session.scalars(select(WebhookReceipt)
            .where(WebhookReceipt.integration_id == id, WebhookReceipt.company_id == user.company_id)
            .order_by(WebhookReceipt.created_at.desc()).limit(100))]}


@router.get('/notifications')
def notifications(user: Principal = Depends(get_current_user)):
    with database.SessionLocal() as session:
        pref = session.get(NotificationPreference, user.user_id)
        return {'channel_enabled': settings.email_enabled, 'email': user.email,
                'enabled': bool(pref and pref.enabled and pref.company_id == user.company_id and pref.recipient_email == user.email),
                'items': [{k: getattr(row, k) for k in ('id', 'kind', 'document_id', 'filename', 'status', 'provider_id')}
                    for row in session.scalars(select(NotificationOutbox).where(NotificationOutbox.company_id == user.company_id,
                        NotificationOutbox.user_id == user.user_id).order_by(NotificationOutbox.created_at.desc()).limit(100))]}


@router.patch('/notifications')
def notification_preference(body: Preference, user: Principal = Depends(get_current_user)):
    with database.SessionLocal() as session:
        account = session.get(User, user.user_id)
        if not account or not account.is_active or account.company_id != user.company_id or account.email != user.email:
            raise HTTPException(403, 'Current account is required')
        row = session.get(NotificationPreference, user.user_id)
        if row is None:
            row = NotificationPreference(user_id=user.user_id, company_id=user.company_id, recipient_email=user.email)
            session.add(row)
        row.enabled, row.recipient_email = body.enabled, user.email
        session.commit()
    return {'enabled': body.enabled}


@router.post('/notifications/{id}/{action}')
def notification_action(id: str, action: Literal['retry', 'cancel'], user: Principal = Depends(get_current_user)):
    with database.SessionLocal() as session:
        row = session.scalar(select(NotificationOutbox).where(NotificationOutbox.id == id,
            NotificationOutbox.company_id == user.company_id, NotificationOutbox.user_id == user.user_id))
        if row is None:
            raise HTTPException(404, 'Notification not found')
        expected = 'failed_before_send' if action == 'retry' else 'pending'
        target = 'pending' if action == 'retry' else 'cancelled'
        result = session.execute(update(NotificationOutbox).where(NotificationOutbox.id == id,
            NotificationOutbox.status == expected).values(status=target, finished_at=None if action == 'retry' else utc_now()))
        if result.rowcount != 1:
            raise HTTPException(409, 'This notification cannot be retried or cancelled in its current state')
        session.commit()
        return {'id': id, 'status': target}


@ingress.post('/{id}/{kind}', status_code=202)
async def receive_webhook(id: str, kind: Literal['email', 'zapier', 'generic'], request: Request):
    # Do not call request.body() on an unbounded anonymous request.
    size = request.headers.get('Content-Length')
    if size and (not size.isdecimal() or int(size) > webhooks.MAX_BODY):
        raise HTTPException(413, 'Webhook body exceeds 28 MiB')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > webhooks.MAX_BODY:
            raise HTTPException(413, 'Webhook body exceeds 28 MiB')
        data.extend(chunk)
    headers = {k: request.headers.get(k, '') for k in ('X-Webhook-Id', 'X-Webhook-Timestamp', 'X-Signature')}
    try:
        return await run_in_threadpool(webhooks.receive, id, kind, headers, bytes(data))
    except SQLAlchemyError:
        raise HTTPException(503, 'Webhook storage unavailable; inspect event history before retrying') from None
