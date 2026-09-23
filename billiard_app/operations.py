"""Single-use form operations, recorded in the same transaction as the change."""
import hashlib
import json
import re
import secrets
from html import unescape

from flask import abort, current_app, flash, g, redirect, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import escape

from .db import get_db


PATHS = re.compile(r"^/(orders/add|sessions/start|sessions/extend/\d+|sessions/\d+/transfer|finance/expenses)$")


def serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="form-operation-v1")


def operation_field(form_tag):
    action = re.search(r'''\baction\s*=\s*['"]([^'"]*)['"]''', form_tag, re.I)
    if not action or not PATHS.fullmatch(unescape(action.group(1))):
        return ""
    owner = session.setdefault("operation_owner", secrets.token_hex(24))
    token = serializer().dumps([owner, unescape(action.group(1)), secrets.token_hex(24)])
    return '<input type="hidden" name="_operation_token" value="' + str(escape(token)) + '">'


def check_operation():
    if request.method != "POST" or not PATHS.fullmatch(request.path):
        return None
    token = request.form.get("_operation_token", "")
    if not token and current_app.testing and not current_app.config.get("CSRF_ENABLED", True):
        return None
    try:
        owner, path, nonce = serializer().loads(token, max_age=86400)
        if owner != session.get("operation_owner") or path != request.path:
            raise ValueError
    except (BadSignature, ValueError, TypeError):
        abort(409, description="操作表單已失效，請重新整理頁面。")
    payload = [(key, request.form.getlist(key)) for key in sorted(request.form)
               if key not in {"_operation_token", "_csrf_token"}]
    fingerprint = hashlib.sha256(json.dumps([path, payload], ensure_ascii=True).encode()).hexdigest()
    previous = get_db().execute("SELECT fingerprint FROM operation_receipts WHERE id = ?", (nonce,)).fetchone()
    if previous:
        if previous["fingerprint"] != fingerprint:
            abort(409, description="這份表單已處理，請重新整理後再操作。")
        flash("這筆操作已處理，沒有重複新增或收款。", "success")
        return redirect(url_for("tables.dashboard"))
    g.operation_receipt = (nonce, fingerprint)
    return None


def commit_operation(db):
    receipt = g.get("operation_receipt")
    if receipt and db.in_transaction:
        db.execute("INSERT INTO operation_receipts (id, fingerprint) VALUES (?, ?)", receipt)
    db.commit()
    g.pop("operation_receipt", None)
