from app.schemas.documents import (
    DocumentType,
    ProcessingStatus,
)


QBO_PUSH_NOT_STARTED = "not_started"
QBO_PUSH_IN_PROGRESS = "in_progress"
QBO_PUSH_SUCCEEDED = "succeeded"
QBO_PUSH_UNCERTAIN = "uncertain"


class QBOPushGuardError(ValueError):
    pass


def initialize_qbo_push_state(
    document: dict,
) -> None:
    """
    Ensure that the document contains the local QBO push-state fields.

    This function does not call QuickBooks.
    """

    document.setdefault(
        "qbo_push_state",
        QBO_PUSH_NOT_STARTED,
    )

    document.setdefault(
        "qbo_transaction_id",
        None,
    )

    document.setdefault(
        "qbo_transaction_type",
        None,
    )

    document.setdefault(
        "qbo_push_error",
        None,
    )


def ensure_invoice_can_start_push(
    document: dict,
) -> None:
    """
    Verify that an invoice is allowed to begin a QBO write.

    This function does not call QuickBooks
    and does not change document state.
    """

    initialize_qbo_push_state(document)

    if (
        document.get("document_type")
        != DocumentType.INVOICE
    ):
        raise QBOPushGuardError(
            "QBO invoice push is available only for invoices"
        )

    if not document.get("extracted_data"):
        raise QBOPushGuardError(
            "Invoice has no extracted data"
        )

    if not document.get("approved", False):
        raise QBOPushGuardError(
            "Invoice must be approved before QBO push"
        )

    validation = document.get("validation")

    if (
        validation is None
        or not validation.get("is_valid", False)
    ):
        raise QBOPushGuardError(
            "Invoice must have valid validation before QBO push"
        )

    if (
        document.get("status")
        == ProcessingStatus.PUSHED
        or document.get("qbo_transaction_id")
    ):
        raise QBOPushGuardError(
            "Invoice has already been pushed to QBO"
        )

    push_state = document.get(
        "qbo_push_state"
    )

    if push_state == QBO_PUSH_IN_PROGRESS:
        raise QBOPushGuardError(
            "QBO push is already in progress"
        )

    if push_state == QBO_PUSH_SUCCEEDED:
        raise QBOPushGuardError(
            "Invoice has already been pushed to QBO"
        )

    if push_state == QBO_PUSH_UNCERTAIN:
        raise QBOPushGuardError(
            "Previous QBO push result is uncertain; "
            "manual verification is required before retrying"
        )


def mark_invoice_push_started(
    document: dict,
) -> None:
    """
    Mark the beginning of the QBO write attempt.
    """

    ensure_invoice_can_start_push(
        document
    )

    document["qbo_push_state"] = (
        QBO_PUSH_IN_PROGRESS
    )

    document["qbo_push_error"] = None


def mark_invoice_push_succeeded(
    document: dict,
    transaction_id: str,
    transaction_type: str,
) -> None:
    """
    Store the successful QBO transaction result.
    """

    if not transaction_id:
        raise QBOPushGuardError(
            "QBO transaction_id is required"
        )

    if transaction_type not in {
        "expense",
        "bill",
    }:
        raise QBOPushGuardError(
            "QBO transaction_type must be "
            "'expense' or 'bill'"
        )

    if (
        document.get("qbo_push_state")
        != QBO_PUSH_IN_PROGRESS
    ):
        raise QBOPushGuardError(
            "QBO push was not marked as in progress"
        )

    document["qbo_transaction_id"] = (
        str(transaction_id)
    )

    document["qbo_transaction_type"] = (
        transaction_type
    )

    document["qbo_push_state"] = (
        QBO_PUSH_SUCCEEDED
    )

    document["qbo_push_error"] = None

    document["status"] = (
        ProcessingStatus.PUSHED
    )


def mark_invoice_push_uncertain(
    document: dict,
    error: str,
) -> None:
    """
    Mark a QBO write attempt whose final provider-side
    result is not known.

    Automatic retry is intentionally blocked.
    """

    document["qbo_push_state"] = (
        QBO_PUSH_UNCERTAIN
    )

    document["qbo_push_error"] = str(error)