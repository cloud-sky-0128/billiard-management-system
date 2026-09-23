from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from ..config import (
    DISCOUNT_MODE_ALL,
    DISCOUNT_PRICING_PACKAGE_HOURLY,
    DISCOUNT_SCOPE_ALL,
    DISCOUNT_SCOPE_TABLE_AND_DRINK,
    DISCOUNT_SCOPE_TABLE_ONLY,
)
from ..db import get_db, get_setting_cents, get_setting_int
from ..money import format_cents, percentage_of_cents, round_payment_cents


@dataclass(frozen=True)
class PaymentSummary:
    table_list_cents: int
    food_list_cents: int
    paid_cents: int

    @property
    def gross_list_cents(self) -> int:
        return self.table_list_cents + self.food_list_cents


def summarize_payments(rows: Iterable[sqlite3.Row]) -> PaymentSummary:
    """Keep list prices separate from the whole-yuan amounts actually received."""
    table_list = food_list = paid = 0
    for row in rows:
        table_list += int(row["table_fee_cents"] or 0)
        food_list += int(row["food_fee_cents"] or 0)
        paid += int(row["amount_cents"] or 0)
    return PaymentSummary(table_list, food_list, paid)


def calculate_timed_charge(
    start_dt: datetime, end_dt: datetime, rate_per_minute_cents: int
) -> tuple[int, int]:
    elapsed_seconds = max(0.0, (end_dt - start_dt).total_seconds())
    elapsed_minutes = max(1, math.ceil(elapsed_seconds / 60))
    return elapsed_minutes, elapsed_minutes * int(rate_per_minute_cents)


def active_session_by_table(table_no: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM sessions WHERE table_no = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (table_no,),
    ).fetchone()


def active_tab_by_table(table_no: int) -> sqlite3.Row | None:
    return get_db().execute(
        """SELECT * FROM customer_tabs
           WHERE table_no = ? AND status IN ('assigned', 'playing')
           LIMIT 1""",
        (table_no,),
    ).fetchone()


def tab_orders(customer_tab_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM orders WHERE customer_tab_id = ? ORDER BY id DESC",
        (customer_tab_id,),
    ).fetchall()


def tab_food_total(customer_tab_id: int) -> int:
    row = get_db().execute(
        "SELECT COALESCE(SUM(subtotal_cents), 0) AS total FROM orders WHERE customer_tab_id = ?",
        (customer_tab_id,),
    ).fetchone()
    return int(row["total"])


def table_rate_for(table_no: int) -> sqlite3.Row | dict:
    row = get_db().execute(
        "SELECT * FROM table_rates WHERE table_no = ?", (table_no,)
    ).fetchone()
    if row:
        return row
    return {
        "table_no": table_no,
        "timed_rate_per_min_cents": get_setting_cents("timed_rate_group", "3.0"),
        "package_rate_per_hour_cents": get_setting_cents("package_hour_rate", "150"),
        "package_enabled": 1,
    }


def discount_type_is_available(discount_type: sqlite3.Row, now: datetime | None = None) -> bool:
    start = (discount_type["start_time"] or "").strip()
    end = (discount_type["end_time"] or "").strip()
    if not start and not end:
        return True
    if not start or not end:
        return False
    current = (now or datetime.now()).strftime("%H:%M")
    if start < end:
        return start <= current < end
    return current >= start or current < end


def discount_type_applies_to_session(
    discount_type: sqlite3.Row, session: sqlite3.Row
) -> bool:
    applicable_mode = discount_type["applicable_mode"] or DISCOUNT_MODE_ALL
    if applicable_mode != DISCOUNT_MODE_ALL and applicable_mode != session["mode"]:
        return False
    if discount_type["pricing_method"] == DISCOUNT_PRICING_PACKAGE_HOURLY:
        if (
            session["mode"] != "package"
            or discount_type["package_rate_per_hour_cents"] is None
        ):
            return False
        base_rate = int(session["rate_per_hour_cents"] or 0)
        package_rate = int(discount_type["package_rate_per_hour_cents"])
        return 0 < package_rate < base_rate
    return True


def available_discount_types(
    session: sqlite3.Row | None = None, now: datetime | None = None
) -> list[sqlite3.Row]:
    rows = get_db().execute(
        """SELECT * FROM discount_types
           WHERE is_active = 1 ORDER BY name, id"""
    ).fetchall()
    return [
        row
        for row in rows
        if discount_type_is_available(row, now)
        and (session is None or discount_type_applies_to_session(row, session))
    ]


def session_orders(session_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY id DESC",
        (session_id,),
    ).fetchall()


def session_food_total(session_id: int) -> int:
    row = get_db().execute(
        """SELECT COALESCE(SUM(subtotal_cents), 0) AS total FROM orders
           WHERE customer_tab_id = (SELECT customer_tab_id FROM sessions WHERE id = ?)""",
        (session_id,),
    ).fetchone()
    return int(row["total"])


def session_drink_total(session_id: int) -> int:
    db = get_db()
    row = db.execute(
        """
        SELECT COALESCE(SUM(subtotal_cents), 0) AS total
        FROM orders
        WHERE customer_tab_id = (SELECT customer_tab_id FROM sessions WHERE id = ?)
          AND (
            item_category_name = '飲料'
            OR (
              item_category_name = ''
              AND item_id IN (
                SELECT m.id
                FROM menu_items m
                JOIN categories c ON c.id = m.category_id
                WHERE c.name = '飲料'
              )
            )
          )
        """,
        (session_id,),
    ).fetchone()
    return int(row["total"])


def unpaid_order_totals(customer_tab_id: int) -> tuple[int, int]:
    row = get_db().execute(
        """SELECT COALESCE(SUM(o.subtotal_cents), 0) AS food_total_cents,
                  COALESCE(SUM(CASE WHEN (
                      o.item_category_name = '飲料'
                      OR (
                          o.item_category_name = ''
                          AND o.item_id IN (
                              SELECT m.id FROM menu_items m
                              JOIN categories c ON c.id = m.category_id
                              WHERE c.name = '飲料'
                          )
                      )
                  ) THEN o.subtotal_cents ELSE 0 END), 0) AS drink_total_cents
           FROM orders o
           WHERE o.customer_tab_id = ? AND o.payment_id IS NULL""",
        (customer_tab_id,),
    ).fetchone()
    return int(row["food_total_cents"]), int(row["drink_total_cents"])


def format_hms(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    abs_seconds = abs(seconds)
    hh = abs_seconds // 3600
    mm = (abs_seconds % 3600) // 60
    ss = abs_seconds % 60
    return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}"


def discount_label(discount_percent: float) -> str:
    if abs(discount_percent - 100.0) < 1e-6:
        return "原價"
    if abs(discount_percent % 10) < 1e-6:
        return f"{int(discount_percent / 10)} 折"
    return f"{discount_percent / 10:.1f} 折"


def discount_pricing_label(
    pricing_method: str,
    discount_percent: float,
    package_rate_per_hour_cents: int | None = None,
) -> str:
    if (
        pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY
        and package_rate_per_hour_cents is not None
    ):
        return f"包台每小時 {format_cents(package_rate_per_hour_cents)} 元"
    return discount_label(discount_percent)


def discount_scope_label(scope: str) -> str:
    if scope == DISCOUNT_SCOPE_TABLE_ONLY:
        return "只折球檯"
    if scope == DISCOUNT_SCOPE_TABLE_AND_DRINK:
        return "球檯+飲料"
    if scope == DISCOUNT_SCOPE_ALL:
        return "全部（球檯+餐點+飲料）"
    return "球檯+飲料"


def calculate_food_discount(
    food_total_cents: int,
    drink_total_cents: int,
    pricing_method: str,
    discount_percent: float,
    discount_scope: str,
) -> int:
    """Calculate only the food/drink portion of a session discount."""
    if pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY:
        return 0
    if discount_scope == DISCOUNT_SCOPE_TABLE_AND_DRINK:
        discount_base_cents = drink_total_cents
    elif discount_scope == DISCOUNT_SCOPE_ALL:
        discount_base_cents = food_total_cents
    else:
        discount_base_cents = 0
    return percentage_of_cents(discount_base_cents, 100 - discount_percent)


def build_session_runtime(session: sqlite3.Row, now: datetime) -> dict:
    start_dt = datetime.fromisoformat(session["start_time"])
    elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
    elapsed_minutes, timed_table_fee_cents = calculate_timed_charge(
        start_dt, now, int(session["rate_per_min_cents"] or 0)
    )

    if bool(session["is_free_practice"]):
        table_fee_cents = 0
        timer_kind = "practice"
        timer_seconds = 0
        timer_text = "使用中"
        mode_label = "免費練習"
    elif session["mode"] == "timed":
        table_fee_cents = timed_table_fee_cents
        timer_kind = "timed"
        timer_seconds = elapsed_seconds
        timer_text = format_hms(elapsed_seconds)
        mode_label = "計時"
    else:
        table_fee_cents = int(session["package_hours"]) * int(
            session["rate_per_hour_cents"]
        )
        try:
            end_dt = start_dt + timedelta(hours=int(session["package_hours"]))
            remain_seconds = int((end_dt - now).total_seconds())
        except OverflowError:
            remain_seconds = 0
        timer_kind = "package"
        timer_seconds = remain_seconds
        timer_text = format_hms(remain_seconds)
        mode_label = "包台"
        if not 1 <= int(session['package_hours']) <= 16:
            mode_label = "包台（歷史時數異常，請管理員核對）"

    food_total_cents = session_food_total(session["id"])
    drink_total_cents = session_drink_total(session["id"])
    gross_total_cents = table_fee_cents + food_total_cents
    prepayment_count = 0
    table_due_cents = table_fee_cents
    unpaid_food_discount_cents = 0
    food_due_cents = food_total_cents
    paid_total_cents = 0
    if session["mode"] == "package":
        payment_rows = get_db().execute(
            """SELECT payment_type, table_fee_cents, food_fee_cents, amount_cents
               FROM payments WHERE session_id = ?
                 AND payment_type IN ('package_start', 'package_extension', 'package_food')""",
            (session["id"],),
        ).fetchall()
        prepayment_count = sum(
            row["payment_type"] in ("package_start", "package_extension")
            for row in payment_rows
        )
        payment_summary = summarize_payments(payment_rows)
        paid_table_gross_cents = payment_summary.table_list_cents
        paid_food_gross_cents = payment_summary.food_list_cents
        paid_total_cents = payment_summary.paid_cents
        remaining_table_gross_cents = max(0, table_fee_cents - paid_table_gross_cents)
        if session["discount_pricing_method"] == DISCOUNT_PRICING_PACKAGE_HOURLY:
            table_due_cents = (
                remaining_table_gross_cents
                // int(session["rate_per_hour_cents"])
                * int(session["discount_package_rate_per_hour_cents"])
            )
        else:
            table_due_cents = remaining_table_gross_cents - percentage_of_cents(
                remaining_table_gross_cents,
                100 - float(session["discount_percent"] or 100),
            )
        unpaid_food_cents, unpaid_drink_cents = unpaid_order_totals(
            int(session["customer_tab_id"])
        )
        unpaid_food_discount_cents = calculate_food_discount(
            unpaid_food_cents,
            unpaid_drink_cents,
            session["discount_pricing_method"],
            float(session["discount_percent"] or 100),
            session["discount_scope"],
        )
        food_due_cents = unpaid_food_cents - unpaid_food_discount_cents
        unpaid_drink_discount_cents = calculate_food_discount(
            unpaid_drink_cents,
            unpaid_drink_cents,
            session["discount_pricing_method"],
            float(session["discount_percent"] or 100),
            session["discount_scope"],
        )
        unpaid_drink_due_cents = round_payment_cents(
            unpaid_drink_cents - unpaid_drink_discount_cents
        )
    else:
        paid_food_gross_cents = 0
        unpaid_food_cents = food_total_cents
        unpaid_drink_cents = drink_total_cents
        unpaid_drink_discount_cents = 0
        unpaid_drink_due_cents = unpaid_drink_cents
    return {
        "session": session,
        "start_dt": start_dt,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_minutes": elapsed_minutes,
        "timer_kind": timer_kind,
        "timer_seconds": timer_seconds,
        "timer_text": timer_text,
        "mode_label": mode_label,
        "discount_label": discount_pricing_label(
            session["discount_pricing_method"],
            float(session["discount_percent"] or 100),
            session["discount_package_rate_per_hour_cents"],
        ),
        "table_fee_cents": table_fee_cents,
        "food_total_cents": food_total_cents,
        "unpaid_food_discount_cents": unpaid_food_discount_cents,
        "food_due_cents": food_due_cents,
        "paid_total_cents": paid_total_cents,
        "paid_food_gross_cents": paid_food_gross_cents,
        "unpaid_food_cents": unpaid_food_cents,
        "unpaid_drink_cents": unpaid_drink_cents,
        "unpaid_drink_discount_cents": unpaid_drink_discount_cents,
        "unpaid_drink_due_cents": unpaid_drink_due_cents,
        "prepayment_count": prepayment_count,
        "table_due_cents": table_due_cents,
        "total_due_cents": round_payment_cents(
            table_due_cents + food_due_cents
        ),
        "gross_total_cents": gross_total_cents,
        "practice_due_cents": round_payment_cents(food_total_cents),
        "orders": tab_orders(session["customer_tab_id"]),
    }


def table_count_value() -> int:
    return get_setting_int("table_count", 10)
