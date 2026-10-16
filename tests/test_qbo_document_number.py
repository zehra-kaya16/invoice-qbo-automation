import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from quickbooks.objects.bill import Bill
from quickbooks.objects.purchase import Purchase

from app.schemas.documents import ReceiptData
from app.services.qbo.mapper import build_qbo_create_plan
from app.services.qbo.document_number import normalize_document_number
from app.services.persistence.repository import DocumentRepository
from tests.test_qbo_recovery_client import sdk_client
from tests.test_document_api_persistence import client, seed_invoice, restart_request
from tests.test_qbo_recovery import RecoveryQBO, request as recover, setup_invoice


@pytest.mark.parametrize('kind',['bill','expense'])
def test_reviewed_number_is_mapped_without_losing_leading_zeros(kind):
    data=ReceiptData(vendor='Vendor',date='2026-10-15',total_amount='21.08',invoice_number='  00109-ABC  ')
    result=build_qbo_create_plan(data,kind,'30','10',bank_account_id='1')
    assert result['kwargs']['doc_number']=='00109-ABC'


@pytest.mark.parametrize('number',[None,'','   '])
def test_missing_numbers_are_not_invented(number):
    data=ReceiptData(date='2026-10-15',total_amount='21.08',invoice_number=number)
    assert 'doc_number' not in build_qbo_create_plan(data,'bill','30','10')['kwargs']


@pytest.mark.parametrize('number',['x'*22,'A\nB','A\x00B','A\tB',123])
def test_invalid_numbers_fail_without_truncating_them(number):
    with pytest.raises(ValueError):normalize_document_number(number)


def test_twenty_one_characters_are_preserved():
    assert normalize_document_number('x'*21)=='x'*21


@pytest.mark.parametrize('kind',['bill','expense'])
def test_real_sdk_payload_contains_docnumber_and_recovery_marker(monkeypatch,kind):
    saved=[]
    def save(value,**kwargs):
        saved.append(json.loads(value.to_json()));value.Id='701'
    monkeypatch.setattr(Bill if kind=='bill' else Purchase,'save',save)
    args={'vendor_id':'30','account_id':'10','amount':Decimal('21.08'),'date':'2026-10-15','doc_number':'00109-ABC','recovery_marker':'receipt-ai:109'}
    if kind=='expense':args['bank_account_id']='1'
    result=getattr(sdk_client(),'create_'+kind)(**args)
    assert saved[0]['DocNumber']=='00109-ABC' and saved[0]['PrivateNote']=='receipt-ai:109'
    assert result['doc_number']=='00109-ABC' and result['id']=='701'


@pytest.mark.parametrize('kind',['bill','expense'])
def test_sdk_rejects_long_numbers_before_save(monkeypatch,kind):
    def forbidden(*args,**kwargs):raise AssertionError('SDK save must not run')
    monkeypatch.setattr(Bill if kind=='bill' else Purchase,'save',forbidden)
    with pytest.raises(ValueError,match='21 characters'):
        getattr(sdk_client(),'create_'+kind)(vendor_id='30',account_id='10',amount=Decimal('1'),date='2026-10-15',doc_number='x'*22)


def test_recovery_reader_returns_remote_docnumber(monkeypatch):
    obj=SimpleNamespace(Id='701',TxnDate='2026-10-15',TotalAmt='21.08',DocNumber='00109-ABC',Line=[])
    monkeypatch.setattr(Bill,'get',lambda *args,**kwargs:obj)
    assert sdk_client().get_recovery_transaction('bill','701')['doc_number']=='00109-ABC'


class NumberQBO(RecoveryQBO):
    wrong_echo=False
    def get_vendors(self):return [{'id':'30','name':'Books by Bessie'}]
    def get_expense_accounts(self):return [{'id':'10','name':'Dues & Subscriptions','type':'Expense'}]
    def get_bank_accounts(self):return [{'id':'1','name':'Savings','type':'Bank'}]
    def create_bill(self,**kwargs):
        assert DocumentRepository().get(self.document_id)['qbo_push_context']['plan']['kwargs']['doc_number']==kwargs['doc_number']
        self.writes+=1
        number='OTHER-NUMBER' if self.wrong_echo else kwargs['doc_number']
        self.transactions['701']={'id':'701','type':'bill','date':kwargs['date'],'amount':str(kwargs['amount']),
            'currency':'USD','vendor_id':kwargs['vendor_id'],'expense_accounts':[kwargs['account_id']],
            'marker':kwargs['recovery_marker'],'doc_number':number}
        return {'id':'701','type':'bill','doc_number':number}
    def create_expense(self,**kwargs):
        result=self.create_bill(**kwargs);result['type']='expense'
        self.transactions['701'].update(type='expense',bank_account_id=kwargs['bank_account_id'])
        return result


@pytest.fixture
def numbered(monkeypatch):
    doc=seed_invoice();doc['extracted_data']['invoice_number']='INV-109-TEST';DocumentRepository().save(doc)
    fake=NumberQBO();fake.document_id=doc['id']
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:fake)
    return doc['id'],fake


def body(kind='bill'):
    return {'create_as':kind,'vendor_id':'30','account_id':'10','bank_account_id':'1'}


@pytest.mark.parametrize('kind',['bill','expense'])
def test_preview_push_attachment_idempotency_and_restart(numbered,isolated_document_database,kind):
    id,fake=numbered;before=DocumentRepository().get(id)
    preview=client.post(f'/api/qbo/invoice-plan/{id}',json=body(kind))
    assert preview.status_code==200 and preview.json()['plan']['kwargs']['doc_number']=='INV-109-TEST'
    assert DocumentRepository().get(id)==before and fake.writes==fake.uploads==0
    pushed=client.post(f'/api/documents/{id}/push-to-qbo',json=body(kind))
    assert pushed.status_code==200 and pushed.json()['success'],pushed.text
    doc=DocumentRepository().get(id);assert doc['qbo_doc_number']=='INV-109-TEST'
    assert fake.writes==fake.uploads==1
    result=restart_request(isolated_document_database,'GET',f'/api/documents/{id}')
    assert result['body']['qbo_doc_number']=='INV-109-TEST'
    assert result['body']['qbo_transaction_id']=='701'
    assert client.post(f'/api/documents/{id}/push-to-qbo',json=body(kind)).status_code==409
    assert fake.writes==1


def test_bad_number_is_rejected_before_qbo_write(numbered):
    id,fake=numbered;doc=DocumentRepository().get(id);doc['extracted_data']['invoice_number']='x'*22;DocumentRepository().save(doc)
    assert client.post(f'/api/qbo/invoice-plan/{id}',json=body()).status_code==400
    assert client.post(f'/api/documents/{id}/push-to-qbo',json=body()).status_code==400
    assert fake.writes==fake.uploads==0


def test_mismatching_echo_keeps_known_transaction_and_requires_verification(numbered):
    id,fake=numbered;fake.wrong_echo=True
    result=client.post(f'/api/documents/{id}/push-to-qbo',json=body())
    assert result.status_code==502,result.text
    doc=DocumentRepository().get(id)
    assert doc['qbo_transaction_id']=='701' and doc['qbo_doc_number_needs_review']
    assert fake.writes==1 and fake.uploads==0
    assert recover(id,'transaction').status_code==409
    assert client.post(f'/api/documents/{id}/push-to-qbo',json=body()).status_code==409
    fake.transactions['701']['doc_number']='INV-109-TEST'  # operator corrected the SAME QBO transaction
    verified=recover(id,'transaction');assert verified.status_code==200,verified.text
    assert not DocumentRepository().get(id)['qbo_doc_number_needs_review']
    assert DocumentRepository().get(id)['qbo_doc_number']=='INV-109-TEST'
    assert recover(id,'source','retry',acknowledged=True).status_code==200
    assert fake.writes==fake.uploads==1


@pytest.mark.parametrize('actual',[None,'OTHER'])
def test_recovery_will_not_adopt_a_different_or_missing_number(monkeypatch,actual):
    id,fake=setup_invoice(monkeypatch);doc=DocumentRepository().get(id)
    doc['qbo_push_context']['plan']['kwargs']['doc_number']='INV-109-TEST';DocumentRepository().save(doc)
    fake.transactions['701']['doc_number']=actual
    before=DocumentRepository().get(id)
    assert recover(id,'transaction').status_code==409
    assert DocumentRepository().get(id)==before and fake.writes==fake.uploads==0


def test_legacy_write_plan_does_not_infer_a_number_from_extracted_data(monkeypatch):
    id,fake=setup_invoice(monkeypatch);doc=DocumentRepository().get(id)
    doc['extracted_data']['invoice_number']='EXTRACTED-BUT-NOT-SENT';DocumentRepository().save(doc)
    assert 'doc_number' not in doc['qbo_push_context']['plan']['kwargs']
    assert recover(id,'transaction').status_code==200
    assert fake.writes==fake.uploads==0

from tests.test_batch_extraction import actor
from tests.test_batch_push import ready as batch_ready, selection as batch_selection, push as batch_send


def test_batch_plan_contains_each_number_and_results_persist_them(batch_ready,monkeypatch,isolated_document_database):
    data,fake,_=batch_ready
    for i,id in enumerate(data['uploaded_documents']):
        doc=DocumentRepository().get(id);doc['extracted_data']['invoice_number']=f'INV-109-BATCH-{i}';DocumentRepository().save(doc)
    original=fake.create_bill
    def create(**kwargs):
        result=original(**kwargs);result['doc_number']=kwargs['doc_number'];return result
    monkeypatch.setattr(fake,'create_bill',create)
    plans=batch_selection(data)
    result=batch_send(data,plans);assert result.status_code==200,result.text
    run=result.json();assert run['status']=='completed',run
    assert {i['doc_number'] for i in run['items']}=={'INV-109-BATCH-0','INV-109-BATCH-1'}
    assert len(fake.writes)==len(fake.attachments)==2
    assert restart_request(isolated_document_database,'GET',f"/api/batch/{data['batch_id']}/push-runs/{run['run_id']}")['body']==run


def test_pending_legacy_numberless_preview_must_be_reviewed_again(batch_ready,isolated_document_database):
    from app.models.database import UploadBatch
    data,fake,_=batch_ready
    id=data['uploaded_documents'][0];doc=DocumentRepository().get(id)
    doc['extracted_data']['invoice_number']='INV-109-OLD';DocumentRepository().save(doc)
    plans=batch_selection(data,(0,))
    with isolated_document_database() as session:
        batch=session.get(UploadBatch,data['batch_id']);saved=copy.deepcopy(batch.qbo_plans)
        saved[id]['preview']['plan']['kwargs'].pop('doc_number')
        batch.qbo_plans=saved;session.commit()
    assert batch_send(data,plans).status_code==409 and not fake.writes


def test_receipt_extraction_preserves_a_printed_reference_number(monkeypatch):
    from app.services.extraction.extractor import DocumentExtractor
    extractor=DocumentExtractor.__new__(DocumentExtractor);prompts=[]
    def read(image,prompt):
        prompts.append(prompt)
        return json.dumps({'vendor':'Vendor','date':'2026-10-15','invoice_number':'000109-REC','total_amount':'9.09','currency':'USD'})
    monkeypatch.setattr(extractor,'_vision_request',read)
    receipt=extractor.extract_receipt(b'printed receipt')
    assert receipt.invoice_number=='000109-REC'
    assert 'receipt/document number' in prompts[0] and 'payment authorization' in prompts[0]
    assert build_qbo_create_plan(receipt,'expense','30','10',bank_account_id='1')['kwargs']['doc_number']=='000109-REC'
