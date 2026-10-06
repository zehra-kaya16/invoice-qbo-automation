from decimal import Decimal

from app.schemas.documents import ReceiptData

MONEY_TOLERANCE = Decimal("0.01")


def _difference_exceeds_tolerance(
    left: Decimal,
    right: Decimal,
) -> bool:
    return abs(left - right) > MONEY_TOLERANCE


def validate_invoice(data: ReceiptData) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    checks_performed: list[str] = []
    checks_skipped: list[str] = []

    # -------------------------------------------------
    # 1. Line-item kontrolü
    # quantity × unit_price ≈ amount
    # -------------------------------------------------
    for index, item in enumerate(data.line_items, start=1):

        if item.quantity is None or item.unit_price is None:
            warnings.append(
                f"Line item {index}: quantity or unit_price is missing."
            )
            checks_skipped.append(
                f"line_item_{index}_amount"
            )
            continue

        expected_amount = (
            Decimal(str(item.quantity))
            * item.unit_price
        )

        checks_performed.append(
            f"line_item_{index}_amount"
        )

        if _difference_exceeds_tolerance(
            expected_amount,
            item.amount,
        ):
            errors.append(
                (
                    f"Line item {index}: "
                    f"quantity × unit_price = {expected_amount}, "
                    f"but amount = {item.amount}."
                )
            )

    # -------------------------------------------------
    # 2. Line-item toplamı ≈ subtotal
    # -------------------------------------------------
    line_items_total = None

    if data.line_items:
        line_items_total = sum(
            (
                item.amount
                for item in data.line_items
            ),
            Decimal("0.00"),
        )

        if data.subtotal is not None:
            checks_performed.append(
                "line_items_total_vs_subtotal"
            )

            if _difference_exceeds_tolerance(
                line_items_total,
                data.subtotal,
            ):
                errors.append(
                    (
                        "Line items total does not match subtotal: "
                        f"{line_items_total} != {data.subtotal}."
                    )
                )

        else:
            warnings.append(
                "Subtotal is missing; "
                "line-items total cannot be compared with subtotal."
            )
            checks_skipped.append(
                "line_items_total_vs_subtotal"
            )

    else:
        warnings.append(
            "No line items were extracted."
        )
        checks_skipped.append(
            "line_items_total_vs_subtotal"
        )

    # -------------------------------------------------
    # 3. subtotal + tax + tip ≈ total_amount
    # -------------------------------------------------
    expected_total = None

    if (
        data.subtotal is not None
        and data.total_amount is not None
    ):
        tax_amount = (
            data.tax_amount
            if data.tax_amount is not None
            else Decimal("0.00")
        )

        tip_amount = (
            data.tip_amount
            if data.tip_amount is not None
            else Decimal("0.00")
        )

        expected_total = (
            data.subtotal
            + tax_amount
            + tip_amount
        )

        checks_performed.append(
            "subtotal_tax_tip_vs_total"
        )

        if _difference_exceeds_tolerance(
            expected_total,
            data.total_amount,
        ):
            errors.append(
                (
                    "Calculated total does not match total_amount: "
                    f"{expected_total} != {data.total_amount}."
                )
            )

    else:
        warnings.append(
            "Subtotal or total_amount is missing; "
            "final total validation was skipped."
        )
        checks_skipped.append(
            "subtotal_tax_tip_vs_total"
        )

    return {
        "is_valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checks_performed": checks_performed,
        "checks_skipped": checks_skipped,
        "line_items_total": line_items_total,
        "expected_total": expected_total,
    }