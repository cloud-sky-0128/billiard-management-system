import sqlite3
import math
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..config import (
    DISCOUNT_MODE_ALL,
    DISCOUNT_MODE_PACKAGE,
    DISCOUNT_MODE_TIMED,
    DISCOUNT_PRICING_PACKAGE_HOURLY,
    DISCOUNT_PRICING_PERCENTAGE,
    DISCOUNT_SCOPE_ALL,
    DISCOUNT_SCOPE_TABLE_AND_DRINK,
    DISCOUNT_SCOPE_TABLE_ONLY,
)
from ..db import get_db, get_setting_cents
from ..money import format_cents, to_cents
from ..services.billing import table_count_value

bp = Blueprint("settings", __name__)
RESET_CONFIRMATION = "永久刪除測試營運資料"


@bp.route("/settings")
def general_settings_page():
    db = get_db()
    counts = {
        "closed_sessions": db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'closed'"
        ).fetchone()[0],
        "active_sessions": db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
        ).fetchone()[0],
        "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "cash_records": db.execute("SELECT COUNT(*) FROM daily_cash_records").fetchone()[0],
        "expenses": db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],
        "reservations": db.execute(
            "SELECT COUNT(*) FROM reservations WHERE status = 'active'"
        ).fetchone()[0],
        "calendar_items": db.execute("SELECT COUNT(*) FROM calendar_items").fetchone()[0],
        "shifts": db.execute("SELECT COUNT(*) FROM shifts").fetchone()[0],
        "employees": db.execute(
            "SELECT COUNT(*) FROM employees WHERE is_active = 1"
        ).fetchone()[0],
        "shift_types": db.execute(
            "SELECT COUNT(*) FROM shift_types WHERE is_active = 1"
        ).fetchone()[0],
        "menu_items": db.execute(
            "SELECT COUNT(*) FROM menu_items WHERE is_active = 1"
        ).fetchone()[0],
    }
    return render_template(
        "settings.html",
        counts=counts,
        confirmation_phrase=RESET_CONFIRMATION,
        today=date.today().isoformat(),
    )


def backup_database() -> Path | None:
    database = current_app.config["DATABASE"]
    if database == ":memory:" or not current_app.config.get("BACKUP_ON_RESET", True):
        return None
    database_path = Path(database)
    backup_directory = database_path.parent / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_directory / f"billiard-before-reset-{timestamp}.db"
    destination = sqlite3.connect(backup_path)
    try:
        get_db().backup(destination)
    finally:
        destination.close()
    return backup_path


@bp.route("/settings/reset-test-data", methods=["POST"])
def reset_test_data():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM sessions WHERE status = 'active'").fetchone()[0]:
        flash("仍有球檯開台中，必須全部結帳後才能清除測試資料。", "error")
        return redirect(url_for(".general_settings_page"))
    if request.form.get("confirm_scope") != "yes":
        flash("請先勾選資料範圍確認。", "error")
        return redirect(url_for(".general_settings_page"))
    if request.form.get("confirmation", "").strip() != RESET_CONFIRMATION:
        flash("確認文字不正確，未刪除任何資料。", "error")
        return redirect(url_for(".general_settings_page"))
    if request.form.get("confirmation_date", "") != date.today().isoformat():
        flash("確認日期必須是今天，未刪除任何資料。", "error")
        return redirect(url_for(".general_settings_page"))

    try:
        db.commit()
        backup_path = backup_database()
        db.execute("BEGIN")
        db.execute("DELETE FROM orders")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM expenses")
        db.execute("DELETE FROM daily_cash_records")
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM calendar_items")
        db.execute("DELETE FROM shifts")
        db.execute(
            """DELETE FROM sqlite_sequence WHERE name IN (
               'orders', 'sessions', 'expenses', 'reservations', 'calendar_items', 'shifts'
               )"""
        )
        db.commit()
    except (OSError, sqlite3.Error) as exc:
        db.rollback()
        flash(f"資料清除失敗，未完成操作：{exc}", "error")
        return redirect(url_for(".general_settings_page"))

    message = "營收、訂單、實收、支出、預約、行事曆事項與班表測試資料已清除。"
    if backup_path:
        message += f" 刪除前備份：{backup_path}"
    flash(message, "success")
    return redirect(url_for(".general_settings_page"))


@bp.route("/settings/rates")
def rate_settings_page():
    db = get_db()
    table_count = table_count_value()
    return render_template(
        "rate_settings.html",
        table_count=table_count,
        table_rates=db.execute(
            "SELECT * FROM table_rates WHERE table_no <= ? ORDER BY table_no",
            (table_count,),
        ).fetchall(),
        discount_types=db.execute(
            "SELECT * FROM discount_types WHERE is_active = 1 ORDER BY name, id"
        ).fetchall(),
        discount_scope_options=[
            (DISCOUNT_SCOPE_TABLE_ONLY, "只折球檯"),
            (DISCOUNT_SCOPE_TABLE_AND_DRINK, "球檯+飲料"),
            (DISCOUNT_SCOPE_ALL, "全部（球檯+餐點+飲料）"),
        ],
        discount_pricing_options=[
            (DISCOUNT_PRICING_PERCENTAGE, "百分比折扣"),
            (DISCOUNT_PRICING_PACKAGE_HOURLY, "固定包台價"),
        ],
        discount_mode_options=[
            (DISCOUNT_MODE_ALL, "計時與包台皆可"),
            (DISCOUNT_MODE_TIMED, "只限計時"),
            (DISCOUNT_MODE_PACKAGE, "只限包台"),
        ],
    )


@bp.route("/settings/rates", methods=["POST"])
def update_rate_settings():
    try:
        table_count = int(request.form.get("table_count", "10"))
    except ValueError:
        flash("設定值格式錯誤，請確認數字欄位。", "error")
        return redirect(url_for(".rate_settings_page"))

    if not (1 <= table_count <= 100):
        flash("球檯數量需介於 1 到 100。", "error")
        return redirect(url_for(".rate_settings_page"))
    db = get_db()
    hidden_active_tables = db.execute(
        """SELECT table_no FROM sessions
           WHERE status = 'active' AND table_no > ? ORDER BY table_no""",
        (table_count,),
    ).fetchall()
    if hidden_active_tables:
        table_labels = "、".join(str(row["table_no"]) for row in hidden_active_tables)
        flash(f"無法縮減球檯數，請先結帳仍在使用的桌號：{table_labels}。", "error")
        return redirect(url_for(".rate_settings_page"))
    existing_rates = {
        int(row["table_no"]): row
        for row in db.execute("SELECT * FROM table_rates").fetchall()
    }
    rows: list[tuple[int, int, int, int]] = []
    try:
        for table_no in range(1, table_count + 1):
            existing = existing_rates.get(table_no)
            default_timed = (
                int(existing["timed_rate_per_min_cents"])
                if existing else get_setting_cents("timed_rate_group", "3.0")
            )
            default_package = (
                int(existing["package_rate_per_hour_cents"])
                if existing else get_setting_cents("package_hour_rate", "150")
            )
            timed_rate_cents = to_cents(
                request.form.get(
                    f"timed_rate_{table_no}", format_cents(default_timed)
                )
            )
            package_rate_cents = to_cents(
                request.form.get(
                    f"package_rate_{table_no}", format_cents(default_package)
                )
            )
            enabled_key = f"package_enabled_{table_no}"
            if enabled_key in request.form:
                package_enabled = request.form.getlist(enabled_key)[-1] == "1"
            elif existing:
                package_enabled = bool(existing["package_enabled"])
            else:
                package_enabled = True
            if (
                timed_rate_cents <= 0
                or package_rate_cents <= 0
            ):
                raise ValueError
            rows.append(
                (table_no, timed_rate_cents, package_rate_cents, int(package_enabled))
            )
    except ValueError:
        flash("每桌的計時與包台費率都必須是大於 0 的數字。", "error")
        return redirect(url_for(".rate_settings_page"))

    db.execute(
        """INSERT INTO settings (key, value) VALUES ('table_count', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (str(table_count),),
    )
    db.executemany(
        """INSERT INTO table_rates
           (table_no, timed_rate_per_min_cents, package_rate_per_hour_cents,
            package_enabled, updated_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(table_no) DO UPDATE SET
               timed_rate_per_min_cents = excluded.timed_rate_per_min_cents,
               package_rate_per_hour_cents = excluded.package_rate_per_hour_cents,
               package_enabled = excluded.package_enabled,
               updated_at = CURRENT_TIMESTAMP""",
        rows,
    )
    db.commit()
    flash("球檯數量與每桌費率已更新。新增桌號會先套用預設費率，可再調整。", "success")
    return redirect(url_for(".rate_settings_page"))


def discount_form_data() -> dict:
    name = request.form.get("name", "").strip()
    pricing_method = request.form.get("pricing_method", DISCOUNT_PRICING_PERCENTAGE)
    applicable_mode = request.form.get("applicable_mode", DISCOUNT_MODE_ALL)
    scope = request.form.get("discount_scope", DISCOUNT_SCOPE_TABLE_AND_DRINK)
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    if not name:
        raise ValueError("請填寫優惠名稱。")
    if pricing_method not in {
        DISCOUNT_PRICING_PERCENTAGE,
        DISCOUNT_PRICING_PACKAGE_HOURLY,
    }:
        raise ValueError("計價方式無效。")
    if applicable_mode not in {
        DISCOUNT_MODE_ALL,
        DISCOUNT_MODE_TIMED,
        DISCOUNT_MODE_PACKAGE,
    }:
        raise ValueError("適用模式無效。")
    if scope not in {
        DISCOUNT_SCOPE_TABLE_ONLY,
        DISCOUNT_SCOPE_TABLE_AND_DRINK,
        DISCOUNT_SCOPE_ALL,
    }:
        raise ValueError("折扣套用範圍無效。")

    package_rate_per_hour_cents = None
    if pricing_method == DISCOUNT_PRICING_PACKAGE_HOURLY:
        try:
            package_rate_per_hour_cents = to_cents(
                request.form.get("package_rate_per_hour", "")
            )
        except ValueError as exc:
            raise ValueError("包台優惠價格式錯誤。") from exc
        if package_rate_per_hour_cents <= 0:
            raise ValueError("包台每小時優惠價必須大於 0。")
        discount_percent = 100.0
        applicable_mode = DISCOUNT_MODE_PACKAGE
        scope = DISCOUNT_SCOPE_TABLE_ONLY
    else:
        try:
            discount_percent = float(request.form.get("discount_percent", "100"))
        except ValueError as exc:
            raise ValueError("折數格式錯誤。") from exc
        if not math.isfinite(discount_percent) or not 0 < discount_percent <= 100:
            raise ValueError("折數百分比需大於 0 且不超過 100。")
    if bool(start_time) != bool(end_time):
        raise ValueError("有效時段的開始與結束必須一起填寫。")
    for value in (start_time, end_time):
        if value:
            datetime.strptime(value, "%H:%M")
    if start_time == end_time and start_time:
        raise ValueError("有效時段的開始與結束不可相同。")
    return {
        "name": name,
        "pricing_method": pricing_method,
        "discount_percent": discount_percent,
        "package_rate_per_hour_cents": package_rate_per_hour_cents,
        "applicable_mode": applicable_mode,
        "discount_scope": scope,
        "start_time": start_time,
        "end_time": end_time,
    }


@bp.route("/settings/discounts/add", methods=["POST"])
def add_discount_type():
    try:
        values = discount_form_data()
    except ValueError as exc:
        flash(f"優惠方案無效：{exc}", "error")
        return redirect(url_for(".rate_settings_page"))

    db = get_db()
    existing = db.execute(
        "SELECT id, is_active FROM discount_types WHERE name = ?", (values["name"],)
    ).fetchone()
    if existing and existing["is_active"]:
        flash("優惠名稱已存在。", "error")
    elif existing:
        db.execute(
            """UPDATE discount_types SET pricing_method = ?, discount_percent = ?,
               package_rate_per_hour_cents = ?, applicable_mode = ?, discount_scope = ?,
               start_time = ?, end_time = ?, is_active = 1 WHERE id = ?""",
            (
                values["pricing_method"], values["discount_percent"],
                values["package_rate_per_hour_cents"], values["applicable_mode"],
                values["discount_scope"], values["start_time"], values["end_time"],
                existing["id"],
            ),
        )
        db.commit()
        flash(f"已重新啟用優惠：{values['name']}", "success")
    else:
        db.execute(
            """INSERT INTO discount_types
               (name, pricing_method, discount_percent, package_rate_per_hour_cents,
                applicable_mode, discount_scope, start_time, end_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                values["name"], values["pricing_method"], values["discount_percent"],
                values["package_rate_per_hour_cents"], values["applicable_mode"],
                values["discount_scope"], values["start_time"], values["end_time"],
            ),
        )
        db.commit()
        flash(f"已新增優惠：{values['name']}", "success")
    return redirect(url_for(".rate_settings_page"))


@bp.route("/settings/discounts/<int:discount_id>/update", methods=["POST"])
def update_discount_type(discount_id: int):
    try:
        values = discount_form_data()
    except ValueError as exc:
        flash(f"優惠方案無效：{exc}", "error")
        return redirect(url_for(".rate_settings_page"))

    db = get_db()
    duplicate = db.execute(
        "SELECT id FROM discount_types WHERE name = ? AND id != ?",
        (values["name"], discount_id),
    ).fetchone()
    if duplicate:
        flash("優惠名稱已被使用。", "error")
        return redirect(url_for(".rate_settings_page"))
    result = db.execute(
        """UPDATE discount_types SET name = ?, pricing_method = ?, discount_percent = ?,
           package_rate_per_hour_cents = ?, applicable_mode = ?, discount_scope = ?,
           start_time = ?, end_time = ? WHERE id = ? AND is_active = 1""",
        (
            values["name"], values["pricing_method"], values["discount_percent"],
            values["package_rate_per_hour_cents"], values["applicable_mode"],
            values["discount_scope"], values["start_time"], values["end_time"],
            discount_id,
        ),
    )
    db.commit()
    flash("優惠方案已更新。" if result.rowcount else "找不到優惠方案。", "success" if result.rowcount else "error")
    return redirect(url_for(".rate_settings_page"))


@bp.route("/settings/discounts/<int:discount_id>/delete", methods=["POST"])
def delete_discount_type(discount_id: int):
    db = get_db()
    row = db.execute(
        "SELECT name FROM discount_types WHERE id = ? AND is_active = 1", (discount_id,)
    ).fetchone()
    if not row:
        flash("找不到優惠方案。", "error")
    else:
        db.execute("UPDATE discount_types SET is_active = 0 WHERE id = ?", (discount_id,))
        db.commit()
        flash(f"已停用優惠：{row['name']}，歷史結帳紀錄會保留。", "success")
    return redirect(url_for(".rate_settings_page"))
