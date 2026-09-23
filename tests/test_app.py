import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

from billiard_app import create_app, default_database_path
from billiard_app.auth import ADMIN_PASSWORD_KEY
from billiard_app.config import CALENDAR_ITEM_TYPES
from billiard_app.db import (
    init_db,
    get_db,
    migrate_calendar_items_for_optional_end,
    migrate_reservations_for_optional_end,
)
from billiard_app.blueprints.finance import csv_safe, month_summary
from billiard_app.money import format_cents, percentage_of_cents, round_payment_cents, to_cents
from billiard_app.maintenance import (
    create_backup, create_daily_backups, create_recent_backup, disk_warning,
    start_backup_scheduler,
    restore_drill, verify_database,
)
from billiard_app.services.billing import (
    build_session_runtime,
    calculate_timed_charge,
    discount_type_is_available,
    session_food_total,
)
from billiard_app.services.business_day import (
    business_day_bounds,
    business_day_for_timestamp,
    current_business_day,
)
from desktop_app import application_url


class BilliardAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_directory.name) / "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": database, "SECRET_KEY": "test"})
        self.client = self.app.test_client()
        with self.app.app_context():
            get_db().execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (ADMIN_PASSWORD_KEY, generate_password_hash("test-admin-password")),
            )
            get_db().commit()
        self.client.post("/settings/admin", data={"password": "test-admin-password"})

    def tearDown(self):
        self.temp_directory.cleanup()

    def db_value(self, sql, parameters=()):
        with self.app.app_context():
            return get_db().execute(sql, parameters).fetchone()[0]

    def insert_test_payment(self, paid_at: str, amount_cents: int) -> int:
        with self.app.app_context():
            db = get_db()
            tab = db.execute(
                """INSERT INTO customer_tabs
                   (display_name, guest_count, table_no, status, opened_at, closed_at,
                    final_total_cents)
                   VALUES ('Boundary', 1, NULL, 'closed', ?, ?, ?)""",
                (paid_at, paid_at, amount_cents),
            )
            db.execute(
                """INSERT INTO payments
                   (customer_tab_id, session_id, payment_type, table_fee_cents,
                    food_fee_cents, discount_amount_cents, amount_cents, paid_at)
                   VALUES (?, NULL, 'food_only', 0, ?, 0, ?, ?)""",
                (tab.lastrowid, amount_cents, amount_cents, paid_at),
            )
            db.commit()
            return int(tab.lastrowid)

    def test_business_day_changes_at_six_in_the_morning(self):
        friday = date(2026, 9, 18)
        start, end = business_day_bounds(friday)
        self.assertEqual(start, datetime(2026, 9, 18, 6, 0))
        self.assertEqual(end, datetime(2026, 9, 19, 6, 0))
        self.assertEqual(
            business_day_for_timestamp(datetime(2026, 9, 19, 5, 59, 59)), friday
        )
        self.assertEqual(
            business_day_for_timestamp(datetime(2026, 9, 19, 6, 0)),
            date(2026, 9, 19),
        )
        sunday_start, sunday_end = business_day_bounds(date(2026, 9, 20))
        self.assertEqual(sunday_start, datetime(2026, 9, 20, 6, 0))
        self.assertEqual(sunday_end, datetime(2026, 9, 21, 6, 0))

    def test_revenue_and_traffic_use_business_day_across_midnight_and_month_end(self):
        self.insert_test_payment("2026-09-19T05:30:00", 12000)
        self.insert_test_payment("2026-09-19T06:00:00", 13000)
        self.insert_test_payment("2026-10-01 05:30:00", 20000)
        self.insert_test_payment("2026-10-01T06:00:00", 30000)
        with self.app.app_context():
            db = get_db()
            db.execute(
                """INSERT INTO sessions (table_no, mode, start_time, status)
                   VALUES (1, 'timed', '2026-09-19T05:30:00', 'active')"""
            )
            db.commit()
            summary = month_summary(date(2026, 9, 1), date(2026, 10, 1))
        rows = {row["date"]: row for row in summary["rows"]}
        self.assertEqual(rows["2026-09-18"]["system_revenue_cents"], 12000)
        self.assertEqual(rows["2026-09-19"]["system_revenue_cents"], 13000)
        self.assertEqual(rows["2026-09-30"]["system_revenue_cents"], 20000)
        self.assertEqual(summary["system_total_cents"], 45000)

        friday_page = self.client.get("/stats?date=2026-09-18").get_data(as_text=True)
        self.assertIn("2026-09-19T05:30:00", friday_page)
        self.assertNotIn("2026-09-19T06:00:00", friday_page)
        self.assertIn("05 點：1 筆", friday_page)
        self.assertNotIn("+1 05", friday_page)
        self.assertIn('<span class="bar-label">24</span>', friday_page)
        saturday_page = self.client.get("/stats?date=2026-09-19").get_data(as_text=True)
        self.assertIn("2026-09-19T06:00:00", saturday_page)
        self.assertNotIn("2026-09-19T05:30:00", saturday_page)

    def test_daily_backup_is_verified_and_keeps_first_snapshot_of_day(self):
        database = self.app.config["DATABASE"]
        with self.app.app_context():
            get_db().execute("INSERT INTO settings (key, value) VALUES ('backup-test', 'before')")
            get_db().commit()
        first = create_backup(database)
        with self.app.app_context():
            get_db().execute("UPDATE settings SET value = 'after' WHERE key = 'backup-test'")
            get_db().commit()
        self.assertEqual(create_backup(database), first)
        with sqlite3.connect(first) as backup:
            self.assertEqual(
                backup.execute("SELECT value FROM settings WHERE key = 'backup-test'").fetchone()[0],
                "before",
            )
        backup.close()
        self.assertIn("orders", verify_database(first))

    def test_recent_backup_updates_and_rotates_without_touching_daily_backup(self):
        database = self.app.config["DATABASE"]
        daily = create_backup(database)
        base = datetime(2026, 9, 23, 10, 0)
        with patch("billiard_app.maintenance.RECENT_RETENTION_COUNT", 3):
            for index in range(4):
                with self.app.app_context():
                    db = get_db()
                    db.execute(
                        "INSERT INTO settings (key, value) VALUES ('recent-test', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(index),),
                    )
                    db.commit()
                latest = create_recent_backup(database, backup_at=base + timedelta(minutes=index * 15))
        snapshots = sorted(latest.parent.glob("billiard-recent-*.db"))
        self.assertEqual(len(snapshots), 3)
        self.assertNotIn("100000", snapshots[0].name)
        with closing(sqlite3.connect(latest)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM settings WHERE key='recent-test'").fetchone()[0], "3")
        self.assertTrue(daily.exists())

    def test_backup_scheduler_takes_first_recent_snapshot_without_waiting(self):
        ready = threading.Event()
        with (
            patch("billiard_app.maintenance.create_daily_backups") as daily,
            patch("billiard_app.maintenance.create_recent_backups", side_effect=lambda *_: ready.set()) as recent,
            patch("billiard_app.maintenance.disk_warning", return_value=None),
        ):
            stop = start_backup_scheduler(self.app.config["DATABASE"])
            try:
                self.assertTrue(ready.wait(3))
            finally:
                stop.set()
            daily.assert_called_once()
            recent.assert_called_once()

    def test_backup_restore_drill_does_not_replace_live_database(self):
        database = self.app.config["DATABASE"]
        extra = Path(self.temp_directory.name) / "other-disk"
        local, secondary = create_daily_backups(database, str(extra))
        self.assertTrue(local.is_file())
        self.assertTrue(secondary.is_file())
        self.assertTrue((local.parent / f"billiard-monthly-{date.today():%Y-%m}.db").is_file())
        self.assertTrue((secondary.parent / f"billiard-monthly-{date.today():%Y-%m}.db").is_file())
        with self.app.app_context():
            get_db().execute("INSERT INTO settings (key, value) VALUES ('after-backup', 'yes')")
            get_db().commit()
        self.assertIn("settings", restore_drill(local))
        self.assertEqual(self.db_value("SELECT value FROM settings WHERE key = 'after-backup'"), "yes")

    def test_automatic_backup_retention_keeps_30_days_and_12_months(self):
        database = self.app.config["DATABASE"]
        current_day = date(2026, 9, 21)
        template = create_backup(database, backup_date=current_day)
        directory = template.parent
        for offset in range(1, 36):
            day = current_day - timedelta(days=offset)
            shutil.copyfile(template, directory / f"billiard-daily-{day.isoformat()}.db")
        for offset in range(1, 15):
            month_index = current_day.year * 12 + current_day.month - 1 - offset
            year, zero_based_month = divmod(month_index, 12)
            shutil.copyfile(
                template,
                directory / f"billiard-monthly-{year:04d}-{zero_based_month + 1:02d}.db",
            )
        manual = create_backup(database, daily=False)
        before_reset = directory / "billiard-before-reset-20250101-000000.db"
        malformed = directory / "billiard-daily-keep-this-note.db"
        shutil.copyfile(template, before_reset)
        shutil.copyfile(template, malformed)

        create_daily_backups(database, backup_date=current_day)

        self.assertEqual(len(list(directory.glob("billiard-daily-????-??-??.db"))), 30)
        self.assertEqual(len(list(directory.glob("billiard-monthly-????-??.db"))), 12)
        self.assertTrue((directory / "billiard-daily-2026-08-23.db").exists())
        self.assertFalse((directory / "billiard-daily-2026-08-22.db").exists())
        self.assertTrue((directory / "billiard-monthly-2025-10.db").exists())
        self.assertFalse((directory / "billiard-monthly-2025-09.db").exists())
        self.assertTrue(manual.exists())
        self.assertTrue(before_reset.exists())
        self.assertTrue(malformed.exists())

    def test_monthly_backup_keeps_first_snapshot_of_month(self):
        database = self.app.config["DATABASE"]
        first_day = date(2026, 9, 1)
        local, _ = create_daily_backups(database, backup_date=first_day)
        monthly = local.parent / "billiard-monthly-2026-09.db"
        with self.app.app_context():
            get_db().execute("INSERT INTO settings (key, value) VALUES ('later-change', 'yes')")
            get_db().commit()
        create_daily_backups(database, backup_date=date(2026, 9, 2))
        with sqlite3.connect(monthly) as backup:
            count = backup.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'later-change'"
            ).fetchone()[0]
        backup.close()
        self.assertEqual(count, 0)

    def test_settings_manual_backup_and_restore_drill(self):
        settings_page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('data-dialog-open="backup-settings-dialog"', settings_page)
        self.assertIn('data-dialog-open="reset-data-dialog"', settings_page)
        self.assertIn('<dialog class="settings-dialog" id="backup-settings-dialog"', settings_page)
        self.assertIn("現在建立一份備份", settings_page)
        self.assertIn("檢查備份是否可用", settings_page)
        self.assertIn("查看備份位置與錯誤日誌", settings_page)
        self.assertEqual(self.client.post("/settings/backups/create").status_code, 302)
        backups = list((Path(self.app.config["DATABASE"]).parent / "backups").glob("billiard-manual-*.db"))
        self.assertEqual(len(backups), 1)
        response = self.client.post(
            "/settings/backups/restore-drill", data={"backup_name": backups[0].name},
            follow_redirects=True,
        )
        self.assertIn("備份檢查完成", response.get_data(as_text=True))
        response = self.client.post(
            "/settings/backups/restore-drill", data={"backup_name": "../test.db"},
            follow_redirects=True,
        )
        self.assertIn("有效的備份檔", response.get_data(as_text=True))

    def test_admin_password_setup_login_logout_and_change(self):
        with self.app.app_context():
            get_db().execute("DELETE FROM settings WHERE key = ?", (ADMIN_PASSWORD_KEY,))
            get_db().commit()
        with self.client.session_transaction() as session:
            session.clear()

        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings/admin", response.location)
        setup_page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("建立管理員密碼", setup_page)

        response = self.client.post(
            "/settings/admin",
            data={"password": "short", "password_confirmation": "short"},
            follow_redirects=True,
        )
        self.assertIn("至少需要 8 個字元", response.get_data(as_text=True))

        password = "first-admin-password"
        response = self.client.post(
            "/settings/admin?next=/settings",
            data={"password": password, "password_confirmation": password},
            follow_redirects=True,
        )
        self.assertIn("整體設定", response.get_data(as_text=True))
        stored_hash = self.db_value(
            "SELECT value FROM settings WHERE key = ?", (ADMIN_PASSWORD_KEY,)
        )
        self.assertNotEqual(stored_hash, password)
        self.assertTrue(check_password_hash(stored_hash, password))

        self.client.post("/settings/admin/logout")
        self.assertEqual(self.client.get("/settings/rates").status_code, 302)
        wrong = self.client.post(
            "/settings/admin?next=/settings/rates",
            data={"password": "wrong-password"},
            follow_redirects=True,
        )
        self.assertIn("密碼不正確", wrong.get_data(as_text=True))
        logged_in = self.client.post(
            "/settings/admin?next=/settings/rates",
            data={"password": password},
            follow_redirects=True,
        )
        self.assertIn('name="table_count"', logged_in.get_data(as_text=True))

        changed = self.client.post(
            "/settings/admin/password",
            data={
                "current_password": password,
                "new_password": "second-admin-password",
                "new_password_confirmation": "second-admin-password",
            },
            follow_redirects=True,
        )
        self.assertIn("管理員密碼已更新", changed.get_data(as_text=True))

    def test_shift_settings_require_admin_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/shifts").status_code, 200)
        response = self.client.get("/shifts/settings")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/settings/admin", response.location)
        self.client.post("/shifts/employees/add", data={"name": "Blocked Employee"})
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM employees WHERE name = 'Blocked Employee'"),
            0,
        )

    def test_menu_and_finance_are_admin_only_settings_entries(self):
        settings_page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('href="/menu"', settings_page)
        self.assertIn('href="/finance"', settings_page)
        self.assertIn('href="/shifts/settings"', settings_page)

        shifts_page = self.client.get("/shifts").get_data(as_text=True)
        self.assertNotIn('href="/shifts/settings"', shifts_page)

        dashboard_page = self.client.get("/").get_data(as_text=True)
        navigation = dashboard_page.split("</nav>", 1)[0]
        self.assertNotIn('href="/menu"', navigation)
        self.assertNotIn('href="/finance"', navigation)

        with self.client.session_transaction() as session:
            session.clear()
        for path in ("/menu", "/finance", "/finance/export"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/settings/admin", response.location)

    def test_corrupted_backup_fails_restore_drill_without_touching_live_data(self):
        backup = create_backup(self.app.config["DATABASE"])
        backup.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            restore_drill(backup)
        self.assertGreater(self.db_value("SELECT COUNT(*) FROM settings"), 0)

    def test_uncommitted_transaction_is_rolled_back_after_process_crash(self):
        database = self.app.config["DATABASE"]
        script = (
            "import os, sqlite3, sys; "
            "db = sqlite3.connect(sys.argv[1]); "
            "db.execute('BEGIN IMMEDIATE'); "
            "db.execute(\"INSERT INTO settings (key, value) VALUES ('interrupted', 'partial')\"); "
            "os._exit(7)"
        )
        result = subprocess.run([sys.executable, "-c", script, database], check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM settings WHERE key = 'interrupted'"), 0)
        self.assertIn("settings", verify_database(Path(database)))

    def test_disk_space_warning_and_corrupt_startup_check(self):
        usage = type("Usage", (), {"free": 1024})()
        with patch("billiard_app.maintenance.shutil.disk_usage", return_value=usage):
            self.assertIn("可用空間", disk_warning(self.app.config["DATABASE"]))
        damaged = Path(self.temp_directory.name) / "damaged.db"
        damaged.write_bytes(b"broken")
        with self.assertRaises(sqlite3.DatabaseError):
            verify_database(damaged, require_schema=False)

    def test_real_startup_creates_backup_and_logs_corrupt_database(self):
        database = Path(self.temp_directory.name) / "startup.db"
        script = """
import logging
from pathlib import Path
from billiard_app import create_app
from billiard_app.maintenance import backup_directory, log_path

app = create_app()
database = app.config['DATABASE']
assert list(backup_directory(database).glob('billiard-daily-*.db'))
Path(database).write_bytes(b'broken')
try:
    create_app()
except Exception:
    pass
else:
    raise AssertionError('Damaged database was accepted')
for handler in logging.getLogger('billiard').handlers:
    handler.flush()
assert 'Database startup check failed' in log_path(database).read_text(encoding='utf-8')
"""
        environment = os.environ.copy()
        environment["BILLIARD_DATABASE"] = str(database)
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
            env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_desktop_paths_and_url(self):
        configured_database = Path(self.temp_directory.name) / "configured.db"
        with patch.dict(
            os.environ,
            {"BILLIARD_DATABASE": str(configured_database)},
        ):
            self.assertEqual(default_database_path(), configured_database.resolve())

        with patch.dict(
            os.environ,
            {"BILLIARD_DATABASE": "", "LOCALAPPDATA": self.temp_directory.name},
        ):
            with patch.object(sys, "frozen", True, create=True):
                expected = Path(self.temp_directory.name) / "BilliardManager" / "billiard.db"
                self.assertEqual(default_database_path(), expected)
                self.assertTrue(expected.parent.is_dir())
        self.assertEqual(application_url(8765), "http://127.0.0.1:8765")

    def test_desktop_database_falls_back_beside_executable(self):
        local_app_data = Path(self.temp_directory.name) / "blocked-local-app-data"
        executable = Path(self.temp_directory.name) / "BilliardManager" / "BilliardManager.exe"

        def allow_only_portable_data(directory):
            if directory == local_app_data / "BilliardManager":
                raise PermissionError("access denied")
            directory.mkdir(parents=True, exist_ok=True)

        with (
            patch.dict(
                os.environ,
                {"BILLIARD_DATABASE": "", "LOCALAPPDATA": str(local_app_data)},
            ),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(executable)),
            patch("billiard_app._ensure_writable_directory", side_effect=allow_only_portable_data),
        ):
            expected = executable.resolve().parent / "data" / "billiard.db"
            self.assertEqual(default_database_path(), expected)

    def test_desktop_database_keeps_existing_fallback_and_refuses_ambiguous_paths(self):
        root = Path(self.temp_directory.name)
        primary = root / "local" / "BilliardManager" / "billiard.db"
        fallback = root / "portable" / "data" / "billiard.db"
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b"existing data")
        with (
            patch.dict(os.environ, {"BILLIARD_DATABASE": "", "LOCALAPPDATA": str(root / "local")}),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(root / "portable" / "BilliardManager.exe")),
        ):
            self.assertEqual(default_database_path().resolve(), fallback.resolve())
            original_stat = Path.stat
            def deny_primary(path, *args, **kwargs):
                if path == primary:
                    raise PermissionError("cannot inspect primary database")
                return original_stat(path, *args, **kwargs)
            with patch.object(Path, "stat", deny_primary):
                with self.assertRaisesRegex(PermissionError, "cannot inspect primary database"):
                    default_database_path()
            primary.parent.mkdir(parents=True)
            primary.write_bytes(b"another database")
            with self.assertRaisesRegex(OSError, "找到兩份資料庫"):
                default_database_path()
            fallback.unlink()
            with patch("billiard_app._ensure_writable_directory", side_effect=PermissionError("blocked")):
                with self.assertRaisesRegex(PermissionError, "blocked"):
                    default_database_path()

    def test_shift_date_picker_keeps_monday_to_sunday_columns(self):
        html = self.client.get(
            "/shifts?month=2026-09&date=2026-09-17"
        ).get_data(as_text=True)
        self.assertEqual(html.count('class="date-picker-weekday"'), 7)
        self.assertEqual(html.count('class="date-option is-empty"'), 5)
        first_empty = html.index('class="date-option is-empty"')
        september_first = html.index('value="2026-09-01"')
        self.assertLess(first_empty, september_first)
        self.assertNotIn("月曆直接顯示每日班次", html)
        self.assertIn("2026 年 09 月 排班", html)
        self.assertNotIn("2026 年 09 月 批次排班", html)
        self.assertIn(
            '<div class="schedule-submit-cell"><button type="submit"', html
        )

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

    def test_checkout_messages_render_in_confirmation_dialog(self):
        self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        response = self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "100", "discount_scope": "all"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn('class="checkout-confirm-dialog js-checkout-confirm"', html)
        self.assertIn("結帳完成", html)
        self.assertIn("1 號桌已結帳", html)
        self.assertNotIn("flash-checkout", html)

        self.client.post("/waiting/create", data={"display_name": "外帶", "guest_count": "1"})
        tab_id = self.db_value("SELECT id FROM customer_tabs WHERE status = 'waiting'")
        response = self.client.post(
            f"/tabs/{tab_id}/checkout", follow_redirects=True
        )
        html = response.get_data(as_text=True)
        self.assertIn('class="checkout-confirm-dialog js-checkout-confirm"', html)
        self.assertIn("餐飲單已結帳", html)
        self.assertIn(">確認</button>", html)

    def test_table_can_order_before_clock_starts(self):
        item_id = self.db_value(
            """SELECT m.id FROM menu_items m JOIN categories c ON c.id = m.category_id
               WHERE c.name = '餐點' LIMIT 1"""
        )
        response = self.client.post(
            "/orders/add", data={"table_no": "3", "item_id": item_id, "quantity": "2"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 0)
        self.assertEqual(
            self.db_value("SELECT status FROM customer_tabs WHERE table_no = 3"), "assigned"
        )
        detail = self.client.get("/tables/3").get_data(as_text=True)
        self.assertIn("待開台", detail)
        self.assertIn("開始計時", detail)
        self.assertIn("已點餐紀錄", detail)
        self.client.post(
            "/sessions/start", data={"table_no": "3", "mode": "package", "package_hours": "1"}
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 1)
        self.assertEqual(
            self.db_value("SELECT status FROM customer_tabs WHERE table_no = 3"), "playing"
        )
        session_id = self.db_value("SELECT id FROM sessions WHERE table_no = 3")
        self.client.post(f"/sessions/end/{session_id}")
        self.assertEqual(
            self.db_value("SELECT food_fee_cents FROM sessions WHERE id = ?", (session_id,)),
            self.db_value("SELECT SUM(subtotal_cents) FROM orders"),
        )
        self.assertEqual(
            self.db_value("SELECT status FROM customer_tabs WHERE table_no = 3"), "closed"
        )

    def test_order_kiosk_renders_categories_items_and_drink_options(self):
        html = self.client.get("/tables/1").get_data(as_text=True)

        self.assertIn("開啟點餐", html)
        self.assertIn('class="order-kiosk js-order-kiosk"', html)
        self.assertIn("特選紅烏龍", html)
        self.assertIn("巧克力厚片", html)
        self.assertIn('name="sugar_level" value="微糖"', html)
        self.assertIn('name="ice_level" value="少冰"', html)

    def test_order_kiosk_drink_submission_keeps_kiosk_open(self):
        with self.app.app_context():
            drink = get_db().execute(
                """SELECT m.id, m.category_id FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '飲料' LIMIT 1"""
            ).fetchone()

        response = self.client.post(
            "/orders/add",
            data={
                "table_no": "2",
                "item_id": drink["id"],
                "category_id": drink["category_id"],
                "quantity": "2",
                "sugar_level": "微糖",
                "ice_level": "少冰",
                "continue_ordering": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("order=1", response.headers["Location"])
        self.assertEqual(
            self.db_value("SELECT sugar_level FROM orders LIMIT 1"), "微糖"
        )
        self.assertEqual(
            self.db_value("SELECT ice_level FROM orders LIMIT 1"), "少冰"
        )
        self.assertEqual(self.db_value("SELECT quantity FROM orders LIMIT 1"), 2)

    def test_waiting_order_assignment_and_food_only_checkout(self):
        item_id = self.db_value(
            """SELECT m.id FROM menu_items m JOIN categories c ON c.id = m.category_id
               WHERE c.name = '餐點' LIMIT 1"""
        )
        response = self.client.post(
            "/waiting/create", data={"display_name": "小美", "guest_count": "2"}
        )
        self.assertEqual(response.status_code, 302)
        tab_id = self.db_value("SELECT id FROM customer_tabs WHERE status = 'waiting'")
        self.client.post(
            "/orders/add",
            data={"customer_tab_id": tab_id, "item_id": item_id, "quantity": "1"},
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 0)
        self.assertIn("小美", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/waiting/{tab_id}").status_code, 200)
        self.client.post(f"/waiting/{tab_id}/assign", data={"table_no": "4"})
        self.assertEqual(
            self.db_value("SELECT status FROM customer_tabs WHERE id = ?", (tab_id,)),
            "assigned",
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM sessions"), 0)
        self.assertIn("待開台", self.client.get("/tables/4").get_data(as_text=True))
        total = self.db_value("SELECT SUM(subtotal_cents) FROM orders")
        self.client.post(f"/tabs/{tab_id}/checkout")
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM customer_tabs WHERE id = ?", (tab_id,)),
            total,
        )
        day = current_business_day().isoformat()
        stats = self.client.get(f"/stats?date={day}").get_data(as_text=True)
        self.assertIn("只結餐飲", stats)
        finance = self.client.get(
            f"/finance?month={day[:7]}&date={day}"
        ).get_data(as_text=True)
        self.assertIn(format_cents(total), finance)

    def test_waiting_assignment_rejects_occupied_table(self):
        item_id = self.db_value(
            """SELECT m.id FROM menu_items m JOIN categories c ON c.id = m.category_id
               WHERE c.name = '餐點' LIMIT 1"""
        )
        self.client.post("/orders/add", data={"table_no": "2", "item_id": item_id})
        self.client.post("/waiting/create", data={"display_name": "候位", "guest_count": "1"})
        tab_id = self.db_value("SELECT id FROM customer_tabs WHERE status = 'waiting'")
        self.client.post(f"/waiting/{tab_id}/assign", data={"table_no": "2"})
        self.assertEqual(
            self.db_value("SELECT status FROM customer_tabs WHERE id = ?", (tab_id,)),
            "waiting",
        )

    def test_legacy_sessions_and_orders_migrate_to_customer_tabs(self):
        legacy_path = Path(self.temp_directory.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE menu_items (
                id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL,
                name TEXT NOT NULL, price_cents INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY, table_no INTEGER NOT NULL,
                mode TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT,
                status TEXT NOT NULL, package_hours INTEGER,
                rate_per_min_cents INTEGER, rate_per_hour_cents INTEGER,
                table_fee_cents INTEGER NOT NULL DEFAULT 0,
                food_fee_cents INTEGER NOT NULL DEFAULT 0,
                discount_amount_cents INTEGER NOT NULL DEFAULT 0,
                final_total_cents INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL, item_name TEXT NOT NULL,
                item_category_name TEXT NOT NULL DEFAULT '',
                sugar_level TEXT NOT NULL DEFAULT '',
                ice_level TEXT NOT NULL DEFAULT '',
                is_served INTEGER NOT NULL DEFAULT 0,
                unit_price_cents INTEGER NOT NULL,
                quantity INTEGER NOT NULL, subtotal_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO sessions
                (id, table_no, mode, start_time, end_time, status,
                 table_fee_cents, food_fee_cents, final_total_cents)
            VALUES (1, 3, 'timed', '2026-09-01T10:00:00',
                    '2026-09-01T11:00:00', 'closed', 10000, 4500, 0);
            INSERT INTO sessions
                (id, table_no, mode, start_time, status, rate_per_min_cents)
            VALUES (2, 4, 'timed', '2026-09-01T12:00:00', 'active', 300);
            INSERT INTO orders
                (id, session_id, item_id, item_name, item_category_name,
                 unit_price_cents, quantity, subtotal_cents)
            VALUES (1, 1, 1, '舊餐點', '餐點', 4500, 1, 4500);
            """
        )
        connection.close()
        migrated = create_app(
            {"TESTING": True, "DATABASE": str(legacy_path), "SECRET_KEY": "test"}
        )
        with migrated.app_context():
            db = get_db()
            self.assertEqual(
                db.execute(
                    "SELECT final_total_cents FROM customer_tabs WHERE status = 'closed'"
                ).fetchone()[0],
                14500,
            )
            self.assertEqual(
                db.execute(
                    "SELECT status FROM customer_tabs WHERE table_no = 4"
                ).fetchone()[0],
                "playing",
            )
            self.assertEqual(
                db.execute("SELECT item_name FROM orders").fetchone()[0], "舊餐點"
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        # Reopening the database must not duplicate migrated bills.
        create_app({"TESTING": True, "DATABASE": str(legacy_path)})
        with migrated.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM customer_tabs").fetchone()[0], 2)

    def test_existing_payments_migrate_without_losing_receipts(self):
        legacy_path = Path(self.temp_directory.name) / "legacy-payments.db"
        schema = (Path(__file__).resolve().parents[1] / "billiard_app" / "schema.sql").read_text(
            encoding="utf-8"
        )
        schema = schema.replace(
            "'package_extension', 'package_food', 'package_close'",
            "'package_extension', 'package_close'",
        )
        connection = sqlite3.connect(legacy_path)
        connection.executescript(schema)
        connection.execute(
            """INSERT INTO customer_tabs
               (id, table_no, status, opened_at, closed_at, final_total_cents)
               VALUES (1, 3, 'closed', '2026-09-01T10:00:00', '2026-09-01T11:00:00', 15000)"""
        )
        connection.execute(
            """INSERT INTO sessions
               (id, customer_tab_id, table_no, mode, start_time, end_time,
                status, package_hours, rate_per_hour_cents, final_total_cents)
               VALUES (1, 1, 3, 'package', '2026-09-01T10:00:00',
                       '2026-09-01T11:00:00', 'closed', 1, 15000, 15000)"""
        )
        connection.execute(
            """INSERT INTO payments
               (id, customer_tab_id, session_id, payment_type, table_fee_cents,
                amount_cents, paid_at)
               VALUES (7, 1, 1, 'package_start', 15000, 15000, '2026-09-01T10:00:00')"""
        )
        connection.commit()
        connection.close()

        upgraded = create_app(
            {"TESTING": True, "DATABASE": str(legacy_path), "SECRET_KEY": "test"}
        )
        with upgraded.app_context():
            db = get_db()
            receipt = db.execute(
                "SELECT id, payment_type, amount_cents FROM payments WHERE id = 7"
            ).fetchone()
            self.assertEqual(tuple(receipt), (7, "package_start", 15000))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn("payment_id", [row["name"] for row in db.execute("PRAGMA table_info(orders)")])
        self.assertEqual(len(list((legacy_path.parent / "backups").glob(
            "billiard-before-payment-migration-*.db"
        ))), 1)

    def test_timed_charge_rounds_up_at_minute_boundaries(self):
        start = datetime(2026, 9, 17, 12, 0, 0)
        cases = [
            (timedelta(seconds=1), 1, 400),
            (timedelta(seconds=60), 1, 400),
            (timedelta(seconds=61), 2, 800),
        ]
        for elapsed, expected_minutes, expected_fee in cases:
            with self.subTest(elapsed=elapsed):
                minutes, fee = calculate_timed_charge(start, start + elapsed, 400)
                self.assertEqual(minutes, expected_minutes)
                self.assertEqual(fee, expected_fee)

    def test_money_uses_integer_cents_and_decimal_rounding(self):
        self.assertEqual(round_payment_cents(49), 0)
        self.assertEqual(round_payment_cents(50), 100)
        self.assertEqual(round_payment_cents(149), 100)
        self.assertEqual(round_payment_cents(150), 200)
        self.assertEqual(to_cents("7.50"), 750)
        self.assertEqual(to_cents("1.005"), 101)
        self.assertEqual(percentage_of_cents(755, "90"), 680)
        self.assertEqual(format_cents(750), "7.5")
        with self.app.app_context():
            columns = {
                row["name"]: row["type"]
                for row in get_db().execute("PRAGMA table_info(sessions)")
            }
        self.assertEqual(columns["final_total_cents"], "INTEGER")
        self.assertNotIn("final_total", columns)

    def test_discounted_charge_is_recorded_and_reported_in_whole_yuan(self):
        self.client.post(
            "/settings/rates",
            data={"table_count": "10", "package_rate_1": "7.5", "package_enabled_1": "1"},
        )
        response = self.client.post(
            "/sessions/start",
            data={"table_no": "1", "mode": "package", "package_hours": "1",
                  "discount_percent": "90"},
            follow_redirects=True,
        )
        self.assertIn("應收 7 元", response.get_data(as_text=True))
        self.assertNotIn("整元調整", response.get_data(as_text=True))
        self.assertIn("已收款：<strong>7 元", response.get_data(as_text=True))
        session_id = self.db_value("SELECT id FROM sessions WHERE table_no=1 AND status='active'")
        self.assertEqual(
            self.db_value("SELECT amount_cents FROM payments WHERE session_id=?", (session_id,)),
            700,
        )
        self.client.post(f"/sessions/end/{session_id}")
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM sessions WHERE id=?", (session_id,)),
            700,
        )
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM customer_tabs WHERE table_no=1"),
            700,
        )
        self.assertEqual(self.db_value("SELECT SUM(amount_cents) FROM payments"), 700)

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
        self.assertIn("包台總時數：2 小時", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value("SELECT package_hours FROM sessions WHERE id = ?", (session_id,)),
            2,
        )
        with self.app.app_context():
            session = get_db().execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            runtime = build_session_runtime(session, datetime.now())
        self.assertEqual(runtime["table_fee_cents"], 30000)
        self.assertGreater(runtime["timer_seconds"], 25 * 60)
        self.assertLess(runtime["timer_seconds"], 35 * 60)
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM payments WHERE session_id = ?",
                (session_id,),
            ),
            2,
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_extension'",
                (session_id,),
            ),
            15000,
        )

    def test_package_prepays_table_and_only_collects_food_when_closed(self):
        self.client.post(
            "/sessions/start",
            data={
                "table_no": "3",
                "mode": "package",
                "package_hours": "1",
                "discount_percent": "90",
                "discount_scope": "all",
            },
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 3 AND status = 'active'"
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_start'",
                (session_id,),
            ),
            13500,
        )
        detail = self.client.get("/tables/3").get_data(as_text=True)
        self.assertIn("已收款", detail)
        self.assertIn("135 元", detail)

        food_item_id = self.db_value(
            """SELECT m.id FROM menu_items m JOIN categories c ON c.id = m.category_id
               WHERE c.name = '餐點' ORDER BY m.id LIMIT 1"""
        )
        self.client.post(
            "/orders/add",
            data={"session_id": session_id, "item_id": food_item_id, "quantity": "1"},
        )
        response = self.client.post(
            f"/sessions/end/{session_id}", follow_redirects=True
        )
        self.assertIn("本次應收 41 元", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            4100,
        )
        self.assertEqual(
            self.db_value(
                "SELECT table_fee_cents FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            0,
        )
        self.assertEqual(
            self.db_value(
                "SELECT final_total_cents FROM sessions WHERE id = ?", (session_id,)
            ),
            17600,
        )
        stats = self.client.get(
            f"/stats?date={current_business_day().isoformat()}"
        ).get_data(as_text=True)
        self.assertIn("包台預收", stats)
        self.assertIn("包台餐飲結清", stats)

    def test_package_start_collects_existing_drinks_but_leaves_food_unpaid(self):
        with self.app.app_context():
            db = get_db()
            drink = db.execute(
                """SELECT m.id, m.price_cents FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '飲料' ORDER BY m.id LIMIT 1"""
            ).fetchone()
            food = db.execute(
                """SELECT m.id, m.price_cents FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '餐點' ORDER BY m.id LIMIT 1"""
            ).fetchone()

        self.client.post(
            "/orders/add",
            data={
                "table_no": "3",
                "item_id": drink["id"],
                "quantity": "1",
                "sugar_level": "微糖",
                "ice_level": "少冰",
            },
        )
        tab_id = self.db_value(
            "SELECT id FROM customer_tabs WHERE table_no = 3 AND status = 'assigned'"
        )
        self.client.post(
            "/orders/add",
            data={"customer_tab_id": tab_id, "item_id": food["id"], "quantity": "1"},
        )
        response = self.client.post(
            "/sessions/start",
            data={
                "table_no": "3",
                "mode": "package",
                "package_hours": "1",
                "discount_percent": "90",
                "discount_scope": "all",
            },
            follow_redirects=True,
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 3 AND status = 'active'"
        )
        drink_cents = int(drink["price_cents"])
        food_cents = int(food["price_cents"])
        expected_start = round_payment_cents(percentage_of_cents(15000 + drink_cents, 90))
        self.assertIn(f"飲料 {format_cents(drink_cents)} 元", response.get_data(as_text=True))
        self.assertEqual(
            self.db_value(
                "SELECT food_fee_cents FROM payments WHERE session_id = ? AND payment_type = 'package_start'",
                (session_id,),
            ),
            drink_cents,
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_start'",
                (session_id,),
            ),
            expected_start,
        )
        self.assertIsNotNone(
            self.db_value("SELECT payment_id FROM orders WHERE item_id = ?", (drink["id"],))
        )
        self.assertIsNone(
            self.db_value("SELECT payment_id FROM orders WHERE item_id = ?", (food["id"],))
        )

        self.client.post(f"/sessions/end/{session_id}")
        expected_food_due = round_payment_cents(percentage_of_cents(food_cents, 90))
        self.assertEqual(
            self.db_value(
                "SELECT food_fee_cents FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            food_cents,
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            expected_food_due,
        )
        self.assertEqual(
            self.db_value(
                "SELECT SUM(amount_cents) FROM payments WHERE session_id = ?",
                (session_id,),
            ),
            expected_start + expected_food_due,
        )

    def test_package_can_checkout_new_drinks_without_ending_session(self):
        self.client.post(
            "/sessions/start",
            data={"table_no": "3", "mode": "package", "package_hours": "1"},
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 3 AND status = 'active'"
        )
        with self.app.app_context():
            drink = get_db().execute(
                """SELECT m.id, m.price_cents FROM menu_items m
                   JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '飲料' ORDER BY m.id LIMIT 1"""
            ).fetchone()
        self.client.post(
            "/orders/add",
            data={
                "session_id": session_id,
                "item_id": drink["id"],
                "quantity": "1",
                "sugar_level": "無糖",
                "ice_level": "去冰",
            },
        )
        order_id = self.db_value("SELECT id FROM orders")
        response = self.client.post(
            f"/sessions/{session_id}/drinks/checkout", follow_redirects=True
        )
        html = response.get_data(as_text=True)
        self.assertIn("飲料已結帳", html)
        self.assertIn("已付款", html)
        self.assertEqual(
            self.db_value("SELECT status FROM sessions WHERE id = ?", (session_id,)),
            "active",
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_food'",
                (session_id,),
            ),
            int(drink["price_cents"]),
        )
        duplicate_response = self.client.post(
            f"/sessions/{session_id}/drinks/checkout", follow_redirects=True
        )
        self.assertIn("目前沒有尚未付款的飲料", duplicate_response.get_data(as_text=True))
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM payments WHERE session_id = ? AND payment_type = 'package_food'",
                (session_id,),
            ),
            1,
        )

        delete_response = self.client.post(
            f"/orders/{order_id}/delete", follow_redirects=True
        )
        self.assertIn("已付款的餐飲不可刪除", delete_response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM orders"), 1)

        self.client.post(f"/sessions/end/{session_id}")
        self.assertEqual(
            self.db_value(
                "SELECT COUNT(*) FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            0,
        )
        stats = self.client.get(
            f"/stats?date={current_business_day().isoformat()}"
        ).get_data(as_text=True)
        self.assertIn("包台飲料結帳", stats)

    def test_split_drink_discount_rounding_never_recharges_paid_drinks(self):
        with self.app.app_context():
            db = get_db()
            drink = db.execute(
                """SELECT m.id FROM menu_items m JOIN categories c ON c.id = m.category_id
                   WHERE c.name = '飲料' ORDER BY m.id LIMIT 1"""
            ).fetchone()
            db.execute("UPDATE menu_items SET price_cents = 5 WHERE id = ?", (drink["id"],))
            db.commit()

        self.client.post(
            "/sessions/start",
            data={
                "table_no": "3", "mode": "package", "package_hours": "1",
                "discount_percent": "90", "discount_scope": "all",
            },
        )
        session_id = self.db_value("SELECT id FROM sessions WHERE status = 'active'")
        for _ in range(2):
            self.client.post(
                "/orders/add",
                data={
                    "session_id": session_id, "item_id": drink["id"], "quantity": "1",
                    "sugar_level": "無糖", "ice_level": "去冰",
                },
            )
            detail = self.client.get("/tables/3").get_data(as_text=True)
            self.assertIn("飲料立即結帳 · 收取 0 元", detail)
            self.client.post(f"/sessions/{session_id}/drinks/checkout")

        detail = self.client.get("/tables/3").get_data(as_text=True)
        self.assertIn("結束包台時待收：<strong>0 元</strong>", detail)
        self.client.post(f"/sessions/end/{session_id}")
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM sessions WHERE id = ?", (session_id,)),
            13500,
        )
        self.assertEqual(
            self.db_value("SELECT SUM(amount_cents) FROM payments WHERE session_id = ?", (session_id,)),
            13500,
        )

    def test_split_package_hour_rounding_matches_payments(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE table_rates SET package_rate_per_hour_cents = 101 WHERE table_no = 3"
            )
            db.commit()
        self.client.post(
            "/sessions/start",
            data={
                "table_no": "3", "mode": "package", "package_hours": "1",
                "discount_percent": "50", "discount_scope": "table_only",
            },
        )
        session_id = self.db_value("SELECT id FROM sessions WHERE status = 'active'")
        self.client.post(f"/sessions/extend/{session_id}", data={"extra_hours": "1"})
        detail = self.client.get("/tables/3").get_data(as_text=True)
        self.assertIn("結束包台時待收：<strong>0 元</strong>", detail)
        self.client.post(f"/sessions/end/{session_id}")
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM sessions WHERE id = ?", (session_id,)),
            200,
        )
        self.assertEqual(
            self.db_value("SELECT SUM(amount_cents) FROM payments WHERE session_id = ?", (session_id,)),
            200,
        )

    def test_timed_session_is_only_paid_when_closed(self):
        self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        self.assertEqual(
            self.db_value("SELECT COUNT(*) FROM payments WHERE session_id = ?", (session_id,)),
            0,
        )
        self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "100", "discount_scope": "table_only"},
        )
        self.assertEqual(
            self.db_value(
                "SELECT payment_type FROM payments WHERE session_id = ?", (session_id,)
            ),
            "timed_close",
        )

    def test_legacy_active_package_without_prepayment_can_still_checkout(self):
        self.client.post(
            "/sessions/start",
            data={"table_no": "3", "mode": "package", "package_hours": "1"},
        )
        session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 3 AND status = 'active'"
        )
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM payments WHERE session_id = ?", (session_id,))
            db.commit()

        detail = self.client.get("/tables/3").get_data(as_text=True)
        self.assertIn("更新前建立的包台", detail)
        self.assertIn("球檯費（尚未收款）", detail)
        self.client.post(
            f"/sessions/end/{session_id}",
            data={"discount_percent": "90", "discount_scope": "table_only"},
        )
        self.assertEqual(
            self.db_value(
                "SELECT amount_cents FROM payments WHERE session_id = ? AND payment_type = 'package_close'",
                (session_id,),
            ),
            13500,
        )

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
                    "SELECT end_time, table_fee_cents, final_total_cents FROM sessions WHERE id = ?",
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
                    "SELECT end_time, table_fee_cents, final_total_cents FROM sessions WHERE id = ?",
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

    def test_table_display_name_is_editable_without_changing_table_number(self):
        default_page = self.client.get("/").get_data(as_text=True)
        self.assertIn("1 號桌", default_page)
        rates_page = self.client.get("/settings/rates").get_data(as_text=True)
        self.assertIn('name="display_name_1" value="1 號桌"', rates_page)

        response = self.client.post(
            "/settings/rates",
            data={"table_count": "10", "display_name_1": "窗邊檯"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.db_value("SELECT display_name FROM table_rates WHERE table_no = 1"),
            "窗邊檯",
        )
        self.assertIn("窗邊檯", self.client.get("/").get_data(as_text=True))
        self.assertIn("窗邊檯細項", self.client.get("/tables/1").get_data(as_text=True))
        self.assertIn("窗邊檯", self.client.get("/calendar").get_data(as_text=True))

        self.client.post("/sessions/start", data={"table_no": "1", "mode": "timed"})
        self.assertEqual(
            self.db_value("SELECT table_no FROM sessions WHERE status = 'active'"), 1
        )

        self.client.post("/settings/rates", data={"table_count": "10", "display_name_1": ""})
        self.assertIn("1 號桌", self.client.get("/").get_data(as_text=True))

    def test_table_display_name_migrates_from_existing_database(self):
        with self.app.app_context():
            db = get_db()
            db.execute("DROP TABLE table_rates")
            db.executescript(
                """CREATE TABLE table_rates (
                       table_no INTEGER PRIMARY KEY,
                       timed_rate_per_min_cents INTEGER NOT NULL,
                       package_rate_per_hour_cents INTEGER NOT NULL,
                       package_enabled INTEGER NOT NULL DEFAULT 1,
                       updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   );
                   INSERT INTO table_rates
                   (table_no, timed_rate_per_min_cents, package_rate_per_hour_cents)
                   VALUES (1, 400, 15000);"""
            )
            init_db()
            row = db.execute(
                "SELECT display_name, timed_rate_per_min_cents FROM table_rates WHERE table_no = 1"
            ).fetchone()
            self.assertEqual((row["display_name"], row["timed_rate_per_min_cents"]), ("", 400))

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
            self.db_value("SELECT timed_rate_per_min_cents FROM table_rates WHERE table_no = 1"),
            750,
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
            self.db_value("SELECT rate_per_min_cents FROM sessions WHERE id = ?", (session_id,)),
            750,
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
            self.db_value("SELECT discount_amount_cents FROM sessions WHERE id = ?", (session_id,)),
            75,
        )
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM sessions WHERE id = ?", (session_id,)),
            700,
        )
        idle_page = self.client.get("/tables/1").get_data(as_text=True)
        self.assertIn("平日包台優惠", idle_page)
        self.assertIn("包台每小時 120 元", idle_page)
        self.client.post(
            "/sessions/start",
            data={
                "table_no": "1",
                "mode": "package",
                "package_hours": "2",
                "discount_type_id": str(package_discount_id),
            },
        )
        self.assertEqual(
            self.db_value(
                "SELECT rate_per_hour_cents FROM sessions WHERE table_no = 1 AND status = 'active'"
            ),
            22000,
        )
        package_session_id = self.db_value(
            "SELECT id FROM sessions WHERE table_no = 1 AND status = 'active'"
        )
        page = self.client.get("/tables/1").get_data(as_text=True)
        self.assertIn("平日包台優惠", page)
        self.assertIn("包台每小時 120 元", page)
        self.client.post(f"/sessions/end/{package_session_id}")
        self.assertEqual(
            self.db_value(
                "SELECT discount_pricing_method FROM sessions WHERE id = ?",
                (package_session_id,),
            ),
            "package_hourly",
        )
        self.assertEqual(
            self.db_value(
                "SELECT discount_package_rate_per_hour_cents FROM sessions WHERE id = ?",
                (package_session_id,),
            ),
            12000,
        )
        self.assertEqual(
            self.db_value("SELECT table_fee_cents FROM sessions WHERE id = ?", (package_session_id,)),
            44000,
        )
        self.assertEqual(
            self.db_value(
                "SELECT discount_amount_cents FROM sessions WHERE id = ?", (package_session_id,)
            ),
            20000,
        )
        self.assertEqual(
            self.db_value("SELECT final_total_cents FROM sessions WHERE id = ?", (package_session_id,)),
            24000,
        )
        stats_page = self.client.get(
            f"/stats?date={current_business_day().isoformat()}"
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

    def test_multi_table_reservation_is_atomic_when_one_table_conflicts(self):
        booking = {
            "date": "2026-09-20", "table_no": "3", "event_type": "course",
            "guest_name": "Course", "phone": "", "start_time": "14:00",
            "end_time": "16:00", "note": "",
        }
        self.client.post("/reservations", data=booking)
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 1)

        response = self.client.post(
            "/reservations",
            data={**booking, "table_nos": ["2", "3", "4"]},
            follow_redirects=True,
        )
        self.assertIn("3 號桌", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 1)

        response = self.client.post(
            "/reservations",
            data={**booking, "table_nos": ["2", "4"]},
            follow_redirects=True,
        )
        self.assertIn("共 2 張球桌", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 3)
        with self.app.app_context():
            rows = get_db().execute(
                "SELECT table_no FROM reservations ORDER BY table_no"
            ).fetchall()
        self.assertEqual([row["table_no"] for row in rows], [2, 3, 4])

    def test_reservation_form_uses_multi_table_picker_and_minute_steps(self):
        page = self.client.get("/calendar?month=2026-09&date=2026-09-20").get_data(
            as_text=True
        )
        self.assertIn("球檯（可複選）", page)
        self.assertIn('name="table_nos"', page)
        self.assertNotIn("全選球檯", page)
        self.assertNotIn("全部取消", page)
        self.assertNotIn("已選 1 桌", page)
        self.assertIn("一般預約", page)
        self.assertIn("名稱／電話", page)
        self.assertNotIn("<label>電話", page)
        self.assertNotIn("同一球檯的預約不可重疊", page)
        self.assertNotEqual(CALENDAR_ITEM_TYPES["todo"]["color"], "#D4B483")
        self.assertEqual(page.count('class="schedule-submit-cell"'), 2)
        self.assertNotIn("每週重複", page)
        self.assertNotIn('name="repeat_weeks"', page)
        self.assertNotIn('step="300"', page)
        self.assertGreaterEqual(page.count('step="60"'), 4)
        self.assertIn(
            '<label>結束（選填） <input name="end_time" type="time" step="60"></label>',
            page,
        )
        self.assertNotIn("日期可以清空；時間可只填開始", page)
        self.assertEqual(page.count('class="muted calendar-day-empty"'), 2)

        response = self.client.post(
            "/reservations",
            data={
                "date": "2026-09-20", "table_nos": ["1", "2"],
                "event_type": "club", "guest_name": "Club", "phone": "",
                "start_time": "14:02", "end_time": "16:03", "note": "",
            },
            follow_redirects=True,
        )
        self.assertIn("預約已儲存", response.get_data(as_text=True))
        self.assertNotIn(">完成預約</button>", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 2)
        self.assertEqual(
            self.db_value("SELECT end_time FROM reservations ORDER BY id LIMIT 1"),
            "2026-09-20T16:03",
        )

    def test_calendar_item_times_allow_any_minute(self):
        response = self.client.post(
            "/calendar-items",
            data={
                "item_type": "event", "title": "Minute event",
                "scheduled_date": "2026-09-20", "start_time": "10:07",
                "end_time": "10:23", "return_date": "2026-09-20",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.db_value("SELECT start_time FROM calendar_items WHERE title = 'Minute event'"),
            "10:07",
        )
        page = response.get_data(as_text=True)
        self.assertIn('name="start_time" step="60" value="10:07"', page)
        self.assertIn('name="end_time" step="60" value="10:23"', page)

    def test_reservation_can_have_an_open_end_and_blocks_later_bookings(self):
        booking = {
            "date": "2026-09-20", "table_no": "3", "event_type": "reservation",
            "guest_name": "Walk-in", "phone": "", "start_time": "14:00",
            "end_time": "", "note": "現場計時",
        }
        response = self.client.post("/reservations", data=booking, follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertIn("預約已儲存", page)
        self.assertIn("14:00 起 · 3 號桌", page)
        self.assertEqual(
            self.db_value("SELECT end_time FROM reservations WHERE table_no = 3"), ""
        )

        response = self.client.post(
            "/reservations",
            data={**booking, "guest_name": "Overlap", "start_time": "14:30", "end_time": "15:30"},
            follow_redirects=True,
        )
        self.assertIn("時段已有預約", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 1)

        response = self.client.post(
            "/reservations",
            data={**booking, "guest_name": "Later", "start_time": "15:00", "end_time": "16:00"},
            follow_redirects=True,
        )
        self.assertIn("預約已儲存", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 2)

        response = self.client.post(
            "/reservations",
            data={**booking, "guest_name": "Earlier", "start_time": "12:00", "end_time": "13:00"},
            follow_redirects=True,
        )
        self.assertIn("預約已儲存", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 3)

    def test_open_end_reservation_blocks_one_hour_across_midnight(self):
        booking = {
            "date": "2026-09-20", "table_no": "3", "event_type": "reservation",
            "guest_name": "Late", "start_time": "23:30", "end_time": "",
        }
        self.client.post("/reservations", data=booking)
        overlap = self.client.post(
            "/reservations",
            data={**booking, "date": "2026-09-21", "start_time": "00:15", "end_time": "01:00"},
            follow_redirects=True,
        )
        self.assertIn("時段已有預約", overlap.get_data(as_text=True))
        adjacent = self.client.post(
            "/reservations",
            data={**booking, "date": "2026-09-21", "start_time": "00:30", "end_time": "01:30"},
            follow_redirects=True,
        )
        self.assertIn("預約已儲存", adjacent.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations"), 2)

    def test_concurrent_reservations_cannot_double_book_a_table(self):
        barrier = threading.Barrier(3)
        responses = []
        booking = {
            "date": "2026-11-10", "table_no": "6", "event_type": "reservation",
            "guest_name": "Concurrent Guest", "phone": "", "start_time": "18:00",
            "end_time": "20:00", "note": "",
        }

        def create_booking():
            with self.app.test_client() as client:
                barrier.wait(timeout=3)
                responses.append(client.post("/reservations", data=booking))

        threads = [threading.Thread(target=create_booking) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([response.status_code for response in responses], [302, 302])
        self.assertEqual(
            self.db_value(
                """SELECT COUNT(*) FROM reservations
                   WHERE table_no = 6 AND start_time = '2026-11-10T18:00'"""
            ),
            1,
        )

    def test_concurrent_shifts_cannot_overlap_for_an_employee(self):
        self.client.post("/shifts/employees/add", data={"name": "Concurrent Staff"})
        employee_id = self.db_value(
            "SELECT id FROM employees WHERE name = 'Concurrent Staff'"
        )
        shift_type_id = self.db_value(
            "SELECT id FROM shift_types WHERE is_active = 1 ORDER BY sort_order, id LIMIT 1"
        )
        barrier = threading.Barrier(3)
        responses = []
        shift = {
            "dates": ["2026-11-11"], "employee_id": str(employee_id),
            "shift_type_id": str(shift_type_id), "start_time": "09:00",
            "end_time": "13:00", "note": "",
        }

        def create_shift():
            with self.app.test_client() as client:
                barrier.wait(timeout=3)
                responses.append(client.post("/shifts/add", data=shift))

        threads = [threading.Thread(target=create_shift) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([response.status_code for response in responses], [302, 302])
        self.assertEqual(
            self.db_value(
                """SELECT COUNT(*) FROM shifts
                   WHERE employee_id = ? AND start_time = '2026-11-11T09:00'""",
                (employee_id,),
            ),
            1,
        )

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
                "end_time": "12:00", "note": "",
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
        self.assertNotIn("球檯預約與館內事項分開管理", page)
        self.assertIn('<input type="hidden" name="date" value="2026-09-18">', page)

        todo_id = self.db_value("SELECT id FROM calendar_items WHERE title = '修補入口地板'")
        self.client.post(
            f"/calendar-items/{todo_id}/toggle", data={"return_date": "2026-09-18"}
        )
        self.assertEqual(
            self.db_value("SELECT status FROM calendar_items WHERE id = ?", (todo_id,)),
            "completed",
        )
        start_only_response = self.client.post(
            "/calendar-items",
            data={
                **todo, "title": "只填開始時間", "unscheduled": "0",
                "start_time": "10:00",
            },
            follow_redirects=True,
        )
        self.assertIn("行事曆事項已儲存", start_only_response.get_data(as_text=True))
        self.assertEqual(
            self.db_value(
                "SELECT end_time FROM calendar_items WHERE title = '只填開始時間'"
            ),
            "",
        )

        self.client.post(
            "/calendar-items",
            data={**dated, "title": "同時段館內盤點"},
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 4)

        self.client.post(
            f"/calendar-items/{dated_id}/delete", data={"return_date": "2026-09-18"}
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM calendar_items"), 3)

    def test_calendar_item_optional_end_migration_preserves_existing_data(self):
        with self.app.app_context():
            self.app.config["BACKUP_ON_MIGRATION"] = False
            db = get_db()
            db.execute("DROP INDEX idx_calendar_items_date_status")
            db.execute("DROP TABLE calendar_items")
            db.executescript(
                """
                CREATE TABLE calendar_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    item_type TEXT NOT NULL DEFAULT 'todo',
                    scheduled_date TEXT,
                    start_time TEXT NOT NULL DEFAULT '',
                    end_time TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    completed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(
                        (start_time = '' AND end_time = '') OR
                        (scheduled_date IS NOT NULL AND start_time != '' AND end_time > start_time)
                    )
                );
                INSERT INTO calendar_items (
                    title, item_type, scheduled_date, start_time, end_time
                ) VALUES ('既有檢修', 'maintenance', '2026-09-18', '10:00', '12:00');
                """
            )
            migrate_calendar_items_for_optional_end(db)
            db.execute(
                """INSERT INTO calendar_items (
                       title, item_type, scheduled_date, start_time, end_time
                   ) VALUES ('只記開始', 'todo', '2026-09-19', '09:00', '')"""
            )
            db.commit()
            rows = db.execute(
                "SELECT title, end_time FROM calendar_items ORDER BY id"
            ).fetchall()

        self.assertEqual(
            [(row["title"], row["end_time"]) for row in rows],
            [("既有檢修", "12:00"), ("只記開始", "")],
        )

    def test_reservation_optional_end_migration_preserves_existing_data(self):
        with self.app.app_context():
            self.app.config["BACKUP_ON_MIGRATION"] = False
            db = get_db()
            db.execute("DROP INDEX idx_reservations_time")
            db.execute("DROP TABLE reservations")
            db.executescript(
                """
                CREATE TABLE reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_no INTEGER NOT NULL,
                    guest_name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT 'reservation',
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(end_time > start_time)
                );
                INSERT INTO reservations (
                    table_no, guest_name, start_time, end_time
                ) VALUES (2, '既有客人', '2026-09-18T10:00', '2026-09-18T12:00');
                """
            )
            migrate_reservations_for_optional_end(db)
            db.execute(
                """INSERT INTO reservations (
                       table_no, guest_name, start_time, end_time
                   ) VALUES (3, '現場計時', '2026-09-19T09:00', '')"""
            )
            db.commit()
            rows = db.execute(
                "SELECT guest_name, end_time FROM reservations ORDER BY id"
            ).fetchall()

        self.assertEqual(
            [(row["guest_name"], row["end_time"]) for row in rows],
            [("既有客人", "2026-09-18T12:00"), ("現場計時", "")],
        )

    def test_reservation_status_migration_allows_completion_on_existing_database(self):
        with self.app.app_context():
            self.app.config["BACKUP_ON_MIGRATION"] = False
            db = get_db()
            db.execute("DROP INDEX idx_reservations_time")
            db.execute("DROP TABLE reservations")
            db.executescript(
                """
                CREATE TABLE reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_no INTEGER NOT NULL,
                    guest_name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT 'reservation',
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'cancelled')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(end_time = '' OR end_time > start_time)
                );
                INSERT INTO reservations (table_no, guest_name, start_time)
                VALUES (1, '既有客人', '2026-09-23T22:05');
                """
            )
            migrate_reservations_for_optional_end(db)
            row = db.execute("SELECT id, guest_name FROM reservations").fetchone()
            reservation_id = row["id"]
            self.assertEqual(row["guest_name"], "既有客人")

        response = self.client.post(
            f"/reservations/{reservation_id}/complete",
            data={"date": "2026-09-23"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.db_value("SELECT status FROM reservations WHERE id = ?", (reservation_id,)),
            "completed",
        )

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

    def test_custom_shift_can_explicitly_end_next_day(self):
        self.client.post("/shifts/employees/add", data={"name": "Custom Night Staff"})
        employee_id = self.db_value(
            "SELECT id FROM employees WHERE name = 'Custom Night Staff'"
        )
        custom_type_id = self.db_value(
            "SELECT id FROM shift_types WHERE name = '自訂' AND is_active = 1"
        )

        response = self.client.post(
            "/shifts/add",
            data={
                "date": "2026-09-21", "dates": ["2026-09-21"],
                "employee_id": str(employee_id), "shift_type_id": str(custom_type_id),
                "start_time": "21:00", "end_time": "02:00",
                "has_ends_next_day_control": "1", "ends_next_day": "1", "note": "",
            },
            follow_redirects=True,
        )
        self.assertIn("班表已儲存，共 1 筆", response.get_data(as_text=True))
        with self.app.app_context():
            row = get_db().execute(
                "SELECT start_time, end_time FROM shifts WHERE employee_id = ?",
                (employee_id,),
            ).fetchone()
        self.assertEqual(row["start_time"], "2026-09-21T21:00")
        self.assertEqual(row["end_time"], "2026-09-22T02:00")

        page = self.client.get(
            "/shifts?month=2026-09&date=2026-09-21"
        ).get_data(as_text=True)
        self.assertIn("21:00–02:00（隔天）", page)
        self.assertIn("結束時間為隔天", page)

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
        self.assertIn("2026-09-17 營業日結算", page)
        self.assertNotIn("2026-09-17 06:00 至 2026-09-18 06:00", page)
        self.assertIn('class="inline-form settlement-date-picker"', page)
        self.assertIn('name="month" value="2026-09"', page)
        self.assertIn('name="date" value="2026-09-17" min="2026-09-01" max="2026-09-30"', page)
        self.assertIn('data-dialog-open="monthly-report-dialog">當月報表</button>', page)
        self.assertIn('<dialog class="settings-dialog finance-report-dialog"', page)
        self.assertNotIn("下載當日結算 CSV", page)

        response = self.client.get("/finance/export?month=2026-09")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        csv_text = response.data.decode("utf-8-sig")
        self.assertIn("2026-09-17", csv_text)
        self.assertIn("drinks", csv_text)
        self.assertIn("收支淨額", csv_text)

        self.assertEqual(self.client.get("/finance/export/day?date=2026-09-17").status_code, 404)
        self.client.post(
            "/finance/expenses",
            data={
                "expense_date": "2026-09-17", "category": "其他",
                "amount": "1", "description": "=2+3",
            },
        )
        expenses = self.client.get("/finance/export/expenses?month=2026-09")
        expense_csv = expenses.data.decode("utf-8-sig")
        self.assertIn("進貨", expense_csv)
        self.assertIn("drinks", expense_csv)
        self.assertIn("'=2+3", expense_csv)
        self.assertIn("月支出總計", expense_csv)

    def test_csv_safe_prefixes_spreadsheet_formulas(self):
        for value in ("=2+3", "+cmd", "-1+2", "@SUM(A1:A2)", "\tformula"):
            self.assertEqual(csv_safe(value), "'" + value)
        self.assertEqual(csv_safe("ordinary note"), "ordinary note")

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

    def test_table_count_cannot_hide_an_active_reservation(self):
        self.client.post(
            "/reservations",
            data={"date": "2026-10-10", "table_no": "10", "guest_name": "Guest",
                  "start_time": "10:00", "end_time": "11:00"},
        )
        self.assertEqual(self.db_value("SELECT COUNT(*) FROM reservations WHERE status='active'"), 1)
        response = self.client.post("/settings/rates", data={"table_count": "9"}, follow_redirects=True)
        self.assertIn("請先移桌、取消或完成這些桌號的預約：10", response.get_data(as_text=True))
        self.assertEqual(self.db_value("SELECT value FROM settings WHERE key='table_count'"), "10")
        reservation_id = self.db_value("SELECT id FROM reservations WHERE table_no=10")
        self.client.post(f"/reservations/{reservation_id}/cancel", data={"date": "2026-10-10"})
        self.client.post("/settings/rates", data={"table_count": "9"})
        self.assertEqual(self.db_value("SELECT value FROM settings WHERE key='table_count'"), "9")

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
                "end_time": "11:00", "note": "",
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


class SecurityProtectionTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database = str(Path(self.temp_directory.name) / "security-test.db")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": database,
            "SECRET_KEY": "security-test",
            "CSRF_ENABLED": True,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_directory.cleanup()

    def csrf_token(self) -> str:
        self.client.get("/")
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def waiting_count(self) -> int:
        with self.app.app_context():
            return get_db().execute("SELECT COUNT(*) FROM customer_tabs").fetchone()[0]

    def test_post_requires_csrf_token(self):
        response = self.client.post(
            "/waiting/create",
            data={"display_name": "blocked", "guest_count": "1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.waiting_count(), 0)

    def test_cross_site_post_is_rejected_even_with_token(self):
        token = self.csrf_token()
        response = self.client.post(
            "/waiting/create",
            data={"display_name": "blocked", "guest_count": "1", "_csrf_token": token},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.waiting_count(), 0)

    def test_same_origin_post_with_token_succeeds(self):
        token = self.csrf_token()
        response = self.client.post(
            "/waiting/create",
            data={"display_name": "allowed", "guest_count": "1", "_csrf_token": token},
            headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.waiting_count(), 1)

    def test_rendered_post_forms_include_csrf_token(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('name="_csrf_token"', page)

    def test_untrusted_host_is_rejected(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
