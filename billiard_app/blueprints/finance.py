from __future__ import annotations

import calendar
import csv
import io
import math
from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

from ..db import get_db

bp = Blueprint("finance", __name__)

EXPENSE_CATEGORIES = ("進貨", "薪資", "設備", "水電", "租金", "其他")


def selected_month() -> tuple[date, date]:
    today = date.today()
    try:
        month = date.fromisoformat(request.args.get("month", today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month, following


def month_summary(month: date, following: date) -> dict:
    db = get_db()
    system_rows = db.execute(
        """SELECT SUBSTR(end_time, 1, 10) AS day,
                  SUM(CASE WHEN final_total > 0 THEN final_total
                           ELSE table_fee + food_fee - discount_amount END) AS total
           FROM sessions
           WHERE status = 'closed' AND end_time >= ? AND end_time < ?
           GROUP BY day""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()
    cash_rows = db.execute(
        """SELECT record_date, actual_revenue, note FROM daily_cash_records
           WHERE record_date >= ? AND record_date < ?""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()
    expense_rows = db.execute(
        """SELECT id, expense_date, category, description, amount FROM expenses
           WHERE expense_date >= ? AND expense_date < ?
           ORDER BY expense_date, id""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()

    system = {row["day"]: float(row["total"] or 0) for row in system_rows}
    cash = {row["record_date"]: row for row in cash_rows}
    expenses: dict[str, list] = {}
    for row in expense_rows:
        expenses.setdefault(row["expense_date"], []).append(row)

    rows = []
    days = calendar.monthrange(month.year, month.month)[1]
    for day_number in range(1, days + 1):
        day = date(month.year, month.month, day_number).isoformat()
        cash_row = cash.get(day)
        actual = float(cash_row["actual_revenue"]) if cash_row and cash_row["actual_revenue"] is not None else None
        day_expenses = expenses.get(day, [])
        expense_total = sum(float(item["amount"]) for item in day_expenses)
        rows.append(
            {
                "date": day,
                "system_revenue": system.get(day, 0.0),
                "actual_revenue": actual,
                "cash_note": cash_row["note"] if cash_row else "",
                "expenses": day_expenses,
                "expense_total": expense_total,
                "net": actual - expense_total if actual is not None else None,
            }
        )

    actual_total = sum(row["actual_revenue"] for row in rows if row["actual_revenue"] is not None)
    expense_total = sum(row["expense_total"] for row in rows)
    missing_actual_days = sum(
        1 for row in rows
        if row["actual_revenue"] is None and (row["system_revenue"] > 0 or row["expense_total"] > 0)
    )
    return {
        "rows": rows,
        "system_total": sum(row["system_revenue"] for row in rows),
        "actual_total": actual_total,
        "expense_total": expense_total,
        "net_total": actual_total - expense_total,
        "missing_actual_days": missing_actual_days,
    }


@bp.route("/finance")
def finance_page():
    month, following = selected_month()
    summary = month_summary(month, following)
    try:
        selected_day = date.fromisoformat(
            request.args.get("date", date.today().isoformat())
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
        day = date.fromisoformat(record_date)
        amount = float(request.form["actual_revenue"])
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("實收不可小於 0。")
    except (KeyError, ValueError) as exc:
        flash(f"實收資料無效：{exc}", "error")
        return redirect(url_for(".finance_page"))
    db = get_db()
    db.execute(
        """INSERT INTO daily_cash_records (record_date, actual_revenue, note, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(record_date) DO UPDATE SET
             actual_revenue = excluded.actual_revenue,
             note = excluded.note,
             updated_at = CURRENT_TIMESTAMP""",
        (day.isoformat(), amount, note),
    )
    db.commit()
    flash("當日實收已儲存。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/expenses", methods=["POST"])
def add_expense():
    expense_date = request.form.get("expense_date", "")
    category = request.form.get("category", "其他")
    description = request.form.get("description", "").strip()
    try:
        day = date.fromisoformat(expense_date)
        amount = float(request.form["amount"])
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("支出金額必須大於 0。")
        if category not in EXPENSE_CATEGORIES:
            raise ValueError("支出分類無效。")
    except (KeyError, ValueError) as exc:
        flash(f"支出資料無效：{exc}", "error")
        return redirect(url_for(".finance_page"))
    db = get_db()
    db.execute(
        "INSERT INTO expenses (expense_date, category, description, amount) VALUES (?, ?, ?, ?)",
        (day.isoformat(), category, description, amount),
    )
    db.commit()
    flash("支出已新增。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id: int):
    db = get_db()
    row = db.execute("SELECT expense_date FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row:
        flash("找不到支出資料。", "error")
        return redirect(url_for(".finance_page"))
    db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    db.commit()
    day = date.fromisoformat(row["expense_date"])
    flash("支出已刪除。", "success")
    return redirect(url_for(".finance_page", month=day.strftime("%Y-%m"), date=day.isoformat()))


@bp.route("/finance/export")
def export_finance():
    month, following = selected_month()
    summary = month_summary(month, following)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日期", "系統營收", "實際實收", "支出", "支出備註", "收支淨額"])
    for row in summary["rows"]:
        notes = "；".join(
            f"{item['category']}：{item['description'] or '無備註'} ({float(item['amount']):.0f})"
            for item in row["expenses"]
        )
        writer.writerow(
            [
                row["date"], f"{row['system_revenue']:.0f}",
                "" if row["actual_revenue"] is None else f"{row['actual_revenue']:.0f}",
                f"{row['expense_total']:.0f}", notes,
                "" if row["net"] is None else f"{row['net']:.0f}",
            ]
        )
    writer.writerow([])
    writer.writerow(["月總計", f"{summary['system_total']:.0f}", f"{summary['actual_total']:.0f}",
                     f"{summary['expense_total']:.0f}", "", f"{summary['net_total']:.0f}"])
    filename = f"finance-{month.strftime('%Y-%m')}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
