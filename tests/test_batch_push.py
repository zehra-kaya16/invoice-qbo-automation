import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, delete
from app.core.auth import current_principal
from app.main import app
from app.models.database import AuthSession, BatchPushRun
from app.services.persistence import batch_push
from app.services.persistence.repository import DocumentRepository
from tests.test_batch_extraction import actor
from tests.test_batch_upload import upload
from tests.test_document_api_persistence import restart_request
client = TestClient(app)

class QBO:
    realm_id = 'realm107'
    def __init__(self):
        self.writes=[]; self.attachments=[]; self.transactions={}; self.fail=False
        self.fail_attachment=False; self.after_write=None; self.account_name='Office'
    def get_vendors(self):return [{'id':'30','name':'Vendor','currency':'USD'}]
    def get_expense_accounts(self):return [{'id':'10','name':self.account_name,'type':'Expense'}]
    def get_bank_accounts(self):return [{'id':'1','name':'Checking','type':'Bank','currency':'USD'}]
    def get_currency_preferences(self):return {'home_currency':'USD','multicurrency_enabled':False}
    def create_bill(self, **kwargs):
        from app.core import database
        with database.SessionLocal() as session:
            run=session.scalar(select(BatchPushRun).where(BatchPushRun.active_key=='active'))
            item=next(i for i in run.items if i['status']=='sending')
        doc=DocumentRepository().get(item['id']); assert doc['qbo_push_state']=='in_progress'
        assert kwargs['recovery_marker']==doc['qbo_push_context']['marker']
        id=str(7100+len(self.writes));self.transactions[id]=doc['id'];self.writes.append(kwargs)
        if self.after_write:self.after_write()
        if self.fail:raise TimeoutError('Provider response lost')
        return {'id':id,'type':'bill'}
    def create_expense(self, **kwargs):
        result=self.create_bill(**kwargs);result['type']='expense';return result
    def upload_attachment(self, **kwargs):
        doc=DocumentRepository().get(self.transactions[kwargs['entity_id']])
        assert Path(kwargs['file_path']).read_bytes()==doc['content']
        assert doc['qbo_attachment_push_status']=='uploading'
        self.attachments.append(kwargs)
        if self.fail_attachment:raise TimeoutError('Attachment response lost')
        return {'id':'8100'+str(len(self.attachments))}

@pytest.fixture
def ready(actor, monkeypatch):
    data=upload().json()
    for index,id in enumerate(data['uploaded_documents']):
        doc=DocumentRepository().get(id)
        doc.update(status='extracted',document_type='invoice' if index==0 else 'receipt',approved=True,
                   validation={'is_valid':True},qbo_push_state='not_started',
                   extracted_data={'vendor':'Vendor','date':'2026-10-01','total_amount':str(10+index),'currency':'USD'})
        DocumentRepository().save(doc)
    fake=QBO();monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    return data,fake,actor

def save(data,index=0,**changes):
    id=data['uploaded_documents'][index]
    body={'expected_revision':DocumentRepository().get(id)['_revision'],'expected_realm_id':'realm107',
          'create_as':'bill' if index==0 else 'expense','vendor_id':'30','account_id':'10','bank_account_id':'1'}
    body.update(changes)
    return client.post(f"/api/batch/{data['batch_id']}/plans/{id}",json=body)

def selection(data,indexes=(0,1)):
    items=[]
    for index in indexes:
        response=save(data,index);assert response.status_code==200,response.text
        items.append({'document_id':response.json()['document_id'],'plan_id':response.json()['plan_id']})
    return items

def push(data,plans,key=None):
    return client.post('/api/batch/push-all/'+data['batch_id'],headers={'Idempotency-Key':key or str(uuid.uuid4())},json={'plans':plans})

def test_bill_expense_attachments_idempotency_and_restart(ready,isolated_document_database):
    data,fake,_=ready;plans=selection(data);key=str(uuid.uuid4());assert not fake.writes
    saved=restart_request(isolated_document_database,'GET','/api/batch/status/'+data['batch_id'])
    assert len(saved['body']['qbo_plans'])==2 and not saved['body']['qbo_plans'][0]['stale']
    response=push(data,plans,key);assert response.status_code==200,response.text
    run=response.json();assert run['status']=='completed'
    assert len(fake.writes)==len(fake.attachments)==2
    assert {i['transaction_type'] for i in run['items']}=={'bill','expense'}
    assert all(i['status']=='succeeded' and i['attachment_id'] for i in run['items'])
    assert push(data,plans,key).json()==run
    assert push(data,plans).status_code==409 and len(fake.writes)==2
    restarted=restart_request(isolated_document_database,'GET',f"/api/batch/{data['batch_id']}/push-runs/{run['run_id']}")
    assert restarted['body']==run
    assert all(p['stale'] for p in client.get('/api/batch/status/'+data['batch_id']).json()['qbo_plans'])
    assert client.get('/api/documents/'+plans[0]['document_id']).json()['batch_id']==data['batch_id']

@pytest.mark.parametrize('change',['revision','approval','validation','realm','mapping','source','type'])
def test_every_plan_rechecked_before_any_write(ready,change):
    data,fake,_=ready;plans=selection(data);id=plans[-1]['document_id']
    if change=='realm':fake.realm_id='different-realm'
    elif change=='mapping':fake.account_name='Renamed account'
    else:
        doc=DocumentRepository().get(id)
        if change=='approval':doc['approved']=False
        elif change=='validation':doc['validation']={'is_valid':False}
        elif change=='source':doc['content']=b''
        elif change=='type':doc['document_type']='check'
        else:doc['extracted_data']['total_amount']='99'
        DocumentRepository().save(doc)
    assert push(data,plans).status_code in (400,409)
    assert not fake.writes and not fake.attachments

@pytest.mark.parametrize('kind',['write','attachment'])
def test_uncertainty_stops_without_repeating_any_item(ready,kind):
    data,fake,_=ready;plans=selection(data);key=str(uuid.uuid4())
    fake.fail=kind=='write';fake.fail_attachment=kind=='attachment'
    response=push(data,plans,key);assert response.status_code==200,response.text
    run=response.json();assert run['status']=='needs_review'
    assert [i['status'] for i in run['items']]==['needs_review','not_started']
    assert len(fake.writes)==1
    assert push(data,plans,key).json()==run and len(fake.writes)==1
    assert push(data,plans).status_code==409
    if kind=='attachment':assert run['items'][0]['transaction_id']

def test_concurrent_http_requests_never_duplicate(ready):
    data,fake,_=ready;plans=selection(data)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:push(data,plans),range(2)))
    assert any(r.status_code==200 and r.json()['status']=='completed' for r in results)
    assert len(fake.writes)==len(fake.attachments)==2

def test_replaced_plan_and_nonce_selection_binding(ready):
    data,fake,_=ready;plans=selection(data,(0,));newer=selection(data,(0,))
    assert push(data,plans).status_code==409 and not fake.writes
    key=str(uuid.uuid4());assert push(data,newer,key).status_code==200
    assert push(data,plans,key).status_code==409

def test_revoked_session_stops_next_document(ready,isolated_document_database):
    data,fake,actor=ready;plans=selection(data)
    def revoke():
        with isolated_document_database() as session:
            session.execute(delete(AuthSession).where(AuthSession.token_hash==actor.session_hash));session.commit()
    fake.after_write=revoke
    response=push(data,plans);assert response.status_code==200,response.text
    assert response.json()['status']=='cancelled'
    assert [i['status'] for i in response.json()['items']]==['succeeded','not_started']
    assert len(fake.writes)==1

def test_other_company_cannot_read_save_push(ready):
    from dataclasses import replace
    from app.core.auth import get_current_user
    from app.services.persistence.auth_repository import company_uuid
    data,fake,actor=ready;plans=selection(data)
    other=replace(actor,company_id=company_uuid('other'),company_key='other')
    async def other_user():return other
    app.dependency_overrides[get_current_user]=other_user
    assert save(data).status_code==404
    assert client.get('/api/batch/status/'+data['batch_id']).status_code==404
    assert push(data,plans).status_code==404 and not fake.writes

def test_prepared_run_survives_and_same_nonce_never_executes_again(ready):
    data,fake,actor=ready;plans=selection(data);key=str(uuid.uuid4())
    token=current_principal.set(actor)
    try:run,mappings=batch_push.prepare(data['batch_id'],plans,actor,key)
    finally:current_principal.reset(token)
    assert push(data,plans,key).json()['status']=='running' and not fake.writes
    assert push(data,plans).status_code==409 and save(data).status_code==409
    recovered=batch_push.recover_run(run['run_id'])
    assert recovered['status']=='interrupted' and all(i['status']=='not_started' for i in recovered['items'])
    assert push(data,plans,key).json()==recovered and not fake.writes
    assert push(data,plans).json()['status']=='completed'

def test_crash_after_remote_write_requires_document_recovery(ready):
    data,fake,actor=ready;plans=selection(data)
    def crash():raise SystemExit('process terminated')
    fake.after_write=crash;token=current_principal.set(actor)
    try:
        with pytest.raises(SystemExit):batch_push.push(data['batch_id'],plans,actor,str(uuid.uuid4()))
    finally:current_principal.reset(token)
    run=client.get('/api/batch/status/'+data['batch_id']).json()['push_run']
    assert run['status']=='running' and len(fake.writes)==1
    recovered=batch_push.recover_run(run['run_id'])
    assert [i['status'] for i in recovered['items']]==['needs_review','not_started']
    assert push(data,plans).status_code==409 and len(fake.writes)==1

def test_status_does_not_load_source_or_workflow_blob(ready,isolated_document_database):
    data,_,_=ready;selection(data);statements=[];engine=isolated_document_database.kw['bind']
    def record(conn,cursor,statement,*args):statements.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:assert client.get('/api/batch/status/'+data['batch_id']).status_code==200
    finally:event.remove(engine,'before_cursor_execute',record)
    assert not any('source_content' in s or 'workflow_state' in s for s in statements)

def test_single_invoice_push_checks_expected_revision(ready):
    data,fake,_=ready;id=data['uploaded_documents'][0];revision=DocumentRepository().get(id)['_revision']
    doc=DocumentRepository().get(id);doc['extracted_data']['total_amount']='77';DocumentRepository().save(doc)
    response=client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10','expected_revision':revision})
    assert response.status_code==409 and not fake.writes

@pytest.mark.parametrize('with_checks',[False,True])
def test_reviewed_bank_plan_categories_checks_and_restart(actor,tmp_path,monkeypatch,with_checks,isolated_document_database):
    from tests.test_document_api_persistence import prepare_bank_workflow, FakeBankQBO
    monkeypatch.setattr(FakeBankQBO, 'realm_id', 'realm107', raising=False)
    from tests.test_bank_statement_without_checks import prepare_no_check_workflow
    from app.core import database
    from app.models.database import UploadBatch
    data=upload().json()
    id,fake=(prepare_bank_workflow if with_checks else prepare_no_check_workflow)(tmp_path,monkeypatch)
    if with_checks:
        prefix='/api/documents/'+id
        assert client.post(prefix+'/match-to-qbo',json={'bank_account_id':'1'}).status_code==200
        assert client.patch(prefix+'/match-review/0',json={'decision':'approve'}).status_code==200
        assert client.post(prefix+'/resolve-vendors').status_code==200
        assert client.post(prefix+'/suggest-categories').status_code==200
        assert client.patch(prefix+'/category-review/0',json={'decision':'approve'}).status_code==200
    doc=DocumentRepository().get(id);doc['batch_id']=data['batch_id'];DocumentRepository().save(doc)
    with database.SessionLocal() as session:
        row=session.get(UploadBatch,data['batch_id']);row.document_ids=list(row.document_ids)+[id];row.total_files+=1;session.commit()
    body={'create_as':'bank_statement','expected_revision':doc['_revision'],'expected_realm_id':doc['qbo_match_realm_id']}
    response=client.post(f"/api/batch/{data['batch_id']}/plans/{id}",json=body)
    assert response.status_code==200,response.text
    plan=response.json();key=str(uuid.uuid4());plans=[{'document_id':id,'plan_id':plan['plan_id']}]
    assert fake.category_writes==0
    response=push(data,plans,key);assert response.status_code==200,response.text
    run=response.json();assert run['status']=='completed',run
    assert fake.category_writes==1
    assert fake.check_uploads==(1 if with_checks else 0)
    assert run['items'][0]['bank_results']['categories'][0]['qbo_transaction_id']=='101'
    assert push(data,plans,key).json()==run and fake.category_writes==1
    assert push(data,plans).status_code==409
    restarted=restart_request(isolated_document_database,'GET',f"/api/batch/{data['batch_id']}/push-runs/{run['run_id']}")
    assert restarted['body']==run


def test_currency_or_missing_approval_prevents_saving(ready):
    data,fake,_=ready;id=data['uploaded_documents'][0];doc=DocumentRepository().get(id)
    doc['extracted_data']['currency']='EUR';DocumentRepository().save(doc)
    assert save(data).status_code in (400,409) and not fake.writes
    doc['extracted_data']['currency']='USD';doc['approved']=False;DocumentRepository().save(doc)
    assert save(data).status_code in (400,409) and not fake.writes


def test_later_doc_changed_mid_run_leaves_previous_success_intact(ready):
    data,fake,_=ready;plans=selection(data)
    second=sorted(plans,key=lambda p:p['document_id'])[1]['document_id']
    def change():
        doc=DocumentRepository().get(second);doc['approved']=False;DocumentRepository().save(doc)
    fake.after_write=change
    result=push(data,plans).json()
    assert [i['status'] for i in result['items']]==['succeeded','blocked']
    assert len(fake.writes)==len(fake.attachments)==1


def test_duplicate_selection_is_rejected(ready):
    data,fake,_=ready;plans=selection(data,(0,))
    assert push(data,plans+plans).status_code==400 and not fake.writes
