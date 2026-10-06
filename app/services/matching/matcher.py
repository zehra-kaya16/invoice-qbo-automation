from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Optional

MATCH_THRESHOLD = 70


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def _to_date(value) -> date:
    if isinstance(value, date):
        return value

    return date.fromisoformat(str(value))


def _normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    return " ".join(
        value.strip().casefold().split()
    )


def _fuzzy_similarity(
    left: Optional[str],
    right: Optional[str],
) -> float:
    left_normalized = _normalize_text(left)
    right_normalized = _normalize_text(right)

    if not left_normalized or not right_normalized:
        return 0.0

    return SequenceMatcher(
        None,
        left_normalized,
        right_normalized,
    ).ratio()


class BankFeedMatcher:
    """
    Match extracted bank-statement transactions
    against normalized QBO transaction candidates.
    """

    def score_candidate(
        self,
        extracted_txn,
        qbo_txn: dict,
    ) -> Optional[float]:

        score = 0.0

        extracted_amount = _to_decimal(
            extracted_txn.amount
        )

        qbo_amount = _to_decimal(
            qbo_txn["amount"]
        )

        # ----------------------------------
        # 1. Amount match — REQUIRED
        # ----------------------------------

        if (
            abs(
                extracted_amount
                - qbo_amount
            )
            < Decimal("0.01")
        ):
            score += 50
        else:
            return None

        # ----------------------------------
        # 2. Date match
        # ----------------------------------

        extracted_date = _to_date(
            extracted_txn.date
        )

        qbo_date = _to_date(
            qbo_txn["date"]
        )

        date_diff = abs(
            (
                extracted_date
                - qbo_date
            ).days
        )

        if date_diff == 0:
            score += 30

        elif date_diff <= 3:
            score += 20

        elif date_diff <= 7:
            score += 10

        # ----------------------------------
        # 3. Check number
        # ----------------------------------

        extracted_check = (
            extracted_txn.check_number
        )

        qbo_check = qbo_txn.get(
            "check_number"
        )

        if extracted_check and qbo_check:
            if (
                str(extracted_check).strip()
                == str(qbo_check).strip()
            ):
                score += 40

        # ----------------------------------
        # 4. Vendor / description similarity
        # ----------------------------------

        extracted_vendor = getattr(
            extracted_txn,
            "vendor_suggestion",
            None,
        )

        if extracted_vendor:
            qbo_text = (
                qbo_txn.get("vendor")
                or qbo_txn.get("description")
                or ""
            )

            similarity = _fuzzy_similarity(
                extracted_vendor,
                qbo_text,
            )

            score += similarity * 20

        return round(score, 2)

    def match_transaction(
        self,
        extracted_txn,
        qbo_transactions: list[dict],
    ) -> dict:

        candidates = []

        for qbo_txn in qbo_transactions:
            score = self.score_candidate(
                extracted_txn,
                qbo_txn,
            )

            if score is None:
                continue

            candidates.append({
                "score": score,
                "transaction": qbo_txn,
            })

        candidates.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        best_match = None

        if (
            candidates
            and candidates[0]["score"]
            >= MATCH_THRESHOLD
        ):
            best_match = candidates[0]

        return {
            "matched": best_match is not None,
            "best_match": best_match,
            "candidates": candidates,
        }

    def match_statement(
        self,
        transactions,
        qbo_transactions: list[dict],
    ) -> list[dict]:

        results = []

        for index, transaction in enumerate(
            transactions
        ):
            result = self.match_transaction(
                transaction,
                qbo_transactions,
            )

            if hasattr(
                transaction,
                "model_dump",
            ):
                extracted_data = (
                    transaction.model_dump(
                        mode="json"
                    )
                )
            else:
                extracted_data = dict(
                    transaction
                )

            results.append({
                "transaction_index": index,
                "extracted_transaction": (
                    extracted_data
                ),
                **result,
            })

        return results

    def match_check_image_to_statement(
        self,
        check_image,
        transactions,
    ) -> dict:
        """
        Match one extracted check image to the
        corresponding debit transaction in the
        extracted bank statement.
        """

        candidates = []

        if check_image.amount is None:
            return {
                "matched": False,
                "best_match": None,
                "candidates": [],
            }

        check_amount = abs(
            _to_decimal(
                check_image.amount
            )
        )

        for index, transaction in enumerate(
            transactions
        ):
            transaction_amount = _to_decimal(
                transaction.amount
            )

            # A check must correspond to money
            # leaving the bank account.
            if transaction_amount >= 0:
                continue

            # ----------------------------------
            # 1. Amount match — REQUIRED
            # ----------------------------------

            if (
                abs(
                    abs(transaction_amount)
                    - check_amount
                )
                < Decimal("0.01")
            ):
                score = 50.0
            else:
                continue

            # ----------------------------------
            # 2. Date match
            # ----------------------------------

            if (
                check_image.date is not None
                and transaction.date is not None
            ):
                check_date = _to_date(
                    check_image.date
                )

                transaction_date = _to_date(
                    transaction.date
                )

                date_diff = abs(
                    (
                        check_date
                        - transaction_date
                    ).days
                )

                if date_diff == 0:
                    score += 30

                elif date_diff <= 3:
                    score += 20

                elif date_diff <= 7:
                    score += 10

            # ----------------------------------
            # 3. Check number
            # ----------------------------------

            if (
                check_image.check_number
                and transaction.check_number
            ):
                image_check_number = str(
                    check_image.check_number
                ).strip()

                transaction_check_number = str(
                    transaction.check_number
                ).strip()

                if (
                    image_check_number
                    == transaction_check_number
                ):
                    score += 40

            # ----------------------------------
            # 4. Payee / description similarity
            # ----------------------------------

            if check_image.payee:
                statement_text = (
                    getattr(
                        transaction,
                        "vendor_suggestion",
                        None,
                    )
                    or getattr(
                        transaction,
                        "description",
                        "",
                    )
                    or ""
                )

                similarity = _fuzzy_similarity(
                    check_image.payee,
                    statement_text,
                )

                score += similarity * 20

            candidates.append({
                "statement_transaction_index": (
                    index
                ),
                "score": round(
                    score,
                    2,
                ),
            })

        candidates.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        best_match = None

        if (
            candidates
            and candidates[0]["score"]
            >= MATCH_THRESHOLD
        ):
            best_match = candidates[0]

        return {
            "matched": best_match is not None,
            "best_match": best_match,
            "candidates": candidates,
        }