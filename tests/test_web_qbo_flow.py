import pytest
from tests.test_receipt_workflow import ReceiptQBO, request_body
from tests.test_document_api_persistence import client, seed_invoice, restart_request
from app.services.persistence.repository import DocumentRepository

@pytest.mark.parametrize('endpoint', ['plan', 'push'])
def test_company_change_blocks_plan_and_push_before_write(monkeypatch, endpoint):
    doc = seed_invoice()
    fake = ReceiptQBO(doc['id']); fake.realm_id = 'realm-new'
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client', lambda: fake)
    body = {**request_body('bill'), 'expected_realm_id': 'realm-old'}
    path = f"/api/qbo/invoice-plan/{doc['id']}" if endpoint == 'plan' else f"/api/documents/{doc['id']}/push-to-qbo"
    response = client.post(path, json=body)
    assert response.status_code == 409, response.text
    assert fake.writes == fake.attachments == 0
    assert DocumentRepository().get(doc['id'])['qbo_push_state'] == 'not_started'

@pytest.mark.parametrize('create_as', ['bill', 'expense'])
def test_ui_plan_contract_and_persistent_result(monkeypatch, isolated_document_database, create_as):
    doc = seed_invoice()
    fake = ReceiptQBO(doc['id']); fake.realm_id = 'realm-97'
    monkeypatch.setattr('app.api.qbo._get_connected_qbo_client', lambda: fake)
    body = {**request_body(create_as), 'expected_realm_id': 'realm-97'}
    plan = client.post(f"/api/qbo/invoice-plan/{doc['id']}", json=body)
    assert plan.status_code == 200, plan.text
    assert plan.json()['realm_id'] == 'realm-97'
    assert fake.writes == 0
    response = client.post(f"/api/documents/{doc['id']}/push-to-qbo", json=body)
    assert response.status_code == 200, response.text
    assert response.json()['transactions_pushed'] == response.json()['attachments_uploaded'] == 1
    result = restart_request(isolated_document_database, 'GET', f"/api/documents/{doc['id']}")['body']
    assert result['qbo_transaction_id'] == ('146' if create_as == 'bill' else '147')
    assert result['qbo_attachment_id'] == 'receipt_attachment'
    assert client.post(f"/api/documents/{doc['id']}/push-to-qbo", json=body).status_code == 409
    assert fake.writes == fake.attachments == 1
