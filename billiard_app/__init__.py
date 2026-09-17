from __future__ import annotations

import os

from flask import Flask

from .config import PROJECT_ROOT
from .db import init_app as init_database


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-before-deployment"),
        DATABASE=str(PROJECT_ROOT / "billiard.db"),
        BACKUP_ON_RESET=True,
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

    init_database(app)
    return app
