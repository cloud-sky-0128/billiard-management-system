from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from waitress import create_server

from billiard_app import create_app


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MUTEX_NAME = "Local\\BilliardManagerDesktop"
ERROR_ALREADY_EXISTS = 183


def application_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def acquire_windows_mutex():
    if os.name != "nt":
        return object(), False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError()
    return handle, kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def open_browser_when_ready(url: str) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                if response.status == 200:
                    if os.environ.get("BILLIARD_NO_BROWSER") != "1":
                        webbrowser.open_new_tab(url)
                    return
        except Exception:
            time.sleep(0.25)
    print(f"伺服器已啟動，但無法自動開啟頁面。請手動開啟：{url}")


def pause_after_error() -> None:
    if getattr(sys, "frozen", False) and sys.stdin and sys.stdin.isatty():
        try:
            input("按 Enter 關閉視窗...")
        except (EOFError, KeyboardInterrupt):
            pass


def main() -> int:
    try:
        _mutex_handle, already_running = acquire_windows_mutex()
        port = int(os.environ.get("BILLIARD_PORT", str(DEFAULT_PORT)))
        if not 1 <= port <= 65535:
            raise ValueError("連接埠必須介於 1 到 65535。")
        url = application_url(port)
        if already_running:
            open_browser_when_ready(url)
            return 0

        app = create_app()
        server = create_server(app, host=HOST, port=port, threads=4)
    except Exception as exc:
        print(f"撞球館管理系統啟動失敗：{exc}")
        pause_after_error()
        return 1

    print("撞球館管理系統已啟動")
    print(f"操作網址：{url}")
    print(f"資料庫：{app.config['DATABASE']}")
    print("關閉此視窗或按 Ctrl+C 即可停止程式。")
    threading.Thread(
        target=open_browser_when_ready,
        args=(url,),
        daemon=True,
    ).start()
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n正在關閉撞球館管理系統...")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
