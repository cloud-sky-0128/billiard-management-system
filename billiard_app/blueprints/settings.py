import sqlite3
import math
import os
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..audit import AUDIT_ACTION_LABELS, record_audit
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
from ..auth import ADMIN_PASSWORD_KEY, admin_is_authenticated, admin_password_hash, safe_admin_next, start_admin_session
from ..db import get_db, get_setting_cents, set_setting
from ..money import format_cents, to_cents
from ..maintenance import backup_directory, backup_warning, create_backup, restore_drill
from ..services.billing import table_count_value

bp = Blueprint("settings", __name__)
RESET_CONFIRMATION = "永久刪除測試營運資料"


@bp.before_request
def require_admin_access():
    if request.endpoint == "settings.admin_access":
        return None
    if not admin_password_hash() or not admin_is_authenticated():
        next_url = request.full_path.rstrip("?")
        return redirect(url_for(".admin_access", next=next_url))
    return None


@bp.route("/settings/admin", methods=["GET", "POST"])
def admin_access():
    password_hash = admin_password_hash()
    setup_mode = not password_hash
    next_url = safe_admin_next(request.values.get("next"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if setup_mode:
            confirmation = request.form.get("password_confirmation", "")
            if len(password) < 8:
                flash("管理員密碼至少需要 8 個字元。", "error")
            elif password != confirmation:
                flash("兩次輸入的密碼不一致。", "error")
            else:
                set_setting(ADMIN_PASSWORD_KEY, generate_password_hash(password))
                start_admin_session()
                flash("管理員密碼已建立。", "success")
                return redirect(next_url)
        elif check_password_hash(password_hash, password):
            start_admin_session()
            return redirect(next_url)
        else:
            flash("管理員密碼不正確。", "error")
    elif password_hash and admin_is_authenticated():
        return redirect(next_url)

    return render_template("admin_access.html", setup_mode=setup_mode, next_url=next_url)


@bp.route("/settings/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_authenticated", None)
    flash("管理員已登出。", "success")
    return redirect(url_for(".admin_access"))


@bp.route("/settings/admin/password", methods=["POST"])
def update_admin_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirmation = request.form.get("new_password_confirmation", "")
    if not check_password_hash(admin_password_hash(), current_password):
        flash("目前的管理員密碼不正確。", "error")
    elif len(new_password) < 8:
        flash("新密碼至少需要 8 個字元。", "error")
    elif new_password != confirmation:
        flash("兩次輸入的新密碼不一致。", "error")
    else:
        set_setting(ADMIN_PASSWORD_KEY, generate_password_hash(new_password))
        start_admin_session()
        flash("管理員密碼已更新。", "success")
    return redirect(url_for(".general_settings_page"))


def backup_display_name(path: Path) -> str:
    name = path.stem
    if name.startswith("billiard-recent-"):
        value = name.removeprefix("billiard-recent-")
        try:
            parsed = datetime.strptime(value, "%Y%m%d-%H%M%S-%f")
            return f"15 分鐘快照 · {parsed:%Y-%m-%d %H:%M}"
        except ValueError:
            pass
    if name.startswith("billiard-daily-"):
        return f"每日備份 · {name.removeprefix('billiard-daily-')}"
    if name.startswith("billiard-monthly-"):
        month = name.removeprefix("billiard-monthly-")
        try:
            parsed = datetime.strptime(month, "%Y-%m")
            return f"每月備份 · {parsed:%Y 年 %m 月}"
        except ValueError:
            pass
    if name.startswith("billiard-manual-"):
        value = name.removeprefix("billiard-manual-")
        try:
            parsed = datetime.strptime(value, "%Y%m%d-%H%M%S-%f")
            return f"手動備份 · {parsed:%Y-%m-%d %H:%M}"
        except ValueError:
            pass
    if "before-reset" in name:
        return "清除資料前備份"
    if "migration" in name:
        return "資料升級前備份"
    return name


def file_size_label(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@bp.route("/settings")
def general_settings_page():
    db = get_db()
    database = current_app.config["DATABASE"]
    extra_backup = os.environ.get("BILLIARD_BACKUP_DIR", "")
    backup_options = []
    for path in backup_directory(database).glob("billiard-*.db"):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        backup_options.append({
            "name": path.name,
            "display_name": backup_display_name(path),
            "created_at": datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "size": file_size_label(info.st_size),
            "sort_time": info.st_mtime,
        })
    backup_options.sort(key=lambda item: item["sort_time"], reverse=True)
    backup_options = backup_options[:20]
    current_backup_warning = backup_warning(database, extra_backup)
    counts = {
        "open_tabs": db.execute(
            "SELECT COUNT(*) FROM customer_tabs WHERE status != 'closed'"
        ).fetchone()[0],
        "closed_tabs": db.execute(
            "SELECT COUNT(*) FROM customer_tabs WHERE status = 'closed'"
        ).fetchone()[0],
        "closed_sessions": db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'closed'"
        ).fetchone()[0],
        "active_sessions": db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'active'"
        ).fetchone()[0],
        "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "payments": db.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
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
        backup_options=backup_options,
        latest_backup=backup_options[0] if backup_options else None,
        backup_directory=backup_directory(database),
        extra_backup=extra_backup,
        backup_warning=current_backup_warning,
        log_file=current_app.config.get("LOG_FILE"),
    )


@bp.route("/settings/backups/create", methods=["POST"])
def create_manual_backup():
    try:
        path = create_backup(current_app.config["DATABASE"], daily=False)
    except Exception:
        current_app.logger.exception("Manual backup failed")
        flash("備份失敗；請檢查磁碟空間與錯誤日誌。", "error")
    else:
        flash(f"已建立並驗證備份：{path}", "success")
    return redirect(url_for(".general_settings_page"))


@bp.route("/settings/backups/restore-drill", methods=["POST"])
def run_restore_drill():
    name = request.form.get("backup_name", "")
    if not name or Path(name).name != name or not name.startswith("billiard-") or not name.endswith(".db"):
        flash("請選擇有效的備份檔。", "error")
        return redirect(url_for(".general_settings_page"))
    path = backup_directory(current_app.config["DATABASE"]) / name
    try:
        counts = restore_drill(path)
    except Exception:
        current_app.logger.exception("Restore drill failed: %s", path)
        flash("備份檢查失敗；請查看進階資訊中的錯誤日誌。正式資料未變更。", "error")
    else:
        flash(
            f"備份檢查完成：{name} 可以正常還原。已驗證 {len(counts)} 張核心資料表，正式資料未變更。",
            "success",
        )
    return redirect(url_for(".general_settings_page"))


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
    if db.execute(
        "SELECT COUNT(*) FROM customer_tabs WHERE status != 'closed'"
    ).fetchone()[0]:
        flash("仍有進行中的消費單，必須全部結帳後才能清除測試資料。", "error")
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
        reset_tables = (
            "orders", "payments", "sessions", "customer_tabs", "expenses",
            "daily_cash_records", "reservations", "calendar_items", "shifts",
        )
        counts_before = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in reset_tables
        }
        db.execute("DELETE FROM orders")
        db.execute("DELETE FROM payments")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM customer_tabs")
        db.execute("DELETE FROM expenses")
        db.execute("DELETE FROM daily_cash_records")
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM calendar_items")
        db.execute("DELETE FROM shifts")
        record_audit(
            db, "data_reset", "營運測試資料",
            "；".join(f"{table} {count}" for table, count in counts_before.items()),
            f"已清除；刪除前備份：{backup_path.name if backup_path else '未建立'}",
        )
        # Keep IDs monotonic so a stale form cannot target a newly created record.
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


@bp.route("/settings/audit")
def audit_log_page():
    events = get_db().execute(
        """SELECT id, action, target, before_value, after_value, created_at
           FROM audit_events ORDER BY id DESC LIMIT 200"""
    ).fetchall()
    return render_template(
        "audit_log.html", events=events, action_labels=AUDIT_ACTION_LABELS,
    )


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
    db.execute("BEGIN IMMEDIATE")
    hidden_active_tables = db.execute(
        """SELECT table_no FROM customer_tabs
           WHERE status IN ('assigned', 'playing') AND table_no > ?
           ORDER BY table_no""",
        (table_count,),
    ).fetchall()
    if hidden_active_tables:
        table_labels = "、".join(str(row["table_no"]) for row in hidden_active_tables)
        db.rollback()
        flash(f"無法縮減球檯數，請先結帳仍在使用的桌號：{table_labels}。", "error")
        return redirect(url_for(".rate_settings_page"))
    hidden_reservations = db.execute(
        """SELECT DISTINCT table_no FROM reservations
           WHERE status = 'active' AND table_no > ? ORDER BY table_no""",
        (table_count,),
    ).fetchall()
    if hidden_reservations:
        table_labels = "、".join(str(row["table_no"]) for row in hidden_reservations)
        db.rollback()
        flash(
            f"無法縮減球檯數，請先移桌、取消或完成這些桌號的預約：{table_labels}。",
            "error",
        )
        return redirect(url_for(".rate_settings_page"))
    existing_rates = {
        int(row["table_no"]): row
        for row in db.execute("SELECT * FROM table_rates").fetchall()
    }
    rows: list[tuple[int, str, int, int, int]] = []
    try:
        for table_no in range(1, table_count + 1):
            existing = existing_rates.get(table_no)
            display_name = request.form.get(
                f"display_name_{table_no}", existing["display_name"] if existing else ""
            ).strip()
            if len(display_name) > 40:
                raise ValueError("球檯名稱不可超過 40 個字。")
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
                (table_no, display_name, timed_rate_cents, package_rate_cents, int(package_enabled))
            )
    except ValueError as exc:
        flash(str(exc) if str(exc).startswith("球檯名稱") else "每桌的計時與包台費率都必須是大於 0 的數字。", "error")
        return redirect(url_for(".rate_settings_page"))

    db.execute(
        """INSERT INTO settings (key, value) VALUES ('table_count', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (str(table_count),),
    )
    db.executemany(
        """INSERT INTO table_rates
           (table_no, display_name, timed_rate_per_min_cents, package_rate_per_hour_cents,
            package_enabled, updated_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(table_no) DO UPDATE SET
               display_name = excluded.display_name,
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
