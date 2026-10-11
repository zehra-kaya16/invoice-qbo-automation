from types import SimpleNamespace

import pytest

from app.services.persistence.repository import DocumentRepository
from app.services.qbo.client import QBOClient
from tests.test_document_api_persistence import client, FakeQBO, seed_invoice


@pytest.mark.parametrize("create_as", ["bill", "expense"])
@pytest.mark.parametrize("currency", ["USD", "EUR", None])
def test_currency_checked_on_both_plan_and_push(monkeypatch, create_as, currency):
    document = seed_invoice()
    document["extracted_data"]["currency"] = currency
    DocumentRepository().save(document)
    fake = FakeQBO(document["id"])
    fake.get_bank_accounts = lambda: [{"id": "1", "name": "Checking", "type": "Bank"}]
    fake.create_expense = lambda **kwargs: fake.create_bill(**kwargs)
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    body = {"create_as": create_as, "vendor_id": "30", "account_id": "10", "bank_account_id": "1", "attach_documents": False}
    plan = client.post(f"/api/qbo/invoice-plan/{document['id']}", json=body)
    assert plan.status_code == (200 if currency == "USD" else 400), plan.text
    assert fake.writes == 0
    push = client.post(f"/api/documents/{document['id']}/push-to-qbo", json=body)
    assert push.status_code == (200 if currency == "USD" else 400), push.text
    assert fake.writes == (1 if currency == "USD" else 0)
    if currency != "USD":
        stored = DocumentRepository().get(document["id"])
        assert stored["qbo_push_state"] == "not_started"
        assert stored.get("qbo_transaction_id") is None


@pytest.mark.parametrize("failure", ["preferences", "home", "vendor", "bank", "missing_vendor_multi", "missing_bank_multi"])
def test_currency_failure_never_marks_or_writes(monkeypatch, failure):
    document = seed_invoice()
    fake = FakeQBO(document["id"])
    preferences = {"home_currency": "USD", "multicurrency_enabled": failure.startswith("missing")}
    if failure == "home":
        preferences["home_currency"] = None
    def prefs():
        if failure == "preferences":
            raise TimeoutError()
        return preferences
    fake.get_currency_preferences = prefs
    fake.get_vendors = lambda: [{"id": "30", "name": "Books", "currency": "EUR" if failure == "vendor" else (None if failure == "missing_vendor_multi" else "USD")}]
    fake.get_bank_accounts = lambda: [{"id": "1", "name": "Checking", "type": "Bank", "currency": "EUR" if failure == "bank" else (None if failure == "missing_bank_multi" else "USD")}]
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    body = {"create_as": "expense", "vendor_id": "30", "account_id": "10", "bank_account_id": "1", "attach_documents": False}
    expected = 502 if failure in ("preferences", "home") else 400
    for path in (f"/api/qbo/invoice-plan/{document['id']}", f"/api/documents/{document['id']}/push-to-qbo"):
        response = client.post(path, json=body)
        assert response.status_code == expected, response.text
    assert fake.writes == 0
    assert DocumentRepository().get(document["id"])["qbo_push_state"] == "not_started"


def test_preferences_sdk_mapping(monkeypatch):
    from quickbooks.objects.preferences import Preferences
    qbo = QBOClient.__new__(QBOClient)
    qbo.qb_client = object()
    seen = []
    def get(qb):
        seen.append(qb)
        return SimpleNamespace(CurrencyPrefs=SimpleNamespace(HomeCurrency=SimpleNamespace(value="TRY"), MultiCurrencyEnabled=True))
    monkeypatch.setattr(Preferences, "get", get)
    assert qbo.get_currency_preferences() == {"home_currency": "TRY", "multicurrency_enabled": True}
    assert seen == [qbo.qb_client]


def test_expense_requires_bank_account_before_write(monkeypatch):
    document = seed_invoice()
    fake = FakeQBO(document["id"])
    monkeypatch.setattr("app.api.qbo._get_connected_qbo_client", lambda: fake)
    body = {"create_as": "expense", "vendor_id": "30", "account_id": "10", "attach_documents": False}
    for path in (f"/api/qbo/invoice-plan/{document['id']}", f"/api/documents/{document['id']}/push-to-qbo"):
        assert client.post(path, json=body).status_code == 400
    assert fake.writes == 0
