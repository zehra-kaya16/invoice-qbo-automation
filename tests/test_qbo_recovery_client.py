from types import SimpleNamespace as Object
from decimal import Decimal

import pytest

from app.services.qbo.client import QBOClient
from quickbooks.objects.bill import Bill
from quickbooks.objects.purchase import Purchase
from quickbooks.objects.attachable import Attachable


def sdk_client():
    client = QBOClient.__new__(QBOClient)
    client.qb_client = object()
    return client


@pytest.mark.parametrize('kind', ['bill', 'expense'])
def test_creation_persists_recovery_identity_in_private_note(monkeypatch, kind):
    saved = []
    def save(value, **kwargs):
        saved.append(value)
        value.Id = '701'
    monkeypatch.setattr(Bill if kind == 'bill' else Purchase, 'save', save)
    kwargs = dict(vendor_id='30', account_id='10', amount=Decimal('550'), date='2026-09-15', recovery_marker='receipt-ai:99')
    if kind == 'expense':
        kwargs['bank_account_id'] = '1'
    result = getattr(sdk_client(), 'create_' + kind)(**kwargs)
    assert result['id'] == '701'
    assert saved[0].PrivateNote == 'receipt-ai:99'
    assert saved[0].Line[0].AccountBasedExpenseLineDetail.AccountRef['value'] == '10'
    if kind == 'expense':
        assert saved[0].AccountRef['value'] == '1'


def test_upload_persists_identity_and_correct_entity_link(monkeypatch, tmp_path):
    saved = []
    def save(value, **kwargs):
        saved.append(value)
        value.Id = '901'
    monkeypatch.setattr(Attachable, 'save', save)
    path = tmp_path / 'source.pdf'
    path.write_bytes(b'%PDF')
    result = sdk_client().upload_attachment(str(path), path.name, 'application/pdf', 'Bill', '701', 'receipt-ai:99:source')
    assert result['id'] == '901'
    assert saved[0].Note == 'receipt-ai:99:source'
    assert saved[0].AttachableRef[0].EntityRef == {'type': 'Bill', 'value': '701'}
    assert saved[0]._FilePath == str(path)


def test_transaction_reader_normalizes_sdk_reference_objects(monkeypatch):
    value = Object(Id='701', TxnDate='2026-09-15', TotalAmt=Decimal('550'), CurrencyRef=Object(value='USD'),
                   VendorRef={'value': '30'}, PrivateNote='receipt-ai:99',
                   Line=[Object(DetailType='AccountBasedExpenseLineDetail',
                                AccountBasedExpenseLineDetail=Object(AccountRef=Object(value='10')))])
    monkeypatch.setattr(Bill, 'get', lambda *args, **kwargs: value)
    result = sdk_client().get_recovery_transaction('bill', '701')
    assert result['vendor_id'] == '30' and result['currency'] == 'USD'
    assert result['expense_accounts'] == ['10'] and result['marker'] == 'receipt-ai:99'


@pytest.mark.parametrize('kind,id', [('unsupported', '701'), ('bill', "701' OR 1=1"), ('purchase', '')])
def test_transaction_reader_rejects_invalid_targets(kind, id):
    with pytest.raises(ValueError):
        sdk_client().get_recovery_transaction(kind, id)


def test_transaction_search_checks_every_page(monkeypatch):
    calls = []
    def page(**kwargs):
        calls.append(kwargs)
        if kwargs['start_position'] == 1:
            return [Object(Id=str(i), PrivateNote='other') for i in range(1000)]
        return [Object(Id='701', PrivateNote='receipt-ai:99')]
    monkeypatch.setattr(Bill, 'filter', page)
    client = sdk_client()
    monkeypatch.setattr(client, 'get_recovery_transaction', lambda kind, id: {'id': id})
    assert client.find_recovery_transactions('bill', '2026-09-15', 'receipt-ai:99') == [{'id': '701'}]
    assert [item['start_position'] for item in calls] == [1, 1001]
    assert all(item['max_results'] == 1000 and item['TxnDate'] == '2026-09-15' for item in calls)


def test_incomplete_search_never_reports_no_match(monkeypatch):
    def page(**kwargs):
        if kwargs['start_position'] == 1:
            return [Object(Id=str(i), PrivateNote='other') for i in range(1000)]
        raise TimeoutError('next page unavailable')
    monkeypatch.setattr(Bill, 'filter', page)
    with pytest.raises(TimeoutError):
        sdk_client().find_recovery_transactions('bill', '2026-09-15', 'receipt-ai:99')


def test_attachment_search_pages_and_checks_both_entity_type_and_id(monkeypatch):
    calls = []
    def attachment(id, kind, target):
        return Object(Id=id, FileName='source.pdf', Size=4, Note='receipt-ai:99:source',
                      AttachableRef=[Object(EntityRef={'type': kind, 'value': target})])
    def page(**kwargs):
        calls.append(kwargs['start_position'])
        if kwargs['start_position'] == 1:
            return [attachment(str(i), 'Purchase', '701') for i in range(1000)]
        return [attachment('901', 'Bill', '701'), attachment('902', 'Bill', '702')]
    monkeypatch.setattr(Attachable, 'all', page)
    assert sdk_client().get_recovery_attachments('Bill', '701') == [
        {'id': '901', 'file_name': 'source.pdf', 'size': 4, 'marker': 'receipt-ai:99:source'}]
    assert calls == [1, 1001]
