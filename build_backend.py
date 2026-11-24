"""打包 Autovisor 内置无感后端（autovisor-backend[.exe]）供 Tauri 桌面端托管。"""
import os
import shutil
import subprocess

from scripts.build_common import (
    EXE_SUFFIX,
    ROOT,
    build_pyinstaller_cmd,
    bundle_windows_python3_dll,
    check_build_python,
    cleanup_build_artifacts,
    copy_required_dir,
    copy_required_files,
)

NAME = "autovisor-backend"


def main():
    check_build_python("build_backend.py")

    dist_dir = os.path.join(ROOT, "dist", NAME)
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)

    cmd = build_pyinstaller_cmd(
        NAME,
        os.path.join(ROOT, "backend_server.py"),
        extra_args=(
            "--hidden-import=Autovisor",
            "--exclude-module",
            "tkinter",
        ),
    )
    code = subprocess.call(cmd, cwd=ROOT)
    if code != 0:
        raise SystemExit(f"PyInstaller 构建内置后端失败 (exit={code})")

    # 拷贝只读内置资源（stealth.min.js、llm_providers.json、mirrors.json、config.ini.example 与 scripts/）
    res_dir = os.path.join(dist_dir, "resources")
    copy_required_files(
        (
            (os.path.join(ROOT, "resources", "stealth.min.js"), os.path.join(res_dir, "stealth.min.js")),
            (
                os.path.join(ROOT, "resources", "llm_providers.json"),
                os.path.join(res_dir, "llm_providers.json"),
            ),
            (os.path.join(ROOT, "data", "mirrors.json"), os.path.join(res_dir, "mirrors.json")),
            (
                os.path.join(ROOT, "config.ini.example"),
                os.path.join(res_dir, "config.ini.example"),
            ),
        )
    )
    copy_required_dir(
        os.path.join(ROOT, "resources", "scripts"),
        os.path.join(res_dir, "scripts"),
    )
    bundle_windows_python3_dll(dist_dir)

    for bundled_pkg in ("numpy", "cv2"):
        pkg_dir = os.path.join(dist_dir, "internal", bundled_pkg)
        if not os.path.isdir(pkg_dir):
            raise SystemExit(f"内置后端构建校验失败：未找到内置依赖目录 {pkg_dir}")

    built_exe = os.path.join(dist_dir, NAME + EXE_SUFFIX)
    verify_code = subprocess.call([built_exe, "--verify-deps"], cwd=dist_dir)
    shutil.rmtree(os.path.join(dist_dir, "logs"), ignore_errors=True)
    if verify_code != 0:
        raise SystemExit(f"内置后端依赖自检失败 (exit={verify_code})")

    cleanup_build_artifacts(NAME, ROOT)
    print(f"内置后端构建完成: {built_exe}")


if __name__ == "__main__":
    main()
