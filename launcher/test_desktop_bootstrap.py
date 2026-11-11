"""启动器双模式调度测试；替换 UI/服务模块，禁止实际进程、网络或 Tk。

运行：python -B -m unittest launcher.test_desktop_bootstrap -v
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager, ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


with patch.object(sys, "dont_write_bytecode", True), patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}):
    from launcher.desktop_config import CONFIG_FILENAME, DesktopConfigError, load_desktop_config


LOCAL_ONLY_MODULES = (
    "launcher.config_init", "launcher.browser_setup", "launcher.activation", "launcher.gui",
    "launcher.service_runner", "launcher.service_manager", "launcher.process_utils",
    "launcher.db_checker", "launcher.config_store",
)


def fake_module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
@patch.object(sys, "dont_write_bytecode", True)
class DesktopBootstrapTests(unittest.TestCase):
    @contextmanager
    def harness(self, config="local", *, raw_config=None, argv=None, service_env="", forbid_local=False, missing_local_components=False):
        with TemporaryDirectory(prefix="desktop-bootstrap-test-") as directory:
            root = Path(directory)
            if raw_config is not None:
                (root / CONFIG_FILENAME).write_text(raw_config, encoding="utf-8")
            elif config is not None:
                (root / CONFIG_FILENAME).write_text(json.dumps({"base_url": config}), encoding="utf-8")
            sequence = []
            remote_instance, local_instance = Mock(name="remote_app"), Mock(name="local_app")
            remote_app, local_app = Mock(return_value=remote_instance), Mock(return_value=local_instance)
            get_root = Mock(side_effect=lambda: sequence.append("project_root") or root)

            def load_config(path):
                sequence.append("desktop_config")
                return load_desktop_config(path)

            loader = Mock(side_effect=load_config)
            initialize = Mock(side_effect=lambda: sequence.append("initialize_local_config"))
            browser_setup = Mock(side_effect=lambda: sequence.append("setup_browser"))
            run_service = Mock(name="run_service")
            forbidden_action = Mock(side_effect=AssertionError("must not touch services or ports"))
            replacements = {
                "launcher.desktop_config": fake_module("launcher.desktop_config", load_desktop_config=loader,
                    DesktopConfigError=DesktopConfigError),
                "launcher.frozen_detect": fake_module("launcher.frozen_detect", get_project_root=get_root,
                    is_frozen=Mock(return_value=False)),
                "launcher.remote_gui": fake_module("launcher.remote_gui", RemoteLauncherApp=remote_app),
                "launcher.config_init": fake_module("launcher.config_init", init_config_files=initialize),
                "launcher.browser_setup": fake_module("launcher.browser_setup", ensure_playwright_browser_path=browser_setup),
                "launcher.gui": fake_module("launcher.gui", LauncherApp=local_app),
                "launcher.service_runner": fake_module("launcher.service_runner", run_service=run_service),
                "launcher.service_manager": fake_module("launcher.service_manager", ServiceManager=forbidden_action),
                "launcher.process_utils": fake_module("launcher.process_utils", kill_process_by_port=forbidden_action,
                    kill_port=forbidden_action),
                "launcher.activation": fake_module("launcher.activation"),
                "launcher.db_checker": fake_module("launcher.db_checker"),
                "launcher.config_store": fake_module("launcher.config_store"),
            }
            imported = []
            original_import = builtins.__import__

            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                imported.append(name)
                if missing_local_components and name == "launcher.config_init":
                    raise ModuleNotFoundError("synthetic slim package omits local components", name=name)
                candidates = [name]
                if name == "launcher":
                    candidates.extend("launcher." + part for part in fromlist or ())
                blocked = LOCAL_ONLY_MODULES if forbid_local else ()
                if any(candidate == forbidden or candidate.startswith(forbidden + ".")
                       for candidate in candidates for forbidden in blocked):
                    raise AssertionError("remote bootstrap imported local component: " + name)
                if any(candidate == forbidden or candidate.startswith(forbidden + ".")
                       for candidate in candidates for forbidden in ("tkinter", "customtkinter", "webview")):
                    raise AssertionError("test must not import a real GUI: " + name)
                return original_import(name, globals, locals, fromlist, level)

            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, replacements))
                stack.enter_context(patch.dict(os.environ, {"XR_RUN_SERVICE": service_env, "PYTHONDONTWRITEBYTECODE": "1"}))
                stack.enter_context(patch.object(sys, "dont_write_bytecode", True))
                stack.enter_context(patch.object(sys, "argv", argv or ["launcher.exe"]))
                stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("must not create a real process")))
                stack.enter_context(patch("subprocess.run", side_effect=AssertionError("must not execute a real command")))
                stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("must not contact a server")))
                stack.enter_context(patch("os.system", side_effect=AssertionError("must not execute a shell")))
                stack.enter_context(patch("builtins.__import__", side_effect=guarded_import))
                spec = importlib.util.spec_from_file_location("launcher._bootstrap_test_subject", Path(__file__).with_name("_bootstrap.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                records = SimpleNamespace(root=root, loader=loader, initialize=initialize, browser_setup=browser_setup,
                    run_service=run_service, remote_app=remote_app, remote_instance=remote_instance,
                    local_app=local_app, local_instance=local_instance, forbidden_action=forbidden_action,
                    sequence=sequence, imported=imported)
                yield module, records

    def assert_no_local_actions(self, records):
        records.initialize.assert_not_called()
        records.browser_setup.assert_not_called()
        records.run_service.assert_not_called()
        records.local_app.assert_not_called()
        records.forbidden_action.assert_not_called()

    def test_remote_mode_loads_config_first_and_runs_only_remote_window(self):
        with self.harness("https://Tickets.Example.test:443/", forbid_local=True) as (bootstrap, records):
            before = {item.name for item in records.root.iterdir()}
            bootstrap.main()
            records.loader.assert_called_once_with(records.root)
            self.assertEqual(records.sequence[:2], ["project_root", "desktop_config"])
            records.remote_app.assert_called_once_with("https://tickets.example.test")
            records.remote_instance.run.assert_called_once_with()
            self.assert_no_local_actions(records)
            self.assertEqual({item.name for item in records.root.iterdir()}, before)

    def test_remote_mode_preserves_existing_local_configuration_and_data(self):
        with self.harness("http://192.168.50.22:9000", forbid_local=True) as (bootstrap, records):
            (records.root / "data").mkdir()
            marker = records.root / "data" / "existing-user-data.txt"
            marker.write_text("synthetic-existing-data", encoding="utf-8")
            environment = records.root / ".env"
            environment.write_text("SYNTHETIC_TEST_VALUE=keep", encoding="utf-8")
            bootstrap.main()
            self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic-existing-data")
            self.assertEqual(environment.read_text(encoding="utf-8"), "SYNTHETIC_TEST_VALUE=keep")
            self.assertEqual({item.name for item in records.root.iterdir()}, {CONFIG_FILENAME, "data", ".env"})
            self.assert_no_local_actions(records)

    def test_remote_mode_rejects_environment_service_mode_before_loading_local_modules(self):
        for service_env in ("backend-web", " websocket ", "scheduler"):
            with self.subTest(service_env=service_env), self.harness("https://tickets.example.test", service_env=service_env, forbid_local=True) as (bootstrap, records):
                with self.assertRaises(DesktopConfigError):
                    bootstrap.main()
                records.remote_app.assert_not_called()
                self.assert_no_local_actions(records)
                self.assertEqual({item.name for item in records.root.iterdir()}, {CONFIG_FILENAME})

    def test_remote_mode_rejects_service_flag_anywhere_even_without_a_service_name(self):
        for argv in (["launcher.exe", "--run-service", "backend-web"],
                     ["launcher.exe", "--run-service"],
                     ["launcher.exe", "--other", "--run-service", "scheduler"]):
            with self.subTest(argv=argv), self.harness("https://tickets.example.test", argv=argv, forbid_local=True) as (bootstrap, records):
                with self.assertRaises(DesktopConfigError):
                    bootstrap.main()
                records.remote_app.assert_not_called()
                self.assert_no_local_actions(records)

    def test_invalid_config_fails_before_any_local_setup_or_ui(self):
        for document in ("{", '{"base_url":"https://tickets.example.test/api"}', '{"base_url":false}'):
            with self.subTest(document=document), self.harness(raw_config=document, forbid_local=True) as (bootstrap, records):
                with self.assertRaises(DesktopConfigError):
                    bootstrap.main()
                records.remote_app.assert_not_called()
                self.assert_no_local_actions(records)
                self.assertEqual({item.name for item in records.root.iterdir()}, {CONFIG_FILENAME})

    def test_slim_package_missing_or_local_config_fails_safely_with_friendly_message(self):
        for config in (None, "local"):
            with self.subTest(config=config), self.harness(config, missing_local_components=True) as (bootstrap, records):
                before = {item.name for item in records.root.iterdir()}
                with self.assertRaises(DesktopConfigError) as raised:
                    bootstrap.main()
                self.assertIn("不包含本地服务组件", str(raised.exception))
                self.assertIn("desktop-config.json", str(raised.exception))
                self.assertIn("local", str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, ImportError)
                self.assert_no_local_actions(records)
                records.remote_app.assert_not_called()
                self.assertNotIn("launcher.browser_setup", records.imported)
                self.assertNotIn("launcher.service_runner", records.imported)
                self.assertEqual({item.name for item in records.root.iterdir()}, before)
                self.assertFalse((records.root / "data").exists())
                self.assertFalse((records.root / ".env").exists())

    def test_remote_window_failure_does_not_fall_back_to_local_services(self):
        with self.harness("https://tickets.example.test", forbid_local=True) as (bootstrap, records):
            records.remote_instance.run.side_effect = RuntimeError("synthetic remote window failure")
            with self.assertRaisesRegex(RuntimeError, "synthetic remote window failure"):
                bootstrap.main()
            self.assert_no_local_actions(records)
            self.assertEqual({item.name for item in records.root.iterdir()}, {CONFIG_FILENAME})

    def test_explicit_and_missing_local_config_keep_original_gui_setup_order(self):
        for config in (None, "local", "http://localhost:9000/"):
            with self.subTest(config=config), self.harness(config) as (bootstrap, records):
                bootstrap.main()
                records.loader.assert_called_once_with(records.root)
                records.initialize.assert_called_once_with()
                records.browser_setup.assert_called_once_with()
                self.assertLess(records.sequence.index("desktop_config"), records.sequence.index("initialize_local_config"))
                self.assertLess(records.sequence.index("initialize_local_config"), records.sequence.index("setup_browser"))
                records.local_app.assert_called_once_with()
                records.local_instance.run.assert_called_once_with()
                records.remote_app.assert_not_called()
                records.run_service.assert_not_called()

    def test_local_service_environment_keeps_priority_over_command_line(self):
        with self.harness("local", argv=["launcher.exe", "--run-service", "backend-web"], service_env=" websocket ") as (bootstrap, records):
            bootstrap.main()
            records.initialize.assert_called_once_with()
            records.browser_setup.assert_called_once_with()
            records.run_service.assert_called_once_with("websocket")
            records.local_app.assert_not_called()
            records.remote_app.assert_not_called()

    def test_local_service_command_line_still_dispatches_without_a_gui(self):
        with self.harness(None, argv=["launcher.exe", "--run-service", "scheduler"]) as (bootstrap, records):
            bootstrap.main()
            records.initialize.assert_called_once_with()
            records.browser_setup.assert_called_once_with()
            records.run_service.assert_called_once_with("scheduler")
            records.local_app.assert_not_called()
            records.remote_app.assert_not_called()


@patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
@patch.object(sys, "dont_write_bytecode", True)
class DesktopFrontendSelectionTests(unittest.TestCase):
    def test_local_frontend_manager_pins_backend_even_when_environment_points_remote(self):
        with TemporaryDirectory(prefix="desktop-proxy-test-") as directory:
            root = Path(directory)
            (root / "frontend" / "dist").mkdir(parents=True)
            handler = Mock(name="FrontendHandler")
            forbidden_action = Mock(side_effect=AssertionError("must not inspect or kill ports"))
            replacements = {
                "launcher.frontend_handler": fake_module("launcher.frontend_handler", FrontendHandler=handler),
                "launcher.browser_setup": fake_module("launcher.browser_setup", ensure_playwright_browser_path=forbidden_action),
                "launcher.process_utils": fake_module("launcher.process_utils", kill_process_tree=forbidden_action,
                    kill_by_port=forbidden_action, check_port=forbidden_action),
                "launcher.frozen_detect": fake_module("launcher.frozen_detect", is_frozen=Mock(return_value=False)),
            }
            with patch.dict(sys.modules, replacements), patch.dict(os.environ, {"BACKEND_WEB_SERVICE_URL": "https://different.example.test"}):
                spec = importlib.util.spec_from_file_location("launcher._service_manager_test_subject", Path(__file__).with_name("service_manager.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                manager = module.ServiceManager(root)
                with patch.object(module.threading, "Thread") as thread, \
                     patch.object(module.http.server, "ThreadingHTTPServer") as server, \
                     patch.object(module.time, "sleep"), \
                     patch.object(module.subprocess, "Popen", side_effect=AssertionError("must not launch a real process")):
                    self.assertTrue(manager.start_frontend_server())
                    thread.return_value.start.assert_called_once_with()
                    # 手动运行捕获的 target，Thread 和 HTTPServer 均为替身，不创建线程或监听端口。
                    thread.call_args.kwargs["target"]()
                    server.assert_called_once()
                    factory = server.call_args.args[1]
                    self.assertIs(factory.func, handler)
                    self.assertEqual(factory.keywords["backend_url"], "http://127.0.0.1:8089")
                    self.assertEqual(factory.keywords["dist_dir"], str(root / "frontend" / "dist"))
                    server.return_value.serve_forever.assert_called_once_with()
                    forbidden_action.assert_not_called()
                    self.assertFalse((root / ".env").exists())

    def test_handler_explicit_backend_overrides_environment_without_opening_a_socket(self):
        with patch.dict(os.environ, {"BACKEND_WEB_SERVICE_URL": "https://different.example.test/"}):
            spec = importlib.util.spec_from_file_location("launcher._frontend_handler_test_subject", Path(__file__).with_name("frontend_handler.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with patch.object(module.http.server.SimpleHTTPRequestHandler, "__init__", return_value=None):
                explicit = module.FrontendHandler(object(), ("127.0.0.1", 1234), object(),
                    dist_dir="synthetic-dist", backend_url="http://127.0.0.1:8089/")
                implicit = module.FrontendHandler(object(), ("127.0.0.1", 1234), object(), dist_dir="synthetic-dist")
            explicit.path = "/api/v1/ticket-registration/types"
            explicit._proxy_request = Mock()
            self.assertTrue(explicit._proxy_if_needed())
            explicit._proxy_request.assert_called_once_with("http://127.0.0.1:8089")
            self.assertEqual(implicit._backend_url, "https://different.example.test")

    def test_main_displays_configuration_error_and_exits_without_real_tk(self):
        error = DesktopConfigError("synthetic configuration error")
        bootstrap = fake_module("_bootstrap", main=Mock(side_effect=error))
        window, messagebox = Mock(name="window"), Mock(name="messagebox")
        tkinter = fake_module("tkinter", Tk=Mock(return_value=window), messagebox=messagebox)
        with patch.dict(sys.modules, {"_bootstrap": bootstrap, "tkinter": tkinter}), patch.object(sys, "path", list(sys.path)):
            spec = importlib.util.spec_from_file_location("launcher._main_test_subject", Path(__file__).with_name("main.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with patch.object(module, "_write_fatal_log") as fatal_log:
                with self.assertRaises(SystemExit) as raised:
                    module._run_main()
                self.assertEqual(raised.exception.code, 1)
                fatal_log.assert_called_once_with("desktop_config")
                window.withdraw.assert_called_once_with()
                window.destroy.assert_called_once_with()
                messagebox.showerror.assert_called_once_with("桌面端连接配置错误", str(error), parent=window)


if __name__ == "__main__":
    unittest.main()
