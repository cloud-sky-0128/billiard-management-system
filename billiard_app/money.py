from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENTS_PER_UNIT = Decimal("100")


def to_cents(value: object) -> int:
    """Convert a user-facing currency amount to integer cents."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金額格式錯誤。") from exc
    if not amount.is_finite():
        raise ValueError("金額必須是有限數字。")
    return int((amount * CENTS_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_decimal(value: int | None) -> Decimal:
    cents = int(value or 0)
    return (Decimal(cents) / CENTS_PER_UNIT).quantize(Decimal("0.01"))


def format_cents(value: int | None) -> str:
    amount = cents_to_decimal(value)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, ".2f").rstrip("0").rstrip(".")


def percentage_of_cents(amount_cents: int, percentage: object) -> int:
    try:
        percent = Decimal(str(percentage))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("百分比格式錯誤。") from exc
    if not percent.is_finite():
        raise ValueError("百分比必須是有限數字。")
    return int(
        (Decimal(int(amount_cents)) * percent / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
