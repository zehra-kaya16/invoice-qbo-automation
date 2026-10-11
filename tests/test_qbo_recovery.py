import copy
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pytest

from app.services.persistence.repository import DocumentRepository
from tests.test_document_api_persistence import client,seed_invoice,prepare_bank_workflow,restart_request

class RecoveryQBO:
    realm_id='realm-99'
    def __init__(self):
        self.transactions={};self.attachments=[];self.writes=0;self.uploads=0;self.fail_category=False;self.fail_attachment=False
    def get_currency_preferences(self):return {'home_currency':'USD','multicurrency_enabled':False}
    def get_recovery_transaction(self,kind,id):return copy.deepcopy(self.transactions[str(id)])
    def find_recovery_transactions(self,kind,date,marker):return [copy.deepcopy(t) for t in self.transactions.values() if t['type']==kind and t['date']==date and t.get('marker')==marker]
    def get_recovery_attachments(self,kind,id):return copy.deepcopy(self.attachments)
    def update_purchase_category(self,purchase_id,account_id):
        self.writes+=1
        if self.fail_category:raise TimeoutError('category timeout')
        self.transactions[purchase_id]['expense_accounts']=[account_id]
        return {'id':purchase_id,'account_id':account_id}
    def upload_attachment(self,**kwargs):
        self.uploads+=1
        assert Path(kwargs['file_path']).is_file()
        value={'id':'901','file_name':kwargs['file_name'],'size':Path(kwargs['file_path']).stat().st_size,'marker':kwargs['recovery_marker']}
        self.attachments.append(value)
        if self.fail_attachment:raise TimeoutError('upload response lost')
        return {'id':'901'}


def setup_invoice(monkeypatch,status='uncertain',attachment_status=None):
    doc=seed_invoice();fake=RecoveryQBO()
    doc['qbo_push_context']={'realm_id':fake.realm_id,'marker':'receipt-ai:operation99','currency':'USD','plan':{'method':'create_bill','kwargs':{'vendor_id':'30','account_id':'10','amount':'550','date':'2026-09-15','due_date':None,'description':None}}}
    doc['qbo_push_state']=status
    if status=='succeeded':doc.update(qbo_transaction_id='701',qbo_transaction_type='bill',status='pushed')
    if attachment_status:doc.update(qbo_attachment_push_status=attachment_status,qbo_attachment_error='lost upload response',qbo_attachment_marker='receipt-ai:operation99:source')
    DocumentRepository().save(doc)
    fake.transactions['701']={'id':'701','type':'bill','date':'2026-09-15','amount':'550','vendor_id':'30','currency':'USD','expense_accounts':['10'],'marker':'receipt-ai:operation99'}
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    return doc['id'],fake


def request(id,key,operation='verify',**kwargs):
    data=client.get(f'/api/documents/{id}/recovery').json()
    return client.post(f'/api/documents/{id}/recovery',json={'key':key,'operation':operation,'expected_revision':data['revision'],'expected_realm_id':data['realm_id'] or 'realm-99',**kwargs})


def test_local_snapshot_does_not_read_qbo(monkeypatch):
    id,fake=setup_invoice(monkeypatch)
    before=DocumentRepository().get(id)
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda: (_ for _ in ()).throw(AssertionError('must not connect')))
    response=client.get(f'/api/documents/{id}/recovery')
    assert response.status_code==200 and response.json()['items'][0]['key']=='transaction'
    assert DocumentRepository().get(id)==before

@pytest.mark.parametrize('status',['uncertain','in_progress'])
def test_verified_original_transaction_is_adopted_without_create(monkeypatch,isolated_document_database,status):
    id,fake=setup_invoice(monkeypatch,status)
    before=DocumentRepository().get(id)
    assert request(id,'transaction','inspect').status_code==200
    assert DocumentRepository().get(id)==before
    response=request(id,'transaction');assert response.status_code==200,response.text
    state=DocumentRepository().get(id)
    assert state['qbo_transaction_id']=='701' and state['qbo_push_state']=='succeeded'
    assert state['qbo_recovery_history'][-1]['operation']=='verify'
    assert fake.writes==fake.uploads==0
    assert restart_request(isolated_document_database,'GET',f'/api/documents/{id}')['body']['qbo_transaction_id']=='701'
    assert client.post(f'/api/documents/{id}/push-to-qbo',json={'vendor_id':'30','account_id':'10'}).status_code==409

@pytest.mark.parametrize('field,value',[('marker','wrong-marker'),('vendor_id','other-vendor'),('expense_accounts',['20']),('amount','551'),('date','2026-09-16'),('currency','EUR')])
def test_wrong_qbo_record_is_not_adopted(monkeypatch,field,value):
    id,fake=setup_invoice(monkeypatch);fake.transactions['701'][field]=value
    before=DocumentRepository().get(id)
    assert request(id,'transaction',transaction_id='701').status_code==409
    assert DocumentRepository().get(id)==before and fake.writes==fake.uploads==0

@pytest.mark.parametrize('count',[0,2])
def test_zero_or_multiple_matches_preserve_transaction_lock(monkeypatch,count):
    id,fake=setup_invoice(monkeypatch)
    if count==0:fake.transactions={}
    else:fake.transactions['702']={**fake.transactions['701'],'id':'702'}
    assert request(id,'transaction').status_code==409
    assert request(id,'transaction','retry',acknowledged=True).status_code==409
    assert DocumentRepository().get(id)['qbo_push_state']=='uncertain'


def test_wrong_company_and_stale_revision_cannot_recover(monkeypatch):
    id,fake=setup_invoice(monkeypatch);fake.realm_id='different-company'
    assert request(id,'transaction').status_code==409
    fake.realm_id='realm-99';data=client.get(f'/api/documents/{id}/recovery').json()
    doc=DocumentRepository().get(id);doc['error']='another session update';DocumentRepository().save(doc)
    response=client.post(f'/api/documents/{id}/recovery',json={'key':'transaction','operation':'verify','expected_revision':data['revision'],'expected_realm_id':'realm-99'})
    assert response.status_code==409
    assert fake.writes==fake.uploads==0


def test_legacy_invoice_without_original_company_is_blocked(monkeypatch):
    doc=seed_invoice();doc['qbo_push_state']='uncertain';DocumentRepository().save(doc)
    data=client.get(f"/api/documents/{doc['id']}/recovery").json();assert data['blocked_reason']
    assert request(doc['id'],'transaction').status_code==409


def test_uncertain_source_is_adopted_without_upload(monkeypatch):
    id,fake=setup_invoice(monkeypatch,'succeeded','uncertain')
    fake.attachments=[{'id':'801','file_name':'original.pdf','size':1,'marker':'receipt-ai:operation99:source'}]
    response=request(id,'source');assert response.status_code==200,response.text
    doc=DocumentRepository().get(id);assert doc['qbo_attachment_id']=='801' and doc['qbo_attachment_push_status']=='uploaded'
    assert fake.uploads==0


def test_uncertain_missing_attachment_is_never_reuploaded(monkeypatch):
    id,fake=setup_invoice(monkeypatch,'succeeded','uncertain')
    assert request(id,'source').status_code==409
    assert request(id,'source','retry',acknowledged=True).status_code==409
    assert fake.uploads==0 and DocumentRepository().get(id)['qbo_attachment_push_status']=='uncertain'


def test_manually_verified_attachment_requires_matching_file_and_confirmation(monkeypatch):
    id,fake=setup_invoice(monkeypatch,'succeeded','uncertain');doc=DocumentRepository().get(id)
    fake.attachments=[{'id':'802','file_name':doc['filename'],'size':len(doc['content']),'marker':None}]
    assert request(id,'source',attachment_id='802').status_code==409
    assert request(id,'source',attachment_id='802',confirmed_attachment=True).status_code==200
    assert DocumentRepository().get(id)['qbo_attachment_id']=='802' and fake.uploads==0

@pytest.mark.parametrize('status',[None,'failed_before_write'])
def test_known_not_started_source_upload_claims_and_succeeds_once(monkeypatch,status):
    id,fake=setup_invoice(monkeypatch,'succeeded',status)
    assert request(id,'source','retry',acknowledged=True).status_code==200
    assert fake.uploads==1
    assert request(id,'source','retry',acknowledged=True).status_code==200
    assert fake.uploads==1


def test_lost_retry_upload_response_can_be_reconciled_without_second_upload(monkeypatch):
    id,fake=setup_invoice(monkeypatch,'succeeded','failed_before_write');fake.fail_attachment=True
    assert request(id,'source','retry',acknowledged=True).status_code==502
    assert DocumentRepository().get(id)['qbo_attachment_push_status']=='uncertain'
    assert request(id,'source').status_code==200
    assert fake.uploads==1


def bank_setup(tmp_path,monkeypatch,state='uncertain',account='10',check_state=None):
    id,initial=prepare_bank_workflow(tmp_path,monkeypatch)
    doc=DocumentRepository().get(id);doc['qbo_match_realm_id']='realm-99'
    doc['category_suggestions'][0].update(qbo_category_push_status=state,qbo_category_account_id='10',qbo_category_push_error='category timeout')
    if check_state:doc['check_match_results'][0].update(attachment_push_status=check_state,attachment_error='check timeout',attachment_marker='receipt-ai:check99')
    DocumentRepository().save(doc)
    fake=RecoveryQBO();fake.transactions['101']={'id':'101','type':'purchase','date':'2026-09-15','amount':'550','expense_accounts':[account]}
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    return id,fake


def test_applied_category_is_verified_without_duplicate_write(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch)
    assert request(id,'category:0').status_code==200
    assert DocumentRepository().get(id)['category_suggestions'][0]['qbo_category_push_status']=='pushed'
    assert fake.writes==0


def test_unapplied_category_requires_explicit_retry_then_checks_can_finish(tmp_path,monkeypatch,isolated_document_database):
    id,fake=bank_setup(tmp_path,monkeypatch,account='20')
    assert request(id,'category:0').status_code==409
    assert request(id,'category:0','retry').status_code==409
    assert request(id,'category:0','retry',acknowledged=True).status_code==200
    assert fake.writes==1
    assert request(id,'check:0','retry',acknowledged=True).status_code==200
    assert fake.uploads==1
    restarted=restart_request(isolated_document_database,'GET',f'/api/documents/{id}/bank-workflow')['body']
    assert restarted['status']=='pushed' and restarted['checks'][0]['attachment_id']=='901'


def test_active_category_retry_is_blocked(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch,state='pushing',account='20')
    doc=DocumentRepository().get(id);doc['category_suggestions'][0]['qbo_category_started_at']=datetime.now(timezone.utc).isoformat();DocumentRepository().save(doc)
    assert request(id,'category:0','retry',acknowledged=True).status_code==409
    assert fake.writes==0


def test_expired_category_claim_can_retry_same_approved_account(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch,state='pushing',account='20')
    doc=DocumentRepository().get(id);doc['category_suggestions'][0]['qbo_category_started_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat();DocumentRepository().save(doc)
    assert request(id,'category:0','retry',acknowledged=True).status_code==200
    assert fake.writes==1


def test_check_uncertain_is_adopted_after_categories_without_upload(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch,state='pushed',check_state='uncertain')
    fake.attachments=[{'id':'803','marker':'receipt-ai:check99','file_name':'check.png','size':11}]
    assert request(id,'check:0').status_code==200
    assert DocumentRepository().get(id)['status']=='pushed' and fake.uploads==0


def test_checks_cannot_upload_before_uncertain_category_is_recovered(tmp_path,monkeypatch):
    id,fake=bank_setup(tmp_path,monkeypatch)
    assert request(id,'check:0','retry',acknowledged=True).status_code==409
    assert fake.uploads==0


def test_provider_read_failure_preserves_snapshot(monkeypatch):
    id,fake=setup_invoice(monkeypatch)
    fake.find_recovery_transactions=lambda *args:(_ for _ in ()).throw(TimeoutError('provider read timeout'))
    before=DocumentRepository().get(id)
    assert request(id,'transaction').status_code==502
    assert DocumentRepository().get(id)==before


@pytest.mark.parametrize('expense_bank', ['1', '2'])
def test_expense_recovery_requires_original_payment_account(monkeypatch, expense_bank):
    id,fake=setup_invoice(monkeypatch)
    doc=DocumentRepository().get(id)
    doc['document_type']='receipt'
    doc['qbo_push_context']['plan']['method']='create_expense'
    doc['qbo_push_context']['plan']['kwargs']['bank_account_id']='1'
    DocumentRepository().save(doc)
    fake.transactions['701'].update(type='expense',bank_account_id=expense_bank)
    response=request(id,'transaction')
    assert response.status_code==(200 if expense_bank=='1' else 409)
    assert fake.writes==fake.uploads==0


def test_recovery_attachment_claim_must_persist_before_upload(monkeypatch):
    from app.api import documents as api
    from sqlalchemy.exc import OperationalError
    id,fake=setup_invoice(monkeypatch,'succeeded','failed_before_write')
    original_save=api.document_repository.save
    def unavailable(document):
        if document.get('qbo_attachment_push_status')=='uploading':
            raise OperationalError('save',{},RuntimeError('database unavailable'))
        return original_save(document)
    monkeypatch.setattr(api.document_repository,'save',unavailable)
    assert request(id,'source','retry',acknowledged=True).status_code==503
    assert fake.uploads==0
    assert DocumentRepository().get(id)['qbo_attachment_push_status']=='failed_before_write'


def test_recovery_category_claim_must_persist_before_update(tmp_path,monkeypatch):
    from app.api import documents as api
    from sqlalchemy.exc import OperationalError
    id,fake=bank_setup(tmp_path,monkeypatch,account='20')
    original_save=api.document_repository.save
    def unavailable(document):
        if document['category_suggestions'][0].get('qbo_category_push_status')=='pushing':
            raise OperationalError('save',{},RuntimeError('database unavailable'))
        return original_save(document)
    monkeypatch.setattr(api.document_repository,'save',unavailable)
    assert request(id,'category:0','retry',acknowledged=True).status_code==503
    assert fake.writes==0
    assert DocumentRepository().get(id)['category_suggestions'][0]['qbo_category_push_status']=='uncertain'
