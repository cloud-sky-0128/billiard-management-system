from __future__ import annotations

import calendar
import csv
import io
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from ..audit import record_audit
from ..auth import admin_is_authenticated, admin_login_redirect, admin_password_hash
from ..db import get_db
from ..money import format_cents, to_whole_cents
from ..operations import commit_operation
from ..services.scheduling import supported_date
from ..services.business_day import (
    business_day_bounds,
    business_day_for_timestamp,
    current_business_day,
)

bp = Blueprint("finance", __name__)

EXPENSE_CATEGORIES = ("進貨", "薪資", "設備", "水電", "租金", "其他")


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + text
    return text


@bp.before_request
def require_admin_access():
    if not admin_password_hash() or not admin_is_authenticated():
        return admin_login_redirect()
    return None


def selected_month() -> tuple[date, date]:
    today = current_business_day()
    try:
        month = supported_date(request.args.get("month", today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month, following


def month_summary(month: date, following: date) -> dict:
    db = get_db()
    range_start = business_day_bounds(month)[0].isoformat(timespec="seconds")
    range_end = business_day_bounds(following)[0].isoformat(timespec="seconds")
    system_rows = db.execute(
        """SELECT recorded_at, amount_cents
           FROM (
               SELECT paid_at AS recorded_at, amount_cents
               FROM payments
               WHERE datetime(paid_at) >= datetime(?) AND datetime(paid_at) < datetime(?)
               UNION ALL
               SELECT closed_at AS recorded_at, final_total_cents AS amount_cents
               FROM customer_tabs t
               WHERE status = 'closed'
                 AND datetime(closed_at) >= datetime(?) AND datetime(closed_at) < datetime(?)
                 AND NOT EXISTS (
                     SELECT 1 FROM payments p WHERE p.customer_tab_id = t.id
                 )
           )
           ORDER BY recorded_at""",
        (range_start, range_end, range_start, range_end),
    ).fetchall()
    cash_rows = db.execute(
        """SELECT record_date, actual_revenue_cents, note FROM daily_cash_records
           WHERE record_date >= ? AND record_date < ?""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()
    expense_rows = db.execute(
        """SELECT id, expense_date, category, description, amount_cents FROM expenses
           WHERE expense_date >= ? AND expense_date < ?
           ORDER BY expense_date, id""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()

    system: dict[str, int] = {}
    for row in system_rows:
        business_day = business_day_for_timestamp(
            datetime.fromisoformat(row["recorded_at"])
        )
        if month <= business_day < following:
            key = business_day.isoformat()
            system[key] = system.get(key, 0) + int(row["amount_cents"] or 0)
    cash = {row["record_date"]: row for row in cash_rows}
    expenses: dict[str, list] = {}
    for row in expense_rows:
        expenses.setdefault(row["expense_date"], []).append(row)

    rows = []
    days = calendar.monthrange(month.year, month.month)[1]
    for day_number in range(1, days + 1):
        day = date(month.year, month.month, day_number).isoformat()
        cash_row = cash.get(day)
        actual_cents = (
            int(cash_row["actual_revenue_cents"])
            if cash_row and cash_row["actual_revenue_cents"] is not None
            else None
        )
        day_expenses = expenses.get(day, [])
        expense_total_cents = sum(int(item["amount_cents"]) for item in day_expenses)
        rows.append(
            {
                "date": day,
                "system_revenue_cents": system.get(day, 0),
                "actual_revenue_cents": actual_cents,
                "cash_note": cash_row["note"] if cash_row else "",
                "expenses": day_expenses,
                "expense_total_cents": expense_total_cents,
                "net_cents": (
                    actual_cents - expense_total_cents
                    if actual_cents is not None
                    else None
                ),
            }
        )

    actual_total_cents = sum(
        row["actual_revenue_cents"]
        for row in rows
        if row["actual_revenue_cents"] is not None
    )
    expense_total_cents = sum(row["expense_total_cents"] for row in rows)
    missing_actual_days = sum(
        1 for row in rows
        if row["actual_revenue_cents"] is None
        and (row["system_revenue_cents"] > 0 or row["expense_total_cents"] > 0)
    )
    return {
        "rows": rows,
        "system_total_cents": sum(row["system_revenue_cents"] for row in rows),
        "actual_total_cents": actual_total_cents,
        "expense_total_cents": expense_total_cents,
        "net_total_cents": actual_total_cents - expense_total_cents,
        "missing_actual_days": missing_actual_days,
    }


@bp.route("/finance")
def finance_page():
    month, following = selected_month()
    summary = month_summary(month, following)
    try:
        selected_day = supported_date(
            request.args.get("date", current_business_day().isoformat())
        )
    except ValueError:
        selected_day = month
    if not month <= selected_day < following:
        selected_day = month
    selected_date = selected_day.isoformat()
    selected_row = next(row for row in summary["rows"] if row["date"] == selected_date)
    return render_template(
        "finance.html",
        month=month,
        selected_date=selected_date,
        selected_row=selected_row,
        expense_categories=EXPENSE_CATEGORIES,
        **summary,
    )


@bp.route("/finance/cash", methods=["POST"])
def save_actual_revenue():
    record_date = request.form.get("record_date", "")
    note = request.form.get("note", "").strip()
    try:
        day = supported_date(record_date)
        amount_cents = to_whole_cents(request.form["actual_revenue"])
        if amount_cents < 0:
            raise ValueError("實收不可小於 0。")
    except (KeyError, ValueError) as exc:
        flash(f"實收資料無效：{exc}", "error")
        return redirect(url_for(".finance_page"))
    db = get_db()
    previous = db.execute(
        "SELECT actual_revenue_cents, note FROM daily_cash_records WHERE record_date = ?",
        (day.isoformat(),),
    ).fetchone()
    db.execute(
        """INSERT INTO daily_cash_records
           (record_date, actual_revenue_cents, note, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(record_date) DO UPDATE SET
             actual_revenue_cents = excluded.actual_revenue_cents,
             note = excluded.note,
             updated_at = CURRENT_TIMESTAMP""",
        (day.isoformat(), amount_cents, note),
    )
    if previous is None or int(previous["actual_revenue_cents"] or 0) != amount_cents or previous["note"] != note:
        before = "未登記" if previous is None else f"{format_cents(previous['actual_revenue_cents'])} 元"
        after = f"{format_cents(amount_cents)} 元"
        if previous is not None and previous["note"] != note:
            after += "；備註已修改"
        record_audit(db, "cash_update", day.isoformat(), before, after)
    db.commit()
    flash("當日實收已儲存。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/expenses", methods=["POST"])
def add_expense():
    expense_date = request.form.get("expense_date", "")
    category = request.form.get("category", "其他")
    description = request.form.get("description", "").strip()
    try:
        day = supported_date(expense_date)
        amount_cents = to_whole_cents(request.form["amount"])
        if amount_cents <= 0:
            raise ValueError("支出金額必須大於 0。")
        if category not in EXPENSE_CATEGORIES:
            raise ValueError("支出分類無效。")
    except (KeyError, ValueError) as exc:
        flash(f"支出資料無效：{exc}", "error")
        return redirect(url_for(".finance_page"))
    db = get_db()
    db.execute(
        """INSERT INTO expenses
           (expense_date, category, description, amount_cents)
           VALUES (?, ?, ?, ?)""",
        (day.isoformat(), category, description, amount_cents),
    )
    commit_operation(db)
    flash("支出已新增。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id: int):
    db = get_db()
    row = db.execute(
        "SELECT expense_date, category, amount_cents FROM expenses WHERE id = ?",
        (expense_id,),
    ).fetchone()
    if not row:
        flash("找不到支出資料。", "error")
        return redirect(url_for(".finance_page"))
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    record_audit(
        db, "expense_delete", str(expense_id),
        f"{row['expense_date']}；{row['category']}；{format_cents(row['amount_cents'])} 元",
        "已刪除",
    )
    db.commit()
    day = supported_date(row["expense_date"])
    flash("支出已刪除。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/export")
def export_finance():
    month, following = selected_month()
    summary = month_summary(month, following)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["營業日", "系統營收", "實際實收", "支出", "支出備註", "收支淨額"])
    for row in summary["rows"]:
        notes = "；".join(
            f"{item['category']}：{item['description'] or '無備註'} "
            f"({format_cents(item['amount_cents'])})"
            for item in row["expenses"]
        )
        writer.writerow(
            [
                csv_safe(row["date"]), format_cents(row["system_revenue_cents"]),
                "" if row["actual_revenue_cents"] is None
                else format_cents(row["actual_revenue_cents"]),
                format_cents(row["expense_total_cents"]), csv_safe(notes),
                "" if row["net_cents"] is None else format_cents(row["net_cents"]),
            ]
        )
    writer.writerow([])
    writer.writerow([
        "月總計", format_cents(summary["system_total_cents"]),
        format_cents(summary["actual_total_cents"]),
        format_cents(summary["expense_total_cents"]), "",
        format_cents(summary["net_total_cents"]),
    ])
    filename = f"finance-{month.strftime('%Y-%m')}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/finance/export/expenses")
def export_expenses():
    month, following = selected_month()
    rows = get_db().execute(
        """SELECT expense_date, category, description, amount_cents
           FROM expenses WHERE expense_date >= ? AND expense_date < ?
           ORDER BY expense_date, id""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["營業日", "分類", "備註", "金額"])
    for row in rows:
        writer.writerow([
            csv_safe(row["expense_date"]), csv_safe(row["category"]), csv_safe(row["description"]),
            format_cents(row["amount_cents"]),
        ])
    writer.writerow([])
    writer.writerow(["月支出總計", "", "", format_cents(sum(int(row["amount_cents"]) for row in rows))])
    filename = f"expenses-{month.strftime('%Y-%m')}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
