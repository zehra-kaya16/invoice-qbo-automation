import pytest

from app.schemas.documents import (
    DocumentType,
    ProcessingStatus,
)

from app.services.qbo.push_guard import (
    QBOPushGuardError,
    QBO_PUSH_IN_PROGRESS,
    QBO_PUSH_NOT_STARTED,
    QBO_PUSH_SUCCEEDED,
    QBO_PUSH_UNCERTAIN,
    ensure_invoice_can_start_push,
    initialize_qbo_push_state,
    mark_invoice_push_started,
    mark_invoice_push_succeeded,
    mark_invoice_push_uncertain,
)


def make_ready_invoice() -> dict:
    return {
        "id": "doc_test",
        "document_type": DocumentType.INVOICE,
        "status": ProcessingStatus.EXTRACTED,
        "extracted_data": {
            "vendor": "Test Vendor",
            "date": "2026-09-15",
            "total_amount": "550.00",
        },
        "validation": {
            "is_valid": True,
            "errors": [],
        },
        "approved": True,
    }


def test_initializes_push_state():
    document = make_ready_invoice()

    initialize_qbo_push_state(
        document
    )

    assert (
        document["qbo_push_state"]
        == QBO_PUSH_NOT_STARTED
    )

    assert (
        document["qbo_transaction_id"]
        is None
    )

    assert (
        document["qbo_transaction_type"]
        is None
    )


def test_ready_invoice_can_start_push():
    document = make_ready_invoice()

    mark_invoice_push_started(
        document
    )

    assert (
        document["qbo_push_state"]
        == QBO_PUSH_IN_PROGRESS
    )


def test_second_push_is_blocked_while_in_progress():
    document = make_ready_invoice()

    mark_invoice_push_started(
        document
    )

    with pytest.raises(
        QBOPushGuardError,
        match="already in progress",
    ):
        mark_invoice_push_started(
            document
        )


def test_unapproved_invoice_is_blocked():
    document = make_ready_invoice()

    document["approved"] = False

    with pytest.raises(
        QBOPushGuardError,
        match="must be approved",
    ):
        ensure_invoice_can_start_push(
            document
        )


def test_invalid_invoice_is_blocked():
    document = make_ready_invoice()

    document["validation"] = {
        "is_valid": False,
        "errors": ["Test validation error"],
    }

    with pytest.raises(
        QBOPushGuardError,
        match="valid validation",
    ):
        ensure_invoice_can_start_push(
            document
        )


def test_successful_push_stores_qbo_result():
    document = make_ready_invoice()

    mark_invoice_push_started(
        document
    )

    mark_invoice_push_succeeded(
        document=document,
        transaction_id="qbo_txn_123",
        transaction_type="bill",
    )

    assert (
        document["qbo_push_state"]
        == QBO_PUSH_SUCCEEDED
    )

    assert (
        document["qbo_transaction_id"]
        == "qbo_txn_123"
    )

    assert (
        document["qbo_transaction_type"]
        == "bill"
    )

    assert (
        document["status"]
        == ProcessingStatus.PUSHED
    )


def test_successful_invoice_cannot_be_pushed_again():
    document = make_ready_invoice()

    mark_invoice_push_started(
        document
    )

    mark_invoice_push_succeeded(
        document=document,
        transaction_id="qbo_txn_123",
        transaction_type="bill",
    )

    with pytest.raises(
        QBOPushGuardError,
        match="already been pushed",
    ):
        ensure_invoice_can_start_push(
            document
        )


def test_uncertain_push_blocks_automatic_retry():
    document = make_ready_invoice()

    mark_invoice_push_started(
        document
    )

    mark_invoice_push_uncertain(
        document=document,
        error="Network error during QBO write",
    )

    assert (
        document["qbo_push_state"]
        == QBO_PUSH_UNCERTAIN
    )

    assert (
        document["qbo_push_error"]
        == "Network error during QBO write"
    )

    with pytest.raises(
        QBOPushGuardError,
        match="manual verification",
    ):
        ensure_invoice_can_start_push(
            document
        )