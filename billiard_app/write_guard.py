"""Serialize application writes, including the backup/check/reset sequence."""
import os
import time
from pathlib import Path

from flask import abort, current_app, g, request

from .db import close_db
from .security import SAFE_METHODS


def acquire_write_guard():
    if request.method in SAFE_METHODS:
        return
    database = current_app.config["DATABASE"]
    if database == ":memory:":
        return
    handle = open(str(Path(database).resolve()) + ".write.lock", "a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + 15
    while True:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            g.write_guard = handle
            return
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                abort(503, description="系統正在處理其他操作，請稍後重試。")
            time.sleep(0.02)


def release_write_guard(_exception=None):
    handle = g.pop("write_guard", None)
    if handle is not None:
        # Roll back unfinished work before allowing the next writer to proceed.
        close_db()
        handle.close()
