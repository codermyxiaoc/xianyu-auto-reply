"""服务器桌面入口的离线测试：网络、浏览器均 mock，Tk 始终隐藏。"""
from __future__ import annotations

import json
import socket
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from launcher.remote_gui import (
    MAX_STATUS_BYTES,
    PROBE_TIMEOUT,
    RemoteLauncherApp,
    RemoteServerStatus,
    _SameOriginRedirectHandler,
    _server_origin,
    open_remote_site,
    probe_remote_server,
)


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        return self.status

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]


class RemoteProbeTests(unittest.TestCase):
    def test_read_only_status_uses_same_origin_with_timeout_and_no_credentials(self):
        payload = {"success": True, "data": {"services": [{"key": "backend", "online": True}]}}
        response = _Response(json.dumps(payload).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch("launcher.remote_gui.urllib.request.build_opener", return_value=opener):
            result = probe_remote_server("http://example.test:8080/")
        self.assertTrue(result.connected)
        self.assertFalse(result.degraded)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://example.test:8080/api/v1/public/status")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertFalse({"authorization", "cookie"} & {key.lower() for key in request.headers})
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": PROBE_TIMEOUT})
        self.assertEqual(response.read_sizes, [MAX_STATUS_BYTES + 1])

    def test_partial_service_outage_still_reports_connection(self):
        payload = {"success": True, "data": {"services": [{"key": "backend", "online": True}, {"key": "websocket", "online": False}]}}
        opener = Mock()
        opener.open.return_value = _Response(json.dumps(payload).encode())
        with patch("launcher.remote_gui.urllib.request.build_opener", return_value=opener):
            result = probe_remote_server("https://example.test")
        self.assertTrue(result.connected)
        self.assertTrue(result.degraded)
        self.assertIn("部分服务暂不可用", result.detail)

    def test_business_failure_and_malformed_response_are_not_online(self):
        fixtures = [
            ({"success": False, "message": "访问过于频繁，请稍后重试", "data": None}, "访问过于频繁"),
            ({"success": True, "data": {}}, "无法识别"),
            ({"success": True, "data": {"services": [{"online": "true"}]}}, "无法识别"),
            ({"success": "true"}, "本系统网站"),
            ([], "本系统网站"),
        ]
        for payload, expected in fixtures:
            with self.subTest(payload=payload):
                opener = Mock()
                opener.open.return_value = _Response(json.dumps(payload).encode())
                with patch("launcher.remote_gui.urllib.request.build_opener", return_value=opener):
                    result = probe_remote_server("https://example.test")
                self.assertFalse(result.connected)
                self.assertIn(expected, result.detail)

    def test_html_oversized_and_network_errors_have_friendly_results(self):
        for raw, expected in [(b"<html>login</html>", "有效状态数据"), (b"x" * (MAX_STATUS_BYTES + 1), "响应内容过大")]:
            with self.subTest(expected=expected):
                opener = Mock()
                opener.open.return_value = _Response(raw)
                with patch("launcher.remote_gui.urllib.request.build_opener", return_value=opener):
                    result = probe_remote_server("https://example.test")
                self.assertFalse(result.connected)
                self.assertIn(expected, result.detail)
        for error, expected in [
            (socket.timeout(), "连接超时"),
            (urllib.error.URLError(socket.timeout()), "连接超时"),
            (urllib.error.URLError("private exception text"), "暂时无法连接服务器"),
            (urllib.error.HTTPError("https://example.test", 404, "missing", {}, None), "服务器暂时无法连接"),
        ]:
            with self.subTest(error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with patch("launcher.remote_gui.urllib.request.build_opener", return_value=opener):
                    result = probe_remote_server("https://example.test")
                self.assertFalse(result.connected)
                self.assertEqual(result.message, expected)
                self.assertNotIn("private exception text", result.detail)

    def test_invalid_addresses_do_not_reach_network_or_browser(self):
        invalid = ["file:///tmp/page.html", "javascript:alert(1)", "http://user:password@example.test", "http://example.test/orders", "http://example.test/?token=secret", "http://example.test/#secret", "http://example.test:99999", "http://example.test\\other", "http://example.test\n/path", "local", "http://127.0.0.1:9000"]
        with patch("launcher.remote_gui.urllib.request.build_opener") as opener, patch("launcher.remote_gui.webbrowser.open") as browser:
            for address in invalid:
                with self.subTest(address=address):
                    self.assertFalse(probe_remote_server(address).connected)
                    with self.assertRaises(ValueError):
                        open_remote_site(address)
            opener.assert_not_called()
            browser.assert_not_called()

    def test_browser_opens_origin_and_redirects_cannot_leave_it(self):
        with patch("launcher.remote_gui.webbrowser.open", return_value=True) as browser:
            self.assertTrue(open_remote_site("https://EXAMPLE.test:8443/"))
            browser.assert_called_once_with("https://example.test:8443", new=2)
        self.assertEqual(_server_origin("http://[2001:db8::1]:9000/"), "http://[2001:db8::1]:9000")
        handler = _SameOriginRedirectHandler("https://example.test")
        request = urllib.request.Request("https://example.test/api/v1/public/status")
        redirected = handler.redirect_request(request, None, 301, "", {}, "https://example.test/api/v1/public/status/")
        self.assertEqual(redirected.get_method(), "GET")
        for url in ["https://other.test/status", "http://example.test/status", "https://user:secret@example.test/status"]:
            with self.subTest(url=url), self.assertRaises(urllib.error.URLError):
                handler.redirect_request(request, None, 302, "", {}, url)


class RemoteGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = RemoteLauncherApp("https://example.test", root=self.root, auto_probe=False)
        self.root.update_idletasks()
        self.assertEqual(self.root.state(), "withdrawn")

    def tearDown(self):
        self.app.close()

    def pump_until(self, condition, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(condition(), "后台结果未在规定时间内回到主线程")

    def test_retry_and_duplicate_click_do_not_block_ui(self):
        started = threading.Event()
        release = threading.Event()
        main_thread = threading.get_ident()

        def probe(_):
            self.assertNotEqual(threading.get_ident(), main_thread)
            started.set()
            release.wait(2)
            return RemoteServerStatus(False, "模拟无法连接", "请重试")

        with patch("launcher.remote_gui.probe_remote_server", side_effect=probe) as mocked:
            self.app.check_connection()
            self.app.check_connection()
            self.assertTrue(started.wait(1))
            self.root.update()
            self.assertTrue(self.app._probe_busy)
            self.assertEqual(str(self.app._probe_button.cget("state")), "disabled")
            self.assertEqual(str(self.app._open_button.cget("state")), "normal")
            mocked.assert_called_once_with("https://example.test")
            release.set()
            self.pump_until(lambda: not self.app._probe_busy)
        self.assertEqual(self.app._status_label.cget("text"), "模拟无法连接")
        with patch("launcher.remote_gui.probe_remote_server", return_value=RemoteServerStatus(True, "已连接服务器", "测试成功")) as mocked:
            self.app.check_connection()
            self.pump_until(lambda: not self.app._probe_busy)
            mocked.assert_called_once()
        self.assertEqual(self.app._status_label.cget("text"), "已连接服务器")

    def test_browser_opens_once_without_probe_or_server_mutation(self):
        started = threading.Event()
        release = threading.Event()

        def open_site(_):
            started.set()
            release.wait(2)
            return True

        with patch("launcher.remote_gui.open_remote_site", side_effect=open_site) as browser, patch("launcher.remote_gui.probe_remote_server") as probe:
            self.app.open_site()
            self.app.open_site()
            self.assertTrue(started.wait(1))
            self.root.update()
            self.assertTrue(self.app._browser_busy)
            browser.assert_called_once_with("https://example.test")
            probe.assert_not_called()
            release.set()
            self.pump_until(lambda: not self.app._browser_busy)
        self.assertIn("已请求打开浏览器", self.app._browser_detail.cget("text"))

    def test_browser_failure_does_not_hide_address(self):
        with patch("launcher.remote_gui.open_remote_site", side_effect=OSError("unavailable")):
            self.app.open_site()
            self.pump_until(lambda: not self.app._browser_busy)
        self.assertIn("复制上方服务器地址", self.app._browser_detail.cget("text"))
        self.assertEqual(self.app._site_var.get(), "https://example.test")

    def test_close_during_probe_never_schedules_tk_from_worker(self):
        started = threading.Event()
        release = threading.Event()

        def probe(_):
            started.set()
            release.wait(2)
            return RemoteServerStatus(True, "已连接服务器")

        with patch("launcher.remote_gui.probe_remote_server", side_effect=probe), patch.object(self.root, "after", wraps=self.root.after) as after:
            self.app.check_connection()
            self.assertTrue(started.wait(1))
            self.app.close()
            calls_after_close = after.call_count
            release.set()
            self.app._probe_thread.join(1)
            self.assertFalse(self.app._probe_thread.is_alive())
            self.app._poll_results()
            self.app.check_connection()
            self.app.open_site()
            self.assertEqual(after.call_count, calls_after_close)
            self.assertTrue(self.app._closed)
            self.assertIsNone(self.app._poll_id)
            self.assertEqual(self.app._outbox.qsize(), 1)

    def test_external_destroy_marks_window_closed(self):
        self.root.destroy()
        self.assertTrue(self.app._closed)
        self.assertIsNone(self.app._poll_id)
        self.app._outbox.put(("probe", RemoteServerStatus(True, "已连接服务器")))
        self.app._poll_results()


if __name__ == "__main__":
    unittest.main()
