from app.api.documents import (
    _build_extraction_response,
)
from app.schemas.documents import (
    DocumentType,
    ProcessingStatus,
)


def test_build_extraction_response_returns_check_data():
    document = {
        "id": "doc_test_check",
        "filename": "check.png",
        "status": ProcessingStatus.EXTRACTED,
        "document_type": DocumentType.CHECK,
        "extracted_data": {
            "check_number": "1234",
            "payee": "Books by Bessie",
            "amount": "550.00",
            "date": "2026-09-15",
            "memo": "Test payment",
            "bank_name": "Test Bank",
            "routing_number": "123456789",
            "account_number_last4": "5678",
            "confidence": 0.85,
            "image_path": None,
        },
    }

    response = _build_extraction_response(
        document
    )

    assert response.check_data is not None
    assert response.check_data.check_number == "1234"
    assert response.check_data.payee == "Books by Bessie"
    assert float(response.check_data.amount) == 550.00
    assert str(response.check_data.date) == "2026-09-15"
    assert response.check_data.memo == "Test payment"