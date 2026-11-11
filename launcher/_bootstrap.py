"""
启动器核心逻辑

所有业务逻辑均在此文件中实现，main.py 仅作为最小入口桩。

功能：
1. 根据桌面连接配置选择本地或服务器模式
2. 本地模式初始化依赖，运行 GUI 或子服务
3. 服务器模式只打开远程站点，不管理本机服务
"""
import os
import sys


def main():
    """
    启动器主函数
    
    根据命令行参数决定运行模式：
    - 无参数：启动GUI界面
    - --run-service <name>：启动指定的子服务（供打包后子进程调用）
    """
    # 先确定连接目标；服务器模式不能初始化或停止本机的任何服务。
    from launcher.desktop_config import DesktopConfigError, load_desktop_config
    from launcher.frozen_detect import get_project_root
    config = load_desktop_config(get_project_root())
    service_env = os.environ.get("XR_RUN_SERVICE", "").strip()
    if config.mode == "remote":
        if service_env or "--run-service" in sys.argv[1:]:
            raise DesktopConfigError("服务器模式不能启动本地子服务；本地运行请使用 local 配置重新打包。")
        from launcher.remote_gui import RemoteLauncherApp
        RemoteLauncherApp(config.base_url).run()
        return

    # 本地模式沿用原有依赖初始化与启动流程。
    try:
        from launcher.config_init import init_config_files
    except ImportError as exc:
        raise DesktopConfigError(
            "当前桌面包不包含本地服务组件。请在 desktop-config.json 中填写服务器网站地址；"
            "需要本地运行时，请选择 local 重新打包。"
        ) from exc
    init_config_files()
    from launcher.browser_setup import ensure_playwright_browser_path
    ensure_playwright_browser_path()

    # 记录一次启动日志，便于诊断启动模式
    try:
        log_dir = get_project_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "launcher.log").open("a", encoding="utf-8") as f:
            f.write(f"args={sys.argv} env_service={service_env}\n")
    except Exception:
        pass

    if service_env:
        # 环境变量指定的子服务模式
        from launcher.service_runner import run_service
        run_service(service_env)
        return

    if len(sys.argv) >= 3 and sys.argv[1] == "--run-service":
        # 子服务运行模式（命令行参数）
        service_name = sys.argv[2]
        from launcher.service_runner import run_service
        run_service(service_name)
        return

    # GUI启动模式
    from launcher.gui import LauncherApp
    app = LauncherApp()
    app.run()
