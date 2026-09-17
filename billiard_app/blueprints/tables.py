from __future__ import annotations

import sqlite3
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import (
    AVAILABLE_DISCOUNTS,
    DISCOUNT_PRICING_PACKAGE_HOURLY,
    DISCOUNT_PRICING_PERCENTAGE,
    DISCOUNT_SCOPE_ALL,
    DISCOUNT_SCOPE_TABLE_AND_DRINK,
    DISCOUNT_SCOPE_TABLE_ONLY,
    ICE_OPTIONS,
    SUGAR_OPTIONS,
)
from ..db import get_db, get_setting_float
from ..services.billing import (
    active_session_by_table,
    available_discount_types,
    build_session_runtime,
    calculate_timed_charge,
    discount_pricing_label,
    discount_scope_label,
    discount_type_applies_to_session,
    discount_type_is_available,
    session_drink_total,
    session_food_total,
    table_count_value,
    table_rate_for,
)

bp = Blueprint("tables", __name__)


def commit_new_session(
    db: sqlite3.Connection, sql: str, parameters: tuple, table_no: int
) -> bool:
    try:
        db.execute(sql, parameters)
        db.commit()
        return True
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"{table_no} 號桌目前已開台，未建立重複紀錄。", "error")
        return False

@bp.route("/")
def dashboard():
    table_count = table_count_value()
    now = datetime.now()
    tables: list[dict] = []

    for table_no in range(1, table_count + 1):
        active = active_session_by_table(table_no)
        if active:
            runtime = build_session_runtime(active, now)
            tables.append(
                {
                    "table_no": table_no,
                    "status": "開台",
                    "status_class": "open",
                    "mode_label": runtime["mode_label"],
                    "timer_kind": runtime["timer_kind"],
                    "timer_seconds": runtime["timer_seconds"],
                    "timer_text": runtime["timer_text"],
                }
            )
        else:
            tables.append(
                {
                    "table_no": table_no,
                    "status": "關台",
                    "status_class": "closed",
                    "mode_label": "-",
                    "timer_kind": "idle",
                    "timer_seconds": 0,
                    "timer_text": "--:--:--",
                }
            )

    return render_template("dashboard.html", tables=tables, table_count=table_count)


@bp.route("/tables/<int:table_no>")
def table_detail(table_no: int):
    table_count = table_count_value()
    if table_no < 1 or table_no > table_count:
        flash(f"桌號需介於 1 到 {table_count}。", "error")
        return redirect(url_for(".dashboard"))

    db = get_db()
    categories = db.execute(
        "SELECT id, name FROM categories WHERE is_active = 1 ORDER BY id"
    ).fetchall()
    category_name_map = {int(row["id"]): row["name"] for row in categories}

    selected_category_id = request.args.get("category_id", "").strip()
    category_id: int | None = None
    if categories:
        try:
            category_id = int(selected_category_id) if selected_category_id else int(categories[0]["id"])
        except ValueError:
            category_id = int(categories[0]["id"])

    selected_items: list[sqlite3.Row] = []
    selected_category_name = ""
    selected_category_is_drink = False
    if category_id is not None:
        selected_category_name = category_name_map.get(category_id, "")
        selected_category_is_drink = selected_category_name == "飲料"
        selected_items = db.execute(
            """
            SELECT id, name, price
            FROM menu_items
            WHERE category_id = ? AND is_active = 1
            ORDER BY id
            """,
            (category_id,),
        ).fetchall()

    active = active_session_by_table(table_no)
    runtime = build_session_runtime(active, datetime.now()) if active else None
    table_rate = table_rate_for(table_no)
    can_package = bool(table_rate["package_enabled"])

    return render_template(
        "table_detail.html",
        table_no=table_no,
        table_count=table_count,
        runtime=runtime,
        categories=categories,
        selected_category_id=category_id,
        selected_items=selected_items,
        can_package=can_package,
        table_rate=table_rate,
        discount_types=available_discount_types(session=active),
        discount_options=AVAILABLE_DISCOUNTS,
        discount_scope_options=[
            (DISCOUNT_SCOPE_TABLE_ONLY, "只折球檯"),
            (DISCOUNT_SCOPE_TABLE_AND_DRINK, "球檯+飲料"),
            (DISCOUNT_SCOPE_ALL, "全部"),
        ],
        selected_category_name=selected_category_name,
        selected_category_is_drink=selected_category_is_drink,
        sugar_options=SUGAR_OPTIONS,
        ice_options=ICE_OPTIONS,
    )


@bp.route("/sessions/start", methods=["POST"])
def start_session():
    db = get_db()
    try:
        table_no = int(request.form["table_no"])
        mode = request.form["mode"]
    except (KeyError, ValueError):
        flash("開台資料無效。", "error")
        return redirect(url_for(".dashboard"))
    if not 1 <= table_no <= table_count_value():
        flash("球檯號碼無效。", "error")
        return redirect(url_for(".dashboard"))

    if active_session_by_table(table_no):
        flash(f"{table_no} 號桌目前已開台。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    now = datetime.now().isoformat(timespec="seconds")

    table_rate = table_rate_for(table_no)

    if mode == "timed":
        rate = float(table_rate["timed_rate_per_min"])
        started = commit_new_session(
            db,
            """
            INSERT INTO sessions (table_no, mode, start_time, rate_per_min)
            VALUES (?, 'timed', ?, ?)
            """,
            (table_no, now, rate),
            table_no,
        )
        if not started:
            return redirect(url_for(".table_detail", table_no=table_no))
        flash(f"{table_no} 號桌開始計時，每分鐘 {rate:.1f} 元。", "success")
        return redirect(url_for(".table_detail", table_no=table_no))

    if mode == "package":
        if not table_rate["package_enabled"]:
            flash(f"{table_no} 號桌目前未啟用包台。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        try:
            package_hours = int(request.form.get("package_hours", "1"))
        except ValueError:
            flash("包台時數必須是整數。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        if package_hours < 1:
            flash("包台時數至少 1 小時。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        rate_per_hour = float(table_rate["package_rate_per_hour"])
        started = commit_new_session(
            db,
            """
            INSERT INTO sessions (table_no, mode, start_time, package_hours, rate_per_hour)
            VALUES (?, 'package', ?, ?, ?)
            """,
            (table_no, now, package_hours, rate_per_hour),
            table_no,
        )
        if not started:
            return redirect(url_for(".table_detail", table_no=table_no))
        flash(
            f"{table_no} 號桌已包台 {package_hours} 小時，每小時 {rate_per_hour:.0f} 元。",
            "success",
        )
        return redirect(url_for(".table_detail", table_no=table_no))

    flash("未知的開台模式。", "error")
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/sessions/extend/<int:session_id>", methods=["POST"])
def extend_package_session(session_id: int):
    db = get_db()
    session = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND status = 'active'",
        (session_id,),
    ).fetchone()
    if not session:
        flash("找不到進行中的球檯紀錄。", "error")
        return redirect(url_for(".dashboard"))

    table_no = int(session["table_no"])
    if session["mode"] != "package":
        flash("只有包台模式可以加時。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    try:
        extra_hours = int(request.form.get("extra_hours", "1"))
    except ValueError:
        flash("加時小時數格式錯誤。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    if extra_hours < 1 or extra_hours > 24:
        flash("每次加時需介於 1 到 24 小時。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    current_hours = int(session["package_hours"] or 0)
    new_hours = current_hours + extra_hours
    result = db.execute(
        "UPDATE sessions SET package_hours = ? WHERE id = ? AND status = 'active'",
        (new_hours, session_id),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未加入時數。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))
    db.commit()

    rate_per_hour = float(session["rate_per_hour"] or get_setting_float("package_hour_rate", 150))
    table_fee = round(new_hours * rate_per_hour, 2)
    flash(
        f"{table_no} 號桌已加時 {extra_hours} 小時，包台總時數 {new_hours} 小時，球檯費更新為 {table_fee:.0f} 元。",
        "success",
    )
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/orders/add", methods=["POST"])
def add_order():
    db = get_db()
    try:
        session_id = int(request.form["session_id"])
        item_id = int(request.form["item_id"])
        quantity = int(request.form.get("quantity", "1"))
    except (KeyError, ValueError):
        flash("點餐資料格式錯誤。", "error")
        return redirect(url_for(".dashboard"))
    if quantity < 1:
        flash("餐點數量至少為 1。", "error")
        return redirect(url_for(".dashboard"))
    category_id = request.form.get("category_id", "").strip()
    sugar_level = request.form.get("sugar_level", "").strip()
    ice_level = request.form.get("ice_level", "").strip()

    session = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND status = 'active'",
        (session_id,),
    ).fetchone()
    if not session:
        flash("該球桌未開台，無法點餐。", "error")
        return redirect(url_for(".dashboard"))

    item = db.execute(
        """
        SELECT m.*, c.name AS category_name
        FROM menu_items m
        JOIN categories c ON c.id = m.category_id
        WHERE m.id = ? AND m.is_active = 1 AND c.is_active = 1
        """,
        (item_id,),
    ).fetchone()
    if not item:
        flash("找不到品項。", "error")
        return redirect(url_for(".table_detail", table_no=session["table_no"]))

    is_drink = (item["category_name"] or "") == "飲料"
    if is_drink:
        if sugar_level not in SUGAR_OPTIONS:
            flash("飲料甜度選項錯誤。", "error")
            return redirect(url_for(".table_detail", table_no=session["table_no"], category_id=category_id or None))
        if ice_level not in ICE_OPTIONS:
            flash("飲料冰塊選項錯誤。", "error")
            return redirect(url_for(".table_detail", table_no=session["table_no"], category_id=category_id or None))
    else:
        sugar_level = ""
        ice_level = ""

    unit_price = float(item["price"])
    subtotal = round(unit_price * quantity, 2)
    result = db.execute(
        """
        INSERT INTO orders (
            session_id, item_id, item_name, item_category_name, sugar_level, ice_level,
            is_served, unit_price, quantity, subtotal
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        WHERE EXISTS (
            SELECT 1 FROM sessions WHERE id = ? AND status = 'active'
        )
        """,
        (
            session_id,
            item_id,
            item["name"],
            item["category_name"] or "",
            sugar_level,
            ice_level,
            0,
            unit_price,
            quantity,
            subtotal,
            session_id,
        ),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未新增餐點。", "error")
        return redirect(url_for(".table_detail", table_no=session["table_no"]))
    db.commit()

    flash(f"已新增：{item['name']} x {quantity}", "success")
    if category_id:
        return redirect(url_for(".table_detail", table_no=session["table_no"], category_id=category_id))
    return redirect(url_for(".table_detail", table_no=session["table_no"]))


@bp.route("/orders/<int:order_id>/served", methods=["POST"])
def toggle_order_served(order_id: int):
    db = get_db()
    row = db.execute(
        """
        SELECT o.id, o.session_id, s.table_no, s.status
        FROM orders o
        JOIN sessions s ON s.id = o.session_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        flash("找不到該筆訂單。", "error")
        return redirect(url_for(".dashboard"))

    if row["status"] != "active":
        flash("已關台訂單不可修改出餐狀態。", "error")
        return redirect(url_for(".table_detail", table_no=row["table_no"]))

    is_served = 1 if request.form.get("is_served") == "1" else 0
    result = db.execute(
        """UPDATE orders SET is_served = ?
           WHERE id = ? AND EXISTS (
               SELECT 1 FROM sessions
               WHERE id = orders.session_id AND status = 'active'
           )""",
        (is_served, order_id),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未修改訂單。", "error")
        return redirect(url_for(".table_detail", table_no=row["table_no"]))
    db.commit()

    category_id = request.form.get("category_id", "").strip()
    if category_id:
        return redirect(url_for(".table_detail", table_no=row["table_no"], category_id=category_id))
    return redirect(url_for(".table_detail", table_no=row["table_no"]))


@bp.route("/orders/<int:order_id>/delete", methods=["POST"])
def delete_order(order_id: int):
    db = get_db()
    row = db.execute(
        """
        SELECT o.id, o.session_id, s.table_no, s.status
        FROM orders o
        JOIN sessions s ON s.id = o.session_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        flash("找不到該筆訂單。", "error")
        return redirect(url_for(".dashboard"))

    if row["status"] != "active":
        flash("已關台訂單不可刪除。", "error")
        return redirect(url_for(".table_detail", table_no=row["table_no"]))

    result = db.execute(
        """DELETE FROM orders
           WHERE id = ? AND EXISTS (
               SELECT 1 FROM sessions
               WHERE id = orders.session_id AND status = 'active'
           )""",
        (order_id,),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未刪除訂單。", "error")
        return redirect(url_for(".table_detail", table_no=row["table_no"]))
    db.commit()
    flash("已刪除該筆訂單。", "success")

    category_id = request.form.get("category_id", "").strip()
    if category_id:
        return redirect(url_for(".table_detail", table_no=row["table_no"], category_id=category_id))
    return redirect(url_for(".table_detail", table_no=row["table_no"]))


@bp.route("/sessions/end/<int:session_id>", methods=["POST"])
def end_session(session_id: int):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    session = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND status = 'active'",
        (session_id,),
    ).fetchone()
    if not session:
        db.rollback()
        flash("找不到進行中的球檯紀錄。", "error")
        return redirect(url_for(".dashboard"))

    discount_type_id: int | None = None
    discount_name = "原價"
    discount_pricing_method = DISCOUNT_PRICING_PERCENTAGE
    discount_package_rate_per_hour = None
    selected_discount_id = request.form.get("discount_type_id", "").strip()
    discount_type = None
    if selected_discount_id:
        try:
            discount_type_id = int(selected_discount_id)
        except ValueError:
            discount_type_id = None
        if discount_type_id is not None:
            discount_type = db.execute(
                "SELECT * FROM discount_types WHERE id = ? AND is_active = 1",
                (discount_type_id,),
            ).fetchone()

    if (
        discount_type
        and discount_type_is_available(discount_type)
        and discount_type_applies_to_session(discount_type, session)
    ):
        discount_name = discount_type["name"]
        discount_pricing_method = discount_type["pricing_method"]
        discount_package_rate_per_hour = discount_type["package_rate_per_hour"]
        discount_percent = float(discount_type["discount_percent"])
        discount_scope = discount_type["discount_scope"]
    else:
        discount_type_id = None
        try:
            discount_percent = float(request.form.get("discount_percent", "100"))
        except ValueError:
            discount_percent = 100.0
        if discount_percent not in [float(x) for x in AVAILABLE_DISCOUNTS]:
            discount_percent = 100.0
        discount_scope = request.form.get("discount_scope", DISCOUNT_SCOPE_TABLE_AND_DRINK)
        if discount_scope not in (
            DISCOUNT_SCOPE_TABLE_ONLY,
            DISCOUNT_SCOPE_TABLE_AND_DRINK,
            DISCOUNT_SCOPE_ALL,
        ):
            discount_scope = DISCOUNT_SCOPE_TABLE_AND_DRINK
        if discount_percent < 100:
            discount_name = "自訂折扣"

    start_dt = datetime.fromisoformat(session["start_time"])
    end_dt = datetime.now()
    if session["mode"] == "timed":
        _, table_fee = calculate_timed_charge(
            start_dt, end_dt, float(session["rate_per_min"])
        )
    else:
        table_fee = round(int(session["package_hours"]) * float(session["rate_per_hour"]), 2)

    food_fee = session_food_total(session_id)
    drink_fee = session_drink_total(session_id)
    gross_total = round(table_fee + food_fee, 2)

    if discount_pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY:
        package_fee = round(
            int(session["package_hours"]) * float(discount_package_rate_per_hour), 2
        )
        discount_amount = round(table_fee - package_fee, 2)
        final_total = round(package_fee + food_fee, 2)
        discount_percent = round(package_fee / table_fee * 100, 4) if table_fee else 100.0
        discount_scope = DISCOUNT_SCOPE_TABLE_ONLY
    else:
        if discount_scope == DISCOUNT_SCOPE_TABLE_ONLY:
            discount_base = table_fee
        elif discount_scope == DISCOUNT_SCOPE_TABLE_AND_DRINK:
            discount_base = table_fee + drink_fee
        else:
            discount_base = gross_total
        discount_amount = round(discount_base * (100 - discount_percent) / 100, 2)
        final_total = round(gross_total - discount_amount, 2)

    result = db.execute(
        """
        UPDATE sessions
        SET end_time = ?, status = 'closed', table_fee = ?, food_fee = ?,
            discount_type_id = ?, discount_name = ?, discount_pricing_method = ?,
            discount_package_rate_per_hour = ?, discount_percent = ?,
            discount_scope = ?, discount_amount = ?, final_total = ?
        WHERE id = ? AND status = 'active'
        """,
        (
            end_dt.isoformat(timespec="seconds"),
            table_fee,
            food_fee,
            discount_type_id,
            discount_name,
            discount_pricing_method,
            discount_package_rate_per_hour,
            discount_percent,
            discount_scope,
            discount_amount,
            final_total,
            session_id,
        ),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已由其他操作完成結帳，未重複寫入。", "error")
        return redirect(url_for(".table_detail", table_no=session["table_no"]))
    db.commit()

    flash(
        (
            f"{session['table_no']} 號桌已結帳。"
            f"球檯費 {table_fee:.0f} 元、飲料+餐點 {food_fee:.0f} 元、"
            f"優惠 {discount_name}：{discount_pricing_label(discount_pricing_method, discount_percent, discount_package_rate_per_hour)}"
            f"（{discount_scope_label(discount_scope)}）"
            f"共折 {discount_amount:.0f} 元，應收 {final_total:.0f} 元。"
        ),
        "success",
    )
    return redirect(url_for(".table_detail", table_no=session["table_no"]))
