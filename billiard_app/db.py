from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, current_app, g

from .config import DEFAULT_SETTINGS, EMPLOYEE_COLOR_PALETTE, SEED_MENU
from .money import to_cents


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_exception: Exception | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_setting_int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default))))
    except ValueError:
        return default


def get_setting_cents(key: str, default: object) -> int:
    try:
        return to_cents(get_setting(key, str(default)))
    except ValueError:
        return to_cents(default)


def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    db.commit()


def ensure_column(table: str, column: str, ddl: str) -> None:
    db = get_db()
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def backup_before_money_migration(db: sqlite3.Connection) -> Path | None:
    database = current_app.config["DATABASE"]
    if database == ":memory:" or not current_app.config.get("BACKUP_ON_MIGRATION", True):
        return None
    database_path = Path(database)
    backup_directory = database_path.parent / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_directory / f"billiard-before-money-migration-{timestamp}.db"
    destination = sqlite3.connect(backup_path)
    try:
        db.backup(destination)
    finally:
        destination.close()
    return backup_path


def migrate_money_to_cents(db: sqlite3.Connection) -> Path | None:
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'menu_items'"
    ).fetchone()
    if not table_exists:
        return None
    columns = {row["name"] for row in db.execute("PRAGMA table_info(menu_items)")}
    if "price_cents" in columns:
        return None
    if "price" not in columns:
        raise RuntimeError("menu_items 缺少可遷移的 price 欄位。")

    db.commit()
    backup_path = backup_before_money_migration(db)
    migration_path = Path(__file__).with_name("migrations") / "001_money_to_cents.sql"
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.executescript(migration_path.read_text(encoding="utf-8"))
    except sqlite3.Error:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")

    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"金額欄位遷移後發現 {len(violations)} 筆外鍵錯誤。")
    return backup_path


def migrate_shift_types_for_overnight(db: sqlite3.Connection) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(shift_types)").fetchall()}
    if "ends_next_day" in columns:
        return

    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.executescript(
            """
            BEGIN;
            CREATE TABLE shift_types_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                ends_next_day INTEGER NOT NULL DEFAULT 0 CHECK(ends_next_day IN (0, 1)),
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(ends_next_day = 1 OR end_time > start_time)
            );
            INSERT INTO shift_types_new (
                id, name, start_time, end_time, ends_next_day, sort_order, is_active, created_at
            )
            SELECT id, name, start_time, end_time, 0, id, is_active, created_at
            FROM shift_types;
            DROP TABLE shift_types;
            ALTER TABLE shift_types_new RENAME TO shift_types;
            COMMIT;
            """
        )
    except sqlite3.Error:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def seed_default_data(db: sqlite3.Connection) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )

    table_count = get_setting_int("table_count", 10)
    package_min_no = get_setting_int("package_min_table_no", 3)
    package_max_no = get_setting_int("package_max_table_no", 10)
    for table_no in range(1, table_count + 1):
        if table_no == 1:
            timed_rate_cents = get_setting_cents("timed_rate_single", "4.0")
        elif table_no == 2:
            timed_rate_cents = get_setting_cents("timed_rate_double", "3.5")
        else:
            timed_rate_cents = get_setting_cents("timed_rate_group", "3.0")
        db.execute(
            """INSERT INTO table_rates
               (table_no, timed_rate_per_min_cents, package_rate_per_hour_cents, package_enabled)
               VALUES (?, ?, ?, ?) ON CONFLICT(table_no) DO NOTHING""",
            (
                table_no,
                timed_rate_cents,
                get_setting_cents("package_hour_rate", "150"),
                int(package_min_no <= table_no <= package_max_no),
            ),
        )

    default_shift_types = (
        ("早班", get_setting("shift_morning_start", "09:00"), get_setting("shift_morning_end", "13:00"), 0),
        ("午班", get_setting("shift_afternoon_start", "13:00"), get_setting("shift_afternoon_end", "18:00"), 0),
        ("晚班", get_setting("shift_evening_start", "18:00"), get_setting("shift_evening_end", "23:00"), 0),
        ("自訂", "09:00", "17:00", 0),
    )
    db.executemany(
        """INSERT INTO shift_types (name, start_time, end_time, ends_next_day)
           VALUES (?, ?, ?, ?) ON CONFLICT(name) DO NOTHING""",
        default_shift_types,
    )

    if db.execute("SELECT COUNT(*) AS count FROM menu_items").fetchone()["count"] == 0:
        for category_name, items in SEED_MENU.items():
            db.execute("INSERT INTO categories (name) VALUES (?)", (category_name,))
            category_id = db.execute(
                "SELECT id FROM categories WHERE name = ?", (category_name,)
            ).fetchone()["id"]
            db.executemany(
                "INSERT INTO menu_items (category_id, name, price_cents) VALUES (?, ?, ?)",
                [(category_id, name, to_cents(price)) for name, price in items],
            )


def verify_active_session_uniqueness(db: sqlite3.Connection) -> None:
    sessions_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
    ).fetchone()
    if not sessions_exists:
        return

    duplicates = db.execute(
        """SELECT table_no, COUNT(*) AS active_count
           FROM sessions
           WHERE status = 'active'
           GROUP BY table_no
           HAVING COUNT(*) > 1
           ORDER BY table_no"""
    ).fetchall()
    if duplicates:
        details = ", ".join(
            f"{row['table_no']} 號桌有 {row['active_count']} 筆" for row in duplicates
        )
        raise RuntimeError(
            f"無法建立同桌開台保護，請先處理重複的進行中紀錄：{details}"
        )


def init_db() -> None:
    db = get_db()
    migrate_money_to_cents(db)
    verify_active_session_uniqueness(db)
    schema_path = Path(__file__).with_name("schema.sql")
    db.executescript(schema_path.read_text(encoding="utf-8"))
    migrate_shift_types_for_overnight(db)

    employee_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(employees)").fetchall()
    }
    employee_color_is_new = "color" not in employee_columns

    ensure_column("sessions", "discount_type_id", "INTEGER")
    ensure_column("sessions", "discount_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column(
        "sessions", "discount_pricing_method", "TEXT NOT NULL DEFAULT 'percentage'"
    )
    ensure_column("sessions", "discount_percent", "REAL NOT NULL DEFAULT 100")
    ensure_column("sessions", "discount_scope", "TEXT NOT NULL DEFAULT 'table_and_drink'")
    ensure_column("categories", "is_active", "INTEGER NOT NULL DEFAULT 1")
    ensure_column("orders", "item_category_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column("orders", "sugar_level", "TEXT NOT NULL DEFAULT ''")
    ensure_column("orders", "ice_level", "TEXT NOT NULL DEFAULT ''")
    ensure_column("orders", "is_served", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(
        "discount_types", "pricing_method", "TEXT NOT NULL DEFAULT 'percentage'"
    )
    ensure_column("discount_types", "applicable_mode", "TEXT NOT NULL DEFAULT 'all'")
    ensure_column("shifts", "employee_id", "INTEGER")
    ensure_column("shifts", "shift_type", "TEXT NOT NULL DEFAULT 'custom'")
    ensure_column("shifts", "shift_type_id", "INTEGER")
    ensure_column("shifts", "shift_type_name", "TEXT NOT NULL DEFAULT ''")
    ensure_column("employees", "color", "TEXT NOT NULL DEFAULT '#C96A4A'")
    ensure_column("shift_types", "sort_order", "INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_shifts_employee_time
           ON shifts(employee_id, start_time, end_time)"""
    )

    db.execute(
        """INSERT OR IGNORE INTO employees (name)
           SELECT DISTINCT staff_name FROM shifts WHERE TRIM(staff_name) != ''"""
    )
    db.execute(
        """UPDATE shifts SET employee_id = (
               SELECT id FROM employees WHERE employees.name = shifts.staff_name
           ) WHERE employee_id IS NULL"""
    )

    seed_default_data(db)
    db.execute(
        """INSERT OR IGNORE INTO schema_migrations (version, name)
           VALUES (1, 'money_to_integer_cents')"""
    )
    db.execute(
        """UPDATE sessions
           SET discount_name = CASE
               WHEN COALESCE(discount_percent, 100) = 100 THEN '原價'
               ELSE '自訂折扣'
           END
           WHERE discount_name = '' AND status = 'closed'"""
    )
    db.execute("UPDATE shift_types SET sort_order = id WHERE sort_order = 0")
    if employee_color_is_new:
        employees = db.execute("SELECT id FROM employees ORDER BY id").fetchall()
        db.executemany(
            "UPDATE employees SET color = ? WHERE id = ?",
            [
                (EMPLOYEE_COLOR_PALETTE[index % len(EMPLOYEE_COLOR_PALETTE)], row["id"])
                for index, row in enumerate(employees)
            ],
        )
    db.execute(
        """UPDATE shifts SET shift_type_id = (
               SELECT id FROM shift_types WHERE name = CASE shifts.shift_type
                   WHEN 'morning' THEN '早班'
                   WHEN 'afternoon' THEN '午班'
                   WHEN 'evening' THEN '晚班'
                   ELSE '自訂'
               END
           ) WHERE shift_type_id IS NULL"""
    )
    db.execute(
        """UPDATE shifts SET shift_type_name = COALESCE(
               (SELECT name FROM shift_types WHERE id = shifts.shift_type_id), '自訂'
           ) WHERE shift_type_name = ''"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_shifts_type
           ON shifts(shift_type_id, start_time)"""
    )

    db.commit()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
