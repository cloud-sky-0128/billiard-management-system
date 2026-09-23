from __future__ import annotations

import os
import logging
import secrets
import sys
from pathlib import Path

from flask import Flask, flash, redirect, url_for

from .config import PROJECT_ROOT
from .db import init_app as init_database, table_label
from .maintenance import backup_warning, configure_file_logging, create_daily_backups, create_recent_backups, verify_database
from .money import format_cents, MoneyLimitError
from .security import add_csrf_fields, csrf_token, protect_unsafe_request
from .write_guard import acquire_write_guard, release_write_guard
from .operations import check_operation


def _ensure_writable_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write-test-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"")
    finally:
        probe.unlink(missing_ok=True)


def _database_file_exists(path: Path) -> bool:
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def default_database_path() -> Path:
    configured_path = os.environ.get("BILLIARD_DATABASE", "").strip()
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    if getattr(sys, "frozen", False):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        primary_directory = (
            Path(local_app_data) / "BilliardManager"
            if local_app_data
            else Path.home() / ".billiard-manager"
        )
        fallback_directory = Path(sys.executable).resolve().parent / "data"
        primary = primary_directory / "billiard.db"
        fallback = fallback_directory / "billiard.db"
        primary_exists = _database_file_exists(primary)
        fallback_exists = _database_file_exists(fallback) if fallback != primary else False
        if primary_exists and fallback_exists:
            raise OSError(
                f"找到兩份資料庫：{primary}；{fallback}。"
                "為避免選錯資料，請先確認資料內容，並用 BILLIARD_DATABASE 指定正式資料庫。"
            )
        existing = primary if primary_exists else fallback if fallback_exists else None
        if existing is not None:
            _ensure_writable_directory(existing.parent)
            return existing
        errors = []
        for data_directory in dict.fromkeys((primary_directory, fallback_directory)):
            try:
                _ensure_writable_directory(data_directory)
                return data_directory / "billiard.db"
            except OSError as exc:
                errors.append(f"{data_directory}: {exc}")
        raise OSError("找不到可寫入的資料夾。" + "；".join(errors))
    return PROJECT_ROOT / "billiard.db"


def create_app(test_config: dict | None = None) -> Flask:
    database_path = default_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=str(database_path),
        BACKUP_ON_RESET=True,
        BACKUP_ON_MIGRATION=True,
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        CSRF_ENABLED=True,
        TRUSTED_HOSTS=("127.0.0.1", "localhost", "::1"),
    )
    if test_config:
        app.config.update(test_config)
        if app.config.get("TESTING") and "CSRF_ENABLED" not in test_config:
            app.config["CSRF_ENABLED"] = False

    if not app.config.get("TESTING") and app.config["DATABASE"] != ":memory:":
        database = app.config["DATABASE"]
        app.config["LOG_FILE"] = str(configure_file_logging(database))
        for handler in logging.getLogger("billiard").handlers:
            if handler not in app.logger.handlers:
                app.logger.addHandler(handler)
        if Path(database).exists():
            try:
                verify_database(Path(database), require_schema=False)
            except Exception:
                app.logger.exception("Database startup check failed")
                raise

    from .blueprints.finance import bp as finance_bp
    from .blueprints.menu import bp as menu_bp
    from .blueprints.reservations import bp as reservations_bp
    from .blueprints.settings import bp as settings_bp
    from .blueprints.shifts import bp as shifts_bp
    from .blueprints.stats import bp as stats_bp
    from .blueprints.tables import bp as tables_bp

    app.register_blueprint(tables_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(menu_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(reservations_bp)
    app.register_blueprint(shifts_bp)
    app.jinja_env.filters["money"] = format_cents
    app.jinja_env.globals["table_label"] = table_label
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def validate_unsafe_request():
        protect_unsafe_request()
        acquire_write_guard()
        return check_operation()

    app.teardown_request(release_write_guard)

    @app.errorhandler(MoneyLimitError)
    def invalid_calculated_amount(error):
        from .db import get_db
        get_db().rollback()
        flash(str(error) + " 本次操作未儲存。", "error")
        return redirect(url_for("tables.dashboard"))

    @app.after_request
    def add_security_headers(response):
        response = add_csrf_fields(response)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    try:
        init_database(app)
    except Exception:
        app.logger.exception("Database initialization failed")
        raise
    if not app.config.get("TESTING") and app.config["DATABASE"] != ":memory:":
        database = app.config["DATABASE"]
        extra = os.environ.get("BILLIARD_BACKUP_DIR", "")
        try:
            create_daily_backups(database, extra)
            create_recent_backups(database, extra)
            app.config["BACKUP_WARNING"] = backup_warning(database, extra)
        except Exception as exc:
            app.logger.exception("Automatic backup failed")
            app.config["BACKUP_WARNING"] = f"自動備份失敗：{exc}。請檢查備份目錄與錯誤日誌。"
    return app
