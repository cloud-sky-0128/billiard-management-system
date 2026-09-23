from datetime import date, datetime, timedelta

from flask import Blueprint, flash, render_template, request

from ..config import DISCOUNT_SCOPE_TABLE_AND_DRINK
from ..db import get_db
from ..money import percentage_of_cents
from ..services.billing import discount_pricing_label, discount_scope_label, summarize_payments
from ..services.business_day import business_day_bounds, current_business_day
from ..services.scheduling import supported_date

bp = Blueprint("stats", __name__)

@bp.route("/stats")
def stats_page():
    db = get_db()
    input_date = request.args.get("date", current_business_day().isoformat())

    try:
        selected_date = supported_date(input_date)
    except ValueError:
        selected_date = current_business_day()
        flash("日期格式錯誤，已切回今天。", "error")

    start_dt, end_dt = business_day_bounds(selected_date)
    start_iso = start_dt.isoformat(timespec="seconds")
    end_iso = end_dt.isoformat(timespec="seconds")

    payment_rows = db.execute(
        """SELECT p.*, s.table_no AS session_table_no, t.table_no AS tab_table_no,
                  s.discount_name, s.discount_pricing_method,
                  s.discount_package_rate_per_hour_cents, s.discount_percent,
                  s.discount_scope, s.is_free_practice
           FROM payments p
           JOIN customer_tabs t ON t.id = p.customer_tab_id
           LEFT JOIN sessions s ON s.id = p.session_id
           WHERE datetime(p.paid_at) >= datetime(?) AND datetime(p.paid_at) < datetime(?)
           ORDER BY datetime(p.paid_at) DESC, p.id DESC""",
        (start_iso, end_iso),
    ).fetchall()

    closed_rows = db.execute(
        """
        SELECT
            s.id,
            s.table_no,
            s.mode,
            s.is_free_practice,
            s.end_time,
            s.table_fee_cents,
            s.discount_name,
            s.discount_pricing_method,
            s.discount_package_rate_per_hour_cents,
            s.discount_percent,
            s.discount_scope,
            s.discount_amount_cents,
            s.final_total_cents,
            COALESCE(o.food_total_cents, 0) AS food_total_cents
        FROM sessions s
        LEFT JOIN (
            SELECT customer_tab_id, SUM(subtotal_cents) AS food_total_cents
            FROM orders
            GROUP BY customer_tab_id
        ) o ON o.customer_tab_id = s.customer_tab_id
        WHERE s.status = 'closed'
          AND datetime(s.end_time) >= datetime(?) AND datetime(s.end_time) < datetime(?)
          AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.session_id = s.id)
        ORDER BY s.end_time DESC
        """,
        (start_iso, end_iso),
    ).fetchall()

    payment_summary = summarize_payments(payment_rows)
    table_list_cents = payment_summary.table_list_cents
    food_list_cents = payment_summary.food_list_cents
    gross_list_cents = payment_summary.gross_list_cents
    paid_total_cents = payment_summary.paid_cents
    session_rows: list[dict] = []

    payment_type_labels = {
        "package_start": "包台預收",
        "package_extension": "包台加時",
        "package_food": "包台飲料結帳",
        "package_close": "包台餐飲結清",
        "timed_close": "計時結帳",
        "food_only": "只結餐飲",
    }
    for row in payment_rows:
        table_fee_cents = int(row["table_fee_cents"] or 0)
        food_fee_cents = int(row["food_fee_cents"] or 0)
        final_total_cents = int(row["amount_cents"] or 0)
        discount_percent = float(row["discount_percent"] or 100)
        discount_scope = row["discount_scope"] or DISCOUNT_SCOPE_TABLE_AND_DRINK
        pricing_method = row["discount_pricing_method"] or "percentage"
        session_rows.append(
            {
                "table_no": row["session_table_no"] or row["tab_table_no"] or "候位",
                "mode_label": (
                    "免費練習餐飲結帳"
                    if bool(row["is_free_practice"])
                    else payment_type_labels.get(row["payment_type"], "結帳")
                ),
                "end_time": row["paid_at"],
                "table_fee_cents": table_fee_cents,
                "food_fee_cents": food_fee_cents,
                "discount_name": row["discount_name"] or "原價",
                "discount_label": discount_pricing_label(
                    pricing_method,
                    discount_percent,
                    row["discount_package_rate_per_hour_cents"],
                ),
                "discount_scope_label": (
                    discount_scope_label(discount_scope) if row["session_id"] else "無"
                ),
                "final_total_cents": final_total_cents,
            }
        )

    for row in closed_rows:
        table_fee_cents = int(row["table_fee_cents"] or 0)
        food_fee_cents = int(row["food_total_cents"] or 0)
        discount_percent = float(row["discount_percent"] or 100)
        discount_scope = row["discount_scope"] or DISCOUNT_SCOPE_TABLE_AND_DRINK
        discount_amount_cents = int(row["discount_amount_cents"] or 0)
        gross_cents = table_fee_cents + food_fee_cents

        final_total_cents = int(row["final_total_cents"] or 0)
        if final_total_cents <= 0:
            if discount_amount_cents > 0:
                final_total_cents = gross_cents - discount_amount_cents
            else:
                final_total_cents = percentage_of_cents(
                    gross_cents, discount_percent
                )

        table_list_cents += table_fee_cents
        food_list_cents += food_fee_cents
        gross_list_cents += gross_cents
        paid_total_cents += final_total_cents

        session_rows.append(
            {
                "table_no": row["table_no"],
                "mode_label": (
                    "免費練習"
                    if bool(row["is_free_practice"])
                    else ("包台" if row["mode"] == "package" else "計時")
                ),
                "end_time": row["end_time"],
                "table_fee_cents": table_fee_cents,
                "food_fee_cents": food_fee_cents,
                "discount_name": row["discount_name"] or (
                    "原價" if discount_percent == 100 else "自訂折扣"
                ),
                "discount_label": discount_pricing_label(
                    row["discount_pricing_method"] or "percentage",
                    discount_percent,
                    row["discount_package_rate_per_hour_cents"],
                ),
                "discount_scope_label": discount_scope_label(discount_scope),
                "final_total_cents": final_total_cents,
            }
        )

    food_only_rows = db.execute(
        """SELECT t.table_no, t.closed_at AS end_time,
                  t.final_total_cents,
                  COALESCE(SUM(o.subtotal_cents), 0) AS food_total_cents
           FROM customer_tabs t
           LEFT JOIN orders o ON o.customer_tab_id = t.id
           WHERE t.status = 'closed'
             AND datetime(t.closed_at) >= datetime(?) AND datetime(t.closed_at) < datetime(?)
             AND NOT EXISTS (
                 SELECT 1 FROM sessions s WHERE s.customer_tab_id = t.id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM payments p WHERE p.customer_tab_id = t.id
             )
           GROUP BY t.id ORDER BY t.closed_at DESC""",
        (start_iso, end_iso),
    ).fetchall()
    for row in food_only_rows:
        food_cents = int(row["food_total_cents"])
        final_cents = int(row["final_total_cents"])
        food_list_cents += food_cents
        gross_list_cents += food_cents
        paid_total_cents += final_cents
        session_rows.append(
            {
                "table_no": row["table_no"] or "候位",
                "mode_label": "只結餐飲",
                "end_time": row["end_time"],
                "table_fee_cents": 0,
                "food_fee_cents": food_cents,
                "discount_name": "原價",
                "discount_label": "原價",
                "discount_scope_label": "無",
                "final_total_cents": final_cents,
            }
        )
    session_rows.sort(key=lambda row: row["end_time"], reverse=True)

    traffic_rows = db.execute(
        """SELECT start_time FROM sessions
           WHERE datetime(start_time) >= datetime(?) AND datetime(start_time) < datetime(?)
           ORDER BY datetime(start_time)""",
        (start_iso, end_iso),
    ).fetchall()
    slot_count = int((end_dt - start_dt).total_seconds() // 3600)
    traffic_data = [0] * slot_count
    for row in traffic_rows:
        started = datetime.fromisoformat(row["start_time"])
        index = int((started - start_dt).total_seconds() // 3600)
        if 0 <= index < slot_count:
            traffic_data[index] += 1
    traffic_points = []
    for index, value in enumerate(traffic_data):
        slot = start_dt + timedelta(hours=index)
        label = "24" if slot.date() > selected_date and slot.hour == 0 else f"{slot:%H}"
        traffic_points.append({"label": label, "value": value})
    max_traffic = max(traffic_data, default=0)

    return render_template(
        "stats.html",
        selected_date=selected_date.isoformat(),
        table_list_cents=table_list_cents,
        food_list_cents=food_list_cents,
        gross_list_cents=gross_list_cents,
        paid_total_cents=paid_total_cents,
        traffic_data=traffic_data,
        traffic_points=traffic_points,
        max_traffic=max_traffic,
        closed_sessions=session_rows,
    )
