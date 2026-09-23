from __future__ import annotations

import hmac
import re
import secrets
from urllib.parse import urlsplit

from flask import abort, current_app, request, session
from markupsafe import escape


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_SESSION_KEY = "_csrf_token"
POST_FORM_PATTERN = re.compile(
    r"<form\b(?=[^>]*\bmethod\s*=\s*(['\"])post\1)[^>]*>",
    flags=re.IGNORECASE,
)


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _same_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.scheme == request.scheme
        and parsed.netloc == request.host
    )


def protect_unsafe_request() -> None:
    if not current_app.config.get("CSRF_ENABLED", True) or request.method in SAFE_METHODS:
        return

    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        abort(403, description="跨網站請求已被拒絕，請重新載入頁面後再試。")

    origin = request.headers.get("Origin")
    if origin and not _same_origin(origin):
        abort(403, description="請求來源不正確，請重新載入頁面後再試。")

    expected = session.get(CSRF_SESSION_KEY, "")
    supplied = request.form.get("_csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
        abort(403, description="安全驗證已失效，請重新載入頁面後再試。")


def add_csrf_fields(response):
    if (
        response.direct_passthrough
        or response.mimetype != "text/html"
    ):
        return response

    body = response.get_data(as_text=True)
    if not POST_FORM_PATTERN.search(body):
        return response

    field = (
        '<input type="hidden" name="_csrf_token" value="'
        + str(escape(csrf_token()))
        + '">'
    )
    if not current_app.config.get("CSRF_ENABLED", True):
        field = ""
    from .operations import operation_field
    response.set_data(POST_FORM_PATTERN.sub(lambda match: match.group(0) + field + operation_field(match.group(0)), body))
    return response
