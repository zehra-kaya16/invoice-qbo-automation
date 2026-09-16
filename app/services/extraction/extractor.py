import base64
import json
import io
import os

from pdf2image import convert_from_bytes
from openai import OpenAI

from app.schemas.documents import (
    BankStatementData,
    BankTransaction,
    DocumentType,
    LineItem,
    ReceiptData,
)

class DocumentExtractor:

    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)
        self.model = "gpt-4o"

    def pdf_to_page_images(self, pdf_data: bytes) -> list[bytes]:
        poppler_path = os.getenv("POPPLER_PATH")

        images = convert_from_bytes(
            pdf_data,
            dpi=200,
            poppler_path=poppler_path,
        )

        page_images: list[bytes] = []

        for image in images:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            page_images.append(buffer.getvalue())

        return page_images

    def _vision_request(self, image_data: bytes, prompt: str) -> str:
        image_base64 = base64.b64encode(image_data).decode("utf-8")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/png;base64,"
                                    + image_base64
                                )
                            },
                        },
                    ],
                }
            ],
            max_tokens=4096,
        )

        return response.choices[0].message.content or ""

    def _parse_json(self, response: str):
        cleaned = response.strip()

        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1]
            cleaned = cleaned.split("```", 1)[0]

        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1]
            cleaned = cleaned.split("```", 1)[0]

        try:
            return json.loads(cleaned.strip())
        except json.JSONDecodeError:
            return {}

    def classify_document(self, image_data: bytes) -> DocumentType:
        prompt = """
        Determine the type of this financial document.

        Allowed values:
        receipt
        invoice
        bank_statement
        check
        credit_card_statement
        unknown

        Return only one value from the list.
        """

        result = self._vision_request(image_data, prompt)
        result = result.strip().lower()

        type_map = {
            "receipt": DocumentType.RECEIPT,
            "invoice": DocumentType.INVOICE,
            "bill": DocumentType.BILL,
            "bank_statement": DocumentType.BANK_STATEMENT,
            "check": DocumentType.CHECK,
            "credit_card_statement":
                DocumentType.CREDIT_CARD_STATEMENT,
        }

        return type_map.get(
            result,
            DocumentType.UNKNOWN,
        )

    def extract_receipt(self, image_data: bytes) -> ReceiptData:
        prompt = """
        Extract the information visible in this receipt or invoice.

        Return only a valid JSON object with these fields:

        {
        "vendor": null,
        "vendor_address": null,
        "date": null,
        "total_amount": null,
        "subtotal": null,
        "tax_amount": null,
        "tip_amount": null,
        "payment_method": null,
        "line_items": [
            {
            "description": "",
            "quantity": 1,
            "unit_price": null,
            "amount": 0
            }
        ],
        "category_suggestion": null
        }

        Use YYYY-MM-DD for the date.
        If a value cannot be determined, use null.
        Do not add explanations outside the JSON.
        """

        response = self._vision_request(
            image_data,
            prompt,
        )

        data = self._parse_json(response)

        line_items = [
            LineItem(**item)
            for item in data.get("line_items", [])
        ]

        return ReceiptData(
            vendor=data.get("vendor"),
            vendor_address=data.get("vendor_address"),
            date=data.get("date"),
            total_amount=data.get("total_amount"),
            subtotal=data.get("subtotal"),
            tax_amount=data.get("tax_amount"),
            tip_amount=data.get("tip_amount"),
            payment_method=data.get("payment_method"),
            line_items=line_items,
            category_suggestion=data.get(
                "category_suggestion"
            ),
            confidence=0.85,
        )

    def extract_bank_statement(
        self,
        page_images: list[bytes],
    ) -> BankStatementData:

        if not page_images:
            raise ValueError(
                "Bank statement contains no page images."
            )

        header_prompt = """
    Extract the header information from this bank statement.

    Return only a valid JSON object with these fields:

    {
    "bank_name": null,
    "account_number_last4": null,
    "account_type": null,
    "statement_period_start": null,
    "statement_period_end": null,
    "beginning_balance": null,
    "ending_balance": null
    }

    Use YYYY-MM-DD for dates.
    For account_number_last4, return only the last four digits
    if they are visible.
    If a value cannot be determined, use null.
    Do not add explanations outside the JSON.
    """

        header_response = self._vision_request(
            page_images[0],
            header_prompt,
        )

        header_data = self._parse_json(
            header_response
        )

        all_transactions: list[BankTransaction] = []

        for page_image in page_images:
            transactions = (
                self._extract_transactions_from_page(
                    page_image
                )
            )

            all_transactions.extend(
                transactions
            )

        total_deposits = sum(
            transaction.amount
            for transaction in all_transactions
            if transaction.amount > 0
        )

        total_withdrawals = sum(
            abs(transaction.amount)
            for transaction in all_transactions
            if transaction.amount < 0
        )

        return BankStatementData(
            bank_name=header_data.get(
                "bank_name"
            ),
            account_number_last4=header_data.get(
                "account_number_last4"
            ),
            account_type=header_data.get(
                "account_type"
            ),
            statement_period_start=header_data.get(
                "statement_period_start"
            ),
            statement_period_end=header_data.get(
                "statement_period_end"
            ),
            beginning_balance=header_data.get(
                "beginning_balance"
            ),
            ending_balance=header_data.get(
                "ending_balance"
            ),
            total_deposits=total_deposits,
            total_withdrawals=total_withdrawals,
            transactions=all_transactions,
            check_images=[],
            confidence=0.80,
        )

    def _extract_transactions_from_page(
        self,
        page_image: bytes,
    ) -> list[BankTransaction]:

        prompt = """
    Extract every bank transaction visible on this
    bank statement page.

    Return only a valid JSON array.

    Each transaction must have this structure:

    [
    {
        "date": "YYYY-MM-DD",
        "description": "",
        "amount": 0,
        "transaction_type": "unknown",
        "check_number": null,
        "running_balance": null,
        "vendor_suggestion": null
    }
    ]

    Use a negative amount for money leaving the account
    and a positive amount for money entering the account.

    transaction_type can describe values such as:
    check, debit, credit, deposit, transfer, fee, or atm.

    If a check number is not visible, use null.
    If a running balance is not visible, use null.
    If no transactions are visible on the page, return [].

    Do not add explanations outside the JSON array.
    """

        response = self._vision_request(
            page_image,
            prompt,
        )

        transactions_data = self._parse_json(
            response
        )

        if not isinstance(
            transactions_data,
            list,
        ):
            return []

        transactions: list[BankTransaction] = []

        for transaction_data in transactions_data:
            transaction = BankTransaction(
                date=transaction_data.get(
                    "date"
                ),
                description=transaction_data.get(
                    "description",
                    "",
                ),
                amount=transaction_data.get(
                    "amount",
                    0,
                ),
                transaction_type=transaction_data.get(
                    "transaction_type",
                    "unknown",
                ),
                check_number=transaction_data.get(
                    "check_number"
                ),
                running_balance=transaction_data.get(
                    "running_balance"
                ),
                vendor_suggestion=transaction_data.get(
                    "vendor_suggestion"
                ),
            )

            transactions.append(
                transaction
            )

        return transactions