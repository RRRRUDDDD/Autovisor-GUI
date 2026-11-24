"""Shared PyInstaller build helpers for build.py and build_backend.py."""
from __future__ import annotations

import os
import shutil
import sys
from typing import Iterable, Sequence

# 必须与 modules.installer.SUPPORTED_PYTHON 一致: 冻结产物内嵌的解释器会由
# 启动时校验, 不在范围内会直接拒绝运行。
BUILD_PYTHON = ((3, 10), (3, 11), (3, 12), (3, 13))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE_SUFFIX = ".exe" if os.name == "nt" else ""


def check_build_python(script_name: str = "build.py") -> None:
    """用固定的解释器构建, 避免 PATH 上另一个 pyinstaller 悄悄换掉运行时。"""
    if sys.version_info[:2] not in BUILD_PYTHON:
        supported = ", ".join(f"{major}.{minor}" for major, minor in BUILD_PYTHON)
        raise SystemExit(
            f"构建需要 Python {supported}, 当前是 {sys.version_info[0]}.{sys.version_info[1]}"
            f"({sys.executable})"
        )
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            f"当前 Python 缺少 PyInstaller: {sys.executable}\n"
            f"可执行: uv run --group build python {script_name}"
        ) from exc
    missing_deps = []
    for mod_name in ("numpy", "cv2"):
        try:
            __import__(mod_name)
        except ImportError:
            missing_deps.append(mod_name)
    if missing_deps:
        raise SystemExit(
            f"当前 Python 缺少内置验证码依赖 {', '.join(missing_deps)}: {sys.executable}\n"
            "请先执行: uv sync"
        )


def build_pyinstaller_cmd(
    name: str,
    entry_script: str,
    *,
    icon_path: str | None = None,
    extra_args: Sequence[str] = (),
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--log-level=INFO",
        "--noconfirm",
        "-c",
        "--onedir",
        "--contents-directory=internal",
        f"--name={name}",
        "--hidden-import=numpy",
        "--hidden-import=cv2",
        "--collect-submodules=modules",
    ]
    if icon_path:
        if not os.path.isfile(icon_path):
            raise SystemExit(f"构建失败：缺少图标资源文件 {icon_path}")
        cmd.extend(["-i", icon_path])
    cmd.extend(extra_args)
    cmd.append(entry_script)
    return cmd


def copy_required_files(pairs: Iterable[tuple[str, str]]) -> None:
    """拷贝必需的构建资源；任一源文件缺失时立即 fail fast。"""
    for src, dst in pairs:
        if not os.path.isfile(src):
            raise SystemExit(f"构建失败：缺少必需资源文件 {src}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)


def copy_required_dir(src: str, dst: str) -> None:
    """拷贝必需的资源目录；目录不存在或为空时立即 fail fast。"""
    if not os.path.isdir(src):
        raise SystemExit(f"构建失败：缺少必需资源目录 {src}")
    if not os.listdir(src):
        raise SystemExit(f"构建失败：必需资源目录为空 {src}")
    shutil.copytree(src, dst, dirs_exist_ok=True)


def bundle_windows_python3_dll(
    dist_dir: str,
    *,
    search_dirs: Sequence[str] | None = None,
) -> str | None:
    """Windows 下稳定 ABI (abi3) 扩展(如 cv2.pyd) 依赖 python3.dll；缺失时 fail fast。"""
    if os.name != "nt":
        return None
    candidates = list(search_dirs) if search_dirs is not None else [
        sys.base_prefix,
        os.path.dirname(sys.executable),
    ]
    for base in candidates:
        if not base:
            continue
        dll = os.path.join(base, "python3.dll")
        if os.path.isfile(dll):
            dst = os.path.join(dist_dir, "internal", "python3.dll")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(dll, dst)
            return dst
    searched = ", ".join(os.path.join(base, "python3.dll") for base in candidates if base)
    raise SystemExit(
        f"构建失败：未找到 python3.dll（已检查：{searched}），"
        "缺少该文件将导致冻结产物加载 cv2 失败。"
    )


def cleanup_build_artifacts(name: str, root: str = ROOT) -> None:
    build_dir = os.path.join(root, "build")
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)
    spec_file = os.path.join(root, f"{name}.spec")
    if os.path.isfile(spec_file):
        os.remove(spec_file)
