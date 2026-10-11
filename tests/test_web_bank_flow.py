import pytest
from tests.test_document_api_persistence import client, prepare_bank_workflow, restart_request, seed_invoice
from app.services.persistence.repository import DocumentRepository

def bound_workflow(tmp_path, monkeypatch, **options):
    id, fake = prepare_bank_workflow(tmp_path, monkeypatch, **options)
    fake.realm_id = 'realm-bank-98'
    repo = DocumentRepository(); doc = repo.get(id)
    doc['qbo_match_realm_id'] = fake.realm_id
    doc['qbo_match_bank_account'] = {'id':'1','name':'Checking'}
    repo.save(doc)
    return id, fake

def test_readonly_snapshot_and_restart(tmp_path, monkeypatch, isolated_document_database):
    id, fake = bound_workflow(tmp_path, monkeypatch)
    before = DocumentRepository().get(id)
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client', lambda: (_ for _ in ()).throw(AssertionError('must not call QBO')))
    response = client.get(f'/api/documents/{id}/bank-workflow')
    assert response.status_code == 200
    data = response.json()
    assert data['push_ready'] is True and data['locked'] is False
    assert data['realm_id'] == 'realm-bank-98' and data['bank_account']['id'] == '1'
    assert data['plan']['category_updates'][0]['qbo_transaction_id'] == '101'
    assert len(data['plan']['check_attachments']) == 1 and 'image_path' not in data['checks'][0]
    assert 'content' not in data and DocumentRepository().get(id) == before
    restarted = restart_request(isolated_document_database,'GET',f'/api/documents/{id}/bank-workflow')['body']
    assert restarted['revision'] == data['revision'] and restarted['matches'][0]['review_status'] == 'approved'
    assert fake.category_writes == fake.check_uploads == 0

def test_rematch_persists_binding_and_invalidates_categories(tmp_path, monkeypatch):
    id, fake = bound_workflow(tmp_path, monkeypatch)
    response = client.post(f'/api/documents/{id}/match-to-qbo',json={'bank_account_id':'1','expected_realm_id':fake.realm_id})
    assert response.status_code == 200, response.text
    data = client.get(f'/api/documents/{id}/bank-workflow').json()
    assert data['realm_id'] == fake.realm_id and data['bank_account']['id'] == '1'
    assert data['categories'] is None and data['matches'][0]['review_status'] == 'pending'
    assert data['push_ready'] is False

@pytest.mark.parametrize('decision',['reject','approve'])
def test_changed_match_invalidates_downstream_decisions(tmp_path, monkeypatch, decision):
    id, fake = bound_workflow(tmp_path, monkeypatch)
    assert client.patch(f'/api/documents/{id}/match-review/0',json={'decision':decision}).status_code == 200
    data = client.get(f'/api/documents/{id}/bank-workflow').json()
    assert data['categories'] is None and data['checks'][0]['vendor_status'] == 'not_checked'
    assert data['push_ready'] is False
    assert client.post(f'/api/documents/{id}/push-to-qbo',json={'expected_realm_id':fake.realm_id}).status_code == 400
    assert fake.category_writes == fake.check_uploads == 0

@pytest.mark.parametrize('operation',['push-to-qbo','resolve-vendors','suggest-categories'])
def test_changed_company_blocks_bank_operations(tmp_path, monkeypatch, operation):
    id, fake = bound_workflow(tmp_path, monkeypatch); fake.realm_id='different-realm'
    response = client.post(f'/api/documents/{id}/{operation}',json={'attach_documents':True})
    assert response.status_code == 409, response.text
    assert fake.category_writes == fake.check_uploads == 0

def test_stale_preview_blocks_push(tmp_path, monkeypatch):
    id, fake = bound_workflow(tmp_path, monkeypatch)
    data = client.get(f'/api/documents/{id}/bank-workflow').json()
    assert client.patch(f'/api/documents/{id}/category-review/0',json={'decision':'reject'}).status_code == 200
    response=client.post(f'/api/documents/{id}/push-to-qbo',json={'expected_realm_id':fake.realm_id,'expected_revision':data['revision']})
    assert response.status_code == 409 and fake.category_writes == fake.check_uploads == 0

@pytest.mark.parametrize('failure',[None,'category','check'])
def test_persisted_push_success_and_uncertain_results(tmp_path,monkeypatch,isolated_document_database,failure):
    id,fake=bound_workflow(tmp_path,monkeypatch,fail_category=failure=='category',fail_check=failure=='check')
    data=client.get(f'/api/documents/{id}/bank-workflow').json()
    response=client.post(f'/api/documents/{id}/push-to-qbo',json={'expected_realm_id':fake.realm_id,'expected_revision':data['revision']})
    assert response.status_code == (502 if failure=='category' else 200), response.text
    result=restart_request(isolated_document_database,'GET',f'/api/documents/{id}/bank-workflow')['body']
    assert result['locked'] is True and result['push_ready'] is False
    assert result['categories'][0]['qbo_category_push_status'] == ('uncertain' if failure=='category' else 'pushed')
    if failure=='check':assert result['checks'][0]['attachment_push_status']=='uncertain'
    elif failure is None:assert result['checks'][0]['attachment_id']=='check_attachment_1'
    assert fake.category_writes==1

def test_snapshot_rejects_wrong_type_and_missing_document():
    doc=seed_invoice()
    assert client.get(f"/api/documents/{doc['id']}/bank-workflow").status_code==400
    assert client.get('/api/documents/doc_not_existing/bank-workflow').status_code==404

def test_vendor_creation_checks_company_before_write(monkeypatch):
    class Fake:
        realm_id='realm-new'
        def create_vendor(self,**kwargs):raise AssertionError('must not create')
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client',lambda:Fake())
    assert client.post('/api/qbo/vendors',json={'display_name':'Missing Vendor','expected_realm_id':'realm-old'}).status_code==409
