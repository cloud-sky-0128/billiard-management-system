from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from flask import redirect, request, url_for

def schedule_selection() -> tuple[date, date, date, date, list[list[date | None]]]:
    today = date.today()
    try:
        month = date.fromisoformat(request.args.get("month", today.strftime("%Y-%m")) + "-01")
    except ValueError:
        month = today.replace(day=1)
    try:
        selected = date.fromisoformat(request.args.get("date", today.isoformat()))
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
    day = date.fromisoformat(request.form["date"])
    start = datetime.fromisoformat(f"{day.isoformat()}T{request.form['start_time']}")
    end = datetime.fromisoformat(f"{day.isoformat()}T{request.form['end_time']}")
    if end <= start:
        raise ValueError("結束時間必須晚於開始時間。")
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def schedule_redirect(endpoint: str):
    try:
        day = date.fromisoformat(request.form.get("date", ""))
    except ValueError:
        day = date.today()
    return redirect(url_for(endpoint, month=day.strftime("%Y-%m"), date=day.isoformat()))
