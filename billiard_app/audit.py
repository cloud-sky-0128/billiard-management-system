from __future__ import annotations

import sqlite3
from datetime import datetime


AUDIT_ACTION_LABELS = {
    "order_delete": "刪除訂單",
    "cash_update": "修改每日實收",
    "expense_delete": "刪除支出",
    "data_reset": "清除測試資料",
}


def record_audit(
    db: sqlite3.Connection,
    action: str,
    target: str,
    before_value: str,
    after_value: str,
) -> None:
    """Add an audit event inside the caller's transaction; never commit separately."""
    db.execute(
        """INSERT INTO audit_events
           (action, target, before_value, after_value, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            action,
            target,
            before_value,
            after_value,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
