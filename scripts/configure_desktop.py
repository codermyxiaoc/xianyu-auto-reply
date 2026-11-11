"""校验并写入桌面启动配置；打包入口只通过文件传递地址，不拼接 shell。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.desktop_config import CONFIG_FILENAME, resolve_desktop_config


def write_config(output: Path, base_url: str) -> str:
    """先完成校验，再原子替换 JSON；错误输入不覆盖已存在的配置。"""
    config = resolve_desktop_config(base_url)
    payload = {"base_url": "local" if config.mode == "local" else config.base_url}
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent,
            prefix=".desktop-config-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return config.mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成桌面端 desktop-config.json")
    parser.add_argument("--base-url", help="local 或服务器网页入口，例如 http://103.73.119.239:9000")
    parser.add_argument("--output", type=Path, help="输出 JSON 文件路径；默认写入项目根目录 desktop-config.json")
    parser.add_argument("--interactive", action="store_true", help="未指定地址或环境变量时交互询问，直接回车为 local")
    parser.add_argument("--print-mode", action="store_true", help="仅向标准输出打印 local/remote，可同时使用 --output")
    args = parser.parse_args(argv)
    if args.output is None and not args.print_mode:
        args.output = Path(__file__).resolve().parent.parent / CONFIG_FILENAME

    base_url = args.base_url
    if base_url is None:
        base_url = os.environ.get("XIANYU_DESKTOP_BASE_URL")
    if base_url is None and args.interactive:
        print("Desktop base URL [local]: ", end="", file=sys.stderr, flush=True)
        try:
            base_url = input()
        except (EOFError, KeyboardInterrupt):
            print("\n未读取到桌面配置，打包已停止。", file=sys.stderr)
            return 2
        if not base_url.strip():
            base_url = "local"
    if base_url is None:
        base_url = "local"

    try:
        config = resolve_desktop_config(base_url)
        if args.output is not None:
            write_config(args.output, base_url)
    except (ValueError, OSError) as exc:
        print(f"桌面配置失败：{exc}", file=sys.stderr)
        return 2
    if args.print_mode:
        print(config.mode)
    else:
        print(f"已保存桌面配置（{config.mode}）：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
