"""打包 Autovisor 内置无感后端（autovisor-backend[.exe]）供 Tauri 桌面端托管。"""
import os
import shutil
import subprocess
import sys

NAME = "autovisor-backend"
EXE_SUFFIX = ".exe" if os.name == "nt" else ""
BUILD_PYTHON = ((3, 10), (3, 11), (3, 12), (3, 13))
ROOT = os.path.dirname(os.path.abspath(__file__))


def check_build_python():
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
            "可执行: uv run --with pyinstaller python build_backend.py"
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


def main():
    check_build_python()

    dist_dir = os.path.join(ROOT, "dist", NAME)
    if os.path.isdir(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--log-level=INFO",
        "--noconfirm",
        "-c",  # console=True 确保 stdin/stdout 管道可用；由 Tauri CREATE_NO_WINDOW 隐藏黑框
        "--onedir",
        "--contents-directory=internal",
        f"--name={NAME}",
        "--hidden-import=Autovisor",
        "--hidden-import=numpy",
        "--hidden-import=cv2",
        "--collect-submodules=modules",
        "--exclude-module", "tkinter",
        os.path.join(ROOT, "backend_server.py"),
    ]
    code = subprocess.call(cmd, cwd=ROOT)
    if code != 0:
        raise SystemExit(f"PyInstaller 构建内置后端失败 (exit={code})")

    # 拷贝只读内置资源（stealth.min.js、mirrors.json、config.ini.example 与 scripts/）
    res_dir = os.path.join(dist_dir, "resources")
    os.makedirs(res_dir, exist_ok=True)
    bundled_files = (
        (os.path.join(ROOT, "resources", "stealth.min.js"), os.path.join(res_dir, "stealth.min.js")),
        (os.path.join(ROOT, "data", "mirrors.json"), os.path.join(res_dir, "mirrors.json")),
        (os.path.join(ROOT, "config.ini.example"), os.path.join(res_dir, "config.ini.example")),
    )
    for src, dst in bundled_files:
        if os.path.isfile(src):
            shutil.copyfile(src, dst)
    scripts_src = os.path.join(ROOT, "resources", "scripts")
    if os.path.isdir(scripts_src):
        shutil.copytree(scripts_src, os.path.join(res_dir, "scripts"), dirs_exist_ok=True)

    # Windows 下稳定 ABI 扩展（内置的 cv2.pyd）依赖 python3.dll
    if os.name == "nt":
        python3_dll = os.path.join(sys.base_prefix, "python3.dll")
        if os.path.isfile(python3_dll):
            shutil.copyfile(python3_dll, os.path.join(dist_dir, "internal", "python3.dll"))
        else:
            print(f"WARNING: 未找到 {python3_dll}, 运行时加载 cv2 可能失败!")

    for bundled_pkg in ("numpy", "cv2"):
        pkg_dir = os.path.join(dist_dir, "internal", bundled_pkg)
        if not os.path.isdir(pkg_dir):
            raise SystemExit(f"内置后端构建校验失败：未找到内置依赖目录 {pkg_dir}")

    built_exe = os.path.join(dist_dir, NAME + EXE_SUFFIX)
    verify_code = subprocess.call([built_exe, "--verify-deps"], cwd=dist_dir)
    shutil.rmtree(os.path.join(dist_dir, "logs"), ignore_errors=True)
    if verify_code != 0:
        raise SystemExit(f"内置后端依赖自检失败 (exit={verify_code})")

    shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
    spec_file = os.path.join(ROOT, f"{NAME}.spec")
    if os.path.isfile(spec_file):
        os.remove(spec_file)

    print(f"内置后端构建完成: {built_exe}")


if __name__ == "__main__":
    main()
