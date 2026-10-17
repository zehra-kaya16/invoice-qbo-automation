import copy
from datetime import timedelta
import pytest
from sqlalchemy import event, select, delete
from sqlalchemy.exc import OperationalError
from app.core import database
from app.models.database import AnalyticsEvent, Document, Company, utc_now
from app.services.persistence.repository import DocumentRepository,DocumentConflictError
from tests.test_document_api_persistence import client,seed_invoice,restart_request
from tests.test_batch_extraction import actor
from tests.test_company_access import tenants
from tests.test_batch_upload import upload as batch_upload


def summary(hours=24):
    r=client.get('/api/analytics/summary',params={'hours':hours});assert r.status_code==200,r.text
    return r.json()['data']


def counts(hours=24):return summary(hours)['counters']


def add_events(factory,values):
    from tests.auth_helpers import TEST_PRINCIPAL
    with factory() as s:
        for i,value in enumerate(values):s.add(AnalyticsEvent(company_id=TEST_PRINCIPAL.company_id,event_key='seed-'+str(i),**value))
        s.commit()


def test_existing_inventory_has_no_fabricated_timing_history(isolated_document_database):
    doc=seed_invoice()
    with isolated_document_database() as s:s.execute(delete(AnalyticsEvent));s.commit()
    result=summary();assert result['inventory']['total']==1 and result['inventory']['by_type']=={'invoice':1}
    assert result['counters']=={} and result['timers']=={} and result['extraction']['success_rate'] is None
    assert result['extraction']['reported_confidence']['avg'] is None


def test_one_document_one_upload_and_stats_after_repeated_saves():
    doc=seed_invoice();before=copy.deepcopy(DocumentRepository().get(doc['id']))
    for _ in range(3):DocumentRepository().save(doc)
    data=summary();assert data['counters']['document.uploaded']==1
    stats=client.get('/api/analytics/stats?metric=document.size_bytes').json()['data']
    assert stats['count']==1 and stats['sum']==len(doc['content'])
    assert DocumentRepository().get(doc['id'])['extracted_data']==before['extracted_data']


def test_batch_upload_request_replay_does_not_duplicate_upload_events(actor):
    key='ab000000-0000-0000-0000-000000000112'
    first=batch_upload(key);assert first.status_code==200,first.text
    assert batch_upload(key).json()==first.json()
    data=summary();assert data['counters']['document.uploaded']==2 and data['inventory']['total']==2
    assert data['api']['by_method']['POST']==2


def test_batch_commit_failure_rolls_back_document_events(actor,monkeypatch):
    def failed(*a,**kw):raise OperationalError('commit',{},Exception('offline'))
    monkeypatch.setattr('sqlalchemy.orm.Session.commit',failed)
    assert batch_upload().status_code==503
    with database.SessionLocal() as s:assert s.scalar(select(AnalyticsEvent.id)) is None


def test_stale_write_does_not_duplicate_metrics():
    doc=seed_invoice();old=DocumentRepository().get(doc['id']);doc['approved']=False;DocumentRepository().save(doc)
    with pytest.raises(DocumentConflictError):DocumentRepository().save(old)
    assert counts()['document.uploaded']==1


def test_extraction_attempts_success_failure_and_retry_count_once(monkeypatch):
    from app.api.documents import _process_document_sync
    from app.core.config import settings
    from app.schemas.documents import DocumentType,ReceiptData
    doc=seed_invoice();doc.update(status='uploaded',approved=False,content_type='image/png');DocumentRepository().save(doc)
    class Extractor:
        bad=False
        def __init__(self,**kw):pass
        def classify_document(self,image):return DocumentType.INVOICE
        def extract_receipt(self,image):
            if type(self).bad:raise ValueError('fake AI failure')
            return ReceiptData(vendor='V',date='2026-10-17',total_amount=10,subtotal=10,tax_amount=0,currency='USD',confidence=.9)
    monkeypatch.setattr(settings,'ai_provider','openai');monkeypatch.setattr(settings,'openai_api_key','fake')
    monkeypatch.setattr('app.services.extraction.extractor.DocumentExtractor',Extractor)
    _process_document_sync(doc['id']);data=summary();assert data['counters']['extraction.succeeded']==1
    assert data['timers']['extraction.duration']['count']==1
    assert data['extraction']['reported_confidence']['avg']==.9
    Extractor.bad=True;_process_document_sync(doc['id']);Extractor.bad=False;_process_document_sync(doc['id'])
    data=summary();assert data['counters']['extraction.started']==3
    assert data['counters']['extraction.failed']==1 and data['counters']['extraction.succeeded']==2
    assert data['extraction']['success_rate']==pytest.approx(2/3)
    assert data['timers']['extraction.duration']['count']==3


def test_source_failure_and_blocked_push_are_not_extraction_or_qbo_success(monkeypatch):
    doc=seed_invoice()
    r=client.post('/api/documents/'+doc['id']+'/push-to-qbo',json={})
    assert r.status_code in (400,409,422)
    data=counts();assert data.get('qbo.transaction.succeeded',0)==0 and data.get('extraction.succeeded',0)==0


def test_qbo_confirmations_and_replay_are_counted_once(monkeypatch):
    from tests.test_document_api_persistence import FakeQBO,push_invoice
    doc=seed_invoice();fake=FakeQBO(doc['id']);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    assert push_invoice(doc['id'],attach=True).status_code==200
    assert push_invoice(doc['id'],attach=True).status_code==409
    data=summary();assert data['counters']['qbo.transaction.succeeded']==1 and data['counters']['attachment.succeeded']==1
    assert data['inventory']['confirmed_financial_documents']==1 and fake.writes==1
    assert data['timers']['qbo.transaction.duration']['count']==1


def test_uncertain_qbo_is_never_reported_as_success(monkeypatch):
    from tests.test_document_api_persistence import FakeQBO,push_invoice
    doc=seed_invoice();fake=FakeQBO(doc['id'],fail=True);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    assert push_invoice(doc['id']).status_code==502
    data=summary();assert data['counters']['qbo.transaction.uncertain']==1
    assert data['counters'].get('qbo.transaction.succeeded',0)==0
    assert data['inventory']['uncertain_financial_documents']==1


def test_bank_category_attachment_and_replay_count_actual_results(tmp_path,monkeypatch):
    from tests.test_document_api_persistence import prepare_bank_workflow
    id,fake=prepare_bank_workflow(tmp_path,monkeypatch)
    path='/api/documents/'+id+'/push-to-qbo'
    assert client.post(path,json={}).status_code==200
    assert client.post(path,json={}).status_code==200
    data=counts();assert data['category.succeeded']==1 and data['attachment.succeeded']==1
    assert data['reconciliation.completed']==1 and data.get('qbo.transaction.succeeded',0)==0
    assert fake.category_writes==fake.check_uploads==1


def test_analytics_reads_do_not_load_sources_or_mutate_document(monkeypatch,isolated_document_database):
    doc=seed_invoice();before=DocumentRepository().get(doc['id']);statements=[]
    event.listen(isolated_document_database.kw['bind'],'before_cursor_execute',lambda c,cu,s,p,ctx,m:statements.append(s))
    monkeypatch.setattr('app.services.storage.objects.read_object',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not read files')))
    for path in ['summary','counters','timers','stats']:
        assert client.get('/api/analytics/'+path).status_code==200
    assert not any('source_content' in s or 'SELECT documents.workflow_state ' in s for s in statements)
    assert DocumentRepository().get(doc['id'],load_sources=False)['_revision']==before['_revision']


def test_time_window_applies_to_every_stat_and_percentile(isolated_document_database):
    seed_invoice()
    with isolated_document_database() as s:s.execute(delete(AnalyticsEvent));s.commit()
    now=utc_now()
    add_events(isolated_document_database,[{'name':'extraction.succeeded','value':.8,'duration_ms':10,'created_at':now},
        {'name':'extraction.failed','duration_ms':100,'created_at':now},
        {'name':'extraction.succeeded','value':.2,'duration_ms':9000,'created_at':now-timedelta(hours=25)},
        {'name':'api.request','request_method':'POST','status_code':201,'duration_ms':20,'created_at':now}])
    data=summary();assert data['extraction']['success_rate']==.5 and data['extraction']['reported_confidence']['avg']==.8
    timer=data['timers']['extraction.duration'];assert timer['count']==2 and timer['p50_ms']==10 and timer['p95_ms']==100
    assert summary(168)['timers']['extraction.duration']['count']==3
    assert client.get('/api/analytics/timers?hours=24').json()['data']['extraction.duration']==timer


@pytest.mark.parametrize('suffix',['summary?hours=0','summary?hours=169','timers?hours=-1','stats?metric=unknown','counters?hours=x'])
def test_invalid_queries_rejected(suffix):assert client.get('/api/analytics/'+suffix).status_code==422


@pytest.mark.parametrize('suffix',['summary','stats','counters','timers'])
def test_cannot_select_other_company(suffix):assert client.get('/api/analytics/'+suffix+'?company_id=other').status_code==403


def test_counts_survive_fresh_process_without_backfill(isolated_document_database):
    seed_invoice();before=summary()
    restarted=restart_request(isolated_document_database,'GET','/api/analytics/summary')
    assert restarted['status']==200
    assert restarted['body']['data']['counters']==before['counters']
    assert restarted['body']['data']['inventory']==before['inventory']


@pytest.mark.auth_live
def test_real_sessions_company_isolation_roles_reset_and_csrf(tenants):
    from tests.test_company_access import signed_in
    from fastapi.testclient import TestClient
    from app.main import app
    a=signed_in(tenants['a']['email']);b=signed_in(tenants['b']['email']);ordinary=signed_in(tenants['user']['email'])
    assert TestClient(app).get('/api/analytics/summary').status_code==401
    assert ordinary.get('/api/analytics/summary').status_code==200
    assert ordinary.post('/api/analytics/reset').status_code==403
    before_b=b.get('/api/analytics/summary').json()['data']
    response=a.post('/api/documents/upload?auto_process=false',files={'file':('extra.pdf',b'one','application/pdf')});assert response.status_code==200
    assert a.get('/api/analytics/summary').json()['data']['inventory']['total']==2
    assert b.get('/api/analytics/summary').json()['data']['inventory']==before_b['inventory']
    assert a.get('/api/analytics/summary?company_id=beta').status_code==403
    assert a.post('/api/analytics/reset?company_id=beta').status_code==403
    csrf=a.headers.pop('X-CSRF-Token');assert a.post('/api/analytics/reset').status_code==403;a.headers['X-CSRF-Token']=csrf
    assert a.post('/api/analytics/reset').status_code==200
    after=a.get('/api/analytics/summary').json()['data'];assert after['counters']=={} and after['inventory']['total']==2
    assert b.get('/api/analytics/summary').json()['data']['counters']==before_b['counters']
    assert a.post('/api/auth/logout').status_code==200 and a.get('/api/analytics/counters').status_code==401


def test_api_telemetry_failure_does_not_turn_committed_upload_into_error(monkeypatch):
    from sqlalchemy.orm import Session
    original=Session.commit
    def commit(self):
        if any(isinstance(row,AnalyticsEvent) and row.name=='api.request' for row in self.new):
            raise OperationalError('metrics',{},Exception('telemetry unavailable'))
        return original(self)
    monkeypatch.setattr(Session,'commit',commit)
    response=client.post('/api/documents/upload?auto_process=false',files={'file':('new.pdf',b'one','application/pdf')})
    assert response.status_code==200,response.text
    data=summary();assert data['inventory']['total']==1 and data['counters']['document.uploaded']==1
    assert data['api']['total_requests']==0


def test_percentiles_use_all_rows_and_nearest_rank(isolated_document_database):
    seed_invoice()
    with isolated_document_database() as s:s.execute(delete(AnalyticsEvent));s.commit()
    add_events(isolated_document_database,[{'name':'api.request','request_method':'GET','status_code':200,'duration_ms':n} for n in range(1,101)])
    timers=client.get('/api/analytics/timers').json()['data']['api.request.duration']
    assert timers['count']==100 and timers['avg_ms']==50.5 and timers['p50_ms']==50 and timers['p95_ms']==95


def test_no_storage_or_qbo_call_when_reading_stats(monkeypatch):
    seed_invoice()
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda: (_ for _ in ()).throw(AssertionError('must not call QBO')))
    monkeypatch.setattr('app.services.storage.objects.backend_for',lambda *a: (_ for _ in ()).throw(AssertionError('must not read storage')))
    assert summary()['inventory']['total']==1


def test_unhandled_authenticated_api_error_is_recorded_as_500():
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import company_access
    seed_invoice()
    async def fail():raise RuntimeError('test-only endpoint error')
    old_routes=list(app.router.routes)
    try:
        app.add_api_route('/api/documents/_analytics/test-error',fail,dependencies=[Depends(company_access)])
        assert TestClient(app,raise_server_exceptions=False).get('/api/documents/_analytics/test-error').status_code==500
        assert summary()['api']['by_status']['500']==1
    finally:app.router.routes[:]=old_routes
