# encoding=utf-8
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_common


class BuildCommonTests(unittest.TestCase):
    def test_build_pyinstaller_cmd_collects_modules_and_validates_icon(self):
        cmd = build_common.build_pyinstaller_cmd("demo", "entry.py")
        self.assertIn("--collect-submodules=modules", cmd)
        self.assertIn("--hidden-import=numpy", cmd)
        self.assertIn("--hidden-import=cv2", cmd)
        self.assertEqual(cmd[-1], "entry.py")

        with self.assertRaises(SystemExit):
            build_common.build_pyinstaller_cmd(
                "demo", "entry.py", icon_path="/nonexistent/icon.ico"
            )

    def test_copy_required_files_and_dir_fail_fast_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                build_common.copy_required_files(
                    ((os.path.join(tmp, "missing.json"), os.path.join(tmp, "out.json")),)
                )
            empty_dir = os.path.join(tmp, "empty")
            os.makedirs(empty_dir)
            with self.assertRaises(SystemExit):
                build_common.copy_required_dir(empty_dir, os.path.join(tmp, "dest"))

    def test_bundle_windows_python3_dll_fails_fast_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist_dir = os.path.join(tmp, "dist")
            empty_prefix = os.path.join(tmp, "py")
            os.makedirs(empty_prefix)
            with patch.object(build_common.os, "name", "nt"):
                with self.assertRaises(SystemExit):
                    build_common.bundle_windows_python3_dll(
                        dist_dir, search_dirs=(empty_prefix,)
                    )
                dll_path = os.path.join(empty_prefix, "python3.dll")
                with open(dll_path, "wb") as fh:
                    fh.write(b"MZ")
                out = build_common.bundle_windows_python3_dll(
                    dist_dir, search_dirs=(empty_prefix,)
                )
                self.assertTrue(os.path.isfile(out))


if __name__ == "__main__":
    unittest.main()
