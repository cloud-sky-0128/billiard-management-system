"""Windows desktop-window entry point for the existing local application."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import urllib.request

from waitress import create_server

from billiard_app import create_app
from billiard_app.maintenance import start_backup_scheduler
from desktop_app import DEFAULT_PORT, HOST, acquire_windows_mutex, application_url


def show_message(message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(
            None, message, "撞球館管理系統", 0x10 if error else 0x40
        )
    else:
        print(message, file=sys.stderr if error else sys.stdout)


def wait_for_server(url: str, server_thread: threading.Thread) -> None:
    for _ in range(60):
        if not server_thread.is_alive():
            raise RuntimeError("本機服務未能啟動，請查看錯誤日誌。")
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("本機服務啟動逾時，請確認連接埠未被占用。")


def main() -> int:
    # A windowed PyInstaller process has no console streams.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    server = None
    server_thread = None
    backup_stop = None
    try:
        _mutex_handle, already_running = acquire_windows_mutex()
        if already_running:
            show_message("撞球館管理系統已在執行。請先關閉原本的視窗或瀏覽器版。")
            return 0

        port = int(os.environ.get("BILLIARD_PORT", str(DEFAULT_PORT)))
        if not 1 <= port <= 65535:
            raise ValueError("連接埠必須介於 1 到 65535。")
        url = application_url(port)

        # Import only after the single-instance check; never fall back to MSHTML.
        import webview

        app = create_app()
        server = create_server(app, host=HOST, port=port, threads=4)
        backup_stop = start_backup_scheduler(
            app.config["DATABASE"], os.environ.get("BILLIARD_BACKUP_DIR", "")
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        wait_for_server(url, server_thread)

        webview.create_window(
            "撞球館管理系統", url, width=1280, height=800, min_size=(800, 600)
        )
        webview.start(gui="edgechromium")
        return 0
    except Exception as exc:
        logging.getLogger("billiard").exception("Desktop window failed")
        show_message(f"撞球館管理系統視窗啟動失敗：\n\n{exc}", error=True)
        return 1
    finally:
        if backup_stop is not None:
            backup_stop.set()
        if server is not None:
            server.close()
        if server_thread is not None:
            server_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
