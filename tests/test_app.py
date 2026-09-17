import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from billiard_app import create_app
from billiard_app.db import get_db
from billiard_app.services.billing import (
    build_session_runtime,
    calculate_timed_charge,
    discount_type_is_available,
    session_food_total,
)


class BilliardAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_directory.name) / "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": database, "SECRET_KEY": "test"})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_directory.cleanup()

    def db_value(self, sql, parameters=()):
        with self.app.app_context():
            return get_db().execute(sql, parameters).fetchone()[0]

    def test_main_pages_render(self):
        paths = [
            "/", "/tables/1", "/menu", "/settings/rates", "/stats",
            "/calendar?month=2026-09&date=2026-09-17",
            "/shifts?month=2026-09&date=2026-09-17",
            "/shifts/settings",
            "/finance?month=2026-09&date=2026-09-17",
            "/settings",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_shift_date_picker_keeps_monday_to_sunday_columns(self):
        html = self.client.get(
            "/shifts?month=2026-09&date=2026-09-17"
        ).get_data(as_text=True)
        self.assertEqual(html.count('class="date-picker-weekday"'), 7)
        self.assertEqual(html.count('class="date-option is-empty"'), 5)
        first_empty = html.index('class="date-option is-empty"')
        september_first = html.index('value="2026-09-01"')
        self.assertLess(first_empty, september_first)

    def test_existing_table_session_flow_still_works(self):
        response = self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        self.assertEqual(response.status_code, 302)
        session_id = self.db_value("SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'")

        with self.app.app_context():
            item_id = get_db().execute(
                "SELECT id FROM menu_items ORDER BY category_id DESC, id LIMIT 1"
            ).fetchone()["id"]
        response = self.client.post(
            "/orders/add",
            data={"session_id": session_id, "item_id": item_id, "quantity": "2"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM orders"), 1)

        response = self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "100", "discount_scope": "all"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.db_value("SELECT status FROM sessions WHERE id = ?", (session_id,)), "closed"
        )

    def test_timed_charge_rounds_up_at_minute_boundaries(self):
        start = datetime(2026, 9, 17, 12, 0, 0)
        cases = [
            (timedelta(seconds=1), 1, 4.0),
            (timedelta(seconds=60), 1, 4.0),
            (timedelta(seconds=61), 2, 8.0),
        ]
        for elapsed, expected_minutes, expected_fee in cases:
            with self.subTest(elapsed=elapsed):
                minutes, fee = calculate_timed_charge(start, start + elapsed, 4.0)
                self.assertEqual(minutes, expected_minutes)
                self.assertEqual(fee, expected_fee)

    def test_expired_package_session_can_be_extended(self):
        self.client.post(
            "/sessions/start",
            data={"table_no": "3", "mode": "package", "package_hours": "1"},
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 3 AND status = 'active'"
        )
        expired_start = datetime.now() - timedelta(minutes=90)
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE sessions SET start_time = ? WHERE id = ?",
                (expired_start.isoformat(timespec="seconds"), session_id),
            )
            db.commit()

        response = self.client.post(
            f"/sessions/extend/{session_id}",
            data={"extra_hours": "1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("包台總時數 2 小時", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value("SELECT package_hours FROM sessions WHERE id = ?", (session_id,)),
            2,
        )
        with self.app.app_context():
            session = get_db().execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            runtime = build_session_runtime(session, datetime.now())
        self.assertEqual(runtime["table_fee"], 300.0)
        self.assertGreater(runtime["timer_seconds"], 25 * 60)
        self.assertLess(runtime["timer_seconds"], 35 * 60)

    def test_overnight_discount_window(self):
        discount = {"start_time": "22:00", "end_time": "02:00"}
        self.assertTrue(
            discount_type_is_available(discount, datetime(2026, 9, 17, 23, 30))
        )
        self.assertTrue(
            discount_type_is_available(discount, datetime(2026, 9, 18, 1, 59))
        )
        self.assertFalse(
            discount_type_is_available(discount, datetime(2026, 9, 18, 2, 0))
        )
        self.assertFalse(
            discount_type_is_available(discount, datetime(2026, 9, 18, 12, 0))
        )

    def test_repeated_checkout_does_not_change_closed_session(self):
        self.client.post("/sessions/start", data={"table_no": "4", "mode": "timed"})
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 4 AND status = 'active'"
        )
        first_response = self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "100", "discount_scope": "all"},
        )
        self.assertEqual(first_response.status_code, 302)
        with self.app.app_context():
            before = tuple(
                get_db()
                .execute(
                    "SELECT end_time, table_fee, final_total FROM sessions WHERE id = ?",
                    (session_id,),
                )
                .fetchone()
            )

        second_response = self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "50", "discount_scope": "all"},
            follow_redirects=True,
        )
        self.assertIn("找不到進行中的球檯紀錄", second_response.get_data(as_text=True))
        with self.app.app_context():
            after = tuple(
                get_db()
                .execute(
                    "SELECT end_time, table_fee, final_total FROM sessions WHERE id = ?",
                    (session_id,),
                )
                .fetchone()
            )
        self.assertEqual(after, before)
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM sessions WHERE table_no = 4"), 1
        )

    def test_checkout_rejects_an_order_that_arrives_during_closing(self):
        self.client.post("/sessions/start", data={"table_no": "4", "mode": "timed"})
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 4 AND status = 'active'"
        )
        with self.app.app_context():
            item_id = get_db().execute(
                """SELECT m.id FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '餐點' AND m.is_active = 1 LIMIT 1"""
            ).fetchone()["id"]

        checkout_paused = threading.Event()
        release_checkout = threading.Event()
        responses = {}

        def paused_food_total(current_session_id):
            checkout_paused.set()
            release_checkout.wait(timeout=3)
            return session_food_total(current_session_id)

        def checkout_request():
            with self.app.test_client() as client:
                responses["checkout"] = client.post(
                    f"/sessions/end/{session_id}",
                    data={"discount_percent": "100", "discount_scope": "all"},
                )

        def order_request():
            with self.app.test_client() as client:
                responses["order"] = client.post(
                    "/orders/add",
                    data={"session_id": session_id, "item_id": item_id, "quantity": "1"},
                )

        with patch(
            "billiard_app.blueprints.tables.session_food_total",
            side_effect=paused_food_total,
        ):
            checkout_thread = threading.Thread(target=checkout_request)
            checkout_thread.start()
            self.assertTrue(checkout_paused.wait(timeout=3))

            order_thread = threading.Thread(target=order_request)
            order_thread.start()
            time.sleep(0.1)
            release_checkout.set()
            checkout_thread.join(timeout=3)
            order_thread.join(timeout=3)

        self.assertFalse(checkout_thread.is_alive())
        self.assertFalse(order_thread.is_alive())
        self.assertEqual(responses["checkout"].status_code, 302)
        self.assertEqual(responses["order"].status_code, 302)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM orders"), 0)
        self.assertEqual(
            self.db_value("SELECT status FROM sessions WHERE id = ?", (session_id,)),
            "closed",
        )

    def test_database_prevents_duplicate_active_sessions(self):
        self.client.post("/sessions/start", data={"table_no": "5", "mode": "timed"})
        with patch(
            "billiard_app.blueprints.tables.active_session_by_table", return_value=None
        ):
            response = self.client.post(
                "/sessions/start",
                data={"table_no": "5", "mode": "timed"},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("未建立重複紀錄", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM sessions WHERE table_no = 5 AND status = 'active'"
            ),
            1,
        )

        stats_page = self.client.get("/stats").get_data(as_text=True)
        self.assertIn("每小時開台筆數", stats_page)
        self.assertNotIn("每小時來客數", stats_page)

    def test_per_table_rates_and_named_discount_types(self):
        response = self.client.post(
            "/settings/rates",
            data={
                "table_count": "10",
                "timed_rate_1": "7.5",
                "package_rate_1": "220",
                "package_enabled_1": ["0", "1"],
                "timed_rate_2": "5",
                "package_rate_2": "180",
                "package_enabled_2": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.db_value("SELECT timed_rate_per_min FROM table_rates WHERE table_no = 1"),
            7.5,
        )
        self.assertEqual(
            self.db_value("SELECT package_enabled FROM table_rates WHERE table_no = 2"), 0
        )

        self.client.post(
            "/sessions/start",
            data={"table_no": "1", "mode": "timed"},
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        self.assertEqual(
            self.db_value("SELECT rate_per_min FROM sessions WHERE id = ?", (session_id,)),
            7.5,
        )
        self.client.post(
            "/sessions/start",
            data={"table_no": "2", "mode": "package", "package_hours": "2"},
        )
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM sessions WHERE table_no = 2"), 0
        )

        self.client.post(
            "/settings/discounts/add",
            data={
                "name": "學生優惠",
                "discount_percent": "90",
                "discount_scope": "table_only",
                "start_time": "",
                "end_time": "",
            },
        )
        discount_id = self.db_value("SELECT id FROM discount_types WHERE name = '學生優惠'")
        self.client.post(
            "/settings/discounts/add",
            data={
                "name": "平日包台優惠",
                "pricing_method": "package_hourly",
                "package_rate_per_hour": "120",
                "applicable_mode": "all",
                "discount_scope": "all",
                "start_time": "",
                "end_time": "",
            },
        )
        package_discount_id = self.db_value(
            "SELECT id FROM discount_types WHERE name = '平日包台優惠'"
        )
        self.assertEqual(
            self.db_value(
                "SELECT applicable_mode FROM discount_types WHERE id = ?",
                (package_discount_id,),
            ),
            "package",
        )
        page = self.client.get("/tables/1").get_data(as_text=True)
        self.assertIn("學生優惠", page)
        discount_select = page.split('<select name="discount_type_id">', 1)[1].split(
            "</select>", 1
        )[0]
        self.assertNotIn(f'<option value="{package_discount_id}">', discount_select)
        self.client.post(
            f"/sessions/end/{session_id}",
            data={
                "discount_type_id": str(discount_id),
                "discount_percent": "100",
                "discount_scope": "all",
            },
        )
        self.assertEqual(
            self.db_value("SELECT discount_name FROM sessions WHERE id = ?", (session_id,)),
            "學生優惠",
        )
        self.assertEqual(
            self.db_value("SELECT discount_percent FROM sessions WHERE id = ?", (session_id,)),
            90,
        )
        self.assertEqual(
            self.db_value("SELECT discount_amount FROM sessions WHERE id = ?", (session_id,)),
            0.75,
        )
        self.assertEqual(
            self.db_value("SELECT final_total FROM sessions WHERE id = ?", (session_id,)),
            6.75,
        )
        self.client.post(
            "/sessions/start",
            data={"table_no": "1", "mode": "package", "package_hours": "2"},
        )
        self.assertEqual(
            self.db_value(
                "SELECT rate_per_hour FROM sessions WHERE table_no = 1 AND status = 'active'"
            ),
            220,
        )
        package_session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        page = self.client.get("/tables/1").get_data(as_text=True)
        self.assertIn("平日包台優惠", page)
        self.assertIn("包台每小時 120 元", page)
        self.client.post(
            f"/sessions/end/{package_session_id}",
            data={
                "discount_type_id": str(package_discount_id),
                "discount_percent": "100",
                "discount_scope": "all",
            },
        )
        self.assertEqual(
            self.db_value(
                "SELECT discount_pricing_method FROM sessions WHERE id = ?",
                (package_session_id,),
            ),
            "package_hourly",
        )
        self.assertEqual(
            self.db_value(
                "SELECT discount_package_rate_per_hour FROM sessions WHERE id = ?",
                (package_session_id,),
            ),
            120,
        )
        self.assertEqual(
            self.db_value("SELECT table_fee FROM sessions WHERE id = ?", (package_session_id,)),
            440,
        )
        self.assertEqual(
            self.db_value(
                "SELECT discount_amount FROM sessions WHERE id = ?", (package_session_id,)
            ),
            200,
        )
        self.assertEqual(
            self.db_value("SELECT final_total FROM sessions WHERE id = ?", (package_session_id,)),
            240,
        )
        stats_page = self.client.get(
            f"/stats?date={date.today().isoformat()}"
        ).get_data(as_text=True)
        self.assertIn("包台每小時 120 元", stats_page)

        self.client.post(
            f"/settings/discounts/{discount_id}/update",
            data={
                "name": "學生證優惠",
                "discount_percent": "85",
                "discount_scope": "all",
                "start_time": "10:00",
                "end_time": "18:00",
            },
        )
        self.assertEqual(
            self.db_value("SELECT discount_percent FROM discount_types WHERE id = ?", (discount_id,)),
            85,
        )
        self.client.post(f"/settings/discounts/{discount_id}/delete")
        self.assertEqual(
            self.db_value("SELECT is_active FROM discount_types WHERE id = ?", (discount_id,)), 0
        )
        self.assertEqual(
            self.db_value("SELECT discount_name FROM sessions WHERE id = ?", (session_id,)),
            "學生優惠",
        )

    def test_recurring_reservation_is_atomic_when_a_date_conflicts(self):
        booking = {
            "date": "2026-09-20", "table_no": "3", "event_type": "course",
            "guest_name": "Course", "phone": "", "start_time": "14:00",
            "end_time": "16:00", "note": "", "repeat_weeks": "3",
        }
        self.client.post("/reservations", data=booking)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 3)

        conflicting = dict(booking, date="2026-09-13", repeat_weeks="2")
        self.client.post("/reservations", data=conflicting)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 3)

    def test_calendar_items_support_dated_events_and_unscheduled_todos(self):
        dated = {
            "item_type": "maintenance",
            "title": "冷氣檢修",
            "scheduled_date": "2026-09-18",
            "start_time": "10:00",
            "end_time": "12:00",
            "location": "吧檯旁",
            "note": "聯絡廠商",
            "return_date": "2026-09-18",
        }
        self.assertEqual(self.client.post("/calendar-items", data=dated).status_code, 302)
        dated_id = self.db_value("SELECT id FROM calendar_items WHERE title = '冷氣檢修'")
        self.client.post(
            f"/calendar-items/{dated_id}",
            data={
                **dated,
                "item_type": "competition",
                "title": "年度比賽",
                "note": "準備獎品",
            },
        )
        self.assertEqual(
            self.db_value("SELECT item_type FROM calendar_items WHERE id = ?", (dated_id,)),
            "competition",
        )
        self.client.post(
            "/reservations",
            data={
                "date": "2026-09-18", "table_no": "1", "event_type": "reservation",
                "guest_name": "Same Time Guest", "phone": "", "start_time": "10:00",
                "end_time": "12:00", "note": "", "repeat_weeks": "1",
            },
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 1)
        todo = {
            "item_type": "todo",
            "title": "修補入口地板",
            "scheduled_date": "2026-09-18",
            "unscheduled": "1",
            "start_time": "",
            "end_time": "",
            "location": "入口",
            "note": "",
            "return_date": "2026-09-18",
        }
        self.client.post("/calendar-items", data=todo)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 2)
        self.assertIsNone(
            self.db_value("SELECT scheduled_date FROM calendar_items WHERE title = '修補入口地板'")
        )

        page = self.client.get(
            "/calendar?month=2026-09&date=2026-09-18"
        ).get_data(as_text=True)
        self.assertIn("年度比賽", page)
        self.assertIn("修補入口地板", page)
        self.assertIn("未排日期待辦", page)

        todo_id = self.db_value("SELECT id FROM calendar_items WHERE title = '修補入口地板'")
        self.client.post(
            f"/calendar-items/{todo_id}/toggle", data={"return_date": "2026-09-18"}
        )
        self.assertEqual(
            self.db_value("SELECT status FROM calendar_items WHERE id = ?", (todo_id,)),
            "completed",
        )
        self.client.post(
            "/calendar-items",
            data={
                **todo, "title": "時間不完整", "unscheduled": "0",
                "start_time": "10:00",
            },
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 2)

        self.client.post(
            f"/calendar-items/{dated_id}/delete", data={"return_date": "2026-09-18"}
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 1)

    def test_employee_and_multi_date_shift_flow(self):
        self.client.post(
            "/shifts/employees/add", data={"name": "Amy", "color": "#123456"}
        )
        employee_id = self.db_value("SELECT id FROM employees WHERE name = 'Amy'")
        self.assertEqual(
            self.db_value("SELECT color FROM employees WHERE id = ?", (employee_id,)),
            "#123456",
        )
        shift_type_id = self.db_value("SELECT id FROM shift_types WHERE name = '早班'")
        shift = {
            "date": "2026-09-17",
            "employee_id": str(employee_id),
            "shift_type_id": str(shift_type_id),
            "start_time": "09:00",
            "end_time": "12:00",
            "dates": ["2026-09-17", "2026-09-19", "2026-09-21"],
            "note": "",
        }
        self.client.post("/shifts/add", data=shift)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM shifts"), 3)

        conflicting = dict(shift, dates=["2026-09-18", "2026-09-19"])
        self.client.post("/shifts/add", data=conflicting)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM shifts"), 3)

        page = self.client.get("/shifts?month=2026-09&date=2026-09-17").get_data(as_text=True)
        self.assertIn("09:00–12:00", page)
        self.assertIn("Amy", page)
        self.assertIn('"color": "#123456"', page)

        self.client.post(
            f"/shifts/employees/{employee_id}/color", data={"color": "#ABCDEF"}
        )
        self.assertEqual(
            self.db_value("SELECT color FROM employees WHERE id = ?", (employee_id,)),
            "#ABCDEF",
        )

        self.client.post(
            f"/shifts/employees/{employee_id}/delete"
        )
        self.assertEqual(self.db_value("SELECT is_active FROM employees WHERE id = ?", (employee_id,)), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM shifts"), 3)

    def test_shift_type_crud_keeps_existing_shift_history(self):
        response = self.client.post(
            "/shifts/types/add",
            data={
                "name": "假日班", "start_time": "10:00", "end_time": "18:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        shift_type_id = self.db_value("SELECT id FROM shift_types WHERE name = '假日班'")

        self.client.post(
            f"/shifts/types/{shift_type_id}/update",
            data={"name": "週末班", "start_time": "11:00", "end_time": "19:00"},
        )
        self.assertEqual(
            self.db_value("SELECT start_time FROM shift_types WHERE id = ?", (shift_type_id,)),
            "11:00",
        )

        self.client.post("/shifts/employees/add", data={"name": "Bob"})
        employee_id = self.db_value("SELECT id FROM employees WHERE name = 'Bob'")
        self.client.post(
            "/shifts/add",
            data={
                "date": "2026-09-17", "dates": ["2026-09-17"],
                "employee_id": str(employee_id), "shift_type_id": str(shift_type_id),
                "start_time": "11:00", "end_time": "19:00", "note": "",
            },
        )
        self.client.post(f"/shifts/types/{shift_type_id}/delete")
        self.assertEqual(
            self.db_value("SELECT is_active FROM shift_types WHERE id = ?", (shift_type_id,)), 0
        )
        self.assertEqual(
            self.db_value("SELECT shift_type_name FROM shifts WHERE employee_id = ?", (employee_id,)),
            "週末班",
        )
        page = self.client.get("/shifts?month=2026-09&date=2026-09-17").get_data(as_text=True)
        self.assertIn("週末班", page)

    def test_shift_type_display_order_can_move_up(self):
        self.client.post(
            "/shifts/types/add",
            data={"name": "排序甲", "start_time": "08:00", "end_time": "10:00"},
        )
        self.client.post(
            "/shifts/types/add",
            data={"name": "排序乙", "start_time": "10:00", "end_time": "12:00"},
        )
        first_id = self.db_value("SELECT id FROM shift_types WHERE name = '排序甲'")
        second_id = self.db_value("SELECT id FROM shift_types WHERE name = '排序乙'")
        self.client.post(f"/shifts/types/{second_id}/move", data={"direction": "up"})

        with self.app.app_context():
            names = [
                row["name"]
                for row in get_db().execute(
                    "SELECT name FROM shift_types WHERE is_active = 1 ORDER BY sort_order, id"
                ).fetchall()
            ]
        self.assertLess(names.index("排序乙"), names.index("排序甲"))
        self.assertNotEqual(first_id, second_id)

    def test_overnight_shift_stays_on_start_day_and_detects_next_day_overlap(self):
        self.client.post("/shifts/employees/add", data={"name": "Night Staff"})
        employee_id = self.db_value("SELECT id FROM employees WHERE name = 'Night Staff'")
        self.client.post(
            "/shifts/types/add",
            data={
                "name": "五六晚班", "start_time": "18:00", "end_time": "02:00",
                "ends_next_day": "1",
            },
        )
        night_type_id = self.db_value("SELECT id FROM shift_types WHERE name = '五六晚班'")
        self.client.post(
            "/shifts/add",
            data={
                "date": "2026-09-19", "dates": ["2026-09-19"],
                "employee_id": str(employee_id), "shift_type_id": str(night_type_id),
                "start_time": "18:00", "end_time": "02:00", "note": "",
            },
        )
        with self.app.app_context():
            row = get_db().execute(
                "SELECT start_time, end_time FROM shifts WHERE employee_id = ?", (employee_id,)
            ).fetchone()
            self.assertEqual(row["start_time"], "2026-09-19T18:00")
            self.assertEqual(row["end_time"], "2026-09-20T02:00")

        self.client.post(
            "/shifts/types/add",
            data={"name": "凌晨班", "start_time": "01:00", "end_time": "04:00"},
        )
        early_type_id = self.db_value("SELECT id FROM shift_types WHERE name = '凌晨班'")
        self.client.post(
            "/shifts/add",
            data={
                "date": "2026-09-20", "dates": ["2026-09-20"],
                "employee_id": str(employee_id), "shift_type_id": str(early_type_id),
                "start_time": "01:00", "end_time": "04:00", "note": "",
            },
        )
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM shifts WHERE employee_id = ?", (employee_id,)), 1
        )
        page = self.client.get("/shifts?month=2026-09&date=2026-09-19").get_data(as_text=True)
        self.assertIn("18:00–02:00（隔天）", page)
        self.assertNotIn("18:00–02:00翌", page)
        self.assertIn('"time": "18:00\\u201302:00"', page)

    def test_finance_actual_revenue_expense_and_csv(self):
        self.client.post(
            "/finance/cash",
            data={"record_date": "2026-09-17", "actual_revenue": "1000", "note": "cash"},
        )
        self.client.post(
            "/finance/expenses",
            data={
                "expense_date": "2026-09-17", "category": "進貨",
                "amount": "200", "description": "drinks",
            },
        )
        page = self.client.get("/finance?month=2026-09&date=2026-09-17").get_data(as_text=True)
        self.assertIn("1000", page)
        self.assertIn("drinks", page)
        self.assertIn("800", page)
        self.assertIn("收支淨額", page)

        response = self.client.get("/finance/export?month=2026-09")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        csv_text = response.data.decode("utf-8-sig")
        self.assertIn("2026-09-17", csv_text)
        self.assertIn("drinks", csv_text)
        self.assertIn("收支淨額", csv_text)

    def test_finance_invalid_selected_date_falls_back_to_month(self):
        response = self.client.get("/finance?month=2026-09&date=2026-09-99")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="2026-09-01"', response.get_data(as_text=True))

    def test_order_rejects_malformed_data_and_inactive_categories(self):
        response = self.client.post(
            "/orders/add",
            data={"session_id": "invalid", "item_id": "1", "quantity": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM orders"), 0)

        self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        with self.app.app_context():
            db = get_db()
            item = db.execute(
                """SELECT m.id, m.category_id FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '餐點' LIMIT 1"""
            ).fetchone()
            db.execute(
                "UPDATE categories SET is_active = 0 WHERE id = ?",
                (item["category_id"],),
            )
            db.commit()

        response = self.client.post(
            "/orders/add",
            data={"session_id": session_id, "item_id": item["id"], "quantity": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM orders"), 0)

    def test_non_finite_money_values_are_rejected(self):
        category_id = self.db_value(
            "SELECT id FROM categories WHERE is_active = 1 ORDER BY id LIMIT 1"
        )
        self.client.post(
            "/menu/items/add",
            data={"category_id": category_id, "name": "Invalid price", "price": "nan"},
        )
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM menu_items WHERE name = 'Invalid price'"),
            0,
        )

        self.client.post(
            "/finance/cash",
            data={"record_date": "2026-09-17", "actual_revenue": "inf", "note": ""},
        )
        self.client.post(
            "/finance/expenses",
            data={
                "expense_date": "2026-09-17",
                "category": "其他",
                "amount": "nan",
                "description": "Invalid amount",
            },
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM daily_cash_records"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM expenses"), 0)

    def test_table_count_cannot_hide_an_active_session(self):
        self.client.post("/sessions/start", data={"table_no": "10", "mode": "timed"})
        response = self.client.post(
            "/settings/rates",
            data={"table_count": "5"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("請先結帳仍在使用的桌號：10", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value("SELECT value FROM settings WHERE key = 'table_count'"),
            "10",
        )

    def test_test_data_reset_clears_operations_but_preserves_configuration(self):
        self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        self.client.post(
            "/finance/cash",
            data={"record_date": date.today().isoformat(), "actual_revenue": "500", "note": ""},
        )
        self.client.post(
            "/finance/expenses",
            data={
                "expense_date": date.today().isoformat(), "category": "其他",
                "amount": "50", "description": "test",
            },
        )
        self.client.post(
            "/reservations",
            data={
                "date": "2026-10-10", "table_no": "2", "event_type": "reservation",
                "guest_name": "Test Guest", "phone": "", "start_time": "10:00",
                "end_time": "11:00", "note": "", "repeat_weeks": "1",
            },
        )
        self.client.post(
            "/calendar-items",
            data={
                "item_type": "todo", "title": "Reset Item", "scheduled_date": "",
                "start_time": "", "end_time": "", "location": "", "note": "",
                "return_date": date.today().isoformat(),
            },
        )
        self.client.post("/shifts/employees/add", data={"name": "Preserved Employee"})
        employee_id = self.db_value("SELECT id FROM employees WHERE name = 'Preserved Employee'")
        self.client.post(
            "/shifts/types/add",
            data={"name": "Preserved Type", "start_time": "08:00", "end_time": "12:00"},
        )
        shift_type_id = self.db_value("SELECT id FROM shift_types WHERE name = 'Preserved Type'")
        self.client.post(
            "/shifts/add",
            data={
                "date": "2026-10-10", "dates": ["2026-10-10"],
                "employee_id": str(employee_id), "shift_type_id": str(shift_type_id),
                "start_time": "08:00", "end_time": "12:00", "note": "",
            },
        )
        preserved_counts = {
            "categories": self.db_value("SELECT COUNT(*) FROM categories"),
            "menu_items": self.db_value("SELECT COUNT(*) FROM menu_items"),
            "employees": self.db_value("SELECT COUNT(*) FROM employees"),
            "shift_types": self.db_value("SELECT COUNT(*) FROM shift_types"),
            "settings": self.db_value("SELECT COUNT(*) FROM settings"),
        }
        confirmation = {
            "confirm_scope": "yes",
            "confirmation": "永久刪除測試營運資料",
            "confirmation_date": date.today().isoformat(),
        }

        self.client.post("/settings/reset-test-data", data=confirmation)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 1)

        session_id = self.db_value("SELECT id FROM sessions WHERE status = 'active'")
        self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "100", "discount_scope": "all"},
        )
        wrong_confirmation = dict(confirmation, confirmation="wrong")
        self.client.post("/settings/reset-test-data", data=wrong_confirmation)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 1)

        self.client.post("/settings/reset-test-data", data=confirmation)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM daily_cash_records"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM expenses"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 0)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM shifts"), 0)
        for table, count in preserved_counts.items():
            self.assertEqual(self.db_value(f"SELECT COUNT(*) FROM {table}"), count)
        backups = list(Path(self.temp_directory.name, "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()
