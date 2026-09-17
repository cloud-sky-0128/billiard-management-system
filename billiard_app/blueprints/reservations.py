from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..config import CALENDAR_ITEM_TYPES
from ..db import get_db
from ..services.billing import table_count_value
from ..services.scheduling import schedule_redirect, schedule_selection, schedule_times

bp = Blueprint("reservations", __name__)


def calendar_item_redirect(preferred_date: str | None = None):
    candidate = preferred_date or request.form.get("return_date", "")
    try:
        day = date.fromisoformat(candidate)
    except ValueError:
        day = date.today()
    return redirect(
        url_for(".calendar_page", month=day.strftime("%Y-%m"), date=day.isoformat())
    )


def calendar_item_form_data() -> tuple[str, str, str | None, str, str, str, str]:
    title = request.form.get("title", "").strip()
    item_type = request.form.get("item_type", "todo")
    scheduled_value = request.form.get("scheduled_date", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    if request.form.get("unscheduled") == "1":
        scheduled_value = ""
        start_time = ""
        end_time = ""
    location = request.form.get("location", "").strip()
    note = request.form.get("note", "").strip()

    if not title:
        raise ValueError("請填寫事項名稱。")
    if item_type not in CALENDAR_ITEM_TYPES:
        raise ValueError("事項類型無效。")
    scheduled_date = date.fromisoformat(scheduled_value).isoformat() if scheduled_value else None
    if bool(start_time) != bool(end_time):
        raise ValueError("開始與結束時間必須一起填寫。")
    if start_time:
        if not scheduled_date:
            raise ValueError("設定時間前必須先選擇日期。")
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
        if end <= start:
            raise ValueError("結束時間必須晚於開始時間。")
    return title, item_type, scheduled_date, start_time, end_time, location, note


@bp.route("/calendar")
def calendar_page():
    month, selected, previous, following, weeks = schedule_selection()
    db = get_db()
    month_start = month.isoformat()
    month_end = following.isoformat()
    reservations = db.execute(
        """SELECT * FROM reservations
           WHERE status = 'active' AND start_time >= ? AND start_time < ?
           ORDER BY start_time, table_no""",
        (month_start, month_end),
    ).fetchall()
    calendar_items = db.execute(
        """SELECT * FROM calendar_items
           WHERE scheduled_date >= ? AND scheduled_date < ?
           ORDER BY scheduled_date, start_time, id""",
        (month_start, month_end),
    ).fetchall()

    calendar_entries: dict[str, list[dict]] = {}
    for row in reservations:
        day = row["start_time"][:10]
        calendar_entries.setdefault(day, []).append(
            {
                "time": row["start_time"][11:16],
                "name": f"{row['table_no']} 號桌 · {row['guest_name']}",
                "color": "#D4B483",
                "sort_key": row["start_time"][11:16],
            }
        )
    for row in calendar_items:
        type_info = CALENDAR_ITEM_TYPES[row["item_type"]]
        calendar_entries.setdefault(row["scheduled_date"], []).append(
            {
                "time": row["start_time"] or type_info["label"],
                "name": (
                    f"{type_info['label']} · {row['title']}"
                    if row["start_time"]
                    else row["title"]
                ),
                "color": type_info["color"],
                "sort_key": row["start_time"] or "00:00",
                "status_class": "is-completed" if row["status"] == "completed" else "",
            }
        )
    for entries in calendar_entries.values():
        entries.sort(key=lambda entry: entry["sort_key"])

    selected_date = selected.isoformat()
    day_reservations = [row for row in reservations if row["start_time"][:10] == selected_date]
    day_calendar_items = [row for row in calendar_items if row["scheduled_date"] == selected_date]
    unscheduled_items = db.execute(
        """SELECT * FROM calendar_items
           WHERE scheduled_date IS NULL AND status = 'pending'
           ORDER BY created_at DESC, id DESC"""
    ).fetchall()
    completed_unscheduled_items = db.execute(
        """SELECT * FROM calendar_items
           WHERE scheduled_date IS NULL AND status = 'completed'
           ORDER BY completed_at DESC, id DESC LIMIT 20"""
    ).fetchall()
    return render_template(
        "calendar.html",
        month=month,
        selected=selected,
        previous=previous,
        following=following,
        weeks=weeks,
        calendar_entries=calendar_entries,
        reservations=day_reservations,
        calendar_items=day_calendar_items,
        unscheduled_items=unscheduled_items,
        completed_unscheduled_items=completed_unscheduled_items,
        calendar_item_types=CALENDAR_ITEM_TYPES,
        table_count=table_count_value(),
    )


@bp.route("/calendar-items", methods=["POST"])
@bp.route("/calendar-items/<int:item_id>", methods=["POST"])
def save_calendar_item(item_id: int | None = None):
    db = get_db()
    if item_id is not None and not db.execute(
        "SELECT id FROM calendar_items WHERE id = ?", (item_id,)
    ).fetchone():
        flash("找不到可修改的行事曆事項。", "error")
        return calendar_item_redirect()
    try:
        values = calendar_item_form_data()
    except ValueError as exc:
        flash(f"行事曆事項無效：{exc}", "error")
        return calendar_item_redirect()

    if item_id is None:
        db.execute(
            """INSERT INTO calendar_items
               (title, item_type, scheduled_date, start_time, end_time, location, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
    else:
        db.execute(
            """UPDATE calendar_items SET title = ?, item_type = ?, scheduled_date = ?,
               start_time = ?, end_time = ?, location = ?, note = ? WHERE id = ?""",
            (*values, item_id),
        )
    db.commit()
    flash("行事曆事項已儲存。", "success")
    return calendar_item_redirect(values[2])


@bp.route("/calendar-items/<int:item_id>/toggle", methods=["POST"])
def toggle_calendar_item(item_id: int):
    db = get_db()
    item = db.execute(
        "SELECT status, scheduled_date FROM calendar_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        flash("找不到行事曆事項。", "error")
        return calendar_item_redirect()
    new_status = "completed" if item["status"] == "pending" else "pending"
    completed_at = datetime.now().isoformat(timespec="seconds") if new_status == "completed" else None
    db.execute(
        "UPDATE calendar_items SET status = ?, completed_at = ? WHERE id = ?",
        (new_status, completed_at, item_id),
    )
    db.commit()
    flash("事項已標示完成。" if new_status == "completed" else "事項已恢復為待辦。", "success")
    return calendar_item_redirect(item["scheduled_date"])


@bp.route("/calendar-items/<int:item_id>/delete", methods=["POST"])
def delete_calendar_item(item_id: int):
    db = get_db()
    item = db.execute(
        "SELECT scheduled_date FROM calendar_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        flash("找不到行事曆事項。", "error")
    else:
        db.execute("DELETE FROM calendar_items WHERE id = ?", (item_id,))
        db.commit()
        flash("行事曆事項已刪除。", "success")
    return calendar_item_redirect(item["scheduled_date"] if item else None)


@bp.route("/reservations", methods=["POST"])
@bp.route("/reservations/<int:reservation_id>", methods=["POST"])
def save_reservation(reservation_id: int | None = None):
    db = get_db()
    if reservation_id is not None and not db.execute(
        "SELECT id FROM reservations WHERE id = ? AND status = 'active'", (reservation_id,)
    ).fetchone():
        flash("找不到可修改的預約。", "error")
        return schedule_redirect("reservations.calendar_page")
    try:
        table_no = int(request.form["table_no"])
        guest_name = request.form["guest_name"].strip()
        phone = request.form.get("phone", "").strip()
        event_type = request.form.get("event_type", "reservation")
        note = request.form.get("note", "").strip()
        start, end = schedule_times()
        repeat_weeks = 1 if reservation_id is not None else int(request.form.get("repeat_weeks", "1"))
        if not 1 <= table_no <= table_count_value() or not guest_name:
            raise ValueError("請填寫姓名並選擇有效桌號。")
        if event_type not in {"reservation", "course", "club"}:
            raise ValueError("活動類型無效。")
        if not 1 <= repeat_weeks <= 24:
            raise ValueError("重複週數需介於 1 到 24 週。")
    except (KeyError, ValueError) as exc:
        flash(f"預約資料無效：{exc}", "error")
        return schedule_redirect("reservations.calendar_page")

    occurrences = [
        (
            (datetime.fromisoformat(start) + timedelta(weeks=week)).isoformat(timespec="minutes"),
            (datetime.fromisoformat(end) + timedelta(weeks=week)).isoformat(timespec="minutes"),
        )
        for week in range(repeat_weeks)
    ]
    for occurrence_start, occurrence_end in occurrences:
        conflict = db.execute(
            """SELECT id FROM reservations
               WHERE table_no = ? AND status = 'active'
                 AND start_time < ? AND end_time > ? AND id != ?
               LIMIT 1""",
            (table_no, occurrence_end, occurrence_start, reservation_id or 0),
        ).fetchone()
        if conflict:
            flash(
                f"{table_no} 號桌在 {occurrence_start[:10]} 的時段已有預約，未建立任何資料。",
                "error",
            )
            return schedule_redirect("reservations.calendar_page")

    if reservation_id is None:
        db.executemany(
            """INSERT INTO reservations
               (table_no, guest_name, phone, event_type, start_time, end_time, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (table_no, guest_name, phone, event_type, occurrence_start, occurrence_end, note)
                for occurrence_start, occurrence_end in occurrences
            ],
        )
    else:
        db.execute(
            """UPDATE reservations SET table_no = ?, guest_name = ?, phone = ?,
               event_type = ?, start_time = ?, end_time = ?, note = ? WHERE id = ?""",
            (table_no, guest_name, phone, event_type, start, end, note, reservation_id),
        )
    db.commit()
    flash(f"預約已儲存，共 {repeat_weeks} 筆。", "success")
    return schedule_redirect("reservations.calendar_page")


@bp.route("/reservations/<int:reservation_id>/cancel", methods=["POST"])
def cancel_reservation(reservation_id: int):
    db = get_db()
    db.execute(
        "UPDATE reservations SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (reservation_id,),
    )
    db.commit()
    flash("預約已取消。", "success")
    return schedule_redirect("reservations.calendar_page")
