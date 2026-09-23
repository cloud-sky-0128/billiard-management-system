from __future__ import annotations

from functools import wraps
import hashlib
import hmac
import time
from urllib.parse import urlsplit

from flask import redirect, request, session, url_for

from .db import get_setting


ADMIN_PASSWORD_KEY = "admin_password_hash"


def admin_password_hash() -> str:
    return get_setting(ADMIN_PASSWORD_KEY)


def admin_is_authenticated() -> bool:
    expected = hashlib.sha256(admin_password_hash().encode()).hexdigest()
    valid = (
        session.get("admin_authenticated")
        and hmac.compare_digest(str(session.get("admin_version", "")), expected)
        and 0 <= time.time() - session.get("admin_last_seen", 0) < 1800
    )
    if not valid:
        session.pop("admin_authenticated", None)
        return False
    session["admin_last_seen"] = time.time()
    return True


def start_admin_session():
    session.clear()
    session["admin_authenticated"] = True
    session["admin_version"] = hashlib.sha256(admin_password_hash().encode()).hexdigest()
    session["admin_last_seen"] = time.time()


def safe_admin_next(value: str | None) -> str:
    if not value:
        return url_for("settings.general_settings_page")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return url_for("settings.general_settings_page")
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return url_for("settings.general_settings_page")
    return value


def admin_login_redirect():
    next_url = request.full_path.rstrip("?")
    return redirect(url_for("settings.admin_access", next=next_url))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_password_hash() or not admin_is_authenticated():
            return admin_login_redirect()
        return view(*args, **kwargs)

    return wrapped
