"""桌面连接配置边界测试；只使用临时目录，不访问网络或本机服务。

运行：python -B -m unittest launcher.test_desktop_config -v
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


with patch.object(sys, "dont_write_bytecode", True), patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}):
    from launcher.desktop_config import (
        CONFIG_FILENAME, DesktopConfig, DesktopConfigError,
        load_desktop_config, resolve_desktop_config,
    )


@patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
@patch.object(sys, "dont_write_bytecode", True)
class DesktopConfigTests(unittest.TestCase):
    def test_local_aliases_use_the_same_existing_frontend_origin(self):
        for value in ("local", "http://127.0.0.1:9000", "http://127.0.0.1:9000/",
                      "http://localhost:9000", "http://localhost:9000/", "http://[::1]:9000/"):
            with self.subTest(value=value):
                self.assertEqual(resolve_desktop_config(value), DesktopConfig(mode="local", base_url="http://127.0.0.1:9000"))

    def test_remote_origins_keep_lan_and_ipv6_support_and_normalize_defaults(self):
        examples = {
            "http://192.168.50.22:9000/": "http://192.168.50.22:9000",
            "http://10.0.0.20:18080": "http://10.0.0.20:18080",
            "https://Tickets.Example.test:443/": "https://tickets.example.test",
            "http://Tickets.Example.test:80": "http://tickets.example.test",
            "https://tickets.example.test:9443/": "https://tickets.example.test:9443",
            "http://[2001:db8::42]:9000/": "http://[2001:db8::42]:9000",
            "https://[2001:db8::42]:443": "https://[2001:db8::42]",
            "http://localhost.example.test:9000": "http://localhost.example.test:9000",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(resolve_desktop_config(value), DesktopConfig(mode="remote", base_url=expected))

    def test_loopback_with_wrong_scheme_or_port_requires_local_mode(self):
        for value in ("http://127.0.0.1", "http://localhost", "http://[::1]",
                      "http://127.0.0.1:8089", "http://localhost:443", "http://[::1]:9001",
                      "https://127.0.0.1:9000", "https://localhost:9000", "https://[::1]:9000"):
            with self.subTest(value=value), self.assertRaises(DesktopConfigError) as raised:
                resolve_desktop_config(value)
            self.assertIn("local", str(raised.exception).lower())

    def test_only_pure_http_origins_are_accepted(self):
        invalid = (
            "", "local/", "tickets.example.test", "//tickets.example.test", "ftp://tickets.example.test",
            "file:///C:/app/index.html", "javascript:alert(1)", "http://", "https:///tickets.example.test",
            "https://tickets.example.test/api", "https://tickets.example.test/api/", "https://tickets.example.test//",
            "http://127.0.0.1:9000/api", "https://tickets.example.test?next=1", "https://tickets.example.test#token",
            "https://tickets.example.test?", "https://tickets.example.test#",
            "https://user:secret@tickets.example.test", "https://@tickets.example.test",
            "https://:secret@tickets.example.test", "https://tickets.example.test:0",
            "https://tickets.example.test:65536", "https://tickets.example.test:-1",
            "https://tickets.example.test:abc", "https://tickets.example.test:",
            "http://0.0.0.0:9000", "http://0.0.0.0", "http://[::]:9000",
            "http://[::1]junk:9000", "http://[2001:db8::1]junk", "http://[::1]:9000:80",
            "http://%31%32%37.0.0.1:9000", "http://[fe80::1%25eth0]:9000",
            "http://127.1:8089", "http://2130706433:9000", "http://0x7f000001:9000",
            "http://0177.0.0.1:9000", "http://0x7f.0.0.1:9000",
            " https://tickets.example.test", "https://tickets.example.test ", "local\n",
            "https://tickets.example.test\n", "https://tickets.\nexample.test", "https://tickets.\texample.test",
            "https://tickets.example.test\r", "https://tickets.example.test\x00", "https://tickets.example.test\x7f",
            "https://tickets.example.test\u00a0", "https://tickets\\.example.test", "https://tickets.example.test\\api",
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(DesktopConfigError):
                resolve_desktop_config(value)

    def test_non_string_inputs_do_not_coerce_to_urls(self):
        for value in (None, True, False, 9000, {}, [], b"local"):
            with self.subTest(value=value), self.assertRaises(DesktopConfigError):
                resolve_desktop_config(value)

    def test_resolved_config_is_immutable(self):
        config = resolve_desktop_config("local")
        with self.assertRaises(FrozenInstanceError):
            config.mode = "remote"

    def test_missing_config_keeps_local_compatibility_without_creating_files(self):
        with TemporaryDirectory(prefix="desktop-config-test-") as directory:
            root = Path(directory)
            before = list(root.iterdir())
            self.assertEqual(load_desktop_config(root), resolve_desktop_config("local"))
            self.assertEqual(list(root.iterdir()), before)

    def test_file_is_read_from_supplied_project_root_and_never_rewritten(self):
        with TemporaryDirectory(prefix="desktop-config-test-") as directory:
            root = Path(directory)
            payload = json.dumps({"base_url": "https://Tickets.Example.test:443/"}, indent=2).encode("utf-8")
            path = root / CONFIG_FILENAME
            path.write_bytes(payload)
            result = load_desktop_config(root)
            self.assertEqual(result, DesktopConfig(mode="remote", base_url="https://tickets.example.test"))
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual({item.name for item in root.iterdir()}, {CONFIG_FILENAME})

    def test_invalid_existing_json_never_silently_falls_back_to_local(self):
        invalid_documents = (
            "", "{", "[]", "null", '"local"', "123", "true", "{}",
            '{"base_url":null}', '{"base_url":9000}', '{"base_url":false}', '{"base_url":[]}',
            '{"base_url":""}', '{"base_url":"http://localhost:8089"}',
            '{"base_url":"local","mode":"local"}', '{"url":"https://tickets.example.test"}',
            '{"base_url":"https://tickets.example.test/api"}',
            '{"base_url":"local","base_url":"https://tickets.example.test"}',
            '{"base_url":"https://tickets.example.test","base_url":"local"}',
        )
        with TemporaryDirectory(prefix="desktop-config-test-") as directory:
            root = Path(directory)
            path = root / CONFIG_FILENAME
            for document in invalid_documents:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaises(DesktopConfigError):
                        load_desktop_config(root)
                    self.assertEqual(path.read_text(encoding="utf-8"), document)
                    self.assertEqual({item.name for item in root.iterdir()}, {CONFIG_FILENAME})

    def test_unreadable_config_and_bad_encoding_raise_configuration_error(self):
        with TemporaryDirectory(prefix="desktop-config-test-") as directory:
            root = Path(directory)
            path = root / CONFIG_FILENAME
            path.mkdir()
            with self.assertRaises(DesktopConfigError):
                load_desktop_config(root)
            path.rmdir()
            path.write_bytes(b"\xff\xfe{\x00")
            with self.assertRaises(DesktopConfigError):
                load_desktop_config(root)

    def test_oversized_config_is_rejected_and_utf8_bom_is_supported(self):
        with TemporaryDirectory(prefix="desktop-config-test-") as directory:
            root = Path(directory)
            path = root / CONFIG_FILENAME
            path.write_bytes(b'\xef\xbb\xbf{"base_url":"local"}')
            self.assertEqual(load_desktop_config(root), resolve_desktop_config("local"))
            path.write_text(json.dumps({"base_url": "https://tickets.example.test"}) + " " * 8193, encoding="utf-8")
            with self.assertRaises(DesktopConfigError):
                load_desktop_config(root)


if __name__ == "__main__":
    unittest.main()
