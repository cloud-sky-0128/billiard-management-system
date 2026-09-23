from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from flask import redirect, request, url_for

def supported_date(value: str) -> date:
    result = date.fromisoformat(value)
    if not 1900 <= result.year <= 9998:
        raise ValueError("日期年份需介於 1900 到 9998。")
    return result


def reservation_end(start: str, end: str) -> str:
    if end:
        return end
    return (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(timespec="minutes")

def schedule_selection() -> tuple[date, date, date, date, list[list[date | None]]]:
    today = date.today()
    try:
        month = supported_date(request.args.get("month", today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    try:
        selected = supported_date(request.args.get("date", today.isoformat()))
    except ValueError:
        selected = today
    if selected.year != month.year or selected.month != month.month:
        selected = month
    previous = (month - timedelta(days=1)).replace(day=1)
    following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    weeks = [
        [date(month.year, month.month, day) if day else None for day in week]
        for week in calendar.monthcalendar(month.year, month.month)
    ]
    return month, selected, previous, following, weeks


def schedule_times() -> tuple[str, str]:
    day = supported_date(request.form["date"])
    start = datetime.combine(day, datetime.strptime(request.form['start_time'], "%H:%M").time())
    end_time = request.form.get("end_time", "").strip()
    if not end_time:
        return start.isoformat(timespec="minutes"), ""
    end = datetime.combine(day, datetime.strptime(end_time, "%H:%M").time())
    if request.form.get("ends_next_day") == "1":
        end += timedelta(days=1)
    supported_date(end.date().isoformat())
    if end <= start:
        raise ValueError("結束時間必須晚於開始時間。")
    if end - start > timedelta(hours=24):
        raise ValueError("預約時間不可超過 24 小時。")
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def schedule_redirect(endpoint: str):
    try:
        day = supported_date(request.form.get("date", ""))
    except ValueError:
        day = date.today()
    return redirect(url_for(endpoint, month=day.strftime("%Y-%m"), date=day.isoformat()))
