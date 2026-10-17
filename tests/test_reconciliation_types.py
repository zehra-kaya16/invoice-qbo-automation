import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
import pytest

from app.schemas.documents import CheckData, BankStatementData
from app.api import documents
from app.services.extraction.extractor import DocumentExtractor
from app.services.extraction.reconciliation import prepare_standalone_check, extract_credit_card_statement
from app.services.persistence.repository import DocumentRepository
from app.services.persistence import batch_push
from tests.test_batch_extraction import actor
from tests.test_document_api_persistence import client, FakeBankQBO, restart_request


class ReconcileQBO(FakeBankQBO):
    realm_id = 'realm110'
    def __init__(self, id, kind='check', **kw):
        super().__init__(id, **kw); self.kind=kind; self.reads=0; self.types=[]; self.uploads=[]
    def get_accounts(self, account_type=None):
        self.types.append(account_type)
        return [{'id':'1','name':'Credit card' if account_type=='CreditCard' else 'Checking','type':account_type}]
    def get_matching_transactions(self, **kwargs):
        self.reads+=1; self.match_args=kwargs
        return [{'id':'101','type':'purchase','amount':'-550.00','date':'2026-09-15',
            'check_number':'00200','description':'Dues & Subscriptions','vendor':'Books by Bessie'}]
    def upload_attachment(self, **kwargs):
        self.uploads.append(dict(kwargs, content=Path(kwargs['file_path']).read_bytes()))
        return super().upload_attachment(**kwargs)


def seed(kind='check'):
    doc={'id':'doc_110_'+kind,'company_id':'default','filename':'original.pdf','content':b'%PDF-original-check',
         'content_type':'application/pdf','document_type':kind,'status':'extracted','qbo_push_state':'not_started'}
    if kind=='check':
        doc['extracted_data']=prepare_standalone_check(doc, CheckData(direction='outgoing',check_number='00200',payee='Books by Bessie',
            amount=Decimal('550'),date=date(2026,9,15),memo='Dues & Subscriptions'))
    else:
        doc['extracted_data']={'statement_period_start':'2026-09-01','statement_period_end':'2026-09-30',
            'account_type':'credit_card','transactions':[{'date':'2026-09-15','amount':'-550',
            'description':'Dues & Subscriptions','transaction_type':'charge'}], 'check_images':[]}
    DocumentRepository().save(doc); return doc


def review(id):
    path='/api/documents/'+id
    for suffix,method,body in [('match-to-qbo','post',{'bank_account_id':'1','expected_realm_id':'realm110'}),
            ('match-review/0','patch',{'decision':'approve'}),('resolve-vendors','post',None),
            ('suggest-categories','post',None),('category-review/0','patch',{'decision':'approve'})]:
        result=getattr(client,method)(path+'/'+suffix,**({'json':body} if body else {}))
        assert result.status_code==200, result.text
    return path


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_review_push_no_new_financial_record_and_restart(kind,monkeypatch,isolated_document_database):
    doc=seed(kind); fake=ReconcileQBO(doc['id'],kind);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    path=review(doc['id']); snapshot=client.get(path+'/bank-workflow').json();assert snapshot['push_ready']
    assert fake.types==['CreditCard' if kind=='credit_card_statement' else 'Bank']
    if kind=='credit_card_statement':assert fake.match_args['account_type']=='CreditCard'
    result=client.post(path+'/push-to-qbo',json={'expected_revision':snapshot['revision'],'expected_realm_id':'realm110'})
    assert result.status_code==200,result.text
    assert result.json()['success'] and result.json()['transactions_pushed']==0
    assert fake.category_writes==1 and fake.check_uploads==(1 if kind=='check' else 0)
    if kind=='check':
        assert fake.uploads[0]['content']==doc['content']
        assert fake.uploads[0]['content_type']=='application/pdf' and fake.uploads[0]['file_name']=='original.pdf'
        source=client.get(path+'/checks/0/source');assert source.content==doc['content'] and source.headers['content-type']=='application/pdf'
    assert client.post(path+'/push-to-qbo',json={}).status_code==200
    assert fake.category_writes==1 and fake.check_uploads==(1 if kind=='check' else 0)
    saved=restart_request(isolated_document_database,'GET',path+'/bank-workflow')
    assert saved['status']==200 and saved['body']['status']=='pushed' and saved['body']['locked']
    assert batch_push.document_result(doc['id'])[1]
    recovery=client.get(path+'/recovery').json();assert recovery['realm_id']=='realm110'


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
@pytest.mark.parametrize('gate',['pending','realm','revision','category'])
def test_gates_prevent_writes(kind,gate,monkeypatch):
    doc=seed(kind);fake=ReconcileQBO(doc['id'],kind);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    path=review(doc['id']);saved=DocumentRepository().get(doc['id'])
    if gate=='pending':saved['qbo_match_results'][0]['review_status']='pending';DocumentRepository().save(saved)
    if gate=='category':saved['category_suggestions'][0]['review_status']='pending';DocumentRepository().save(saved)
    body={'expected_revision':saved['_revision'],'expected_realm_id':'realm110'}
    if gate=='revision':body['expected_revision']-=1
    if gate=='realm':body['expected_realm_id']='wrong'
    result=client.post(path+'/push-to-qbo',json=body)
    assert result.status_code in (400,409),result.text
    assert fake.category_writes==fake.check_uploads==0


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_edit_clears_reviews_and_requires_current_revision(kind,monkeypatch):
    doc=seed(kind);fake=ReconcileQBO(doc['id'],kind);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    path=review(doc['id']);saved=DocumentRepository().get(doc['id'])
    body={'expected_revision':saved['_revision']}
    body['check_data' if kind=='check' else 'statement_data']=copy.deepcopy(saved['extracted_data'])
    result=client.patch(path+'/reconciliation-data',json=body);assert result.status_code==200,result.text
    saved=DocumentRepository().get(doc['id']);assert saved['status']=='extracted' and 'qbo_match_results' not in saved
    assert client.patch(path+'/reconciliation-data',json=body).status_code==409
    assert client.post(path+'/push-to-qbo',json={}).status_code in (400,409)
    assert fake.category_writes==fake.check_uploads==0


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_edit_locked_after_write(kind,monkeypatch):
    doc=seed(kind);fake=ReconcileQBO(doc['id'],kind);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    path=review(doc['id']);assert client.post(path+'/push-to-qbo',json={}).status_code==200
    saved=DocumentRepository().get(doc['id'])
    assert client.patch(path+'/reconciliation-data',json={'expected_revision':saved['_revision'],
        'check_data' if kind=='check' else 'statement_data':saved['extracted_data']}).status_code==409


def test_wrong_credit_account_rejected_before_read(monkeypatch):
    doc=seed('credit_card_statement');fake=ReconcileQBO(doc['id']);fake.get_accounts=lambda account_type=None:[{'id':'1','type':'Bank','name':'Wrong'}]
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    assert client.post('/api/documents/'+doc['id']+'/match-to-qbo',json={'bank_account_id':'1'}).status_code==400
    assert not fake.reads


def test_standalone_rejects_different_check_number(monkeypatch):
    doc=seed();fake=ReconcileQBO(doc['id']);fake.get_matching_transactions=lambda **kw:[{'id':'101','type':'purchase','amount':'-550',
        'date':'2026-09-15','check_number':'999','description':'Same amount'}]
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    result=client.post('/api/documents/'+doc['id']+'/match-to-qbo',json={'bank_account_id':'1'})
    assert result.status_code==200,result.text
    assert not result.json()['matches'][0]['matched']


def test_credit_extraction_reads_all_pages_and_preserves_identical_rows():
    extractor=DocumentExtractor.__new__(DocumentExtractor); seen=[]
    extractor._vision_request_pages=lambda pages,prompt: json.dumps({'statement_period_start':'2026-09-01','statement_period_end':'2026-09-30'})
    def vision(page,prompt):
        seen.append(page); assert 'NEGATIVE' in prompt and 'POSITIVE' in prompt
        return json.dumps([{'date':'2026-09-15','description':'Office','amount':-10,'transaction_type':'charge'}])
    extractor._vision_request=vision
    result=extract_credit_card_statement(extractor,[b'p1',b'p2'])
    assert seen==[b'p1',b'p2'] and len(result.transactions)==2 and result.total_withdrawals==20


def test_multipage_check_sends_every_view_and_rejects_multiple_checks():
    extractor=DocumentExtractor.__new__(DocumentExtractor);seen=[]
    extractor._vision_request_pages=lambda pages,prompt: seen.extend(pages) or '{"check_number":"0002","amount":5,"date":"2026-09-15"}'
    assert extractor.extract_check([b'front',b'back']).check_number=='0002'
    assert seen==[b'front',b'back']
    extractor._vision_request_pages=lambda *a:'{"multiple_checks":true}'
    with pytest.raises(ValueError,match='one physical check'):extractor.extract_check([b'a',b'b'])


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_source_and_reviews_stay_company_scoped(kind,monkeypatch):
    doc=seed(kind)
    from app.core.auth import current_principal
    from tests.auth_helpers import TEST_PRINCIPAL
    from dataclasses import replace
    token=current_principal.set(replace(TEST_PRINCIPAL,company_key='other',company_id='b0000000-0000-0000-0000-000000000110'))
    try:assert DocumentRepository().get(doc['id']) is None
    finally:current_principal.reset(token)

@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_batch_plan_and_push_use_reviewed_kernel(kind,actor,monkeypatch):
    from tests.test_batch_upload import upload
    from tests.test_batch_push import push
    batch=upload().json();id=batch['uploaded_documents'][0];doc=DocumentRepository().get(id)
    evidence=seed(kind)
    doc.update({k:v for k,v in evidence.items() if k not in {'id','_revision','batch_id','source_storage','storage_key'}})
    if kind=='check':doc['extracted_data']=prepare_standalone_check(doc,CheckData(**evidence['extracted_data']))
    DocumentRepository().save(doc)
    fake=ReconcileQBO(id,kind);monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    review(id)
    response=client.post(f"/api/batch/{batch['batch_id']}/plans/{id}",json={'create_as':'bank_statement',
        'expected_revision':DocumentRepository().get(id)['_revision'],'expected_realm_id':'realm110'})
    assert response.status_code==200,response.text
    assert fake.category_writes==fake.check_uploads==0
    run=push(batch,[{'document_id':id,'plan_id':response.json()['plan_id']}])
    assert run.status_code==200,run.text
    assert run.json()['status']=='completed' and run.json()['items'][0]['bank_results']
    assert fake.category_writes==1 and fake.check_uploads==(1 if kind=='check' else 0)


@pytest.mark.parametrize('kind',['check','credit_card_statement'])
def test_uncertain_category_never_replayed(kind,monkeypatch):
    doc=seed(kind);fake=ReconcileQBO(doc['id'],kind,fail_category=True)
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake);path=review(doc['id'])
    assert client.post(path+'/push-to-qbo',json={}).status_code==502
    assert client.post(path+'/push-to-qbo',json={}).status_code==409
    assert fake.category_writes==1 and fake.check_uploads==0
    snap=client.get(path+'/recovery').json();assert snap['realm_id']=='realm110' and snap['items'][0]['status']=='uncertain'


def test_credit_qbo_reads_charges_refunds_transfers_card_payments(monkeypatch):
    from app.services.qbo import client as sdk
    values={'Purchase':[
        NS(Id='10',AccountRef={'value':'cc'},TotalAmt=12,TxnDate='2026-09-15',Credit=False,Line=[]),
        NS(Id='11',AccountRef={'value':'cc'},TotalAmt=2,TxnDate='2026-09-15',Credit=True,Line=[]),
        NS(Id='99',AccountRef={'value':'other'},TotalAmt=1,TxnDate='2026-09-15',Credit=False,Line=[])],
        'Transfer':[NS(Id='20',FromAccountRef={'value':'bank'},ToAccountRef={'value':'cc'},Amount=20,TxnDate='2026-09-15')],
        'CreditCardPaymentEntity':[NS(Id='30',CreditCardAccountRef={'value':'cc'},Amount=30,TxnDate='2026-09-15')],
        'BillPayment':[NS(Id='40',CreditCardPayment=NS(CCAccountRef={'value':'cc'}),TotalAmt=40,TxnDate='2026-09-15')],
        'Deposit':[],'Payment':[]}
    for name,rows in values.items():monkeypatch.setattr(getattr(sdk,name),'where',lambda *a,_rows=rows,**kw:_rows)
    q=sdk.QBOClient.__new__(sdk.QBOClient);q.qb_client=object()
    result=q.get_matching_transactions('2026-09-01','2026-09-30','cc',account_type='CreditCard')
    assert {r['id']:r['amount'] for r in result}=={'10':'-12','11':'2','20':'20','30':'30','40':'-40'}


def test_pipeline_credit_does_not_report_empty_raw_type(monkeypatch):
    from app.core.config import settings
    doc=seed('credit_card_statement');doc.update(status='uploaded',extracted_data=None,content_type='image/png')
    DocumentRepository().save(doc)
    class Extractor:
        def __init__(self,**kw):pass
        def classify_document(self,image):return 'credit_card_statement'
        def _parse_json(self,value):return json.loads(value)
        def _vision_request_pages(self,pages,prompt):return '{"statement_period_start":"2026-09-01","statement_period_end":"2026-09-30"}'
        def _vision_request(self,page,prompt):return '[{"date":"2026-09-15","amount":-5,"description":"Office","transaction_type":"charge"}]'
    monkeypatch.setattr(settings,'openai_api_key','fake');monkeypatch.setattr(settings,'ai_provider','openai')
    monkeypatch.setattr('app.services.extraction.extractor.DocumentExtractor',Extractor)
    documents._process_document_sync(doc['id'])
    result=client.get('/api/documents/'+doc['id']+'/extracted')
    assert result.status_code==200,result.text
    assert result.json()['credit_card_statement_data']['transactions'][0]['amount']=='-5'


def test_standalone_recovery_checks_reviewed_clearing_date():
    from app.api.recovery import _bank_identity
    doc=seed();doc['qbo_match_results']=[{'transaction_index':0,'reviewed_match':{'transaction':{'date':'2026-09-20','amount':'-550'}}}]
    _bank_identity(doc,{'statement_transaction_index':0},{'date':'2026-09-20','amount':'550','doc_number':'00200'})
    with pytest.raises(Exception):_bank_identity(doc,{'statement_transaction_index':0},{'date':'2026-09-20','amount':'550','doc_number':'999'})


def test_incoming_check_attaches_to_existing_customer_payment(monkeypatch):
    doc=seed();check=CheckData(**doc['extracted_data']);check.direction='incoming'
    doc['extracted_data']=prepare_standalone_check(doc,check);DocumentRepository().save(doc)
    fake=ReconcileQBO(doc['id'])
    fake.get_matching_transactions=lambda **kw:[{'id':'101','type':'payment','amount':'550','date':'2026-09-15',
        'check_number':'00200','description':'Dues & Subscriptions'}]
    def attachment(**kw):
        fake.uploads.append(kw); assert kw['entity_type']=='Payment'; return {'id':'incoming-attachment'}
    fake.upload_attachment=attachment
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    path='/api/documents/'+doc['id']
    for suffix,method,body in [('match-to-qbo','post',{'bank_account_id':'1'}),('match-review/0','patch',{'decision':'approve'}),
        ('resolve-vendors','post',None),('suggest-categories','post',None)]:
        r=getattr(client,method)(path+'/'+suffix,**({'json':body} if body else {}));assert r.status_code==200,r.text
    assert client.get(path+'/bank-workflow').json()['push_ready']
    r=client.post(path+'/push-to-qbo',json={});assert r.status_code==200 and r.json()['success'],r.text
    assert fake.category_writes==0 and len(fake.uploads)==1
    assert client.post(path+'/push-to-qbo',json={}).status_code==200 and len(fake.uploads)==1


def test_check_direction_must_be_explicit(monkeypatch):
    doc=seed();data=CheckData(**doc['extracted_data']);data.direction=None
    with pytest.raises(ValueError,match='outgoing or incoming'):prepare_standalone_check(doc,data)


def test_credit_statement_json_export_contains_rows_without_sources():
    doc=seed('credit_card_statement');before=DocumentRepository().get(doc['id'])
    response=client.get('/api/export/json?document_type=credit_card_statement')
    assert response.status_code==200,response.text
    item=response.json()['documents'][0]
    assert item['extracted_data']['transactions'][0]['amount']=='-550'
    assert 'content' not in item and 'check_image_sources' not in response.text
    assert DocumentRepository().get(doc['id'])==before
