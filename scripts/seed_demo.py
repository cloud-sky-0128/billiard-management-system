from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from billiard_app import create_app
from billiard_app.db import get_db


def seed_demo_database(database_path: Path) -> None:
    database_path = database_path.resolve()
    if database_path.exists():
        database_path.unlink()

    app = create_app(
        {
            "DATABASE": str(database_path),
            "SECRET_KEY": "local-demo-only",
        }
    )
    now = datetime.now()

    with app.app_context():
        db = get_db()
        db.execute(
            """INSERT INTO sessions
               (table_no, mode, start_time, rate_per_min)
               VALUES (1, 'timed', ?, 4)""",
            ((now - timedelta(minutes=47)).isoformat(timespec="seconds"),),
        )
        timed_session_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            """INSERT INTO sessions
               (table_no, mode, start_time, package_hours, rate_per_hour)
               VALUES (3, 'package', ?, 2, 150)""",
            ((now - timedelta(minutes=20)).isoformat(timespec="seconds"),),
        )
        db.execute(
            """INSERT INTO orders
               (session_id, item_id, item_name, item_category_name, sugar_level,
                ice_level, unit_price, quantity, subtotal)
               VALUES (?, 1, '特選紅烏龍', '飲料', '微糖', '少冰', 45, 2, 90)""",
            (timed_session_id,),
        )

        closed_sessions = [
            (2, "timed", "2026-09-17T13:05:00", "2026-09-17T14:12:00", 201, 70, 271),
            (4, "package", "2026-09-17T15:00:00", "2026-09-17T17:00:00", 300, 100, 400),
            (5, "timed", "2026-09-17T18:10:00", "2026-09-17T19:05:00", 165, 90, 255),
        ]
        db.executemany(
            """INSERT INTO sessions
               (table_no, mode, start_time, end_time, status, package_hours,
                rate_per_min, rate_per_hour, table_fee, food_fee, discount_name,
                discount_percent, discount_scope, final_total)
               VALUES (?, ?, ?, ?, 'closed',
                       CASE WHEN ? = 'package' THEN 2 ELSE NULL END,
                       CASE WHEN ? = 'timed' THEN 3 ELSE NULL END,
                       CASE WHEN ? = 'package' THEN 150 ELSE NULL END,
                       ?, ?, '原價', 100, 'all', ?)""",
            [
                (table_no, mode, start, end, mode, mode, mode, table_fee, food_fee, total)
                for table_no, mode, start, end, table_fee, food_fee, total in closed_sessions
            ],
        )
        for table_no, _mode, _start, end, _table_fee, food_fee, _total in closed_sessions:
            session_id = db.execute(
                "SELECT id FROM sessions WHERE table_no = ? AND end_time = ?",
                (table_no, end),
            ).fetchone()["id"]
            db.execute(
                """INSERT INTO orders
                   (session_id, item_id, item_name, item_category_name,
                    unit_price, quantity, subtotal)
                   VALUES (?, 1, '展示餐飲', '餐點', ?, 1, ?)""",
                (session_id, food_fee, food_fee),
            )

        db.executemany(
            """INSERT INTO reservations
               (table_no, guest_name, event_type, start_time, end_time, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (2, "練球預約", "reservation", "2026-09-17T14:00:00", "2026-09-17T16:00:00", "兩位"),
                (6, "社團社課", "course", "2026-09-19T18:00:00", "2026-09-19T21:00:00", "固定使用"),
            ],
        )
        db.executemany(
            """INSERT INTO calendar_items
               (title, item_type, scheduled_date, start_time, end_time, location, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("冷氣定期保養", "maintenance", "2026-09-18", "10:00", "12:00", "店內", "開店前完成"),
                ("週末九號球交流賽", "competition", "2026-09-20", "15:00", "20:00", "全館", "預留 4 桌"),
            ],
        )
        db.execute(
            """INSERT INTO calendar_items
               (title, item_type, note) VALUES ('補充清潔用品', 'todo', '本週完成')"""
        )

        db.executemany(
            "INSERT INTO employees (name, color) VALUES (?, ?)",
            [("小美", "#C96A4A"), ("小安", "#3D7EA6"), ("阿哲", "#568C65")],
        )
        employee_ids = {
            row["name"]: row["id"]
            for row in db.execute(
                "SELECT id, name FROM employees WHERE name IN ('小美', '小安', '阿哲')"
            )
        }
        shift_types = {
            row["name"]: row
            for row in db.execute(
                "SELECT * FROM shift_types WHERE name IN ('早班', '午班', '晚班')"
            )
        }
        shifts = [
            ("小美", "早班", "2026-09-17"),
            ("小安", "午班", "2026-09-17"),
            ("阿哲", "晚班", "2026-09-17"),
            ("小美", "午班", "2026-09-18"),
            ("小安", "晚班", "2026-09-18"),
            ("阿哲", "早班", "2026-09-19"),
        ]
        for employee, shift_name, shift_date in shifts:
            shift_type = shift_types[shift_name]
            start_time = f"{shift_date}T{shift_type['start_time']}:00"
            end_date = datetime.fromisoformat(f"{shift_date}T00:00:00")
            if shift_type["ends_next_day"]:
                end_date += timedelta(days=1)
            end_time = f"{end_date.date().isoformat()}T{shift_type['end_time']}:00"
            db.execute(
                """INSERT INTO shifts
                   (employee_id, shift_type_id, staff_name, shift_type_name,
                    shift_type, start_time, end_time)
                   VALUES (?, ?, ?, ?, 'custom', ?, ?)""",
                (
                    employee_ids[employee],
                    shift_type["id"],
                    employee,
                    shift_name,
                    start_time,
                    end_time,
                ),
            )

        db.execute(
            """INSERT INTO daily_cash_records (record_date, actual_revenue, note)
               VALUES ('2026-09-17', 900, '展示資料')"""
        )
        db.executemany(
            """INSERT INTO expenses (expense_date, category, description, amount)
               VALUES ('2026-09-17', ?, ?, ?)""",
            [("進貨", "飲料補貨", 180), ("設備", "球桿皮頭", 120)],
        )
        db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="建立不含真實個資的作品展示資料庫。")
    parser.add_argument("database", nargs="?", default="demo.db")
    args = parser.parse_args()
    seed_demo_database(Path(args.database))
    print(f"Demo database created: {Path(args.database).resolve()}")


if __name__ == "__main__":
    main()
