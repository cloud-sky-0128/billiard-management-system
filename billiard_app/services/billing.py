from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

from ..config import (
    DISCOUNT_MODE_ALL,
    DISCOUNT_PRICING_PACKAGE_HOURLY,
    DISCOUNT_SCOPE_ALL,
    DISCOUNT_SCOPE_TABLE_AND_DRINK,
    DISCOUNT_SCOPE_TABLE_ONLY,
)
from ..db import get_db, get_setting_float, get_setting_int


def calculate_timed_charge(
    start_dt: datetime, end_dt: datetime, rate_per_minute: float
) -> tuple[int, float]:
    elapsed_seconds = max(0.0, (end_dt - start_dt).total_seconds())
    elapsed_minutes = max(1, math.ceil(elapsed_seconds / 60))
    return elapsed_minutes, round(elapsed_minutes * float(rate_per_minute), 2)


def active_session_by_table(table_no: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM sessions WHERE table_no = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (table_no,),
    ).fetchone()


def table_rate_for(table_no: int) -> sqlite3.Row | dict:
    row = get_db().execute(
        "SELECT * FROM table_rates WHERE table_no = ?", (table_no,)
    ).fetchone()
    if row:
        return row
    return {
        "table_no": table_no,
        "timed_rate_per_min": get_setting_float("timed_rate_group", 3.0),
        "package_rate_per_hour": get_setting_float("package_hour_rate", 150),
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
        if session["mode"] != "package" or discount_type["package_rate_per_hour"] is None:
            return False
        base_rate = float(session["rate_per_hour"] or 0)
        package_rate = float(discount_type["package_rate_per_hour"])
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


def session_food_total(session_id: int) -> float:
    row = get_db().execute(
        "SELECT COALESCE(SUM(subtotal), 0) AS total FROM orders WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return float(row["total"])


def session_drink_total(session_id: int) -> float:
    db = get_db()
    row = db.execute(
        """
        SELECT COALESCE(SUM(subtotal), 0) AS total
        FROM orders
        WHERE session_id = ?
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
    return float(row["total"])


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
    package_rate_per_hour: float | None = None,
) -> str:
    if pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY and package_rate_per_hour is not None:
        rate = float(package_rate_per_hour)
        formatted_rate = f"{rate:.0f}" if rate.is_integer() else f"{rate:.1f}"
        return f"包台每小時 {formatted_rate} 元"
    return discount_label(discount_percent)


def discount_scope_label(scope: str) -> str:
    if scope == DISCOUNT_SCOPE_TABLE_ONLY:
        return "只折球檯"
    if scope == DISCOUNT_SCOPE_TABLE_AND_DRINK:
        return "球檯+飲料"
    if scope == DISCOUNT_SCOPE_ALL:
        return "全部（球檯+餐點+飲料）"
    return "球檯+飲料"


def build_session_runtime(session: sqlite3.Row, now: datetime) -> dict:
    start_dt = datetime.fromisoformat(session["start_time"])
    elapsed_seconds = max(0, int((now - start_dt).total_seconds()))
    elapsed_minutes, timed_table_fee = calculate_timed_charge(
        start_dt, now, float(session["rate_per_min"] or 0)
    )

    if session["mode"] == "timed":
        table_fee = timed_table_fee
        timer_kind = "timed"
        timer_seconds = elapsed_seconds
        timer_text = format_hms(elapsed_seconds)
        mode_label = "計時"
    else:
        table_fee = round(int(session["package_hours"]) * float(session["rate_per_hour"]), 2)
        end_dt = start_dt + timedelta(hours=int(session["package_hours"]))
        remain_seconds = int((end_dt - now).total_seconds())
        timer_kind = "package"
        timer_seconds = remain_seconds
        timer_text = format_hms(remain_seconds)
        mode_label = "包台"

    food_total = session_food_total(session["id"])
    gross_total = round(table_fee + food_total, 2)
    return {
        "session": session,
        "start_dt": start_dt,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_minutes": elapsed_minutes,
        "timer_kind": timer_kind,
        "timer_seconds": timer_seconds,
        "timer_text": timer_text,
        "mode_label": mode_label,
        "table_fee": table_fee,
        "food_total": food_total,
        "gross_total": gross_total,
        "orders": session_orders(session["id"]),
    }


def table_count_value() -> int:
    return get_setting_int("table_count", 10)
