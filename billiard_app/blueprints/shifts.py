from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import EMPLOYEE_COLOR_PALETTE
from ..db import get_db
from ..services.scheduling import schedule_redirect, schedule_selection

bp = Blueprint("shifts", __name__)
COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")


def parse_shift_time(day: date, field: str) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{request.form[field]}")


def validate_time_range(start: str, end: str, ends_next_day: bool) -> None:
    start_time = datetime.strptime(start, "%H:%M")
    end_time = datetime.strptime(end, "%H:%M")
    if ends_next_day and end_time >= start_time:
        raise ValueError("選擇隔天結束時，結束時間必須早於開始時間。")
    if not ends_next_day and end_time <= start_time:
        raise ValueError("結束時間必須晚於開始時間。")


def active_shift_types():
    return get_db().execute(
        """SELECT id, name, start_time, end_time, ends_next_day, sort_order
           FROM shift_types WHERE is_active = 1 ORDER BY sort_order, id"""
    ).fetchall()


def employee_color(value: str) -> str:
    if not COLOR_PATTERN.fullmatch(value):
        raise ValueError("請選擇有效的員工顏色。")
    return value.upper()


def next_employee_color() -> str:
    count = get_db().execute("SELECT COUNT(*) AS count FROM employees").fetchone()["count"]
    return EMPLOYEE_COLOR_PALETTE[count % len(EMPLOYEE_COLOR_PALETTE)]


def next_shift_type_order() -> int:
    row = get_db().execute(
        "SELECT COALESCE(MAX(sort_order), 0) AS max_order FROM shift_types WHERE is_active = 1"
    ).fetchone()
    return row["max_order"] + 1


@bp.route("/shifts")
def shifts_page():
    month, selected, previous, following, weeks = schedule_selection()
    db = get_db()
    shifts = db.execute(
        """SELECT s.*, COALESCE(NULLIF(s.shift_type_name, ''), t.name, s.shift_type) AS shift_type_label,
                  COALESCE(e.color, '#C96A4A') AS staff_color
           FROM shifts s
           LEFT JOIN shift_types t ON t.id = s.shift_type_id
           LEFT JOIN employees e ON e.id = s.employee_id
           WHERE s.start_time >= ? AND s.start_time < ?
           ORDER BY s.start_time, s.staff_name""",
        (month.isoformat(), following.isoformat()),
    ).fetchall()
    employees = db.execute(
        "SELECT id, name, color FROM employees WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    shift_types = active_shift_types()
    entries_by_day: dict[str, list[dict]] = {}
    for row in shifts:
        day = row["start_time"][:10]
        entries_by_day.setdefault(day, []).append(
            {
                "time": f"{row['start_time'][11:16]}–{row['end_time'][11:16]}",
                "name": row["staff_name"],
                "type": row["shift_type_label"],
                "color": row["staff_color"],
            }
        )
    return render_template(
        "shifts.html",
        month=month,
        selected=selected,
        previous=previous,
        following=following,
        weeks=weeks,
        calendar_entries=entries_by_day,
        shifts=[row for row in shifts if row["start_time"][:10] == selected.isoformat()],
        export_shifts=[dict(row) for row in shifts],
        employees=employees,
        shift_types=shift_types,
        shift_type_options=[dict(row) for row in shift_types],
    )


@bp.route("/shifts/settings")
def shift_settings_page():
    db = get_db()
    employees = db.execute(
        "SELECT id, name, color FROM employees WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    return render_template(
        "shift_settings.html",
        employees=employees,
        shift_types=active_shift_types(),
        default_employee_color=next_employee_color(),
    )


@bp.route("/shifts/types/add", methods=["POST"])
def add_shift_type():
    name = request.form.get("name", "").strip()
    start = request.form.get("start_time", "")
    end = request.form.get("end_time", "")
    ends_next_day = request.form.get("ends_next_day") == "1"
    try:
        if not name:
            raise ValueError("班別名稱不可空白。")
        validate_time_range(start, end, ends_next_day)
    except ValueError as exc:
        flash(f"班別資料無效：{exc}", "error")
        return redirect(url_for(".shift_settings_page"))

    db = get_db()
    existing = db.execute("SELECT id, is_active FROM shift_types WHERE name = ?", (name,)).fetchone()
    if existing and existing["is_active"]:
        flash("班別名稱已存在。", "error")
    elif existing:
        sort_order = next_shift_type_order()
        db.execute(
            """UPDATE shift_types SET start_time = ?, end_time = ?, ends_next_day = ?,
               sort_order = ?, is_active = 1 WHERE id = ?""",
            (start, end, int(ends_next_day), sort_order, existing["id"]),
        )
        db.commit()
        flash(f"已重新啟用班別：{name}", "success")
    else:
        sort_order = next_shift_type_order()
        db.execute(
            """INSERT INTO shift_types (name, start_time, end_time, ends_next_day, sort_order)
               VALUES (?, ?, ?, ?, ?)""",
            (name, start, end, int(ends_next_day), sort_order),
        )
        db.commit()
        flash(f"已新增班別：{name}", "success")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/types/<int:shift_type_id>/update", methods=["POST"])
def update_shift_type(shift_type_id: int):
    name = request.form.get("name", "").strip()
    start = request.form.get("start_time", "")
    end = request.form.get("end_time", "")
    ends_next_day = request.form.get("ends_next_day") == "1"
    try:
        if not name:
            raise ValueError("班別名稱不可空白。")
        validate_time_range(start, end, ends_next_day)
    except ValueError as exc:
        flash(f"班別資料無效：{exc}", "error")
        return redirect(url_for(".shift_settings_page"))

    db = get_db()
    duplicate = db.execute(
        "SELECT id FROM shift_types WHERE name = ? AND id != ?", (name, shift_type_id)
    ).fetchone()
    if duplicate:
        flash("班別名稱已被使用。", "error")
        return redirect(url_for(".shift_settings_page"))
    result = db.execute(
        """UPDATE shift_types SET name = ?, start_time = ?, end_time = ?, ends_next_day = ?
           WHERE id = ? AND is_active = 1""",
        (name, start, end, int(ends_next_day), shift_type_id),
    )
    db.commit()
    flash("班別已更新。" if result.rowcount else "找不到班別。", "success" if result.rowcount else "error")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/types/<int:shift_type_id>/delete", methods=["POST"])
def delete_shift_type(shift_type_id: int):
    db = get_db()
    row = db.execute(
        "SELECT name FROM shift_types WHERE id = ? AND is_active = 1", (shift_type_id,)
    ).fetchone()
    if not row:
        flash("找不到班別。", "error")
    else:
        db.execute("UPDATE shift_types SET is_active = 0 WHERE id = ?", (shift_type_id,))
        db.commit()
        flash(f"已刪除班別：{row['name']}，既有班表會保留。", "success")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/types/<int:shift_type_id>/move", methods=["POST"])
def move_shift_type(shift_type_id: int):
    direction = request.form.get("direction")
    if direction not in {"up", "down"}:
        flash("班別移動方向無效。", "error")
        return redirect(url_for(".shift_settings_page"))

    db = get_db()
    rows = db.execute(
        "SELECT id FROM shift_types WHERE is_active = 1 ORDER BY sort_order, id"
    ).fetchall()
    ids = [row["id"] for row in rows]
    if shift_type_id not in ids:
        flash("找不到班別。", "error")
        return redirect(url_for(".shift_settings_page"))

    current_index = ids.index(shift_type_id)
    target_index = current_index - 1 if direction == "up" else current_index + 1
    if target_index < 0 or target_index >= len(ids):
        flash("班別已在最前或最後。", "error")
        return redirect(url_for(".shift_settings_page"))

    ids[current_index], ids[target_index] = ids[target_index], ids[current_index]
    db.executemany(
        "UPDATE shift_types SET sort_order = ? WHERE id = ?",
        [(index, item_id) for index, item_id in enumerate(ids, start=1)],
    )
    db.commit()
    flash("班別顯示順序已更新。", "success")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/employees/add", methods=["POST"])
def add_employee():
    name = request.form.get("name", "").strip()
    try:
        if not name:
            raise ValueError("員工姓名不可空白。")
        color = employee_color(request.form.get("color", next_employee_color()))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".shift_settings_page"))
    db = get_db()
    existing = db.execute("SELECT * FROM employees WHERE name = ?", (name,)).fetchone()
    if existing:
        if existing["is_active"]:
            flash("員工已在清單中。", "error")
        else:
            db.execute(
                "UPDATE employees SET color = ?, is_active = 1 WHERE id = ?",
                (color, existing["id"]),
            )
            db.commit()
            flash(f"已重新啟用員工：{name}", "success")
    else:
        db.execute("INSERT INTO employees (name, color) VALUES (?, ?)", (name, color))
        db.commit()
        flash(f"已新增員工：{name}", "success")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/employees/<int:employee_id>/color", methods=["POST"])
def update_employee_color(employee_id: int):
    try:
        color = employee_color(request.form.get("color", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".shift_settings_page"))

    db = get_db()
    result = db.execute(
        "UPDATE employees SET color = ? WHERE id = ? AND is_active = 1",
        (color, employee_id),
    )
    db.commit()
    flash(
        "員工顏色已更新。" if result.rowcount else "找不到員工。",
        "success" if result.rowcount else "error",
    )
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/employees/<int:employee_id>/delete", methods=["POST"])
def delete_employee(employee_id: int):
    db = get_db()
    employee = db.execute(
        "SELECT name FROM employees WHERE id = ? AND is_active = 1", (employee_id,)
    ).fetchone()
    if not employee:
        flash("找不到員工。", "error")
    else:
        db.execute("UPDATE employees SET is_active = 0 WHERE id = ?", (employee_id,))
        db.commit()
        flash(f"已從選單移除員工：{employee['name']}，既有班表會保留。", "success")
    return redirect(url_for(".shift_settings_page"))


@bp.route("/shifts/add", methods=["POST"])
@bp.route("/shifts/<int:shift_id>/update", methods=["POST"])
def save_shift(shift_id: int | None = None):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        flash("班表目前忙碌，請稍後重試。", "error")
        return schedule_redirect("shifts.shifts_page")
    if shift_id is not None and not db.execute(
        "SELECT id FROM shifts WHERE id = ?", (shift_id,)
    ).fetchone():
        db.rollback()
        flash("找不到班次。", "error")
        return schedule_redirect("shifts.shifts_page")

    try:
        employee_id = int(request.form["employee_id"])
        employee = db.execute(
            "SELECT id, name FROM employees WHERE id = ? AND is_active = 1", (employee_id,)
        ).fetchone()
        if not employee:
            raise ValueError("請選擇有效員工。")
        shift_type_id = int(request.form["shift_type_id"])
        shift_type = db.execute(
            """SELECT id, name, ends_next_day FROM shift_types
               WHERE id = ? AND is_active = 1""",
            (shift_type_id,),
        ).fetchone()
        if not shift_type:
            raise ValueError("請選擇有效班別。")
        raw_dates = [request.form["date"]] if shift_id is not None else request.form.getlist("dates")
        selected_dates = sorted({date.fromisoformat(value) for value in raw_dates})
        if not selected_dates or len(selected_dates) > 31:
            raise ValueError("請選擇 1 到 31 個排班日期。")
        note = request.form.get("note", "").strip()
        occurrences = []
        for day in selected_dates:
            start = parse_shift_time(day, "start_time")
            end = parse_shift_time(day, "end_time")
            if shift_type["ends_next_day"]:
                end += timedelta(days=1)
            duration = end - start
            if duration <= timedelta(0) or duration >= timedelta(hours=24):
                raise ValueError("班次時間必須大於 0 且少於 24 小時。")
            occurrences.append(
                (start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"))
            )
    except (KeyError, ValueError) as exc:
        db.rollback()
        flash(f"班表資料無效：{exc}", "error")
        return schedule_redirect("shifts.shifts_page")

    for start, end in occurrences:
        conflict = db.execute(
            """SELECT id FROM shifts
               WHERE employee_id = ? AND start_time < ? AND end_time > ? AND id != ?
               LIMIT 1""",
            (employee_id, end, start, shift_id or 0),
        ).fetchone()
        if conflict:
            db.rollback()
            flash(f"{employee['name']} 在 {start[:10]} 的時段已有班次，未建立任何資料。", "error")
            return schedule_redirect("shifts.shifts_page")

    if shift_id is None:
        db.executemany(
            """INSERT INTO shifts
               (employee_id, shift_type_id, staff_name, shift_type_name, shift_type,
                start_time, end_time, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    employee_id, shift_type_id, employee["name"], shift_type["name"],
                    shift_type["name"], start, end, note,
                )
                for start, end in occurrences
            ],
        )
    else:
        start, end = occurrences[0]
        db.execute(
            """UPDATE shifts SET employee_id = ?, shift_type_id = ?, staff_name = ?,
               shift_type_name = ?, shift_type = ?, start_time = ?, end_time = ?, note = ?
               WHERE id = ?""",
            (
                employee_id, shift_type_id, employee["name"], shift_type["name"],
                shift_type["name"], start, end, note, shift_id,
            ),
        )
    db.commit()
    flash(f"班表已儲存，共 {len(occurrences)} 筆。", "success")
    return schedule_redirect("shifts.shifts_page")


@bp.route("/shifts/<int:shift_id>/delete", methods=["POST"])
def delete_shift(shift_id: int):
    db = get_db()
    db.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
    db.commit()
    flash("班次已刪除。", "success")
    return schedule_redirect("shifts.shifts_page")
