import unicodedata
from datetime import date as Date, datetime, timedelta
from decimal import Decimal
from quickbooks.objects.purchase import Purchase
from quickbooks.objects import (
    BillPayment,
    Deposit,
    Payment,
    Transfer,
)
from typing import Optional, List

from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from quickbooks import QuickBooks
from quickbooks.objects import CompanyInfo
from quickbooks.objects.vendor import Vendor
from quickbooks.objects.account import Account
from quickbooks.objects.bill import Bill

from quickbooks.objects.detailline import (
    AccountBasedExpenseLine,
    AccountBasedExpenseLineDetail,
)

from quickbooks.objects.attachable import (
    Attachable,
    AttachableRef,
)

def _normalize_vendor_name(value: str) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFKC",
        value,
    )

    normalized = " ".join(
        normalized.strip().split()
    )

    return normalized.casefold()

def _qbo_ref_value(ref):
    if ref is None:
        return None

    if isinstance(ref, dict):
        value = ref.get("value")
        return str(value) if value is not None else None

    value = getattr(ref, "value", None)

    if value is None:
        value = getattr(ref, "Value", None)

    return str(value) if value is not None else None


def _qbo_ref_name(ref):
    if ref is None:
        return None

    if isinstance(ref, dict):
        return ref.get("name")

    return (
        getattr(ref, "name", None)
        or getattr(ref, "Name", None)
    )

def _first_line_description(transaction):
    lines = (
        getattr(
            transaction,
            "Line",
            None,
        )
        or []
    )

    for line in lines:
        description = getattr(
            line,
            "Description",
            None,
        )

        if description:
            return str(description)

    return None

class QBOClient:
    """QuickBooks Online API client."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        environment: str = "sandbox",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.environment = environment

        self.auth_client = AuthClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=self.redirect_uri,
            environment=self.environment,
        )

        self.qb_client: Optional[QuickBooks] = None

        self.realm_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None

    def get_auth_url(self, state: Optional[str] = None) -> str:
        """Create the Intuit OAuth authorization URL."""

        scopes = [Scopes.ACCOUNTING]

        return self.auth_client.get_authorization_url(
            scopes,
            state_token=state,
        )

    def handle_callback(
        self,
        auth_code: str,
        realm_id: str,
    ) -> dict:
        """Exchange the OAuth authorization code for tokens."""

        self.auth_client.get_bearer_token(
            auth_code,
            realm_id=realm_id,
        )

        self.access_token = self.auth_client.access_token
        self.refresh_token = self.auth_client.refresh_token
        self.realm_id = realm_id
        self.token_expires_at = datetime.now() + timedelta(hours=1)

        self._init_qb_client()

        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "realm_id": self.realm_id,
            "expires_at": self.token_expires_at.isoformat(),
        }

    def set_tokens(
        self,
        access_token: str,
        refresh_token: str,
        realm_id: str,
        expires_at: Optional[datetime] = None,
    ) -> None:
        """Restore previously stored OAuth tokens."""

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.realm_id = realm_id

        self.token_expires_at = (
            expires_at or datetime.now() + timedelta(hours=1)
        )

        self.auth_client.access_token = access_token
        self.auth_client.refresh_token = refresh_token
        self.auth_client.realm_id = realm_id

        self._init_qb_client()

    def refresh_tokens(self) -> dict:
        """Refresh the QuickBooks access token."""

        if not self.refresh_token:
            raise ValueError("QuickBooks refresh token is not available.")

        self.auth_client.refresh(
            refresh_token=self.refresh_token,
        )

        self.access_token = self.auth_client.access_token
        self.refresh_token = self.auth_client.refresh_token
        self.token_expires_at = datetime.now() + timedelta(hours=1)

        self._init_qb_client()

        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.token_expires_at.isoformat(),
        }

    def _init_qb_client(self) -> None:
        """Initialize the python-quickbooks client."""

        if not self.realm_id:
            raise ValueError("QuickBooks realm_id is not available.")

        self.qb_client = QuickBooks(
            auth_client=self.auth_client,
            refresh_token=self.refresh_token,
            company_id=self.realm_id,
        )

    def get_company_info(self) -> dict:
        """Get connected QuickBooks company information."""

        if not self.qb_client:
            raise ValueError("QuickBooks client is not initialized.")

        if not self.realm_id:
            raise ValueError("QuickBooks realm_id is not available.")

        info = CompanyInfo.get(
            self.realm_id,
            qb=self.qb_client,
        )

        return {
            "company_name": info.CompanyName,
            "company_id": self.realm_id,
            "country": info.Country,
            "email": info.Email.Address if info.Email else None,
        }

    def get_vendors(self, active_only: bool = True) -> List[dict]:
        """Get list of vendors."""
        vendors = Vendor.all(qb=self.qb_client)

        result = []

        for v in vendors:
            if active_only and not v.Active:
                continue

            result.append({
                "id": v.Id,
                "name": v.DisplayName,
                "email": (
                    v.PrimaryEmailAddr.Address
                    if v.PrimaryEmailAddr
                    else None
                ),
                "active": v.Active,
            })

        return result

    def find_vendor_by_name(
        self,
        vendor_name: str,
    ):
        target_name = _normalize_vendor_name(
            vendor_name
        )

        if not target_name:
            return None

        vendors = Vendor.all(
            qb=self.qb_client
        )

        for vendor in vendors:
            display_name = getattr(
                vendor,
                "DisplayName",
                None,
            )

            if not display_name:
                continue

            if (
                _normalize_vendor_name(display_name)
                == target_name
            ):
                return {
                    "id": str(vendor.Id),
                    "name": display_name,
                }

        return None

    def create_vendor(
        self,
        display_name: str,
    ) -> dict:
        cleaned_name = " ".join(
            (display_name or "").strip().split()
        )

        if not cleaned_name:
            raise ValueError(
                "Vendor display name is required"
            )

        existing_vendor = (
            self.find_vendor_by_name(
                cleaned_name
            )
        )

        if existing_vendor is not None:
            return {
                "id": existing_vendor["id"],
                "name": existing_vendor["name"],
                "created": False,
            }

        vendor = Vendor()
        vendor.DisplayName = cleaned_name

        vendor.save(
            qb=self.qb_client
        )

        if not getattr(vendor, "Id", None):
            raise RuntimeError(
                "QBO vendor creation returned no vendor ID"
            )

        return {
            "id": str(vendor.Id),
            "name": vendor.DisplayName,
            "created": True,
        }

    def find_vendor(self, name: str):
        if not name:
            return None

        vendors = self.get_vendors()

        name_lower = name.strip().lower()

        for vendor in vendors:
            vendor_name = (vendor.get("name") or "").strip().lower()

            if not vendor_name:
                continue

            if name_lower in vendor_name or vendor_name in name_lower:
                return vendor

        return None

    def get_accounts(self, account_type: str = None) -> List[dict]:
        """
        Get chart of accounts.

        Args:
            account_type: Filter by type (Expense, Bank, etc.)
        """
        accounts = Account.all(qb=self.qb_client)

        result = []

        for a in accounts:
            if account_type and a.AccountType != account_type:
                continue

            result.append({
                "id": a.Id,
                "name": a.Name,
                "type": a.AccountType,
                "sub_type": a.AccountSubType,
                "fully_qualified_name": a.FullyQualifiedName,
            })

        return result

    def get_purchase_transactions(
        self,
        start_date,
        end_date,
    ) -> List[dict]:
        """
        Read QBO Purchase/Expense transactions
        for a statement period and normalize them
        for bank-statement matching.
        """

        if not self.qb_client:
            raise ValueError(
                "QuickBooks client is not initialized."
            )

        start = Date.fromisoformat(
            str(start_date)
        )

        end = Date.fromisoformat(
            str(end_date)
        )

        if start > end:
            raise ValueError(
                "start_date cannot be after end_date"
            )

        # Dates are parsed and serialized again
        # before being placed into the QBO query.
        where_clause = (
            f"TxnDate >= '{start.isoformat()}' "
            f"AND TxnDate <= '{end.isoformat()}'"
        )

        purchases = Purchase.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        result = []

        for purchase in purchases:
            total_amount = Decimal(
                str(
                    getattr(
                        purchase,
                        "TotalAmt",
                        0,
                    )
                )
            )

            # Our bank-statement extractor uses:
            # withdrawal = negative
            # deposit    = positive
            #
            # A normal QBO Purchase represents
            # money leaving the account.
            signed_amount = -total_amount

            entity_ref = getattr(
                purchase,
                "EntityRef",
                None,
            )

            vendor_name = None

            if isinstance(
                entity_ref,
                dict,
            ):
                vendor_name = (
                    entity_ref.get("name")
                )

            elif entity_ref is not None:
                vendor_name = (
                    getattr(
                        entity_ref,
                        "name",
                        None,
                    )
                    or getattr(
                        entity_ref,
                        "Name",
                        None,
                    )
                )

            line_description = None

            lines = (
                getattr(
                    purchase,
                    "Line",
                    None,
                )
                or []
            )

            for line in lines:
                description = getattr(
                    line,
                    "Description",
                    None,
                )

                if description:
                    line_description = (
                        description
                    )
                    break

            result.append({
                "id": str(purchase.Id),
                "type": "purchase",
                "date": str(
                    purchase.TxnDate
                ),
                "amount": str(
                    signed_amount
                ),
                "check_number": (
                    getattr(
                        purchase,
                        "DocNumber",
                        None,
                    )
                ),
                "vendor": vendor_name,
                "description": (
                    line_description
                    or vendor_name
                    or ""
                ),
            })

        return result

    def get_matching_transactions(
        self,
        start_date,
        end_date,
        bank_account_id: str,
    ) -> List[dict]:
        """
        Read QBO transactions affecting one selected
        bank account and normalize them for matching.
        """

        if not self.qb_client:
            raise ValueError(
                "QuickBooks client is not initialized."
            )

        start = Date.fromisoformat(
            str(start_date)
        )

        end = Date.fromisoformat(
            str(end_date)
        )

        if start > end:
            raise ValueError(
                "start_date cannot be after end_date"
            )

        selected_account_id = str(
            bank_account_id
        )

        where_clause = (
            f"TxnDate >= '{start.isoformat()}' "
            f"AND TxnDate <= '{end.isoformat()}'"
        )

        result = []

        # --------------------------------------
        # 1. PURCHASE / EXPENSE
        # --------------------------------------

        purchases = Purchase.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        for purchase in purchases:
            account_ref = getattr(
                purchase,
                "AccountRef",
                None,
            )

            account_id = _qbo_ref_value(
                account_ref
            )

            if account_id != selected_account_id:
                continue

            total_amount = Decimal(
                str(
                    getattr(
                        purchase,
                        "TotalAmt",
                        0,
                    )
                )
            )

            is_credit = bool(
                getattr(
                    purchase,
                    "Credit",
                    False,
                )
            )

            signed_amount = (
                total_amount
                if is_credit
                else -total_amount
            )

            entity_ref = getattr(
                purchase,
                "EntityRef",
                None,
            )

            vendor_name = _qbo_ref_name(
                entity_ref
            )

            description = (
                _first_line_description(
                    purchase
                )
                or vendor_name
                or getattr(
                    purchase,
                    "Memo",
                    None,
                )
                or ""
            )

            result.append({
                "id": str(purchase.Id),
                "type": "purchase",
                "date": str(
                    purchase.TxnDate
                ),
                "amount": str(
                    signed_amount
                ),
                "check_number": (
                    getattr(
                        purchase,
                        "DocNumber",
                        None,
                    )
                    or getattr(
                        purchase,
                        "PaymentRefNum",
                        None,
                    )
                ),
                "vendor": vendor_name,
                "description": description,
            })

        # --------------------------------------
        # 2. DEPOSIT
        # --------------------------------------

        deposits = Deposit.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        for deposit in deposits:
            account_ref = getattr(
                deposit,
                "DepositToAccountRef",
                None,
            )

            account_id = _qbo_ref_value(
                account_ref
            )

            if account_id != selected_account_id:
                continue

            total_amount = Decimal(
                str(
                    getattr(
                        deposit,
                        "TotalAmt",
                        0,
                    )
                )
            )

            result.append({
                "id": str(deposit.Id),
                "type": "deposit",
                "date": str(
                    deposit.TxnDate
                ),
                "amount": str(
                    total_amount
                ),
                "check_number": getattr(
                    deposit,
                    "DocNumber",
                    None,
                ),
                "vendor": None,
                "description": (
                    _first_line_description(
                        deposit
                    )
                    or getattr(
                        deposit,
                        "PrivateNote",
                        None,
                    )
                    or "Deposit"
                ),
            })

        # --------------------------------------
        # 3. TRANSFER
        # --------------------------------------

        transfers = Transfer.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        for transfer in transfers:
            from_account_id = (
                _qbo_ref_value(
                    getattr(
                        transfer,
                        "FromAccountRef",
                        None,
                    )
                )
            )

            to_account_id = (
                _qbo_ref_value(
                    getattr(
                        transfer,
                        "ToAccountRef",
                        None,
                    )
                )
            )

            total_amount = Decimal(
                str(
                    getattr(
                        transfer,
                        "Amount",
                        0,
                    )
                )
            )

            if (
                from_account_id
                == selected_account_id
            ):
                signed_amount = -total_amount

            elif (
                to_account_id
                == selected_account_id
            ):
                signed_amount = total_amount

            else:
                continue

            result.append({
                "id": str(transfer.Id),
                "type": "transfer",
                "date": str(
                    transfer.TxnDate
                ),
                "amount": str(
                    signed_amount
                ),
                "check_number": None,
                "vendor": None,
                "description": (
                    getattr(
                        transfer,
                        "PrivateNote",
                        None,
                    )
                    or "Transfer"
                ),
            })

        # --------------------------------------
        # 4. BILL PAYMENT
        # --------------------------------------

        bill_payments = BillPayment.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        for payment in bill_payments:
            check_payment = getattr(
                payment,
                "CheckPayment",
                None,
            )

            if check_payment is None:
                continue

            bank_ref = getattr(
                check_payment,
                "BankAccountRef",
                None,
            )

            payment_account_id = (
                _qbo_ref_value(
                    bank_ref
                )
            )

            if (
                payment_account_id
                != selected_account_id
            ):
                continue

            total_amount = Decimal(
                str(
                    getattr(
                        payment,
                        "TotalAmt",
                        0,
                    )
                )
            )

            vendor_ref = getattr(
                payment,
                "VendorRef",
                None,
            )

            vendor_name = _qbo_ref_name(
                vendor_ref
            )

            result.append({
                "id": str(payment.Id),
                "type": "bill_payment",
                "date": str(
                    payment.TxnDate
                ),
                "amount": str(
                    -total_amount
                ),
                "check_number": getattr(
                    payment,
                    "DocNumber",
                    None,
                ),
                "vendor": vendor_name,
                "description": (
                    vendor_name
                    or "Bill payment"
                ),
            })

        # --------------------------------------
        # 5. CUSTOMER PAYMENT
        # --------------------------------------

        customer_payments = Payment.where(
            where_clause,
            order_by="TxnDate",
            qb=self.qb_client,
        )

        for payment in customer_payments:
            deposit_ref = getattr(
                payment,
                "DepositToAccountRef",
                None,
            )

            account_id = _qbo_ref_value(
                deposit_ref
            )

            if account_id != selected_account_id:
                continue

            total_amount = Decimal(
                str(
                    getattr(
                        payment,
                        "TotalAmt",
                        0,
                    )
                )
            )

            customer_ref = getattr(
                payment,
                "CustomerRef",
                None,
            )

            customer_name = _qbo_ref_name(
                customer_ref
            )

            result.append({
                "id": str(payment.Id),
                "type": "payment",
                "date": str(
                    payment.TxnDate
                ),
                "amount": str(
                    total_amount
                ),
                "check_number": getattr(
                    payment,
                    "PaymentRefNum",
                    None,
                ),
                "vendor": customer_name,
                "description": (
                    customer_name
                    or "Customer payment"
                ),
            })

        result.sort(
            key=lambda item: (
                item["date"],
                item["type"],
                item["id"],
            )
        )

        return result

    def get_expense_accounts(self):
        return self.get_accounts(account_type="Expense")

    def get_bank_accounts(self):
        return self.get_accounts(account_type="Bank")

    def create_bill(
        self,
        vendor_id: str,
        account_id: str,
        amount: Decimal,
        date: str,
        due_date: str = None,
        description: str = None,
    ) -> dict:
        """
        Create a bill (accounts payable) in QBO.
        """

        bill = Bill()
        bill.TxnDate = date
        bill.DueDate = due_date or date

        # Vendor reference
        bill.VendorRef = {"value": vendor_id}

        # Line item
        line = AccountBasedExpenseLine()
        line.Amount = float(amount)
        line.Description = description
        line.DetailType = "AccountBasedExpenseLineDetail"

        line.AccountBasedExpenseLineDetail = (
            AccountBasedExpenseLineDetail()
        )
        line.AccountBasedExpenseLineDetail.AccountRef = {
            "value": account_id
        }

        bill.Line = [line]

        bill.save(qb=self.qb_client)

        return {
            "id": bill.Id,
            "type": "bill",
            "amount": amount,
            "vendor_id": vendor_id,
            "date": date,
        }

    def create_expense(
        self,
        vendor_id: str,
        account_id: str,
        amount: Decimal,
        date: str,
        description: str = None,
        bank_account_id: str = None,
    ) -> dict:
        """
        Create an expense (purchase) in QBO.

        Args:
            vendor_id: QBO Vendor ID
            account_id: Expense account ID for categorization
            amount: Transaction amount
            date: Transaction date (YYYY-MM-DD)
            description: Line item description
            bank_account_id: Bank account to pay from
        """
        from quickbooks.objects.detailline import (
            AccountBasedExpenseLine,
            AccountBasedExpenseLineDetail,
        )

        purchase = Purchase()
        purchase.PaymentType = "Cash"
        purchase.TxnDate = date

        # Vendor reference
        purchase.EntityRef = {
            "value": vendor_id,
            "type": "Vendor",
        }

        # Bank account (if specified)
        if bank_account_id:
            purchase.AccountRef = {
                "value": bank_account_id,
            }

        # Expense line
        line = AccountBasedExpenseLine()
        line.Amount = float(amount)
        line.Description = description
        line.DetailType = "AccountBasedExpenseLineDetail"

        line.AccountBasedExpenseLineDetail = AccountBasedExpenseLineDetail()
        line.AccountBasedExpenseLineDetail.AccountRef = {
            "value": account_id,
        }

        purchase.Line = [line]

        # Real QBO write
        purchase.save(qb=self.qb_client)

        return {
            "id": purchase.Id,
            "type": "expense",
            "amount": amount,
            "vendor_id": vendor_id,
            "date": date,
        }

    def upload_attachment(
        self,
        file_path: str,
        file_name: str,
        content_type: str,
        entity_type: str,
        entity_id: str,
    ) -> dict:
        """
        Upload a source document and attach it to a QBO entity.

        entity_type examples:
        - "Bill"
        - "Purchase"
        """

        attachable = Attachable()

        attachable.FileName = file_name
        attachable.ContentType = content_type

        attachable_ref = AttachableRef()
        attachable_ref.EntityRef = {
            "type": entity_type,
            "value": entity_id,
        }

        attachable.AttachableRef = [attachable_ref]

        # python-quickbooks compatible file upload
        attachable._FilePath = file_path

        attachable.save(qb=self.qb_client)

        return {
            "id": str(attachable.Id),
            "file_name": file_name,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
        }