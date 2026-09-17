CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS menu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price REAL NOT NULL CHECK(price > 0),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS table_rates (
    table_no INTEGER PRIMARY KEY CHECK(table_no > 0),
    timed_rate_per_min REAL NOT NULL CHECK(timed_rate_per_min > 0),
    package_rate_per_hour REAL NOT NULL CHECK(package_rate_per_hour > 0),
    package_enabled INTEGER NOT NULL DEFAULT 1 CHECK(package_enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS discount_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    pricing_method TEXT NOT NULL DEFAULT 'percentage'
        CHECK(pricing_method IN ('percentage', 'package_hourly')),
    discount_percent REAL NOT NULL CHECK(discount_percent > 0 AND discount_percent <= 100),
    package_rate_per_hour REAL CHECK(package_rate_per_hour IS NULL OR package_rate_per_hour > 0),
    applicable_mode TEXT NOT NULL DEFAULT 'all'
        CHECK(applicable_mode IN ('all', 'timed', 'package')),
    discount_scope TEXT NOT NULL DEFAULT 'table_and_drink'
        CHECK(discount_scope IN ('table_only', 'table_and_drink', 'all')),
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('timed', 'package')),
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
    package_hours INTEGER,
    rate_per_min REAL,
    rate_per_hour REAL,
    table_fee REAL NOT NULL DEFAULT 0,
    food_fee REAL NOT NULL DEFAULT 0,
    discount_type_id INTEGER,
    discount_name TEXT NOT NULL DEFAULT '',
    discount_pricing_method TEXT NOT NULL DEFAULT 'percentage',
    discount_package_rate_per_hour REAL,
    discount_percent REAL NOT NULL DEFAULT 100,
    discount_scope TEXT NOT NULL DEFAULT 'table_and_drink',
    discount_amount REAL NOT NULL DEFAULT 0,
    final_total REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(discount_type_id) REFERENCES discount_types(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_one_active_per_table
    ON sessions(table_no) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_sessions_closed_end_time
    ON sessions(status, end_time);

CREATE INDEX IF NOT EXISTS idx_sessions_start_time
    ON sessions(start_time);

CREATE INDEX IF NOT EXISTS idx_discount_types_active
    ON discount_types(is_active, name);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    item_category_name TEXT NOT NULL DEFAULT '',
    sugar_level TEXT NOT NULL DEFAULT '',
    ice_level TEXT NOT NULL DEFAULT '',
    is_served INTEGER NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    subtotal REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_session_id ON orders(session_id);

CREATE INDEX IF NOT EXISTS idx_menu_items_category_active
    ON menu_items(category_id, is_active);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no INTEGER NOT NULL,
    guest_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'reservation',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(end_time > start_time)
);

CREATE INDEX IF NOT EXISTS idx_reservations_time
    ON reservations(table_no, status, start_time, end_time);

CREATE TABLE IF NOT EXISTS calendar_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    item_type TEXT NOT NULL DEFAULT 'todo'
        CHECK(item_type IN ('todo', 'maintenance', 'event', 'competition', 'other')),
    scheduled_date TEXT,
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(scheduled_date IS NULL OR length(scheduled_date) = 10),
    CHECK(
        (start_time = '' AND end_time = '') OR
        (scheduled_date IS NOT NULL AND start_time != '' AND end_time > start_time)
    )
);

CREATE INDEX IF NOT EXISTS idx_calendar_items_date_status
    ON calendar_items(scheduled_date, status);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    color TEXT NOT NULL DEFAULT '#C96A4A',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shift_types (
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

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    shift_type_id INTEGER,
    staff_name TEXT NOT NULL,
    shift_type_name TEXT NOT NULL DEFAULT '',
    shift_type TEXT NOT NULL DEFAULT 'custom',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(end_time > start_time),
    FOREIGN KEY(employee_id) REFERENCES employees(id),
    FOREIGN KEY(shift_type_id) REFERENCES shift_types(id)
);

CREATE INDEX IF NOT EXISTS idx_shifts_start ON shifts(start_time);

CREATE TABLE IF NOT EXISTS daily_cash_records (
    record_date TEXT PRIMARY KEY,
    actual_revenue REAL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(actual_revenue IS NULL OR actual_revenue >= 0)
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL CHECK(amount > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
