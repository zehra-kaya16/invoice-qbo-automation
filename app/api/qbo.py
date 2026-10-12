"""
QuickBooks Online OAuth and integration endpoints.
"""

import secrets
from typing import Optional
from datetime import timedelta
from sqlalchemy.exc import SQLAlchemyError
from app.models.database import utc_now
from app.services.persistence.qbo_repository import QBOConnectionRepository, QBOStorageError, QBOConnectionConflict

from fastapi import APIRouter, HTTPException, Depends
from app.core.auth import require_admin
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.documents import (
    DocumentType,
    ReceiptData,
)
from app.services.qbo.client import QBOClient
from app.services.qbo.currency_guard import check_document_currency
from app.services.qbo.mapper import (
    build_qbo_create_plan,
)
from app.services.qbo.matcher import (
    find_expense_account_candidates,
)
from app.services.qbo.push_guard import (
    QBOPushGuardError,
    ensure_invoice_can_start_push,
)

router = APIRouter()

class VendorCreateRequest(BaseModel):
    expected_realm_id: Optional[str] = None
    display_name: str

class QBOConnectionStatus(BaseModel):
    connected: bool
    reconnect_required: bool = False
    company_name: Optional[str] = None
    company_id: Optional[str] = None
    expires_at: Optional[str] = None

class QBOVendorOption(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    active: bool = True
    currency: Optional[str] = None


class QBOAccountOption(BaseModel):
    id: str
    name: str
    type: str
    sub_type: Optional[str] = None
    fully_qualified_name: Optional[str] = None
    currency: Optional[str] = None


class InvoiceQBOOptionsResponse(BaseModel):
    document_id: str
    extracted_vendor: Optional[str] = None
    category_suggestion: Optional[str] = None

    vendor_match: Optional[QBOVendorOption] = None

    category_account_matches: list[
        QBOAccountOption
    ] = Field(default_factory=list)

    expense_accounts: list[
        QBOAccountOption
    ] = Field(default_factory=list)


class QBOPlanRequest(BaseModel):
    expected_realm_id: Optional[str] = None
    create_as: str
    vendor_id: str
    account_id: str
    description: Optional[str] = None
    bank_account_id: Optional[str] = None


class QBOPlanPreviewResponse(BaseModel):
    realm_id: Optional[str] = None
    document_id: str
    create_as: str
    vendor: QBOVendorOption
    account: QBOAccountOption
    plan: dict
    currency_check: dict

# Each request restores its own SDK instance from company-scoped storage.


def _storage_call(operation, *args):
    try:
        return operation(*args)
    except QBOConnectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except QBOStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="QBO connection storage is unavailable") from None


def _get_qbo_client() -> QBOClient:
    if not settings.qbo_client_id or not settings.qbo_client_secret:
        raise HTTPException(status_code=500, detail="QBO_CLIENT_ID and QBO_CLIENT_SECRET must be configured")
    # SDK tokens must never be shared across simultaneous company requests.
    return QBOClient(client_id=settings.qbo_client_id, client_secret=settings.qbo_client_secret,
        redirect_uri=settings.qbo_redirect_uri, environment=settings.qbo_environment)


def _restore_connection(force_refresh=False) -> tuple[QBOClient, dict]:
    repository = QBOConnectionRepository()
    record = _storage_call(repository.active)
    if record is None:
        raise HTTPException(status_code=400, detail="QuickBooks is not connected")
    if record["token_state"] != "ready":
        raise HTTPException(status_code=409, detail="QBO token refresh is incomplete; connect again")
    client = _get_qbo_client()
    # Set AuthClient tokens before QuickBooks construction so the SDK does not
    # implicitly refresh outside our persistence transaction.
    client.set_tokens(record["access_token"], record["refresh_token"], record["realm_id"], record["expires_at"])
    if force_refresh or record["expires_at"] is None or record["expires_at"] <= utc_now() + timedelta(seconds=60):
        revision = _storage_call(repository.claim_refresh, record)
        try:
            client.refresh_tokens()
            _storage_call(repository.finish_refresh, record, revision, client)
        except Exception:
            # The response or commit may have been lost. Never retry old tokens.
            try:
                repository.fail_refresh(record, revision)
            except (SQLAlchemyError, QBOStorageError):
                pass  # Committed 'refreshing' state already blocks reuse.
            raise HTTPException(status_code=502, detail="QBO token refresh could not be saved; connect again") from None
        original_id = record["id"]
        record = _storage_call(repository.active)
        if (record is None or record["token_state"] != "ready"
                or record["id"] != original_id or record["revision"] != revision + 1):
            raise HTTPException(status_code=409, detail="QBO connection changed during refresh; connect again")
    return client, record


def _get_connected_qbo_client() -> QBOClient:
    return _restore_connection()[0]


@router.post("/connect", dependencies=[Depends(require_admin)])
async def connect_qbo():
    repository = QBOConnectionRepository()
    state = secrets.token_urlsafe(32)
    _storage_call(repository.begin_oauth, state)
    return {"authorization_url": _get_qbo_client().get_auth_url(state=state)}


@router.get("/callback", dependencies=[Depends(require_admin)])
async def qbo_callback(code: str, state: str, realmId: str):
    repository = QBOConnectionRepository()
    try:
        _storage_call(repository.claim_oauth, state)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(status_code=400, detail=exc.detail) from None
        raise
    client = _get_qbo_client()
    try:
        client.handle_callback(auth_code=code, realm_id=realmId)
    except Exception:
        raise HTTPException(status_code=502, detail="QuickBooks authorization failed; connect again") from None
    # Save rotated credentials before the optional company-name request.
    _storage_call(repository.save_oauth, state, client)
    record = _storage_call(repository.active)
    try:
        info = client.get_company_info()
    except Exception:
        info = {}
    if record is None or record["realm_id"] != client.realm_id:
        raise HTTPException(status_code=409, detail="QBO connection changed during authorization")
    _storage_call(repository.save_name, record, info.get("company_name"))
    return {"status": "connected", "company_name": info.get("company_name"),
        "company_id": client.realm_id, "environment": settings.qbo_environment}


@router.get("/status", response_model=QBOConnectionStatus)
async def qbo_status():
    record = _storage_call(QBOConnectionRepository().active, False)
    if record is None:
        return QBOConnectionStatus(connected=False)
    if record["token_state"] != "ready":
        return QBOConnectionStatus(connected=False, reconnect_required=True, company_id=record["realm_id"])
    client, record = _restore_connection()
    return QBOConnectionStatus(connected=True, company_name=record["company_name"],
        company_id=record["realm_id"], expires_at=client.token_expires_at.isoformat() + "Z")


@router.post("/refresh", response_model=QBOConnectionStatus, dependencies=[Depends(require_admin)])
async def refresh_qbo():
    client, record = _restore_connection(force_refresh=True)
    return QBOConnectionStatus(connected=True, company_name=record["company_name"],
        company_id=record["realm_id"], expires_at=client.token_expires_at.isoformat() + "Z")


@router.get("/accounts")
async def get_qbo_accounts():
    """
    Read the connected company's chart of accounts.
    """

    client = _get_connected_qbo_client()

    accounts = client.get_accounts()

    return {
        "accounts": accounts,
    }


@router.get("/vendors")
async def get_qbo_vendors():
    """
    Read the connected company's vendors.
    """

    client = _get_connected_qbo_client()

    vendors = client.get_vendors()

    return {
        "vendors": vendors,
    }

@router.post("/vendors")
def create_qbo_vendor(
    request: VendorCreateRequest,
):
    client = _get_connected_qbo_client()

    if request.expected_realm_id is not None and request.expected_realm_id != getattr(client, "realm_id", None):
        raise HTTPException(409, "QBO company changed; reload before creating a vendor")

    try:
        result = client.create_vendor(
            display_name=request.display_name,
        )

        return result

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to create vendor in QBO: "
                f"{exc}"
            ),
        ) from exc

@router.get(
    "/invoice-options/{document_id}",
    response_model=InvoiceQBOOptionsResponse,
)
async def get_invoice_qbo_options(
    document_id: str,
):
    """
    Build read-only QBO matching options
    for an approved invoice.

    This endpoint does not create vendors,
    expenses, bills, or any QBO transaction.
    """

    # Lazy import prevents API-module import cycles
    from app.api.documents import _load_document

    doc = _load_document(document_id)

    if (
        doc.get("document_type")
        not in (DocumentType.INVOICE, DocumentType.RECEIPT)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO invoice options are available "
                "only for invoices and receipts"
            ),
        )

    if not doc.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Document has no extracted data"
            ),
        )

    if not doc.get("approved", False):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invoice/receipt must be approved before "
                "QBO mapping"
            ),
        )

    receipt = ReceiptData(
        **doc["extracted_data"]
    )

    client = _get_connected_qbo_client()

    # ------------------------------------------
    # 1. Vendor matching
    # READ ONLY
    # ------------------------------------------

    vendor_match = None

    if receipt.vendor:
        vendor_match = (
            client.find_vendor_by_name(
                receipt.vendor
            )
        )

    # ------------------------------------------
    # 2. Expense accounts
    # READ ONLY
    # ------------------------------------------

    expense_accounts = (
        client.get_expense_accounts()
    )

    # ------------------------------------------
    # 3. Conservative category candidates
    # LOCAL ONLY
    # ------------------------------------------

    category_account_matches = (
        find_expense_account_candidates(
            receipt.category_suggestion,
            expense_accounts,
        )
    )

    return InvoiceQBOOptionsResponse(
        document_id=document_id,
        extracted_vendor=receipt.vendor,
        category_suggestion=(
            receipt.category_suggestion
        ),
        vendor_match=vendor_match,
        category_account_matches=(
            category_account_matches
        ),
        expense_accounts=expense_accounts,
    )


@router.post("/disconnect", dependencies=[Depends(require_admin)])
async def disconnect_qbo():
    """Clear stored credentials locally; this does not revoke access at Intuit."""
    _storage_call(QBOConnectionRepository().disconnect)
    return {"status": "disconnected"}

@router.post(
    "/invoice-plan/{document_id}",
    response_model=QBOPlanPreviewResponse,
)
async def build_invoice_qbo_plan(
    document_id: str,
    request: QBOPlanRequest,
):
    """
    Validate explicitly selected QBO IDs
    and build a local QBO creation plan.

    This endpoint does NOT write to QBO.
    """

    from app.api.documents import _load_document

    doc = _load_document(document_id)

    if (
        doc.get("document_type")
        not in (DocumentType.INVOICE, DocumentType.RECEIPT)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO invoice planning is available "
                "only for invoices and receipts"
            ),
        )

    if not doc.get("extracted_data"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Document has no extracted data"
            ),
        )
    try:
        ensure_invoice_can_start_push(
            doc
        )

    except QBOPushGuardError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    if not doc.get("approved", False):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invoice/receipt must be approved before "
                "QBO mapping"
            ),
        )

    if request.create_as not in {
        "expense",
        "bill",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "create_as must be "
                "'expense' or 'bill'"
            ),
        )

    client = _get_connected_qbo_client()
    if request.expected_realm_id is not None and request.expected_realm_id != client.realm_id:
        raise HTTPException(409, "QBO company changed; reload mapping options before continuing")


    # ------------------------------------------
    # Validate vendor ID against real QBO data
    # ------------------------------------------

    vendors = client.get_vendors()

    selected_vendor = next(
        (
            vendor
            for vendor in vendors
            if vendor["id"] == request.vendor_id
        ),
        None,
    )

    if selected_vendor is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Selected vendor_id was not found "
                "in the connected QBO company"
            ),
        )

    # ------------------------------------------
    # Validate account ID against REAL
    # Expense accounts
    # ------------------------------------------

    expense_accounts = (
        client.get_expense_accounts()
    )

    selected_account = next(
        (
            account
            for account in expense_accounts
            if account["id"] == request.account_id
        ),
        None,
    )

    if selected_account is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Selected account_id was not found "
                "in QBO Expense accounts"
            ),
        )

    # ------------------------------------------
    # Optional bank account validation
    # ------------------------------------------

    selected_bank_account = None
    if request.create_as == "expense" and not request.bank_account_id:
        raise HTTPException(400, "bank_account_id is required for expense creation")
    if request.bank_account_id:
        bank_accounts = (
            client.get_bank_accounts()
        )

        selected_bank_account = next(
            (
                account
                for account in bank_accounts
                if (
                    account["id"]
                    == request.bank_account_id
                )
            ),
            None,
        )

        if selected_bank_account is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Selected bank_account_id was "
                    "not found in QBO Bank accounts"
                ),
            )

    receipt = ReceiptData(
        **doc["extracted_data"]
    )

    currency_check = check_document_currency(
        client, receipt, selected_vendor, selected_account, selected_bank_account
    )

    try:
        plan = build_qbo_create_plan(
            data=receipt,
            create_as=request.create_as,
            vendor_id=request.vendor_id,
            account_id=request.account_id,
            description=request.description,
            bank_account_id=(
                request.bank_account_id
            ),
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    return QBOPlanPreviewResponse(
        realm_id=getattr(client, "realm_id", None),
        document_id=document_id,
        create_as=request.create_as,
        vendor=selected_vendor,
        account=selected_account,
        plan=plan,
        currency_check=currency_check,
    )