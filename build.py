import os
import shutil
import subprocess

from scripts.build_common import (
    ROOT,
    build_pyinstaller_cmd,
    bundle_windows_python3_dll,
    check_build_python,
    cleanup_build_artifacts,
    copy_required_dir,
    copy_required_files,
)

NAME = "Autovisor"


def main():
    check_build_python("build.py")

    dist_dir = os.path.join(ROOT, "dist", NAME)
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir, ignore_errors=True)

    cmd = build_pyinstaller_cmd(
        NAME,
        os.path.join(ROOT, "Autovisor.py"),
        icon_path=os.path.join(ROOT, "resources", "zhs.ico"),
    )
    code = subprocess.call(cmd, cwd=ROOT)
    if code != 0:
        raise SystemExit(f"pyinstaller 构建失败: {NAME} (exit={code})")

    dist_resources = os.path.join(dist_dir, "resources")
    dist_data = os.path.join(dist_dir, "data")
    copy_required_files(
        (
            (os.path.join(ROOT, "config.ini.example"), os.path.join(dist_dir, "config.ini")),
            (os.path.join(ROOT, "data", "mirrors.json"), os.path.join(dist_data, "mirrors.json")),
            (
                os.path.join(ROOT, "resources", "stealth.min.js"),
                os.path.join(dist_resources, "stealth.min.js"),
            ),
            (
                os.path.join(ROOT, "resources", "llm_providers.json"),
                os.path.join(dist_resources, "llm_providers.json"),
            ),
        )
    )
    llm_example = os.path.join(ROOT, "data", "llm.json.example")
    if os.path.isfile(llm_example):
        shutil.copyfile(llm_example, os.path.join(dist_data, "llm.json"))

    copy_required_dir(
        os.path.join(ROOT, "resources", "scripts"),
        os.path.join(dist_resources, "scripts"),
    )
    bundle_windows_python3_dll(dist_dir)
    cleanup_build_artifacts(NAME, ROOT)


if __name__ == "__main__":
    main()
