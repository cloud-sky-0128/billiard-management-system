from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from flask import Flask

from .config import PROJECT_ROOT
from .db import init_app as init_database
from .money import format_cents


def _ensure_writable_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write-test-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        probe.write_bytes(b"")
    finally:
        probe.unlink(missing_ok=True)


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
    )
    if test_config:
        app.config.update(test_config)

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

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    init_database(app)
    return app
