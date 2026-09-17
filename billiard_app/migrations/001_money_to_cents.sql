BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE menu_items RENAME TO legacy_menu_items_money;
ALTER TABLE table_rates RENAME TO legacy_table_rates_money;
ALTER TABLE discount_types RENAME TO legacy_discount_types_money;
ALTER TABLE sessions RENAME TO legacy_sessions_money;
ALTER TABLE orders RENAME TO legacy_orders_money;
ALTER TABLE daily_cash_records RENAME TO legacy_daily_cash_records_money;
ALTER TABLE expenses RENAME TO legacy_expenses_money;

CREATE TABLE menu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id)
);

CREATE TABLE table_rates (
    table_no INTEGER PRIMARY KEY CHECK(table_no > 0),
    timed_rate_per_min_cents INTEGER NOT NULL CHECK(timed_rate_per_min_cents > 0),
    package_rate_per_hour_cents INTEGER NOT NULL CHECK(package_rate_per_hour_cents > 0),
    package_enabled INTEGER NOT NULL DEFAULT 1 CHECK(package_enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE discount_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    pricing_method TEXT NOT NULL DEFAULT 'percentage'
        CHECK(pricing_method IN ('percentage', 'package_hourly')),
    discount_percent REAL NOT NULL CHECK(discount_percent > 0 AND discount_percent <= 100),
    package_rate_per_hour_cents INTEGER
        CHECK(package_rate_per_hour_cents IS NULL OR package_rate_per_hour_cents > 0),
    applicable_mode TEXT NOT NULL DEFAULT 'all'
        CHECK(applicable_mode IN ('all', 'timed', 'package')),
    discount_scope TEXT NOT NULL DEFAULT 'table_and_drink'
        CHECK(discount_scope IN ('table_only', 'table_and_drink', 'all')),
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('timed', 'package')),
    start_time TEXT NOT NULL,
    end_time TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'closed')),
    package_hours INTEGER,
    rate_per_min_cents INTEGER,
    rate_per_hour_cents INTEGER,
    table_fee_cents INTEGER NOT NULL DEFAULT 0,
    food_fee_cents INTEGER NOT NULL DEFAULT 0,
    discount_type_id INTEGER,
    discount_name TEXT NOT NULL DEFAULT '',
    discount_pricing_method TEXT NOT NULL DEFAULT 'percentage',
    discount_package_rate_per_hour_cents INTEGER,
    discount_percent REAL NOT NULL DEFAULT 100,
    discount_scope TEXT NOT NULL DEFAULT 'table_and_drink',
    discount_amount_cents INTEGER NOT NULL DEFAULT 0,
    final_total_cents INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(discount_type_id) REFERENCES discount_types(id)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    item_category_name TEXT NOT NULL DEFAULT '',
    sugar_level TEXT NOT NULL DEFAULT '',
    ice_level TEXT NOT NULL DEFAULT '',
    is_served INTEGER NOT NULL DEFAULT 0,
    unit_price_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE daily_cash_records (
    record_date TEXT PRIMARY KEY,
    actual_revenue_cents INTEGER,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(actual_revenue_cents IS NULL OR actual_revenue_cents >= 0)
);

CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO menu_items (id, category_id, name, price_cents, is_active, created_at)
SELECT id, category_id, name, CAST(ROUND(price * 100) AS INTEGER), is_active, created_at
FROM legacy_menu_items_money;

INSERT INTO table_rates (
    table_no, timed_rate_per_min_cents, package_rate_per_hour_cents,
    package_enabled, updated_at
)
SELECT table_no,
       CAST(ROUND(timed_rate_per_min * 100) AS INTEGER),
       CAST(ROUND(package_rate_per_hour * 100) AS INTEGER),
       package_enabled, updated_at
FROM legacy_table_rates_money;

INSERT INTO discount_types (
    id, name, pricing_method, discount_percent, package_rate_per_hour_cents,
    applicable_mode, discount_scope, start_time, end_time, is_active, created_at
)
SELECT id, name, pricing_method, discount_percent,
       CASE WHEN package_rate_per_hour IS NULL THEN NULL
            ELSE CAST(ROUND(package_rate_per_hour * 100) AS INTEGER) END,
       applicable_mode, discount_scope, start_time, end_time, is_active, created_at
FROM legacy_discount_types_money;

INSERT INTO sessions (
    id, table_no, mode, start_time, end_time, status, package_hours,
    rate_per_min_cents, rate_per_hour_cents, table_fee_cents, food_fee_cents,
    discount_type_id, discount_name, discount_pricing_method,
    discount_package_rate_per_hour_cents, discount_percent, discount_scope,
    discount_amount_cents, final_total_cents, created_at
)
SELECT id, table_no, mode, start_time, end_time, status, package_hours,
       CASE WHEN rate_per_min IS NULL THEN NULL ELSE CAST(ROUND(rate_per_min * 100) AS INTEGER) END,
       CASE WHEN rate_per_hour IS NULL THEN NULL ELSE CAST(ROUND(rate_per_hour * 100) AS INTEGER) END,
       CAST(ROUND(COALESCE(table_fee, 0) * 100) AS INTEGER),
       CAST(ROUND(COALESCE(food_fee, 0) * 100) AS INTEGER),
       discount_type_id, discount_name, discount_pricing_method,
       CASE WHEN discount_package_rate_per_hour IS NULL THEN NULL
            ELSE CAST(ROUND(discount_package_rate_per_hour * 100) AS INTEGER) END,
       discount_percent, discount_scope,
       CAST(ROUND(COALESCE(discount_amount, 0) * 100) AS INTEGER),
       CAST(ROUND(COALESCE(final_total, 0) * 100) AS INTEGER),
       created_at
FROM legacy_sessions_money;

INSERT INTO orders (
    id, session_id, item_id, item_name, item_category_name, sugar_level,
    ice_level, is_served, unit_price_cents, quantity, subtotal_cents, created_at
)
SELECT id, session_id, item_id, item_name, item_category_name, sugar_level,
       ice_level, is_served, CAST(ROUND(unit_price * 100) AS INTEGER), quantity,
       CAST(ROUND(subtotal * 100) AS INTEGER), created_at
FROM legacy_orders_money;

INSERT INTO daily_cash_records (record_date, actual_revenue_cents, note, updated_at)
SELECT record_date,
       CASE WHEN actual_revenue IS NULL THEN NULL
            ELSE CAST(ROUND(actual_revenue * 100) AS INTEGER) END,
       note, updated_at
FROM legacy_daily_cash_records_money;

INSERT INTO expenses (id, expense_date, category, description, amount_cents, created_at)
SELECT id, expense_date, category, description,
       CAST(ROUND(amount * 100) AS INTEGER), created_at
FROM legacy_expenses_money;

DROP TABLE legacy_orders_money;
DROP TABLE legacy_sessions_money;
DROP TABLE legacy_discount_types_money;
DROP TABLE legacy_table_rates_money;
DROP TABLE legacy_menu_items_money;
DROP TABLE legacy_daily_cash_records_money;
DROP TABLE legacy_expenses_money;

INSERT OR IGNORE INTO schema_migrations (version, name)
VALUES (1, 'money_to_integer_cents');

COMMIT;
