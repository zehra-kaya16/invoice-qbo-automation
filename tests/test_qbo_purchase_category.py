import pytest

from app.services.qbo.client import QBOClient


class FakeExpenseDetail:
    def __init__(self):
        self.AccountRef = {
            "value": "OLD_ACCOUNT"
        }


class FakeExpenseLine:
    def __init__(self):
        self.DetailType = "AccountBasedExpenseLineDetail"
        self.AccountBasedExpenseLineDetail = FakeExpenseDetail()


class FakeOtherLine:
    def __init__(self):
        self.DetailType = "OtherDetail"


class FakePurchase:
    def __init__(self):
        self.Id = "101"
        self.Line = [
            FakeOtherLine(),
            FakeExpenseLine(),
        ]
        self.save_calls = []

    def save(self, qb):
        self.save_calls.append(qb)


def make_client():
    client = QBOClient.__new__(QBOClient)
    client.qb_client = object()

    return client

def test_update_purchase_category_updates_account_and_saves(
    monkeypatch,
):
    client = make_client()

    purchase = FakePurchase()

    def fake_purchase_get(
        purchase_id,
        qb,
    ):
        assert purchase_id == "101"
        assert qb is client.qb_client

        return purchase

    monkeypatch.setattr(
        "app.services.qbo.client.Purchase.get",
        fake_purchase_get,
    )

    result = client.update_purchase_category(
        purchase_id="101",
        account_id="55",
    )

    expense_line = purchase.Line[1]

    assert (
        expense_line
        .AccountBasedExpenseLineDetail
        .AccountRef
    ) == {
        "value": "55"
    }

    assert purchase.save_calls == [
        client.qb_client
    ]

    assert result == {
        "id": "101",
        "type": "purchase",
        "account_id": "55",
    }

def test_update_purchase_category_requires_qbo_client():
    client = QBOClient.__new__(QBOClient)
    client.qb_client = None

    with pytest.raises(
        ValueError,
        match="QuickBooks client is not initialized",
    ):
        client.update_purchase_category(
            purchase_id="101",
            account_id="55",
        )

def test_update_purchase_category_fails_when_purchase_not_found(
    monkeypatch,
):
    client = make_client()

    monkeypatch.setattr(
        "app.services.qbo.client.Purchase.get",
        lambda purchase_id, qb: None,
    )

    with pytest.raises(
        ValueError,
        match="QBO Purchase was not found",
    ):
        client.update_purchase_category(
            purchase_id="999",
            account_id="55",
        )

def test_update_purchase_category_requires_expense_line(
    monkeypatch,
):
    client = make_client()

    purchase = FakePurchase()

    purchase.Line = [
        FakeOtherLine(),
    ]

    monkeypatch.setattr(
        "app.services.qbo.client.Purchase.get",
        lambda purchase_id, qb: purchase,
    )

    with pytest.raises(
        ValueError,
        match=(
            "QBO Purchase has no "
            "AccountBasedExpenseLineDetail"
        ),
    ):
        client.update_purchase_category(
            purchase_id="101",
            account_id="55",
        )

    assert purchase.save_calls == []

def test_update_purchase_category_requires_expense_detail(
    monkeypatch,
):
    client = make_client()

    purchase = FakePurchase()

    expense_line = FakeExpenseLine()
    expense_line.AccountBasedExpenseLineDetail = None

    purchase.Line = [
        expense_line
    ]

    monkeypatch.setattr(
        "app.services.qbo.client.Purchase.get",
        lambda purchase_id, qb: purchase,
    )

    with pytest.raises(
        ValueError,
        match=(
            "QBO Purchase expense line "
            "detail is missing"
        ),
    ):
        client.update_purchase_category(
            purchase_id="101",
            account_id="55",
        )

    assert purchase.save_calls == []
    