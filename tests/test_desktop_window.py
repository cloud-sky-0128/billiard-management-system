import threading
import unittest
from unittest.mock import MagicMock, patch

import desktop_window


class DesktopWindowTestCase(unittest.TestCase):
    @patch("desktop_window.show_message")
    @patch("desktop_window.acquire_windows_mutex", return_value=(object(), True))
    def test_duplicate_instance_does_not_start_server(self, _mutex, message):
        with patch("desktop_window.create_app") as create_app:
            self.assertEqual(desktop_window.main(), 0)
            create_app.assert_not_called()
        message.assert_called_once()

    @patch("desktop_window.urllib.request.urlopen")
    def test_wait_for_server_requires_live_thread(self, urlopen):
        thread = MagicMock()
        thread.is_alive.return_value = False
        with self.assertRaises(RuntimeError):
            desktop_window.wait_for_server("http://127.0.0.1:8765", thread)
        urlopen.assert_not_called()

    @patch("desktop_window.show_message")
    @patch("desktop_window.acquire_windows_mutex", return_value=(object(), False))
    @patch("desktop_window.create_app")
    @patch("desktop_window.create_server")
    @patch("desktop_window.start_backup_scheduler")
    @patch("desktop_window.wait_for_server")
    def test_window_close_stops_server_and_backup(
        self, ready, scheduler, create_server, create_app, _mutex, message
    ):
        import sys

        webview = MagicMock()
        create_app.return_value.config = {"DATABASE": "test.db"}
        server = create_server.return_value
        scheduler.return_value = threading.Event()
        with patch.dict(sys.modules, {"webview": webview}):
            self.assertEqual(desktop_window.main(), 0)
        ready.assert_called_once()
        webview.start.assert_called_once_with(gui="edgechromium")
        self.assertEqual(webview.create_window.call_args.args[:2], (
            "撞球館管理系統", "http://127.0.0.1:8765"
        ))
        self.assertTrue(scheduler.return_value.is_set())
        server.close.assert_called_once()
        message.assert_not_called()

    @patch("desktop_window.show_message")
    @patch("desktop_window.acquire_windows_mutex", return_value=(object(), False))
    @patch("desktop_window.create_app")
    @patch("desktop_window.create_server")
    @patch("desktop_window.start_backup_scheduler")
    @patch("desktop_window.wait_for_server")
    def test_window_failure_still_stops_server_and_backup(
        self, _ready, scheduler, create_server, create_app, _mutex, message
    ):
        import sys

        webview = MagicMock()
        webview.start.side_effect = RuntimeError("WebView2 unavailable")
        create_app.return_value.config = {"DATABASE": "test.db"}
        scheduler.return_value = threading.Event()
        with patch.dict(sys.modules, {"webview": webview}), patch("desktop_window.logging.getLogger"):
            self.assertEqual(desktop_window.main(), 1)
        self.assertTrue(scheduler.return_value.is_set())
        create_server.return_value.close.assert_called_once()
        self.assertIn("WebView2 unavailable", message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
