"""只在 TEMP 小型目录中验证运行库复制，不运行真实打包或后台。"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.bundle_desktop_runtime import bundle_runtime
from scripts.prepare_desktop_build import UnsafeBuildPath


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-runtime-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.runtime = self.base / "python-base"
        self.purelib = self.base / "venv/Lib/site-packages"
        self.platlib = self.base / "venv-platform/site-packages"
        self.target = self.project / "build_output/main.dist"
        for path in (self.target, self.purelib, self.platlib, self.runtime / "DLLs"):
            path.mkdir(parents=True)
        self.write(self.project / "launcher/main.py", "# project marker")
        self.write(self.project / "EXE打包构建.bat", "@echo off")
        self.write(self.target / "XianyuAutoReply.exe", "compiled launcher")
        for name in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "vcruntime140.dll"):
            self.write(self.runtime / name, "base: " + name)
        self.write(self.runtime / "pyvenv.cfg", "home = C:/not-portable")
        self.write(self.runtime / "Lib/encodings/__init__.py", "# standard encodings")
        self.write(self.runtime / "Lib/json/__init__.py", "# standard json")
        self.write(self.runtime / "Lib/site-packages/system_only.py", "# exclude system packages")
        self.write(self.runtime / "Lib/__pycache__/unused.pyc", "cache")
        self.write(self.runtime / "DLLs/_ssl.pyd", "ssl extension")
        self.write(self.runtime / "DLLs/libssl.dll", "ssl library")
        self.write(self.purelib / "fastapi/__init__.py", "# current venv fastapi")
        self.write(self.purelib / "fastapi-1.0.dist-info/METADATA", "Name: fastapi")
        self.write(self.purelib / "__pycache__/cache.pyc", "cache")
        self.write(self.platlib / "cryptography/_rust.pyd", "platform extension")

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def bundle(self, **kwargs):
        return bundle_runtime(self.project, base_prefix=self.runtime, package_paths=[self.purelib, self.platlib], **kwargs)

    def assert_untouched(self):
        self.assertEqual([path.name for path in self.target.iterdir()], ["XianyuAutoReply.exe"])
        self.assertEqual((self.target / "XianyuAutoReply.exe").read_text(), "compiled launcher")

    def test_copies_current_venv_dependencies_stdlib_executables_and_dlls(self):
        result = self.bundle()
        for relative in ("python.exe", "pythonw.exe", "python3.dll", "python311.dll", "vcruntime140.dll", "Lib/encodings/__init__.py", "Lib/json/__init__.py", "DLLs/_ssl.pyd", "DLLs/libssl.dll", "Lib/site-packages/fastapi/__init__.py", "Lib/site-packages/fastapi-1.0.dist-info/METADATA", "Lib/site-packages/cryptography/_rust.pyd"):
            self.assertTrue((self.target / relative).is_file(), relative)
        self.assertEqual(result.file_count, 12)
        self.assertEqual((self.target / "Lib/site-packages/fastapi/__init__.py").read_text(), "# current venv fastapi")
        self.assertFalse((self.target / "Lib/site-packages/system_only.py").exists())
        self.assertFalse(list(self.target.rglob("__pycache__")))
        self.assertFalse(list(self.target.rglob("pyvenv.cfg")))
        self.assertEqual((self.target / "XianyuAutoReply.exe").read_text(), "compiled launcher")

    def test_default_paths_come_from_current_build_interpreter_and_deduplicate(self):
        with patch("scripts.bundle_desktop_runtime.sys.base_prefix", str(self.runtime)), patch("scripts.bundle_desktop_runtime.sysconfig.get_paths", return_value={"purelib": str(self.purelib), "platlib": str(self.purelib)}):
            result = bundle_runtime(self.project)
        self.assertEqual(result.site_packages, (self.purelib.resolve(),))
        self.assertEqual(result.file_count, 11)
        self.assertTrue((self.target / "Lib/site-packages/fastapi/__init__.py").is_file())

    def test_dry_run_validates_without_copying(self):
        result = self.bundle(dry_run=True)
        self.assertEqual(result.file_count, 12)
        self.assertTrue(result.dry_run)
        self.assert_untouched()

    def test_missing_python_or_dependencies_fails_before_writing(self):
        (self.runtime / "pythonw.exe").unlink()
        with self.assertRaisesRegex(ValueError, "pythonw.exe"):
            self.bundle()
        self.assert_untouched()
        self.write(self.runtime / "pythonw.exe", "pythonw")
        with self.assertRaises(FileNotFoundError):
            bundle_runtime(self.project, base_prefix=self.runtime, package_paths=[self.base / "missing-packages"])
        self.assert_untouched()

    def test_target_requires_a_recognized_project_and_existing_nuitka_dist(self):
        with self.assertRaises(UnsafeBuildPath):
            bundle_runtime(self.base, base_prefix=self.runtime, package_paths=[self.purelib])
        self.assert_untouched()
        empty_project = self.base / "empty-project"
        self.write(empty_project / "launcher/main.py", "")
        self.write(empty_project / "EXE打包构建.bat", "")
        with self.assertRaises(UnsafeBuildPath):
            bundle_runtime(empty_project, base_prefix=self.runtime, package_paths=[self.purelib])
        self.assertFalse((empty_project / "build_output").exists())
        result = bundle_runtime(empty_project, base_prefix=self.runtime, package_paths=[self.purelib], dry_run=True)
        self.assertEqual(result.file_count, 11)
        self.assertFalse((empty_project / "build_output").exists())

    def test_file_in_place_of_target_directory_fails_before_writing(self):
        self.write(self.target / "Lib", "unexpected existing file")
        with self.assertRaises(UnsafeBuildPath):
            self.bundle()
        self.assertFalse((self.target / "python.exe").exists())
        self.assertEqual((self.target / "Lib").read_text(), "unexpected existing file")

    def test_sources_cannot_contain_or_be_contained_by_target(self):
        with self.assertRaises(UnsafeBuildPath):
            bundle_runtime(self.project, base_prefix=self.project, package_paths=[self.purelib])
        with self.assertRaises(UnsafeBuildPath):
            bundle_runtime(self.project, base_prefix=self.runtime, package_paths=[self.target])
        self.assert_untouched()

    def make_directory_link(self, source, destination):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(source), str(destination))
            self.addCleanup(lambda: os.rmdir(destination) if destination.exists() else None)
        else:
            destination.symlink_to(source, target_is_directory=True)
            self.addCleanup(lambda: destination.unlink(missing_ok=True))

    def test_target_junction_does_not_write_outside_package(self):
        outside = self.base / "outside"
        self.write(outside / "sentinel", "untouched")
        self.make_directory_link(outside, self.target / "Lib")
        with self.assertRaises(UnsafeBuildPath):
            self.bundle()
        self.assertEqual(list(outside.iterdir()), [outside / "sentinel"])
        self.assertEqual((outside / "sentinel").read_text(), "untouched")
        self.assertFalse((self.target / "python.exe").exists())

    def test_source_junction_is_rejected_before_any_copy(self):
        outside = self.base / "outside"
        self.write(outside / "not-a-dependency.txt", "untouched")
        self.make_directory_link(outside, self.purelib / "linked-package")
        with self.assertRaises(UnsafeBuildPath):
            self.bundle()
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
