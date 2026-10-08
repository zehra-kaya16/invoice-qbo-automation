import base64
import io
import json
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from app.schemas.documents import (
    BankStatementData,
    BankTransaction,
    CheckData,
    CheckImage,
    DocumentType,
    LineItem,
    ReceiptData,
)


class DocumentExtractor:

    def __init__(
        self,
        api_key: str,
        provider: str = "openai",
    ):
        self.api_key = api_key
        self.provider = provider

        if provider == "openai":
            from openai import OpenAI

            self.client = OpenAI(
                api_key=api_key
            )

            self.model = "gpt-4o"

        elif provider == "anthropic":
            from anthropic import Anthropic

            self.client = Anthropic(
                api_key=api_key
            )

            self.model = (
                "claude-3-5-sonnet-20241022"
            )

        else:
            raise ValueError(
                f"Unknown provider: {provider}"
            )

    def pdf_to_page_images(self, pdf_data: bytes) -> list[bytes]:
        pdf_document = pdfium.PdfDocument(pdf_data)
        page_images: list[bytes] = []

        try:
            scale = 200 / 72

            for page_index in range(len(pdf_document)):
                page = pdf_document[page_index]

                try:
                    bitmap = page.render(scale=scale)

                    try:
                        image = bitmap.to_pil()
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG")
                        page_images.append(buffer.getvalue())
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        finally:
            pdf_document.close()

        return page_images

    def _vision_request(self, image_data: bytes, prompt: str) -> str:
        return self._vision_request_pages([image_data], prompt)

    def _vision_request_pages(self, page_images: list[bytes], prompt: str) -> str:
        if not page_images or any(
            not isinstance(page, bytes) or not page for page in page_images
        ):
            raise ValueError("At least one non-empty page image is required")

        encoded = [base64.b64encode(page).decode("utf-8") for page in page_images]
        if self.provider == "openai":
            content = [{"type": "text", "text": prompt}]
            for index, page in enumerate(encoded, start=1):
                content.extend([
                    {"type": "text", "text": f"Page {index} of {len(encoded)}"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + page,
                    }},
                ])
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=4096,
            )
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise ValueError("Extraction response was truncated; document was not extracted")
            return choice.message.content or ""

        if self.provider == "anthropic":
            content = [{"type": "text", "text": prompt}]
            for index, page in enumerate(encoded, start=1):
                content.extend([
                    {"type": "text", "text": f"Page {index} of {len(encoded)}"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": page,
                    }},
                ])
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": content}],
            )
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise ValueError("Extraction response was truncated; document was not extracted")
            return response.content[0].text

        raise ValueError(f"Unknown provider: {self.provider}")

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

    def extract_receipt(self, image_data: bytes | list[bytes]) -> ReceiptData:
        prompt = """
        Extract ONE receipt or invoice from all supplied pages in their order.
        These are pages of the same document, not separate transactions.
        Return one document-level result and include line items from every page.
        Use the final document grand total. Do not sum repeated totals,
        page subtotals, carried-forward balances, or repeated header/footer data.
        Do not duplicate a line item repeated only as a carry-forward summary.
        If the pages contain different receipts/invoices, return
        {"error": "Multiple independent documents must be uploaded separately"}.
        Do not invent missing fields.

        Return only a valid JSON object with these fields:

        {
        "vendor": null,
        "vendor_address": null,
        "date": null,
        "invoice_number": "Invoice number if this is an invoice, otherwise null",
        "due_date": "Invoice due date in YYYY-MM-DD format if visible, otherwise null",
        "total_amount": null,
        "subtotal": null,
        "tax_amount": null,
        "tip_amount": null,
        "currency": null,
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

        Return currency as a three-letter ISO 4217 currency code,
        such as USD, EUR, GBP, or TRY.
        Determine the currency from an explicit currency code,
        currency symbol, vendor address, and document context.
        If the currency cannot be determined reliably, use null.

        Return monetary values as JSON numbers without currency
        symbols or thousands separators.
        For example, return 350.00 instead of "$350.00".

        If a value cannot be determined, use null.
        Do not add explanations outside the JSON.
        """

        if isinstance(image_data, bytes):
            response = self._vision_request(image_data, prompt)
        else:
            response = self._vision_request_pages(image_data, prompt)

        data = self._parse_json(response)
        if not isinstance(data, dict) or not data:
            raise ValueError("Receipt/invoice extraction did not return a valid JSON object")
        if data.get("error"):
            raise ValueError(str(data["error"]))

        line_items = [
            LineItem(**item)
            for item in data.get("line_items", [])
        ]

        return ReceiptData(
            vendor=data.get("vendor"),
            vendor_address=data.get("vendor_address"),
            date=data.get("date"),
            invoice_number=data.get("invoice_number"),
            due_date=data.get("due_date"),
            total_amount=data.get("total_amount"),
            subtotal=data.get("subtotal"),
            tax_amount=data.get("tax_amount"),
            tip_amount=data.get("tip_amount"),
            currency=data.get("currency"),
            payment_method=data.get("payment_method"),
            line_items=line_items,
            category_suggestion=data.get(
                "category_suggestion"
            ),
            confidence=0.85,
        )

    def extract_check(
        self,
        image_data: bytes,
    ) -> CheckData:
        prompt = """
        Extract the information visible in this check image.

        Return only a valid JSON object with these fields:

        {
            "check_number": null,
            "payee": null,
            "amount": null,
            "date": null,
            "memo": null,
            "bank_name": null,
            "routing_number": null,
            "account_number_last4": null
        }

        Use YYYY-MM-DD for the date.

        Return monetary values as JSON numbers without
        currency symbols or thousands separators.

        For account_number_last4, return only the last
        four digits if visible.

        If a value cannot be determined reliably,
        use null.

        Do not add explanations outside the JSON.
        """

        response = self._vision_request(
            image_data,
            prompt,
        )

        data = self._parse_json(
            response
        )

        return CheckData(
            check_number=data.get(
                "check_number"
            ),
            payee=data.get(
                "payee"
            ),
            amount=data.get(
                "amount"
            ),
            date=data.get(
                "date"
            ),
            memo=data.get(
                "memo"
            ),
            bank_name=data.get(
                "bank_name"
            ),
            routing_number=data.get(
                "routing_number"
            ),
            account_number_last4=data.get(
                "account_number_last4"
            ),
            confidence=0.85,
        )

    def _detect_check_regions(
        self,
        page_image: bytes,
    ) -> list[dict]:

        prompt = """
    Inspect this bank statement page.

    Find every embedded image of an actual bank check.

    Do NOT mark:
    - transaction table rows
    - logos
    - advertisements
    - signatures that are not part of a full check
    - ordinary text blocks

    For every check image, return its bounding box.
    The bounding box must contain the ENTIRE physical check.

    Do not crop only the text or transaction area.

    The box must include, when present:
    - bank/check header
    - check number
    - date
    - PAY TO THE ORDER OF field
    - payee
    - numeric amount box
    - written amount
    - memo
    - signature area
    - routing/account/check-number line

    Include a small margin around the complete check.
    It is better to include extra whitespace than to cut off
    any part of the check.

    Coordinates must be normalized integers from 0 to 1000.

    Return only a valid JSON array in this format:

    [
    {
        "x1": 100,
        "y1": 200,
        "x2": 900,
        "y2": 600
    }
    ]

    x1 = left
    y1 = top
    x2 = right
    y2 = bottom

    0,0 is the top-left corner.
    1000,1000 is the bottom-right corner.

    If there is no check image on this page, return [].

    Do not include explanations.
    """

        response = self._vision_request(
            page_image,
            prompt,
        )

        data = self._parse_json(
            response
        )

        if not isinstance(data, list):
            return []

        regions = []

        for item in data:
            if not isinstance(item, dict):
                continue

            try:
                x1 = int(item["x1"])
                y1 = int(item["y1"])
                x2 = int(item["x2"])
                y2 = int(item["y2"])
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            x1 = max(0, min(1000, x1))
            y1 = max(0, min(1000, y1))
            x2 = max(0, min(1000, x2))
            y2 = max(0, min(1000, y2))

            if x2 <= x1 or y2 <= y1:
                continue

            regions.append({
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            })

        return regions

    def _save_check_image(
        self,
        image_data: bytes,
        document_id: str,
        page_number: int,
        check_number_on_page: int,
    ) -> str:

        directory = (
            Path("uploads")
            / "checks"
            / document_id
        )

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        filename = (
            f"page_{page_number:03d}"
            f"_check_{check_number_on_page:03d}"
            ".png"
        )

        path = directory / filename

        path.write_bytes(
            image_data
        )

        return path.as_posix()

    def snip_checks_from_statement(
        self,
        page_images: list[bytes],
        document_id: str,
    ) -> list[CheckImage]:

        check_images: list[CheckImage] = []

        for page_index, page_image in enumerate(
            page_images,
            start=1,
        ):
            regions = self._detect_check_regions(
                page_image
            )

            for check_index, region in enumerate(
                regions,
                start=1,
            ):
                cropped_bytes = self._crop_region(
                    page_image,
                    region,
                )

                image_path = (
                    self._save_check_image(
                        image_data=cropped_bytes,
                        document_id=document_id,
                        page_number=page_index,
                        check_number_on_page=check_index,
                    )
                )

                check_data = (
                    self._extract_check_fields(
                        cropped_bytes
                    )
                )

                check_image = CheckImage(
                    image_path=image_path,
                    check_number=(
                        check_data.get(
                            "check_number"
                        )
                    ),
                    payee=check_data.get(
                        "payee"
                    ),
                    amount=check_data.get(
                        "amount"
                    ),
                    date=check_data.get(
                        "date"
                    ),
                    memo=check_data.get(
                        "memo"
                    ),
                    matched_transaction_id=None,
                )

                check_images.append(
                    check_image
                )

        return check_images

    def _extract_check_fields(
        self,
        check_image: bytes,
    ) -> dict:

        prompt = """
        Extract the visible information from this bank check.

        Return only a valid JSON object:

        {
            "check_number": null,
            "pay
            e": null,
            "amount": null,
            "date": null,
            "memo": null
        }

        Rules:

        - date must use YYYY-MM-DD when determinable.
        - amount must be a JSON number.
        - Do not include a currency symbol in amount.
        - check_number must contain only the visible check number.
        - check_number must always be returned as a JSON string,
        never as a JSON number.
        - For example, return "2103", not 2103.
        - payee is the person or organization being paid.
        - memo is the memo line, if visible.
        - If a field cannot be read reliably, use null.
        - Do not guess missing values.
        - Do not add explanations outside the JSON.

        Field rules:

        check_number:
        Return only the check number printed on the check.

        payee:
        Return the beneficiary written after
        "PAY TO THE ORDER OF".
        Do not use the memo text as the payee.

        amount:
        Return the numeric check amount.
        Prefer the numeric amount box.
        Return it as a JSON number without a currency symbol
        or thousands separators.

        date:
        Return only the date printed or written in the check's
        date field.
        Use YYYY-MM-DD.

        memo:
        Look specifically for the line labeled "MEMO".
        Return the text written to the right of or after "MEMO".
        If "MEMO" is visible and text is present on that line,
        do not return null.

        Do not guess missing fields.
        If a field is truly not visible, return null.
        """

        response = self._vision_request(
            check_image,
            prompt,
        )

        data = self._parse_json(
            response
        )

        if not isinstance(data, dict):
            data = {}

        # Normalize check number to string.
        check_number = data.get(
            "check_number"
        )

        if check_number is not None:
            data["check_number"] = str(
                check_number
            ).strip()

        expected_fields = [
            "check_number",
            "payee",
            "amount",
            "date",
            "memo",
        ]

        missing_fields = [
            field
            for field in expected_fields
            if data.get(field) is None
        ]

        # If GPT missed a visible field, make one focused retry.
        if missing_fields:

            retry_prompt = f"""
            Re-inspect this bank check carefully.

            The previous extraction missed these fields:

            {missing_fields}

            Look ONLY for those missing fields.

            Important rules:

            - For memo, inspect the line explicitly labeled "MEMO".
            Return the text written on that line.
            - For date, inspect the check's DATE field.
            Return YYYY-MM-DD.
            - For payee, inspect "PAY TO THE ORDER OF".
            - For amount, prefer the numeric amount box.
            - For check_number, return a JSON string.

            Return only a valid JSON object containing
            these same field names:

            {{
                "check_number": null,
                "payee": null,
                "amount": null,
                "date": null,
                "memo": null
            }}

            Do not guess.
            """

            retry_response = self._vision_request(
                check_image,
                retry_prompt,
            )

            retry_data = self._parse_json(
                retry_response
            )

            if isinstance(retry_data, dict):

                for field in missing_fields:

                    retry_value = retry_data.get(
                        field
                    )

                    if retry_value is not None:
                        data[field] = retry_value

        # Normalize again because retry may return an integer.
        check_number = data.get(
            "check_number"
        )

        if check_number is not None:
            data["check_number"] = str(
                check_number
            ).strip()

        return data

    def _crop_region(
        self,
        image_data: bytes,
        region: dict,
    ) -> bytes:

        image = Image.open(
            io.BytesIO(image_data)
        ).convert("RGB")

        width, height = image.size

        x1 = float(region.get("x1", 0))
        y1 = float(region.get("y1", 0))
        x2 = float(region.get("x2", 1000))
        y2 = float(region.get("y2", 1000))

        left = int(
            (x1 / 1000.0) * width
        )

        top = int(
            (y1 / 1000.0) * height
        )

        right = int(
            (x2 / 1000.0) * width
        )

        bottom = int(
            (y2 / 1000.0) * height
        )

        if right <= left or bottom <= top:
            raise ValueError(
                f"Invalid check bounding box: {region}"
            )

        box_width = right - left
        box_height = bottom - top

        # Vision bounding boxes can be too tight.
        # Keep extra horizontal space and give the top
        # more padding because check number/date/header
        # are commonly located above the detected body.
        padding_x = int(
            box_width * 0.20
        )

        padding_top = int(
            box_height * 0.45
        )

        padding_bottom = int(
            box_height * 0.25
        )

        left = max(
            0,
            left - padding_x,
        )

        top = max(
            0,
            top - padding_top,
        )

        right = min(
            width,
            right + padding_x,
        )

        bottom = min(
            height,
            bottom + padding_bottom,
        )

        cropped = image.crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

        buffer = io.BytesIO()

        cropped.save(
            buffer,
            format="PNG",
        )

        return buffer.getvalue()

    def extract_bank_statement(
        self,
        page_images: list[bytes],
        document_id: str,
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
            Return monetary values as JSON numbers without currency
            symbols or thousands separators.
            For example, return 5000.00 instead of "$5,000.00".
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

        deduplicated_transactions: list[BankTransaction] = []
        seen_checks = set()

        for transaction in all_transactions:

            check_number = (
                transaction.check_number.strip()
                if transaction.check_number
                else None
            )

            if check_number:
                check_key = (
                    transaction.date,
                    transaction.amount,
                    check_number,
                )

                if check_key in seen_checks:
                    continue

                seen_checks.add(
                    check_key
                )

            deduplicated_transactions.append(
                transaction
            )

        all_transactions = (
            deduplicated_transactions
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

        check_images = (
            self.snip_checks_from_statement(
                page_images=page_images,
                document_id=document_id,
            )
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
            check_images=check_images,
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
    If a check number is visible, always return it as a JSON string,
    never as a JSON number.
    For example, return "1042", not 1042.
    If a running balance is not visible, use null.
    If no transactions are visible on the page, return [].

    Do not add explanations outside the JSON array.

    IMPORTANT:

    Extract transactions only from an actual bank statement
    transaction table or transaction list.

    Do NOT create a transaction from a reproduced check image,
    check detail image, check scan, or check attachment.

    If the page contains only a check image and no bank
    statement transaction table/list, return [].
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
            check_number = transaction_data.get(
                "check_number"
            )

            if check_number is not None:
                check_number = str(check_number).strip()

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
                check_number=check_number,
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