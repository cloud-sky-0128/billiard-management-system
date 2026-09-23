from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENTS_PER_UNIT = Decimal("100")
MAX_MONEY_UNITS = 2_147_483_647
MAX_MONEY_CENTS = MAX_MONEY_UNITS * 100

class MoneyLimitError(ValueError):
    pass


def validate_cents(value: int) -> int:
    if abs(value) > MAX_MONEY_CENTS:
        raise MoneyLimitError("金額不可超過 2,147,483,647 元。")
    return value


def to_cents(value: object) -> int:
    """Convert a user-facing currency amount to integer cents."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金額格式錯誤。") from exc
    if not amount.is_finite():
        raise ValueError("金額必須是有限數字。")
    if amount.copy_abs() > MAX_MONEY_UNITS:
        raise ValueError("金額不可超過 2,147,483,647 元。")
    try:
        return validate_cents(int((amount * CENTS_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
    except InvalidOperation as exc:
        raise ValueError("金額格式錯誤。") from exc


def to_whole_cents(value: object) -> int:
    """Validate cash amounts as whole currency units without rounding input."""
    cents = to_cents(value)
    if cents % 100 or Decimal(str(value)) != Decimal(cents) / CENTS_PER_UNIT:
        raise ValueError("金額請輸入整數元，不接受小數。")
    return cents


def cents_to_decimal(value: int | None) -> Decimal:
    cents = int(value or 0)
    return (Decimal(cents) / CENTS_PER_UNIT).quantize(Decimal("0.01"))


def format_cents(value: int | None) -> str:
    amount = cents_to_decimal(value)
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, ".2f").rstrip("0").rstrip(".")


def round_payment_cents(value: int) -> int:
    """Round a nonnegative amount in cents to the nearest whole currency unit."""
    cents = int(value)
    if cents < 0:
        raise ValueError("應收金額不可為負數。")
    return ((cents + 50) // 100) * 100


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
