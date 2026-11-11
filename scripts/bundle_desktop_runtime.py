"""为本地桌面包补齐当前构建解释器的独立 Python 运行库。"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import sys
import sysconfig
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.prepare_desktop_build import UnsafeBuildPath, checked_path, project_root


@dataclass(frozen=True)
class RuntimeBundleResult:
    target: Path
    base_prefix: Path
    site_packages: tuple[Path, ...]
    file_count: int
    dry_run: bool


def _is_link(path: Path) -> bool:
    details = path.lstat()
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _check_destination(root: Path, target: Path, *, dry_run: bool = False) -> None:
    """在复制前检查现有目标树，目录联接和符号链接都不能作为写入入口。"""
    checked_path(root, target.relative_to(root))
    if dry_run and not target.exists():
        return
    if not target.is_dir():
        raise UnsafeBuildPath("build_output/main.dist 不存在，请先完成 Nuitka 编译")
    for directory, folders, files in os.walk(target, followlinks=False):
        for name in folders + files:
            child = Path(directory) / name
            if _is_link(child):
                raise UnsafeBuildPath(f"运行库目标目录包含链接：{child.relative_to(root)}")


def _source_directory(path: Path, target: Path) -> Path:
    source = path.resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"运行库来源不是目录：{path}")
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise UnsafeBuildPath(f"运行库来源和打包目标不能互相包含：{source}")
    return source


def _tree_files(source: Path, *, standard_library: bool = False):
    """遍历明确的运行库目录；标准库不混入系统解释器的 site-packages。"""
    for directory, folders, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(directory)
        for name in list(folders):
            if name == "__pycache__" or (standard_library and current == source and name.lower() == "site-packages"):
                folders.remove(name)
                continue
            child = current / name
            if _is_link(child):
                raise UnsafeBuildPath(f"运行库来源包含目录链接，无法安全复制：{child}")
        for name in files:
            if name.endswith((".pyc", ".pyo")) or name.lower() == "pyvenv.cfg":
                continue
            child = current / name
            if _is_link(child) or not child.is_file():
                raise UnsafeBuildPath(f"运行库来源包含非普通文件，无法安全复制：{child}")
            yield child, child.relative_to(source)


def bundle_runtime(
    root: Path = PROJECT_ROOT,
    *,
    base_prefix: Path | None = None,
    package_paths: Iterable[Path] | None = None,
    dry_run: bool = False,
) -> RuntimeBundleResult:
    """复制到固定 main.dist；可注入小型来源目录做离线测试，不启动服务。"""
    root = project_root(Path(root))
    target = checked_path(root, "build_output/main.dist")
    _check_destination(root, target, dry_run=dry_run)
    base = _source_directory(Path(base_prefix) if base_prefix is not None else Path(sys.base_prefix), target)
    stdlib = _source_directory(base / "Lib", target)
    dlls = _source_directory(base / "DLLs", target)
    if not (stdlib / "encodings" / "__init__.py").is_file():
        raise ValueError("Python 标准库缺少 encodings，无法生成可运行的本地包")

    if package_paths is None:
        paths = sysconfig.get_paths()
        package_paths = (Path(paths["purelib"]), Path(paths["platlib"]))
    packages = tuple(dict.fromkeys(_source_directory(Path(path), target) for path in package_paths))
    if not packages:
        raise ValueError("未找到当前构建解释器的 site-packages")

    # 先完整生成与校验清单，来源缺失或目标异常时不写入任何运行库文件。
    manifest: dict[Path, Path] = {}

    def add(source: Path, relative: Path) -> None:
        if not source.is_file() or _is_link(source):
            raise ValueError(f"缺少运行库文件或来源是链接：{source}")
        destination = checked_path(root, target.relative_to(root) / relative)
        if destination.exists() and not destination.is_file():
            raise UnsafeBuildPath(f"运行库目标文件被目录占用：{destination.relative_to(root)}")
        for parent in destination.parents:
            if parent == root:
                break
            if parent.exists() and not parent.is_dir():
                raise UnsafeBuildPath(f"运行库目标目录被文件占用：{parent.relative_to(root)}")
        manifest[relative] = source

    for name in ("python.exe", "pythonw.exe"):
        add(base / name, Path(name))
    python_dlls = sorted(base.glob("python3*.dll"))
    if not python_dlls:
        raise ValueError("Python 根目录缺少 python3*.dll，无法生成可运行的本地包")
    for source in python_dlls + sorted(base.glob("vcruntime*.dll")):
        add(source, Path(source.name))
    for source, relative in _tree_files(stdlib, standard_library=True):
        add(source, Path("Lib") / relative)
    for source, relative in _tree_files(dlls):
        add(source, Path("DLLs") / relative)
    for package in packages:
        for source, relative in _tree_files(package):
            add(source, Path("Lib/site-packages") / relative)

    if not dry_run:
        for relative, source in manifest.items():
            destination = checked_path(root, target.relative_to(root) / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # 再检查一次，防止复制期间新增的目录联接改变写入位置。
            checked_path(root, destination.relative_to(root))
            shutil.copy2(source, destination)
    return RuntimeBundleResult(target, base, packages, len(manifest), dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只校验来源、目标和文件清单，不复制运行库")
    args = parser.parse_args(argv)
    try:
        result = bundle_runtime(dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"打包本地 Python 运行库失败：{error}", file=sys.stderr)
        return 2
    print(f"Python 运行库来源：{result.base_prefix}")
    for path in result.site_packages:
        print(f"构建环境依赖来源：{path}")
    print(f"{'已检查' if result.dry_run else '已复制'} {result.file_count} 个文件到：{result.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
