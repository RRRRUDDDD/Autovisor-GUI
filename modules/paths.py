# encoding=utf-8
"""统一解析运行时数据目录与只读内置资源路径（单点真相源）。"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_runtime_root() -> str:
    """返回可写运行时数据根目录（支持 AUTOVISOR_DATA_DIR 与 PyInstaller 打包模式）。"""
    override = os.environ.get("AUTOVISOR_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return _REPO_ROOT


def get_runtime_path(*parts: str) -> str:
    """拼接运行时数据目录下的子路径。"""
    return os.path.join(get_runtime_root(), *parts)


def get_resource_path(*parts: str) -> str:
    """定位只读内置资源（如 stealth.min.js / llm_providers.json），支持数据目录与程序目录分离。"""
    res_override = os.environ.get("AUTOVISOR_RESOURCES_DIR", "").strip()
    if res_override:
        base = os.path.abspath(res_override)
        candidate = os.path.join(base, *parts)
        if os.path.exists(candidate):
            return candidate
        if parts and parts[0] == "resources":
            candidate = os.path.join(base, *parts[1:])
            if os.path.exists(candidate):
                return candidate
    if getattr(sys, "frozen", False):
        exe_candidate = os.path.join(os.path.dirname(sys.executable), *parts)
        if os.path.exists(exe_candidate):
            return exe_candidate
    repo_candidate = os.path.join(_REPO_ROOT, *parts)
    if os.path.exists(repo_candidate):
        return repo_candidate
    return get_runtime_path(*parts)


def find_bundled_resource_file(filename: str) -> str | None:
    """查找随程序打包分发的只读资源文件（如 mirrors.json / llm_providers.json）。"""
    res_override = os.environ.get("AUTOVISOR_RESOURCES_DIR", "").strip()
    candidates: list[str] = []
    if res_override:
        base = os.path.abspath(res_override)
        candidates.append(os.path.join(base, filename))
        candidates.append(os.path.join(base, "resources", filename))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "resources", filename))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None
