"""桌面配置和打包清理回归；所有输出、删除目标均在 TemporaryDirectory 中。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_desktop_build import UnsafeBuildPath, prepare


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIGURE = PROJECT_ROOT / "scripts" / "configure_desktop.py"


class DesktopBuildToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xianyu-desktop-tools-")
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "desktop-config.json"

    def tearDown(self):
        self.temporary.cleanup()

    def run_configure(self, *args, input_text=None, environment_url=None):
        environment = os.environ.copy()
        environment.pop("XIANYU_DESKTOP_BASE_URL", None)
        environment["PYTHONIOENCODING"] = "utf-8"
        if environment_url is not None:
            environment["XIANYU_DESKTOP_BASE_URL"] = environment_url
        return subprocess.run(
            [sys.executable, "-B", str(CONFIGURE), *args], cwd=self.root,
            env=environment, input=input_text, capture_output=True, encoding="utf-8",
            timeout=15,
        )

    def create_project(self):
        (self.root / "launcher").mkdir(exist_ok=True)
        (self.root / "launcher/main.py").write_text("# synthetic project marker\n", encoding="utf-8")
        (self.root / "EXE打包构建.bat").write_text("@echo off\n", encoding="utf-8")

    def test_local_remote_and_environment_precedence_write_canonical_utf8(self):
        cases = [
            (("--base-url", "local"), None, "local", "local"),
            (("--base-url", "http://localhost:9000/"), None, "local", "local"),
            (("--base-url", "http://[::1]:9000"), None, "local", "local"),
            (("--base-url", "http://103.73.119.239:9000/"), None, "remote", "http://103.73.119.239:9000"),
            (("--base-url", "https://TICKETS.example.com:443/"), None, "remote", "https://tickets.example.com"),
            ((), "https://tickets.example.com", "remote", "https://tickets.example.com"),
            (("--base-url", "local"), "https://tickets.example.com", "local", "local"),
        ]
        for args, environment_url, mode, value in cases:
            with self.subTest(value=value):
                result = self.run_configure(*args, "--output", str(self.config), "--print-mode", environment_url=environment_url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), mode)
                self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"base_url": value})
                self.assertEqual(list(self.root.glob(".desktop-config-*.tmp")), [])

    def test_interactive_blank_defaults_local_and_eof_does_not_write(self):
        result = self.run_configure("--interactive", "--output", str(self.config), "--print-mode", input_text="\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "local")
        self.assertIn("Desktop base URL", result.stderr)
        saved = self.config.read_bytes()
        result = self.run_configure("--interactive", "--output", str(self.config), input_text="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), saved)

    def test_print_mode_only_never_creates_output(self):
        result = self.run_configure("--print-mode", "--base-url", "https://tickets.example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "remote")
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_urls_and_shell_characters_do_not_overwrite_or_execute(self):
        self.config.write_text('{"base_url":"local"}\n', encoding="utf-8")
        saved = self.config.read_bytes()
        for value in (
            "", "https://tickets.example.com/api/v1", "https://user:secret@tickets.example.com",
            "https://tickets.example.com?next=foo", "https://tickets.example.com#fragment",
            'https://tickets.example.com/&echo injected>marker.txt',
            'https://tickets.example.com/%PATH%', 'https://tickets.example.com/" & mkdir marker',
            "javascript:alert(1)", "http://127.0.0.1:8089", "http://0.0.0.0:9000",
        ):
            with self.subTest(value=value):
                result = self.run_configure("--output", str(self.config), "--print-mode", environment_url=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.config.read_bytes(), saved)
                self.assertEqual({path.name for path in self.root.iterdir()}, {"desktop-config.json"})

    def test_clean_checks_all_paths_before_deleting_and_preserves_project_data(self):
        self.create_project()
        for name in ("build_output", "release", "data", "backups", ".venv", "frontend/dist"):
            directory = self.root / name
            directory.mkdir(parents=True)
            (directory / "keep.txt").write_text(name, encoding="utf-8")
        self.config.write_text('{"base_url":"local"}', encoding="utf-8")
        plan = prepare(self.root, "clean", dry_run=True)
        self.assertEqual(set(plan), {self.root / "build_output", self.root / "release"})
        self.assertTrue((self.root / "release/keep.txt").exists())
        prepare(self.root, "clean")
        self.assertFalse((self.root / "build_output").exists())
        self.assertTrue((self.root / "release").is_dir())
        self.assertEqual(list((self.root / "release").iterdir()), [])
        for name in ("data", "backups", ".venv", "frontend/dist"):
            self.assertEqual((self.root / name / "keep.txt").read_text(), name)
        self.assertTrue(self.config.exists())

    def test_sanitizing_copies_keeps_configuration_and_source(self):
        self.create_project()
        dist = self.root / "build_output/main.dist"
        for name in ("backend-web/logs", "backend-web/static/uploads", "backend-web/app/__pycache__", "websocket/browser_data", "common/temp", "data"):
            directory = dist / name
            directory.mkdir(parents=True)
            (directory / "synthetic.txt").write_text("remove", encoding="utf-8")
        for name in ("backend-web/.env", "backend-web/.env.example", "backend-web/pyproject.toml", "common/compiled.pyc"):
            (dist / name).write_text("remove", encoding="utf-8")
        (dist / "backend-web/app/main.py").write_text("# keep source", encoding="utf-8")
        (dist / "desktop-config.json").write_text('{"base_url":"https://tickets.example.com"}', encoding="utf-8")
        prepare(self.root, "sanitize-dist")
        self.assertTrue((dist / "backend-web/app/main.py").exists())
        self.assertEqual(json.loads((dist / "desktop-config.json").read_text()), {"base_url": "https://tickets.example.com"})
        for name in ("backend-web/logs", "backend-web/static/uploads", "backend-web/app/__pycache__", "websocket/browser_data", "common/temp", "data", "backend-web/.env", "backend-web/.env.example", "backend-web/pyproject.toml", "common/compiled.pyc"):
            self.assertFalse((dist / name).exists(), name)
        release = self.root / "release/XianyuAutoReply"
        (release / "data").mkdir(parents=True)
        (release / "data/synthetic.txt").write_text("remove", encoding="utf-8")
        (release / "desktop-config.json").write_text('{"base_url":"local"}', encoding="utf-8")
        prepare(self.root, "sanitize-release")
        self.assertFalse((release / "data").exists())
        self.assertTrue((release / "desktop-config.json").exists())

    def test_clean_rejects_junction_escape_without_removing_other_targets(self):
        self.create_project()
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("outside", encoding="utf-8")
        (self.root / "build_output").mkdir()
        existing = self.root / "build_output/keep.txt"
        existing.write_text("untouched", encoding="utf-8")
        link = self.root / "release"
        if os.name == "nt":
            # 只创建测试 junction，不通过 cmd 删除任何文件。
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(UnsafeBuildPath):
                prepare(self.root, "clean")
            self.assertEqual(sentinel.read_text(), "outside")
            self.assertEqual(existing.read_text(), "untouched")
        finally:
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()

    def test_root_without_project_markers_is_rejected(self):
        (self.root / "build_output").mkdir()
        with self.assertRaises(UnsafeBuildPath):
            prepare(self.root, "clean")
        self.assertTrue((self.root / "build_output").exists())


if __name__ == "__main__":
    unittest.main()
