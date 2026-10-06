from app.services.extraction.extractor import (
    DocumentExtractor,
)


def test_extract_check_returns_check_data(
    monkeypatch,
):
    extractor = DocumentExtractor.__new__(
        DocumentExtractor
    )

    def fake_vision_request(
        image_data,
        prompt,
    ):
        assert image_data == b"fake-check-image"

        return """
        {
            "check_number": "1234",
            "payee": "Books by Bessie",
            "amount": 550.00,
            "date": "2026-09-15",
            "memo": "Test payment",
            "bank_name": "Test Bank",
            "routing_number": "123456789",
            "account_number_last4": "5678"
        }
        """

    monkeypatch.setattr(
        extractor,
        "_vision_request",
        fake_vision_request,
    )

    result = extractor.extract_check(
        b"fake-check-image"
    )

    assert result.check_number == "1234"
    assert result.payee == "Books by Bessie"
    assert float(result.amount) == 550.00
    assert str(result.date) == "2026-09-15"
    assert result.memo == "Test payment"
    assert result.bank_name == "Test Bank"
    assert result.routing_number == "123456789"
    assert result.account_number_last4 == "5678"
    assert result.confidence == 0.85

def test_extract_check_allows_missing_optional_fields(
    monkeypatch,
):
    extractor = DocumentExtractor.__new__(
        DocumentExtractor
    )

    monkeypatch.setattr(
        extractor,
        "_vision_request",
        lambda image_data, prompt: """
        {
            "check_number": "9876",
            "payee": null,
            "amount": 125.50,
            "date": null,
            "memo": null,
            "bank_name": null,
            "routing_number": null,
            "account_number_last4": null
        }
        """,
    )

    result = extractor.extract_check(
        b"fake-check-image"
    )

    assert result.check_number == "9876"
    assert float(result.amount) == 125.50
    assert result.payee is None
    assert result.date is None
    assert result.memo is None