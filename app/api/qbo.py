"""
QuickBooks Online OAuth and integration endpoints.
"""

import secrets
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.documents import (
    DocumentType,
    ReceiptData,
)

from app.services.qbo.mapper import (
    build_qbo_create_plan,
)

from app.services.qbo.matcher import (
    find_expense_account_candidates,
)

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.services.qbo.client import QBOClient

from app.services.qbo.push_guard import (
    QBOPushGuardError,
    ensure_invoice_can_start_push,
)

router = APIRouter()

class VendorCreateRequest(BaseModel):
    display_name: str

class QBOConnectionStatus(BaseModel):
    connected: bool
    company_name: Optional[str] = None
    company_id: Optional[str] = None
    expires_at: Optional[str] = None

class QBOVendorOption(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    active: bool = True


class QBOAccountOption(BaseModel):
    id: str
    name: str
    type: str
    sub_type: Optional[str] = None
    fully_qualified_name: Optional[str] = None


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
    create_as: str
    vendor_id: str
    account_id: str
    description: Optional[str] = None
    bank_account_id: Optional[str] = None


class QBOPlanPreviewResponse(BaseModel):
    document_id: str
    create_as: str
    vendor: QBOVendorOption
    account: QBOAccountOption
    plan: dict

# ---------------------------------------------------------
# Local development OAuth state
#
# This is intentionally in-memory for the current local MVP.
# A server restart clears this data.
# ---------------------------------------------------------

qbo_client: Optional[QBOClient] = None
oauth_state: Optional[str] = None
company_info: Optional[dict] = None


def _get_qbo_client() -> QBOClient:
    """
    Create the QBO client lazily using application settings.
    """

    global qbo_client

    if not settings.qbo_client_id:
        raise HTTPException(
            status_code=500,
            detail="QBO_CLIENT_ID is not configured",
        )

    if not settings.qbo_client_secret:
        raise HTTPException(
            status_code=500,
            detail="QBO_CLIENT_SECRET is not configured",
        )

    if qbo_client is None:
        qbo_client = QBOClient(
            client_id=settings.qbo_client_id,
            client_secret=settings.qbo_client_secret,
            redirect_uri=settings.qbo_redirect_uri,
            environment=settings.qbo_environment,
        )

    return qbo_client


def _get_connected_qbo_client() -> QBOClient:
    """
    Return a connected QBO client.
    """

    client = _get_qbo_client()

    if client.qb_client is None or client.realm_id is None:
        raise HTTPException(
            status_code=400,
            detail="QuickBooks is not connected",
        )

    return client


@router.get("/connect")
async def connect_qbo():
    """
    Start the QuickBooks Online OAuth flow.
    """

    global oauth_state

    client = _get_qbo_client()

    oauth_state = secrets.token_urlsafe(32)

    auth_url = client.get_auth_url(
        state=oauth_state,
    )

    return RedirectResponse(
        url=auth_url,
    )


@router.get("/callback")
async def qbo_callback(
    code: str,
    state: str,
    realmId: str,
):
    """
    Handle the callback from Intuit.
    """

    global oauth_state
    global company_info

    if oauth_state is None or state != oauth_state:
        raise HTTPException(
            status_code=400,
            detail="Invalid OAuth state",
        )

    client = _get_qbo_client()

    client.handle_callback(
        auth_code=code,
        realm_id=realmId,
    )

    company_info = client.get_company_info()

    oauth_state = None

    return {
        "status": "connected",
        "company_name": company_info.get("company_name"),
        "company_id": company_info.get("company_id"),
        "environment": settings.qbo_environment,
    }


@router.get(
    "/status",
    response_model=QBOConnectionStatus,
)
async def qbo_status():
    """
    Check the current local QBO connection.
    """

    if (
        qbo_client is None
        or qbo_client.qb_client is None
        or qbo_client.realm_id is None
    ):
        return QBOConnectionStatus(
            connected=False,
        )

    expires_at = None

    if qbo_client.token_expires_at is not None:
        expires_at = qbo_client.token_expires_at.isoformat()

    return QBOConnectionStatus(
        connected=True,
        company_name=(
            company_info.get("company_name")
            if company_info
            else None
        ),
        company_id=qbo_client.realm_id,
        expires_at=expires_at,
    )


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
    # while documents are still stored in-memory.
    from app.api.documents import documents_db

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    doc = documents_db[document_id]

    if (
        doc.get("document_type")
        != DocumentType.INVOICE
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO invoice options are available "
                "only for invoices"
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
                "Invoice must be approved before "
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


@router.post("/disconnect")
async def disconnect_qbo():
    """
    Clear the local development QBO session.

    This does not revoke the token at Intuit.
    """

    global qbo_client
    global oauth_state
    global company_info

    qbo_client = None
    oauth_state = None
    company_info = None

    return {
        "status": "disconnected",
    }

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

    from app.api.documents import documents_db

    if document_id not in documents_db:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    doc = documents_db[document_id]

    if (
        doc.get("document_type")
        != DocumentType.INVOICE
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "QBO invoice planning is available "
                "only for invoices"
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
                "Invoice must be approved before "
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
        document_id=document_id,
        create_as=request.create_as,
        vendor=selected_vendor,
        account=selected_account,
        plan=plan,
    )