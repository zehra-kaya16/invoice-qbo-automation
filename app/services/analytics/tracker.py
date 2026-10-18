"""Record committed transitions, never infer historical attempts from final state."""
import logging
import math
import uuid
from datetime import datetime, timezone
from sqlalchemy import inspect, select

from app.models.database import AnalyticsEvent, Company

logger = logging.getLogger(__name__)


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def _elapsed(start):
    try:
        value = datetime.fromisoformat(start)
        if value.tzinfo is None:
            return None
        return _number((datetime.now(timezone.utc) - value).total_seconds() * 1000)
    except (ValueError, TypeError):
        return None


def track_transition(session, company_id, document_id, revision, before, after):
    # Older migrations/restore tools may use this repository before upgrading.
    # Do not invent events for that period; analytics APIs require the new table.
    if not inspect(session.connection()).has_table('analytics_events'):
        return
    before = before or {}
    def event(name, suffix='', value=None, duration=None):
        from app.services.integrations.notifications import queue_transition
        queue_transition(session, company_id, document_id,
            f'{document_id}:{revision}:{name}:{suffix}', name, after)
        session.add(AnalyticsEvent(company_id=company_id, document_id=document_id,
            event_key=f'{document_id}:{revision}:{name}:{suffix}', name=name,
            value=_number(value), duration_ms=_number(duration)))
    if not before:
        event('document.uploaded', value=after.get('file_size'))
    claim = after.get('extraction_claim')
    if claim and claim != before.get('extraction_claim'):
        event('extraction.started')
    if before.get('extraction_claim') and not claim and after.get('status') in {'extracted', 'failed'}:
        confidence = _number((after.get('extracted_data') or {}).get('confidence'))
        if confidence is not None and confidence > 1:
            confidence = None
        event('extraction.succeeded' if after['status'] == 'extracted' else 'extraction.failed',
            value=confidence if after['status'] == 'extracted' else None, duration=after.get('extraction_duration_ms'))
    for state, name in [('in_progress','started'),('succeeded','succeeded'),('uncertain','uncertain')]:
        if after.get('qbo_push_state') == state and before.get('qbo_push_state') != state:
            event('qbo.transaction.' + name, duration=_elapsed(after.get('qbo_push_started_at')) if name != 'started' else None)
    if after.get('qbo_attachment_id') and not before.get('qbo_attachment_id'):
        event('attachment.succeeded', 'source')
    if after.get('qbo_attachment_push_status') == 'uncertain' and before.get('qbo_attachment_push_status') != 'uncertain':
        event('attachment.uncertain', 'source')
    for field, index, status, success, family in [
        ('category_suggestions','transaction_index','qbo_category_push_status','pushed','category'),
        ('check_match_results','check_index','attachment_push_status','uploaded','attachment')]:
        def items(value):
            return [item for item in value if isinstance(item, dict)
                    and type(item.get(index)) is int and item[index] >= 0] if isinstance(value, list) else []
        old = {item.get(index):item for item in items(before.get(field))}
        for item in items(after.get(field)):
            previous = old.get(item.get(index)) or {}
            if item.get(status) == success and previous.get(status) != success:
                event(family+'.succeeded', f'{field}:{item.get(index)}')
            elif item.get(status) == 'uncertain' and previous.get(status) != 'uncertain':
                event(family+'.uncertain', f'{field}:{item.get(index)}')
    if after.get('document_type') in {'bank_statement','check','credit_card_statement'} and after.get('status') == 'pushed' and before.get('status') != 'pushed':
        event('reconciliation.completed')


def track_request(user, method, status, duration_ms):
    """Best-effort telemetry cannot turn a committed QBO response into a failure."""
    from app.core import database
    try:
        with database.SessionLocal() as session:
            if session.scalar(select(Company.id).where(Company.id == user.company_id)) is None:
                return
            session.add(AnalyticsEvent(company_id=user.company_id, event_key=uuid.uuid4().hex,
                name='api.request', request_method=method[:10], status_code=status,
                duration_ms=_number(duration_ms)))
            session.commit()
    except Exception:
        logger.warning('API analytics could not be persisted')
