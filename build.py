import os
import shutil
import subprocess
import sys

name = "Autovisor"

# 必须与 modules.installer.SUPPORTED_PYTHON 一致: 冻结产物内嵌的解释器会由
# Autovisor.exe 启动时校验, 不在范围内会直接拒绝运行。
BUILD_PYTHON = ((3, 10), (3, 11), (3, 12), (3, 13))

ROOT = os.path.dirname(os.path.abspath(__file__))


def check_build_python():
    """用固定的解释器构建, 避免 PATH 上另一个 pyinstaller 悄悄换掉运行时。"""
    if sys.version_info[:2] not in BUILD_PYTHON:
        supported = ", ".join(f"{major}.{minor}" for major, minor in BUILD_PYTHON)
        raise SystemExit(
            f"构建需要 Python {supported}, 当前是 {sys.version_info[0]}.{sys.version_info[1]}"
            f"({sys.executable}); 否则打包出来的 Autovisor.exe 会在启动时拒绝运行。"
        )
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            f"当前 Python 缺少 PyInstaller: {sys.executable}\n"
            "可执行: uv run --with pyinstaller python build.py"
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


check_build_python()

dist_dir = os.path.join(ROOT, "dist", name)
if os.path.exists(dist_dir):
    shutil.rmtree(dist_dir, ignore_errors=True)

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--log-level=INFO",
    "--noconfirm",
    "-c",
    "-i", os.path.join(ROOT, "resources", "zhs.ico"),
    "--onedir",
    "--contents-directory=internal",
    f"--name={name}",
    "--hidden-import=numpy",
    "--hidden-import=cv2",
    os.path.join(ROOT, "Autovisor.py"),
]
code = subprocess.call(cmd)
if code != 0:
    raise SystemExit(f"pyinstaller 构建失败: {name} (exit={code})")

dist_resources = os.path.join(dist_dir, "resources")
dist_data = os.path.join(dist_dir, "data")
os.makedirs(dist_resources, exist_ok=True)
os.makedirs(dist_data, exist_ok=True)
shutil.copyfile(os.path.join(ROOT, "config.ini.example"), os.path.join(dist_dir, "config.ini"))
shutil.copyfile(os.path.join(ROOT, "data", "mirrors.json"), os.path.join(dist_data, "mirrors.json"))
llm_example = os.path.join(ROOT, "data", "llm.json.example")
if os.path.isfile(llm_example):
    shutil.copyfile(llm_example, os.path.join(dist_data, "llm.json"))
shutil.copyfile(
    os.path.join(ROOT, "resources", "stealth.min.js"),
    os.path.join(dist_resources, "stealth.min.js"),
)
llm_providers_src = os.path.join(ROOT, "resources", "llm_providers.json")
if os.path.isfile(llm_providers_src):
    shutil.copyfile(
        llm_providers_src,
        os.path.join(dist_resources, "llm_providers.json"),
    )
src_scripts = os.path.join(ROOT, "resources", "scripts")
if os.path.isdir(src_scripts):
    shutil.copytree(src_scripts, os.path.join(dist_resources, "scripts"), dirs_exist_ok=True)

# 稳定 ABI (abi3) 扩展(如 opencv 的 cv2.pyd)依赖 python3.dll,
# PyInstaller 不会自动收集,必须随包提供,否则运行时报 "DLL load failed while importing cv2".
if os.name == "nt":
    python3_dll = os.path.join(sys.base_prefix, "python3.dll")
    if os.path.isfile(python3_dll):
        shutil.copyfile(python3_dll, os.path.join(dist_dir, "internal", "python3.dll"))
    else:
        print(f"WARNING: 未找到 {python3_dll}, 发行版可能因缺少 python3.dll 导致 cv2 加载失败!")

build_dir = os.path.join(ROOT, "build")
if os.path.exists(build_dir):
    shutil.rmtree(build_dir, ignore_errors=True)
spec_file = os.path.join(ROOT, f"{name}.spec")
if os.path.isfile(spec_file):
    os.remove(spec_file)
