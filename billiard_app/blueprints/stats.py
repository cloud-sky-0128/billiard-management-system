from datetime import date, datetime, timedelta

from flask import Blueprint, flash, render_template, request

from ..config import DISCOUNT_SCOPE_TABLE_AND_DRINK
from ..db import get_db
from ..money import percentage_of_cents
from ..services.billing import discount_pricing_label, discount_scope_label

bp = Blueprint("stats", __name__)

@bp.route("/stats")
def stats_page():
    db = get_db()
    input_date = request.args.get("date", date.today().isoformat())

    try:
        selected_date = datetime.strptime(input_date, "%Y-%m-%d").date()
    except ValueError:
        selected_date = date.today()
        flash("日期格式錯誤，已切回今天。", "error")

    start_dt = datetime.combine(selected_date, datetime.min.time())
    end_dt = start_dt + timedelta(days=1)
    start_iso = start_dt.isoformat(timespec="seconds")
    end_iso = end_dt.isoformat(timespec="seconds")

    closed_rows = db.execute(
        """
        SELECT
            s.id,
            s.table_no,
            s.mode,
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
            SELECT session_id, SUM(subtotal_cents) AS food_total_cents
            FROM orders
            GROUP BY session_id
        ) o ON o.session_id = s.id
        WHERE s.status = 'closed' AND s.end_time >= ? AND s.end_time < ?
        ORDER BY s.end_time DESC
        """,
        (start_iso, end_iso),
    ).fetchall()

    table_revenue_cents = 0
    food_revenue_cents = 0
    gross_total_cents = 0
    net_total_cents = 0
    session_rows: list[dict] = []

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

        table_revenue_cents += table_fee_cents
        food_revenue_cents += food_fee_cents
        gross_total_cents += gross_cents
        net_total_cents += final_total_cents

        session_rows.append(
            {
                "table_no": row["table_no"],
                "mode_label": "包台" if row["mode"] == "package" else "計時",
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

    traffic_rows = db.execute(
        """
        SELECT CAST(strftime('%H', start_time) AS INTEGER) AS hour, COUNT(*) AS customer_count
        FROM sessions
        WHERE start_time >= ? AND start_time < ?
        GROUP BY hour
        ORDER BY hour
        """,
        (start_iso, end_iso),
    ).fetchall()

    traffic_map = {int(row["hour"]): int(row["customer_count"]) for row in traffic_rows}
    traffic_data = [traffic_map.get(hour, 0) for hour in range(24)]
    max_traffic = max(traffic_data) if traffic_data else 0

    return render_template(
        "stats.html",
        selected_date=selected_date.isoformat(),
        table_revenue_cents=table_revenue_cents,
        food_revenue_cents=food_revenue_cents,
        gross_total_cents=gross_total_cents,
        net_total_cents=net_total_cents,
        traffic_data=traffic_data,
        max_traffic=max_traffic,
        closed_sessions=session_rows,
    )
