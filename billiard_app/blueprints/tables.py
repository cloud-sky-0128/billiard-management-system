from __future__ import annotations

import sqlite3
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..audit import record_audit
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
from ..db import get_db, table_label
from ..money import format_cents, percentage_of_cents, round_payment_cents, validate_cents
from ..operations import commit_operation
from ..services.billing import (
    active_session_by_table,
    active_tab_by_table,
    available_discount_types,
    build_session_runtime,
    calculate_food_discount,
    calculate_timed_charge,
    discount_pricing_label,
    discount_scope_label,
    discount_type_applies_to_session,
    discount_type_is_available,
    session_drink_total,
    session_food_total,
    tab_food_total,
    tab_orders,
    table_count_value,
    table_rate_for,
    unpaid_order_totals,
)

bp = Blueprint("tables", __name__)


def active_menu_catalog(db: sqlite3.Connection) -> tuple[list[sqlite3.Row], list[dict]]:
    categories = db.execute(
        "SELECT id, name FROM categories WHERE is_active = 1 ORDER BY id"
    ).fetchall()
    item_rows = db.execute(
        """
        SELECT m.id, m.category_id, m.name, m.price_cents
        FROM menu_items m
        JOIN categories c ON c.id = m.category_id
        WHERE m.is_active = 1 AND c.is_active = 1
        ORDER BY m.category_id, m.id
        """
    ).fetchall()
    items_by_category: dict[int, list[dict]] = {
        int(category["id"]): [] for category in categories
    }
    for item in item_rows:
        items_by_category[int(item["category_id"])].append(
            {
                "id": int(item["id"]),
                "name": item["name"],
                "price_cents": int(item["price_cents"]),
            }
        )
    catalog = [
        {
            "id": int(category["id"]),
            "name": category["name"],
            "is_drink": category["name"] == "飲料",
            "items": items_by_category[int(category["id"])],
        }
        for category in categories
    ]
    return categories, catalog


def selected_discount(db: sqlite3.Connection, session: sqlite3.Row | dict) -> dict:
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
        return {
            "discount_type_id": int(discount_type["id"]),
            "discount_name": discount_type["name"],
            "pricing_method": discount_type["pricing_method"],
            "package_rate_cents": discount_type["package_rate_per_hour_cents"],
            "percent": float(discount_type["discount_percent"]),
            "scope": discount_type["discount_scope"],
        }

    try:
        percent = float(request.form.get("discount_percent", "100"))
    except ValueError:
        percent = 100.0
    if percent not in [float(value) for value in AVAILABLE_DISCOUNTS]:
        percent = 100.0
    scope = request.form.get("discount_scope", DISCOUNT_SCOPE_TABLE_AND_DRINK)
    if scope not in (
        DISCOUNT_SCOPE_TABLE_ONLY,
        DISCOUNT_SCOPE_TABLE_AND_DRINK,
        DISCOUNT_SCOPE_ALL,
    ):
        scope = DISCOUNT_SCOPE_TABLE_AND_DRINK
    return {
        "discount_type_id": None,
        "discount_name": "原價" if percent == 100 else "自訂折扣",
        "pricing_method": DISCOUNT_PRICING_PERCENTAGE,
        "package_rate_cents": None,
        "percent": percent,
        "scope": scope,
    }


def package_table_charge(session: sqlite3.Row | dict, hours: int) -> tuple[int, int, int]:
    gross_cents = validate_cents(hours * int(session["rate_per_hour_cents"]))
    if session["discount_pricing_method"] == DISCOUNT_PRICING_PACKAGE_HOURLY:
        net_cents = validate_cents(hours * int(session["discount_package_rate_per_hour_cents"]))
        discount_cents = gross_cents - net_cents
    else:
        discount_cents = percentage_of_cents(
            gross_cents, 100 - float(session["discount_percent"])
        )
        net_cents = gross_cents - discount_cents
    return gross_cents, discount_cents, net_cents


def unpaid_drink_orders(
    db: sqlite3.Connection, customer_tab_id: int
) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT o.* FROM orders o
           WHERE o.customer_tab_id = ? AND o.payment_id IS NULL
             AND (
                 o.item_category_name = '飲料'
                 OR (
                     o.item_category_name = ''
                     AND o.item_id IN (
                         SELECT m.id FROM menu_items m
                         JOIN categories c ON c.id = m.category_id
                         WHERE c.name = '飲料'
                     )
                 )
             )
           ORDER BY o.id""",
        (customer_tab_id,),
    ).fetchall()


def mark_orders_paid(
    db: sqlite3.Connection, order_ids: list[int], payment_id: int, paid_at: str
) -> None:
    if not order_ids:
        return
    placeholders = ", ".join("?" for _ in order_ids)
    result = db.execute(
        f"""UPDATE orders SET payment_id = ?, paid_at = ?
            WHERE id IN ({placeholders}) AND payment_id IS NULL""",
        (payment_id, paid_at, *order_ids),
    )
    if result.rowcount != len(order_ids):
        raise sqlite3.IntegrityError("order payment state changed")


def insert_payment(
    db: sqlite3.Connection,
    *,
    customer_tab_id: int,
    session_id: int | None,
    payment_type: str,
    table_fee_cents: int,
    food_fee_cents: int,
    discount_amount_cents: int,
    amount_cents: int,
    paid_at: str,
) -> int:
    for value in (table_fee_cents, food_fee_cents, discount_amount_cents, amount_cents):
        validate_cents(value)
    if amount_cents % 100:
        raise ValueError("實收金額必須是整元。")
    cursor = db.execute(
        """
        INSERT INTO payments (
            customer_tab_id, session_id, payment_type, table_fee_cents,
            food_fee_cents, discount_amount_cents, amount_cents, paid_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            customer_tab_id, session_id, payment_type, table_fee_cents,
            food_fee_cents, discount_amount_cents, amount_cents, paid_at,
        ),
    )
    return int(cursor.lastrowid)


def commit_new_session(
    db: sqlite3.Connection, sql: str, parameters: tuple, table_no: int,
    payment: dict | None = None,
    include_unpaid_drinks: bool = False,
    drink_discount: dict | None = None,
) -> dict | None:
    try:
        db.execute("BEGIN IMMEDIATE")
        if not 1 <= table_no <= table_count_value():
            raise sqlite3.IntegrityError("table no longer available")
        if active_session_by_table(table_no):
            raise sqlite3.IntegrityError("table already playing")
        tab = active_tab_by_table(table_no)
        if tab is None:
            tab_id = db.execute(
                """INSERT INTO customer_tabs (table_no, status, opened_at)
                   VALUES (?, 'assigned', ?)""",
                (table_no, datetime.now().isoformat(timespec="seconds")),
            ).lastrowid
        else:
            tab_id = tab["id"]
        db.execute("UPDATE customer_tabs SET status = 'playing' WHERE id = ?", (tab_id,))
        parameters = (tab_id, *parameters)
        cursor = db.execute(sql, parameters)
        session_id = int(cursor.lastrowid)
        settled_drink_cents = 0
        payment_values = dict(payment) if payment else None
        drink_order_ids: list[int] = []
        if payment_values and include_unpaid_drinks:
            drink_rows = unpaid_drink_orders(db, int(tab_id))
            drink_order_ids = [int(row["id"]) for row in drink_rows]
            settled_drink_cents = sum(int(row["subtotal_cents"]) for row in drink_rows)
            drink_discount_cents = calculate_food_discount(
                settled_drink_cents,
                settled_drink_cents,
                drink_discount["pricing_method"],
                float(drink_discount["percent"]),
                drink_discount["scope"],
            ) if drink_discount else 0
            payment_values["food_fee_cents"] += settled_drink_cents
            payment_values["discount_amount_cents"] += drink_discount_cents
            payment_values["amount_cents"] += (
                settled_drink_cents - drink_discount_cents
            )
        if payment_values:
            payment_values["amount_cents"] = round_payment_cents(
                payment_values["amount_cents"]
            )
            payment_id = insert_payment(
                db,
                customer_tab_id=int(tab_id),
                session_id=session_id,
                **payment_values,
            )
            mark_orders_paid(
                db, drink_order_ids, payment_id, payment_values["paid_at"]
            )
            db.execute(
                "UPDATE sessions SET final_total_cents = ? WHERE id = ?",
                (payment_values["amount_cents"], session_id),
            )
        commit_operation(db)
        return {
            "session_id": session_id,
            "payment": payment_values,
            "settled_drink_cents": settled_drink_cents,
        }
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"{table_label(table_no)}目前已開台，未建立重複紀錄。", "error")
        return None

@bp.route("/")
def dashboard():
    table_count = table_count_value()
    now = datetime.now()
    tables: list[dict] = []

    for table_no in range(1, table_count + 1):
        active = active_session_by_table(table_no)
        tab = active_tab_by_table(table_no)
        if active:
            runtime = build_session_runtime(active, now)
            tables.append(
                {
                    "table_no": table_no,
                    "status": "使用中" if runtime["timer_kind"] == "practice" else "開台",
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
                    "status": "待開台" if tab else "空桌",
                    "status_class": "waiting" if tab else "idle",
                    "mode_label": "尚未開台" if tab else "-",
                    "timer_kind": "idle",
                    "timer_seconds": 0,
                    "timer_text": "--:--:--",
                }
            )

    db = get_db()
    waiting_tabs = db.execute(
        """SELECT t.*, COALESCE(SUM(o.subtotal_cents), 0) AS food_total_cents
           FROM customer_tabs t LEFT JOIN orders o ON o.customer_tab_id = t.id
           WHERE t.status = 'waiting' GROUP BY t.id ORDER BY t.id"""
    ).fetchall()
    return render_template(
        "dashboard.html", tables=tables, table_count=table_count,
        waiting_tabs=waiting_tabs,
    )


@bp.route("/tables/<int:table_no>")
def table_detail(table_no: int):
    table_count = table_count_value()
    if table_no < 1 or table_no > table_count:
        flash(f"桌號需介於 1 到 {table_count}。", "error")
        return redirect(url_for(".dashboard"))

    db = get_db()
    categories, menu_catalog = active_menu_catalog(db)
    category_ids = {int(row["id"]) for row in categories}

    selected_category_id = request.args.get("category_id", "").strip()
    category_id: int | None = None
    if categories:
        try:
            category_id = int(selected_category_id) if selected_category_id else int(categories[0]["id"])
        except ValueError:
            category_id = int(categories[0]["id"])
        if category_id not in category_ids:
            category_id = int(categories[0]["id"])

    active = active_session_by_table(table_no)
    tab = active_tab_by_table(table_no)
    tab_food_total_cents, tab_drink_total_cents = (
        unpaid_order_totals(int(tab["id"])) if tab else (0, 0)
    )
    runtime = build_session_runtime(active, datetime.now()) if active else None
    table_rate = table_rate_for(table_no)
    can_package = bool(table_rate["package_enabled"])
    package_discount_types = available_discount_types(
        session={
            "mode": "package",
            "rate_per_hour_cents": int(table_rate["package_rate_per_hour_cents"]),
        }
    )

    return render_template(
        "table_detail.html",
        table_no=table_no,
        table_count=table_count,
        runtime=runtime,
        transfer_tables=[no for no in range(1, table_count + 1)
                         if no != table_no and not active_tab_by_table(no)
                         and not active_session_by_table(no)],
        transfer_history=db.execute(
            "SELECT * FROM session_transfers WHERE session_id = ? ORDER BY id",
            (active['id'],),
        ).fetchall() if active else [],
        tab=tab,
        tab_orders=tab_orders(tab["id"]) if tab else [],
        tab_food_total_cents=tab_food_total_cents,
        tab_drink_total_cents=tab_drink_total_cents,
        categories=categories,
        menu_catalog=menu_catalog,
        selected_category_id=category_id,
        can_package=can_package,
        table_rate=table_rate,
        discount_types=available_discount_types(session=active),
        package_discount_types=package_discount_types,
        discount_options=AVAILABLE_DISCOUNTS,
        discount_scope_options=[
            (DISCOUNT_SCOPE_TABLE_ONLY, "只折球檯"),
            (DISCOUNT_SCOPE_TABLE_AND_DRINK, "球檯+飲料"),
            (DISCOUNT_SCOPE_ALL, "全部"),
        ],
        sugar_options=SUGAR_OPTIONS,
        ice_options=ICE_OPTIONS,
    )


@bp.route("/waiting/<int:tab_id>")
def waiting_detail(tab_id: int):
    db = get_db()
    tab = db.execute(
        "SELECT * FROM customer_tabs WHERE id = ? AND status = 'waiting'", (tab_id,)
    ).fetchone()
    if tab is None:
        flash("找不到候位消費單。", "error")
        return redirect(url_for(".dashboard"))
    categories, menu_catalog = active_menu_catalog(db)
    try:
        category_id = int(request.args.get("category_id", categories[0]["id"] if categories else 0))
    except ValueError:
        category_id = categories[0]["id"] if categories else 0
    category = next((row for row in categories if row["id"] == category_id), None)
    if category is None and categories:
        category = categories[0]
        category_id = category["id"]
    free_tables = [
        number for number in range(1, table_count_value() + 1)
        if active_tab_by_table(number) is None and active_session_by_table(number) is None
    ]
    return render_template(
        "waiting_detail.html", tab=tab, categories=categories,
        menu_catalog=menu_catalog, selected_category_id=category_id,
        orders=tab_orders(tab_id), food_total_cents=tab_food_total(tab_id),
        free_tables=free_tables, sugar_options=SUGAR_OPTIONS, ice_options=ICE_OPTIONS,
    )


@bp.route("/waiting/create", methods=["POST"])
def create_waiting():
    name = request.form.get("display_name", "").strip()
    try:
        guest_count = int(request.form.get("guest_count", "1"))
    except ValueError:
        guest_count = 0
    if guest_count < 1 or guest_count > 99:
        flash("人數需介於 1 到 99。", "error")
        return redirect(url_for(".dashboard"))
    db = get_db()
    tab_id = db.execute(
        """INSERT INTO customer_tabs (display_name, guest_count, opened_at)
           VALUES (?, ?, ?)""",
        (name, guest_count, datetime.now().isoformat(timespec="seconds")),
    ).lastrowid
    commit_operation(db)
    return redirect(url_for(".waiting_detail", tab_id=tab_id))


@bp.route("/waiting/<int:tab_id>/assign", methods=["POST"])
def assign_waiting(tab_id: int):
    try:
        table_no = int(request.form["table_no"])
    except (KeyError, ValueError):
        flash("請選擇球檯。", "error")
        return redirect(url_for(".waiting_detail", tab_id=tab_id))
    if not 1 <= table_no <= table_count_value():
        flash("球檯號碼無效。", "error")
        return redirect(url_for(".waiting_detail", tab_id=tab_id))
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        if active_tab_by_table(table_no) is not None or active_session_by_table(table_no):
            raise ValueError("該球檯已被使用。")
        result = db.execute(
            """UPDATE customer_tabs SET table_no = ?, status = 'assigned'
               WHERE id = ? AND status = 'waiting'""",
            (table_no, tab_id),
        )
        if not result.rowcount:
            raise ValueError("候位消費單已變更。")
        commit_operation(db)
    except (ValueError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(str(exc), "error")
        return redirect(url_for(".dashboard"))
    flash(f"已將候位消費單分配至 {table_label(table_no)}，尚未開始計時。", "success")
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/tabs/<int:tab_id>/checkout", methods=["POST"])
def checkout_food_only(tab_id: int):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    tab = db.execute(
        """SELECT * FROM customer_tabs WHERE id = ?
           AND status IN ('waiting', 'assigned')""",
        (tab_id,),
    ).fetchone()
    if tab is None:
        db.rollback()
        flash("消費單已結帳或正在打球。", "error")
        return redirect(url_for(".dashboard"))
    total = tab_food_total(tab_id)
    amount_due_cents = round_payment_cents(total)
    order_ids = [
        int(row["id"])
        for row in db.execute(
            "SELECT id FROM orders WHERE customer_tab_id = ? AND payment_id IS NULL",
            (tab_id,),
        ).fetchall()
    ]
    paid_at = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """UPDATE customer_tabs SET status = 'closed', closed_at = ?,
           final_total_cents = ? WHERE id = ?""",
        (paid_at, amount_due_cents, tab_id),
    )
    payment_id = insert_payment(
        db,
        customer_tab_id=tab_id,
        session_id=None,
        payment_type="food_only",
        table_fee_cents=0,
        food_fee_cents=total,
        discount_amount_cents=0,
        amount_cents=amount_due_cents,
        paid_at=paid_at,
    )
    mark_orders_paid(db, order_ids, payment_id, paid_at)
    commit_operation(db)
    flash(f"餐飲單已結帳，應收 {format_cents(amount_due_cents)} 元。", "checkout")
    return redirect(url_for(".dashboard"))


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
        flash(f"{table_label(table_no)}目前已開台。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    now = datetime.now().isoformat(timespec="seconds")

    table_rate = table_rate_for(table_no)

    if mode == "practice":
        started = commit_new_session(
            db,
            """
            INSERT INTO sessions
                (customer_tab_id, table_no, mode, is_free_practice, start_time,
                 rate_per_min_cents, discount_name)
            VALUES (?, ?, 'timed', 1, ?, 0, '免費練習')
            """,
            (table_no, now),
            table_no,
        )
        if not started:
            return redirect(url_for(".table_detail", table_no=table_no))
        flash(f"{table_label(table_no)}已開啟免費練習，只標示使用中、不計球檯費。", "success")
        return redirect(url_for(".table_detail", table_no=table_no))

    if mode == "timed":
        rate_cents = int(table_rate["timed_rate_per_min_cents"])
        started = commit_new_session(
            db,
            """
            INSERT INTO sessions (customer_tab_id, table_no, mode, start_time, rate_per_min_cents)
            VALUES (?, ?, 'timed', ?, ?)
            """,
            (table_no, now, rate_cents),
            table_no,
        )
        if not started:
            return redirect(url_for(".table_detail", table_no=table_no))
        flash(
            f"{table_label(table_no)}開始計時，每分鐘 {format_cents(rate_cents)} 元。",
            "success",
        )
        return redirect(url_for(".table_detail", table_no=table_no))

    if mode == "package":
        if not table_rate["package_enabled"]:
            flash(f"{table_label(table_no)}目前未啟用包台。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        try:
            package_hours = int(request.form.get("package_hours", "1"))
        except ValueError:
            flash("包台時數必須是整數。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        if not 1 <= package_hours <= 16:
            flash("包台時數需介於 1 到 16 小時。", "error")
            return redirect(url_for(".table_detail", table_no=table_no))

        rate_per_hour_cents = int(table_rate["package_rate_per_hour_cents"])
        discount = selected_discount(
            db,
            {"mode": "package", "rate_per_hour_cents": rate_per_hour_cents},
        )
        charge_snapshot = {
            "rate_per_hour_cents": rate_per_hour_cents,
            "discount_pricing_method": discount["pricing_method"],
            "discount_package_rate_per_hour_cents": discount["package_rate_cents"],
            "discount_percent": discount["percent"],
        }
        table_fee_cents, discount_amount_cents, prepaid_cents = package_table_charge(
            charge_snapshot, package_hours
        )
        started = commit_new_session(
            db,
            """
            INSERT INTO sessions
                (customer_tab_id, table_no, mode, start_time, package_hours,
                 rate_per_hour_cents, table_fee_cents, discount_type_id,
                 discount_name, discount_pricing_method,
                 discount_package_rate_per_hour_cents, discount_percent,
                 discount_scope, discount_amount_cents, final_total_cents)
            VALUES (?, ?, 'package', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                table_no, now, package_hours, rate_per_hour_cents,
                table_fee_cents, discount["discount_type_id"],
                discount["discount_name"], discount["pricing_method"],
                discount["package_rate_cents"], discount["percent"],
                discount["scope"], discount_amount_cents, prepaid_cents,
            ),
            table_no,
            payment={
                "payment_type": "package_start",
                "table_fee_cents": table_fee_cents,
                "food_fee_cents": 0,
                "discount_amount_cents": discount_amount_cents,
                "amount_cents": prepaid_cents,
                "paid_at": now,
            },
            include_unpaid_drinks=True,
            drink_discount=discount,
        )
        if not started:
            return redirect(url_for(".table_detail", table_no=table_no))
        payment = started["payment"]
        message = (
            f"{table_label(table_no)}包台 {package_hours} 小時已先結帳。"
            f"球檯費 {format_cents(table_fee_cents)} 元"
        )
        if started["settled_drink_cents"]:
            message += f"、飲料 {format_cents(started['settled_drink_cents'])} 元"
        message += (
            f"，優惠 {discount['discount_name']}，"
            f"應收 {format_cents(payment['amount_cents'])} 元。倒數已開始。"
        )
        flash(message, "checkout")
        return redirect(url_for(".table_detail", table_no=table_no))

    flash("未知的開台模式。", "error")
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/sessions/<int:session_id>/transfer", methods=["POST"])
def transfer_session(session_id: int):
    db = get_db()
    try:
        source = int(request.form.get('from_table', ''))
        target = int(request.form.get('to_table', ''))
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT * FROM sessions WHERE id = ? AND status = 'active'", (session_id,)).fetchone()
        if not active or active['table_no'] != source:
            raise ValueError('球檯已結帳或已換桌，請重新整理後再操作。')
        if target == source or not 1 <= target <= table_count_value():
            raise ValueError('請選擇不同的有效球檯。')
        if active_tab_by_table(target) or active_session_by_table(target):
            raise ValueError('目標球檯已有消費單，不能換入。')
        result = db.execute(
            "UPDATE customer_tabs SET table_no = ? WHERE id = ? AND table_no = ? AND status = 'playing'",
            (target, active['customer_tab_id'], source),
        )
        if result.rowcount != 1:
            raise ValueError('消費單狀態已改變，未進行換桌。')
        db.execute('UPDATE sessions SET table_no = ? WHERE id = ?', (target, session_id))
        db.execute('INSERT INTO session_transfers (session_id, from_table, to_table) VALUES (?, ?, ?)',
                   (session_id, source, target))
        commit_operation(db)
    except (ValueError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else '球檯狀態已變更，未進行換桌。', 'error')
        return redirect(url_for('.dashboard'))
    if bool(active["is_free_practice"]):
        message = f'{table_label(source)}已換至 {table_label(target)}；免費練習狀態與帳單均保留。'
    else:
        message = f'{table_label(source)}已換至 {table_label(target)}；原計時、費率、優惠與帳單均保留。'
    flash(message, 'success')
    return redirect(url_for('.table_detail', table_no=target))


@bp.route("/sessions/extend/<int:session_id>", methods=["POST"])
def extend_package_session(session_id: int):
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

    table_no = int(session["table_no"])
    if session["mode"] != "package":
        db.rollback()
        flash("只有包台模式可以加時。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    try:
        extra_hours = int(request.form.get("extra_hours", "1"))
    except ValueError:
        db.rollback()
        flash("加時小時數格式錯誤。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    if extra_hours < 1 or extra_hours + int(session["package_hours"] or 0) > 16:
        db.rollback()
        flash("加時至少 1 小時，包台累積不可超過 16 小時。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    current_hours = int(session["package_hours"] or 0)
    new_hours = current_hours + extra_hours
    extra_table_fee_cents, extra_discount_cents, extra_due_cents = (
        package_table_charge(session, extra_hours)
    )
    extra_due_cents = round_payment_cents(extra_due_cents)
    prepaid_before = int(db.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) FROM payments
           WHERE session_id = ? AND payment_type IN (
               'package_start', 'package_extension', 'package_food'
           )""",
        (session_id,),
    ).fetchone()[0])
    result = db.execute(
        """UPDATE sessions
           SET package_hours = ?, table_fee_cents = ?,
               discount_amount_cents = ?, final_total_cents = ?
           WHERE id = ? AND status = 'active'""",
        (
            new_hours,
            int(session["table_fee_cents"] or 0) + extra_table_fee_cents,
            int(session["discount_amount_cents"] or 0) + extra_discount_cents,
            validate_cents(prepaid_before + extra_due_cents),
            session_id,
        ),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未加入時數。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))
    paid_at = datetime.now().isoformat(timespec="seconds")
    insert_payment(
        db,
        customer_tab_id=int(session["customer_tab_id"]),
        session_id=session_id,
        payment_type="package_extension",
        table_fee_cents=extra_table_fee_cents,
        food_fee_cents=0,
        discount_amount_cents=extra_discount_cents,
        amount_cents=extra_due_cents,
        paid_at=paid_at,
    )
    commit_operation(db)

    flash(
        f"{table_label(table_no)}加時 {extra_hours} 小時已先結帳，"
        f"應收 {format_cents(extra_due_cents)} 元。"
        f"包台總時數更新為 {new_hours} 小時。",
        "checkout",
    )
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/sessions/<int:session_id>/drinks/checkout", methods=["POST"])
def checkout_package_drinks(session_id: int):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    session = db.execute(
        "SELECT * FROM sessions WHERE id = ? AND status = 'active'",
        (session_id,),
    ).fetchone()
    if session is None or session["mode"] != "package":
        db.rollback()
        flash("只有進行中的包台可以單獨結算飲料。", "error")
        return redirect(url_for(".dashboard"))

    table_no = int(session["table_no"])
    drink_rows = unpaid_drink_orders(db, int(session["customer_tab_id"]))
    if not drink_rows:
        db.rollback()
        flash("目前沒有尚未付款的飲料。", "error")
        return redirect(url_for(".table_detail", table_no=table_no))

    drink_total_cents = sum(int(row["subtotal_cents"]) for row in drink_rows)
    discount_cents = calculate_food_discount(
        drink_total_cents,
        drink_total_cents,
        session["discount_pricing_method"],
        float(session["discount_percent"] or 100),
        session["discount_scope"],
    )
    amount_cents = round_payment_cents(drink_total_cents - discount_cents)
    paid_at = datetime.now().isoformat(timespec="seconds")
    payment_id = insert_payment(
        db,
        customer_tab_id=int(session["customer_tab_id"]),
        session_id=session_id,
        payment_type="package_food",
        table_fee_cents=0,
        food_fee_cents=drink_total_cents,
        discount_amount_cents=discount_cents,
        amount_cents=amount_cents,
        paid_at=paid_at,
    )
    mark_orders_paid(
        db,
        [int(row["id"]) for row in drink_rows],
        payment_id,
        paid_at,
    )
    commit_operation(db)
    flash(
        f"{table_label(table_no)}飲料已結帳。飲料 {format_cents(drink_total_cents)} 元、"
        f"優惠 {format_cents(discount_cents)} 元，應收 {format_cents(amount_cents)} 元。",
        "checkout",
    )
    return redirect(url_for(".table_detail", table_no=table_no))


@bp.route("/orders/add", methods=["POST"])
def add_order():
    db = get_db()
    try:
        item_id = int(request.form["item_id"])
        quantity = int(request.form.get("quantity", "1"))
    except (KeyError, ValueError):
        flash("點餐資料格式錯誤。", "error")
        return redirect(url_for(".dashboard"))
    if not 1 <= quantity <= 99:
        flash("單次餐點數量需介於 1 到 99。", "error")
        return redirect(url_for(".dashboard"))
    category_id = request.form.get("category_id", "").strip()
    sugar_level = request.form.get("sugar_level", "").strip()
    ice_level = request.form.get("ice_level", "").strip()

    try:
        db.execute("BEGIN IMMEDIATE")
        if request.form.get("customer_tab_id"):
            tab = db.execute(
                """SELECT * FROM customer_tabs WHERE id = ? AND status != 'closed'""",
                (int(request.form["customer_tab_id"]),),
            ).fetchone()
        elif request.form.get("session_id"):
            tab = db.execute(
                """SELECT t.* FROM customer_tabs t
                   JOIN sessions s ON s.customer_tab_id = t.id
                   WHERE s.id = ? AND s.status = 'active'""",
                (int(request.form["session_id"]),),
            ).fetchone()
        else:
            table_no = int(request.form["table_no"])
            if not 1 <= table_no <= table_count_value():
                raise ValueError("球檯號碼無效。")
            tab = active_tab_by_table(table_no)
            if tab is None:
                tab_id = db.execute(
                    """INSERT INTO customer_tabs (table_no, status, opened_at)
                       VALUES (?, 'assigned', ?)""",
                    (table_no, datetime.now().isoformat(timespec="seconds")),
                ).lastrowid
                tab = db.execute("SELECT * FROM customer_tabs WHERE id = ?", (tab_id,)).fetchone()
        if tab is None:
            raise ValueError("找不到進行中的消費單。")
    except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
        db.rollback()
        flash(f"點餐失敗：{exc}", "error")
        return redirect(url_for(".dashboard"))

    def back():
        continue_ordering = "1" if request.form.get("continue_ordering") == "1" else None
        if tab["table_no"] is None:
            return redirect(url_for(
                ".waiting_detail", tab_id=tab["id"],
                category_id=category_id or None, order=continue_ordering,
            ))
        return redirect(url_for(
            ".table_detail", table_no=tab["table_no"],
            category_id=category_id or None, order=continue_ordering,
        ))

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
        db.rollback()
        flash("找不到品項。", "error")
        return back()

    is_drink = (item["category_name"] or "") == "飲料"
    if is_drink:
        if sugar_level not in SUGAR_OPTIONS:
            db.rollback()
            flash("飲料甜度選項錯誤。", "error")
            return back()
        if ice_level not in ICE_OPTIONS:
            db.rollback()
            flash("飲料冰塊選項錯誤。", "error")
            return back()
    else:
        sugar_level = ""
        ice_level = ""

    unit_price_cents = int(item["price_cents"])
    subtotal_cents = validate_cents(unit_price_cents * quantity)
    validate_cents(tab_food_total(tab["id"]) + subtotal_cents)
    session = db.execute(
        "SELECT id FROM sessions WHERE customer_tab_id = ? AND status = 'active'",
        (tab["id"],),
    ).fetchone()
    db.execute(
        """
        INSERT INTO orders (
            customer_tab_id, session_id, item_id, item_name, item_category_name, sugar_level, ice_level,
            is_served, unit_price_cents, quantity, subtotal_cents
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tab["id"],
            session["id"] if session else None,
            item_id,
            item["name"],
            item["category_name"] or "",
            sugar_level,
            ice_level,
            0,
            unit_price_cents,
            quantity,
            subtotal_cents,
        ),
    )
    commit_operation(db)

    flash(f"已新增：{item['name']} x {quantity}", "success")
    return back()


@bp.route("/orders/<int:order_id>/served", methods=["POST"])
def toggle_order_served(order_id: int):
    db = get_db()
    row = db.execute(
        """
        SELECT o.id, o.payment_id, t.id AS tab_id, t.table_no, t.status
        FROM orders o
        JOIN customer_tabs t ON t.id = o.customer_tab_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        flash("找不到該筆訂單。", "error")
        return redirect(url_for(".dashboard"))

    if row["status"] == "closed":
        flash("已關台訂單不可修改出餐狀態。", "error")
        return redirect(url_for(".dashboard"))

    is_served = 1 if request.form.get("is_served") == "1" else 0
    result = db.execute(
        """UPDATE orders SET is_served = ? WHERE id = ? AND EXISTS (
               SELECT 1 FROM customer_tabs
               WHERE id = orders.customer_tab_id AND status != 'closed')""",
        (is_served, order_id),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未修改訂單。", "error")
        return redirect(url_for(".dashboard"))
    commit_operation(db)

    category_id = request.form.get("category_id", "").strip()
    if row["table_no"] is None:
        return redirect(url_for(".waiting_detail", tab_id=row["tab_id"], category_id=category_id or None))
    return redirect(url_for(".table_detail", table_no=row["table_no"], category_id=category_id or None))


@bp.route("/orders/<int:order_id>/delete", methods=["POST"])
def delete_order(order_id: int):
    db = get_db()
    row = db.execute(
        """
        SELECT o.id, o.payment_id, o.quantity, o.subtotal_cents,
               t.id AS tab_id, t.table_no, t.status
        FROM orders o
        JOIN customer_tabs t ON t.id = o.customer_tab_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row:
        flash("找不到該筆訂單。", "error")
        return redirect(url_for(".dashboard"))

    if row["status"] == "closed":
        flash("已關台訂單不可刪除。", "error")
        return redirect(url_for(".dashboard"))
    if row["payment_id"] is not None:
        flash("已付款的餐飲不可刪除；若要退款，請另外留下退款紀錄。", "error")
        if row["table_no"] is None:
            return redirect(url_for(".waiting_detail", tab_id=row["tab_id"]))
        return redirect(url_for(".table_detail", table_no=row["table_no"]))

    result = db.execute(
        """DELETE FROM orders WHERE id = ? AND payment_id IS NULL AND EXISTS (
               SELECT 1 FROM customer_tabs
               WHERE id = orders.customer_tab_id AND status != 'closed')""",
        (order_id,),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已結帳，未刪除訂單。", "error")
        return redirect(url_for(".dashboard"))
    record_audit(
        db, "order_delete", str(order_id),
        f"消費單 #{row['tab_id']}；數量 {row['quantity']}；金額 {format_cents(row['subtotal_cents'])} 元",
        "已刪除",
    )
    commit_operation(db)
    flash("已刪除該筆訂單。", "success")

    category_id = request.form.get("category_id", "").strip()
    if row["table_no"] is None:
        return redirect(url_for(".waiting_detail", tab_id=row["tab_id"], category_id=category_id or None))
    return redirect(url_for(".table_detail", table_no=row["table_no"], category_id=category_id or None))


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

    prepayment = db.execute(
        """SELECT
                  COALESCE(SUM(CASE WHEN payment_type IN (
                      'package_start', 'package_extension'
                  ) THEN 1 ELSE 0 END), 0) AS table_payment_count,
                  COALESCE(SUM(table_fee_cents), 0) AS table_fee_cents,
                  COALESCE(SUM(food_fee_cents), 0) AS food_fee_cents,
                  COALESCE(SUM(discount_amount_cents), 0) AS discount_amount_cents,
                  COALESCE(SUM(amount_cents), 0) AS amount_cents
           FROM payments
           WHERE session_id = ?
             AND payment_type IN (
                 'package_start', 'package_extension', 'package_food'
             )""",
        (session_id,),
    ).fetchone()
    package_was_prepaid = (
        session["mode"] == "package"
        and int(prepayment["table_payment_count"]) > 0
    )

    is_free_practice = bool(session["is_free_practice"])

    if is_free_practice:
        discount_type_id = None
        discount_name = "免費練習"
        discount_pricing_method = DISCOUNT_PRICING_PERCENTAGE
        discount_package_rate_per_hour_cents = None
        discount_percent = 100.0
        discount_scope = DISCOUNT_SCOPE_TABLE_ONLY
    elif package_was_prepaid:
        discount_type_id = session["discount_type_id"]
        discount_name = session["discount_name"] or "原價"
        discount_pricing_method = session["discount_pricing_method"]
        discount_package_rate_per_hour_cents = session[
            "discount_package_rate_per_hour_cents"
        ]
        discount_percent = float(session["discount_percent"] or 100)
        discount_scope = session["discount_scope"] or DISCOUNT_SCOPE_TABLE_AND_DRINK
    else:
        discount = selected_discount(db, session)
        discount_type_id = discount["discount_type_id"]
        discount_name = discount["discount_name"]
        discount_pricing_method = discount["pricing_method"]
        discount_package_rate_per_hour_cents = discount["package_rate_cents"]
        discount_percent = float(discount["percent"])
        discount_scope = discount["scope"]

    start_dt = datetime.fromisoformat(session["start_time"])
    end_dt = datetime.now()
    food_fee_cents = session_food_total(session_id)
    drink_fee_cents = session_drink_total(session_id)
    unpaid_order_rows = db.execute(
        """SELECT id, subtotal_cents FROM orders
           WHERE customer_tab_id = ? AND payment_id IS NULL
           ORDER BY id""",
        (session["customer_tab_id"],),
    ).fetchall()
    unpaid_order_ids = [int(row["id"]) for row in unpaid_order_rows]
    unpaid_food_fee_cents = sum(
        int(row["subtotal_cents"]) for row in unpaid_order_rows
    )
    _, unpaid_drink_fee_cents = unpaid_order_totals(int(session["customer_tab_id"]))

    if is_free_practice:
        table_fee_cents = 0
        discount_amount_cents = 0
        final_total_cents = food_fee_cents
        payment_type = "timed_close"
        payment_table_fee_cents = 0
        payment_food_fee_cents = unpaid_food_fee_cents
        payment_discount_cents = 0
        amount_due_cents = round_payment_cents(unpaid_food_fee_cents)
        final_total_cents = amount_due_cents
        prepaid_cents = 0
        food_discount_cents = 0
    elif session["mode"] == "timed":
        _, table_fee_cents = calculate_timed_charge(
            start_dt, end_dt, int(session["rate_per_min_cents"])
        )
        if discount_scope == DISCOUNT_SCOPE_TABLE_ONLY:
            discount_base_cents = table_fee_cents
        elif discount_scope == DISCOUNT_SCOPE_TABLE_AND_DRINK:
            discount_base_cents = table_fee_cents + drink_fee_cents
        else:
            discount_base_cents = table_fee_cents + food_fee_cents
        discount_amount_cents = percentage_of_cents(
            discount_base_cents, 100 - discount_percent
        )
        final_total_cents = round_payment_cents(
            table_fee_cents + food_fee_cents - discount_amount_cents
        )
        payment_type = "timed_close"
        payment_table_fee_cents = table_fee_cents
        payment_food_fee_cents = food_fee_cents
        payment_discount_cents = discount_amount_cents
        amount_due_cents = final_total_cents
        prepaid_cents = 0
        food_discount_cents = max(0, discount_amount_cents - percentage_of_cents(
            table_fee_cents, 100 - discount_percent
        )) if discount_scope != DISCOUNT_SCOPE_TABLE_ONLY else 0
    else:
        charge_snapshot = {
            "rate_per_hour_cents": int(session["rate_per_hour_cents"]),
            "discount_pricing_method": discount_pricing_method,
            "discount_package_rate_per_hour_cents": discount_package_rate_per_hour_cents,
            "discount_percent": discount_percent,
        }
        table_fee_cents, _, _ = package_table_charge(
            charge_snapshot, int(session["package_hours"])
        )
        food_discount_cents = calculate_food_discount(
            unpaid_food_fee_cents,
            unpaid_drink_fee_cents,
            discount_pricing_method,
            discount_percent,
            discount_scope,
        )
        prepaid_cents = int(prepayment["amount_cents"] or 0)
        payment_type = "package_close"
        payment_table_fee_cents = max(
            0, table_fee_cents - int(prepayment["table_fee_cents"] or 0)
        )
        payment_food_fee_cents = unpaid_food_fee_cents
        if discount_pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY:
            remaining_hours = payment_table_fee_cents // int(session["rate_per_hour_cents"])
            remaining_table_discount_cents = (
                payment_table_fee_cents
                - remaining_hours * int(discount_package_rate_per_hour_cents)
            )
        else:
            remaining_table_discount_cents = percentage_of_cents(
                payment_table_fee_cents, 100 - discount_percent
            )
        payment_discount_cents = remaining_table_discount_cents + food_discount_cents
        discount_amount_cents = (
            int(prepayment["discount_amount_cents"] or 0)
            + payment_discount_cents
        )
        amount_due_cents = round_payment_cents(
            payment_table_fee_cents + payment_food_fee_cents
            - payment_discount_cents
        )
        final_total_cents = prepaid_cents + amount_due_cents

    result = db.execute(
        """
        UPDATE sessions
        SET end_time = ?, status = 'closed', table_fee_cents = ?, food_fee_cents = ?,
            discount_type_id = ?, discount_name = ?, discount_pricing_method = ?,
            discount_package_rate_per_hour_cents = ?, discount_percent = ?,
            discount_scope = ?, discount_amount_cents = ?, final_total_cents = ?
        WHERE id = ? AND status = 'active'
        """,
        (
            end_dt.isoformat(timespec="seconds"),
            table_fee_cents,
            food_fee_cents,
            discount_type_id,
            discount_name,
            discount_pricing_method,
            discount_package_rate_per_hour_cents,
            discount_percent,
            discount_scope,
            discount_amount_cents,
            final_total_cents,
            session_id,
        ),
    )
    if not result.rowcount:
        db.rollback()
        flash("球檯已由其他操作完成結帳，未重複寫入。", "error")
        return redirect(url_for(".table_detail", table_no=session["table_no"]))
    tab_result = db.execute(
        """UPDATE customer_tabs
           SET status = 'closed', closed_at = ?, final_total_cents = ?
           WHERE id = ? AND status = 'playing'""",
        (end_dt.isoformat(timespec="seconds"), final_total_cents, session["customer_tab_id"]),
    )
    if not tab_result.rowcount:
        db.rollback()
        flash("消費單狀態異常，未完成結帳。", "error")
        return redirect(url_for(".table_detail", table_no=session["table_no"]))
    if payment_table_fee_cents or payment_food_fee_cents or (
        session["mode"] == "timed" and not is_free_practice
    ):
        payment_id = insert_payment(
            db,
            customer_tab_id=int(session["customer_tab_id"]),
            session_id=session_id,
            payment_type=payment_type,
            table_fee_cents=payment_table_fee_cents,
            food_fee_cents=payment_food_fee_cents,
            discount_amount_cents=payment_discount_cents,
            amount_cents=amount_due_cents,
            paid_at=end_dt.isoformat(timespec="seconds"),
        )
        mark_orders_paid(
            db,
            unpaid_order_ids,
            payment_id,
            end_dt.isoformat(timespec="seconds"),
        )
    commit_operation(db)

    if is_free_practice:
        if amount_due_cents:
            message = (
                f"{table_label(session['table_no'])}免費練習已結束。"
                f"球檯費 0 元、餐飲應收 {format_cents(amount_due_cents)} 元。"
            )
        else:
            message = (
                f"{table_label(session['table_no'])}免費練習已結束，"
                "球檯費 0 元，本次無待收款。"
            )
    elif session["mode"] == "timed":
        message = (
            f"{table_label(session['table_no'])}已結帳。"
            f"球檯費 {format_cents(table_fee_cents)} 元、"
            f"飲料+餐點 {format_cents(food_fee_cents)} 元、"
            f"優惠 {discount_name}："
            f"{discount_pricing_label(discount_pricing_method, discount_percent, discount_package_rate_per_hour_cents)}"
            f"（{discount_scope_label(discount_scope)}）"
            f"共折 {format_cents(discount_amount_cents)} 元，"
            f"應收 {format_cents(final_total_cents)} 元。"
        )
    elif amount_due_cents > 0:
        message = (
            f"{table_label(session['table_no'])}包台已結束。"
            f"先前已收 {format_cents(prepaid_cents)} 元、"
            f"本次未結餐飲 {format_cents(unpaid_food_fee_cents)} 元，"
            f"本次應收 {format_cents(amount_due_cents)} 元。"
        )
    else:
        message = (
            f"{table_label(session['table_no'])}包台已結束。"
            f"先前款項 {format_cents(prepaid_cents)} 元已收清，"
            "本次無待收款。"
        )
    flash(message, "checkout")
    return redirect(url_for(".table_detail", table_no=session["table_no"]))
