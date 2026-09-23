from __future__ import annotations

from datetime import date, datetime, time, timedelta


BUSINESS_DAY_CLOSING_TIME = time(6, 0)


def closing_time_for_business_day(day: date) -> time:
    return BUSINESS_DAY_CLOSING_TIME


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    previous_day = day - timedelta(days=1)
    start = datetime.combine(day, closing_time_for_business_day(previous_day))
    end = datetime.combine(day + timedelta(days=1), closing_time_for_business_day(day))
    return start, end


def business_day_for_timestamp(value: datetime) -> date:
    previous_day = value.date() - timedelta(days=1)
    previous_close = datetime.combine(value.date(), closing_time_for_business_day(previous_day))
    return previous_day if value < previous_close else value.date()


def current_business_day(now: datetime | None = None) -> date:
    return business_day_for_timestamp(now or datetime.now())


def business_day_label(day: date) -> str:
    start, end = business_day_bounds(day)
    return f"{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}"
