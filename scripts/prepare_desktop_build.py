"""只清理项目内固定打包目录，拒绝符号链接、目录联接和越界路径。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import sys


class UnsafeBuildPath(ValueError):
    pass


def project_root(path: Path) -> Path:
    root = path.resolve(strict=True)
    if root.parent == root or not root.is_dir():
        raise UnsafeBuildPath("项目路径不能是磁盘根目录")
    if not (root / "launcher" / "main.py").is_file() or not (root / "EXE打包构建.bat").is_file():
        raise UnsafeBuildPath("项目路径缺少桌面启动器或打包脚本")
    return root


def _is_link(path: Path) -> bool:
    details = path.lstat()
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def checked_path(root: Path, relative: Path | str) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise UnsafeBuildPath("清理路径必须位于项目的固定打包目录内")
    target = root / relative
    resolved = target.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root) or resolved != target:
        raise UnsafeBuildPath(f"拒绝清理越界或链接路径：{relative}")
    if target.exists() and _is_link(target):
        raise UnsafeBuildPath(f"拒绝清理链接路径：{relative}")
    return target


def _check_tree(root: Path, path: Path) -> None:
    """在开始删除前检查整个待删树，Windows junction 也按链接拒绝。"""
    checked_path(root, path.relative_to(root))
    if not path.exists():
        return
    if path.is_dir():
        for directory, folders, files in os.walk(path, followlinks=False):
            for name in folders + files:
                child = Path(directory) / name
                if _is_link(child):
                    raise UnsafeBuildPath(f"待清理目录内含链接：{child.relative_to(root)}")


def removal_plan(root: Path, action: str) -> list[Path]:
    root = project_root(root)
    if action == "clean":
        relatives = [Path("build_output"), Path("release")]
    elif action == "sanitize-release":
        package = checked_path(root, "release/XianyuAutoReply")
        if not package.is_dir():
            raise UnsafeBuildPath("release/XianyuAutoReply 不存在")
        relatives = [Path("release/XianyuAutoReply/data")]
    elif action == "sanitize-dist":
        dist = checked_path(root, "build_output/main.dist")
        if not dist.is_dir():
            raise UnsafeBuildPath("build_output/main.dist 不存在")
        relatives = [Path("build_output/main.dist/data")]
        for service in ("backend-web", "websocket", "scheduler", "common"):
            service_dir = checked_path(root, Path("build_output/main.dist") / service)
            if not service_dir.exists():
                continue
            _check_tree(root, service_dir)
            for directory, folders, files in os.walk(service_dir, topdown=True, followlinks=False):
                parent = Path(directory)
                for name in list(folders):
                    if name in {"__pycache__", ".pytest_cache", ".mypy_cache", "logs", "temp", "tmp", "browser_data"} or (name == "uploads" and parent.name == "static"):
                        relatives.append((parent / name).relative_to(root))
                        folders.remove(name)
                for name in files:
                    if name.endswith((".pyc", ".pyo")) or name in {".env", ".env.example", "pyproject.toml"}:
                        relatives.append((parent / name).relative_to(root))
    else:
        raise ValueError("未知清理阶段")
    targets = [checked_path(root, relative) for relative in relatives]
    for target in targets:
        _check_tree(root, target)
    return targets


def prepare(root: Path, action: str, *, dry_run: bool = False) -> list[Path]:
    root = project_root(root)
    targets = removal_plan(root, action)
    if dry_run:
        return targets
    for target in targets:
        _check_tree(root, target)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    if action == "clean":
        (root / "release").mkdir(exist_ok=True)
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("clean", "sanitize-dist", "sanitize-release"))
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--dry-run", action="store_true", help="只校验并列出范围，不删除文件")
    args = parser.parse_args(argv)
    try:
        paths = prepare(args.project_root, args.action, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"打包目录检查失败：{exc}", file=sys.stderr)
        return 2
    for path in paths:
        print(f"{'检查' if args.dry_run else '清理'}：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
