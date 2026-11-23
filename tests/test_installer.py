import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from modules.installer import (
    DEFAULT_PACKAGES,
    build_wheel_url,
    install_package,
    is_compatible_wheel,
    load_preinstalled_modules,
    runtime_packages,
    start,
    validate_python_version,
    wheel_tags,
)
from modules.progress import show_progress


class InstallerTests(unittest.TestCase):
    def test_rejects_unsupported_python_version(self):
        for minor in (9, 14):
            with self.assertRaises(RuntimeError):
                validate_python_version(SimpleNamespace(major=3, minor=minor))

    def test_accepts_supported_python_versions(self):
        for minor in (10, 11, 12, 13):
            validate_python_version(SimpleNamespace(major=3, minor=minor))

    def test_runtime_packages_match_python_version(self):
        self.assertEqual(
            {"numpy": "2.1.3", "opencv-python": "4.10.0.84"},
            runtime_packages(SimpleNamespace(major=3, minor=13)),
        )
        for minor in (10, 11, 12):
            self.assertEqual(
                DEFAULT_PACKAGES,
                runtime_packages(SimpleNamespace(major=3, minor=minor)),
            )

    def test_parses_wheel_tags(self):
        self.assertEqual(
            wheel_tags("numpy-1.26.4-cp311-cp311-win_amd64.whl"),
            ("1.26.4", "cp311", "cp311", "win_amd64"),
        )

    def test_requires_matching_python_abi_and_platform(self):
        self.assertTrue(is_compatible_wheel(
            "numpy-1.26.4-cp311-cp311-win_amd64.whl",
            "numpy", "1.26.4", "cp311", "cp311", "win_amd64",
        ))
        self.assertFalse(is_compatible_wheel(
            "numpy-1.26.4-cp310-cp310-win_amd64.whl",
            "numpy", "1.26.4", "cp311", "cp311", "win_amd64",
        ))

    def test_accepts_opencv_abi3_wheel(self):
        self.assertTrue(is_compatible_wheel(
            "opencv_python-4.10.0.82-cp37-abi3-win_amd64.whl",
            "opencv-python", "4.10.0.82", "cp311", "cp311", "win_amd64",
        ))

    def test_opencv_patch_build_is_not_interchangeable(self):
        self.assertFalse(is_compatible_wheel(
            "opencv_python-4.10.0.82-cp37-abi3-win_amd64.whl",
            "opencv-python", "4.10.0.84", "cp313", "cp313", "win_amd64",
        ))
        self.assertTrue(is_compatible_wheel(
            "opencv_python-4.10.0.84-cp37-abi3-win_amd64.whl",
            "opencv-python", "4.10.0.84", "cp313", "cp313", "win_amd64",
        ))

    def test_rejects_free_threaded_wheel(self):
        self.assertTrue(is_compatible_wheel(
            "numpy-2.1.3-cp313-cp313-win_amd64.whl",
            "numpy", "2.1.3", "cp313", "cp313", "win_amd64",
        ))
        self.assertFalse(is_compatible_wheel(
            "numpy-2.1.3-cp313-cp313t-win_amd64.whl",
            "numpy", "2.1.3", "cp313", "cp313", "win_amd64",
        ))

    def test_progress_handles_missing_content_length(self):
        output = StringIO()
        with redirect_stdout(output):
            show_progress("下载进度:", current=512, total=0)
        self.assertIn("已下载 512 bytes", output.getvalue())

    def test_resolves_relative_mirror_wheel_url(self):
        url = build_wheel_url(
            "https://mirrors.example/repository/pypi/simple/numpy/",
            "../../packages/abc/numpy-1.26.4-cp310-cp310-win_amd64.whl#sha256=test",
        )
        self.assertEqual(
            url,
            "https://mirrors.example/repository/pypi/packages/abc/numpy-1.26.4-cp310-cp310-win_amd64.whl",
        )

    @patch("modules.installer.import_module")
    @patch("modules.installer.extract_whl")
    @patch("modules.installer.clear_package_files")
    @patch("modules.installer.download_wheel")
    def test_falls_back_when_a_mirror_fails(
        self, download_wheel, clear_package_files, extract_whl, import_module
    ):
        download_wheel.side_effect = [
            RuntimeError("429 Client Error"),
            "numpy-1.26.4.whl",
        ]
        import_module.return_value = object()

        with patch("modules.installer.get_res_dir", return_value="packages"):
            result = install_package(
                "numpy", "1.26.4", [("华为", "https://huawei"), ("清华", "https://tsinghua")]
            )

        self.assertIsNotNone(result)
        self.assertEqual(download_wheel.call_count, 2)
        self.assertEqual(download_wheel.call_args_list[1].args[:3],
                         ("清华", "https://tsinghua", "numpy"))

    def test_start_uses_preinstalled_modules_without_network(self):
        fake_np = SimpleNamespace(__version__="2.5.1")
        fake_cv2 = SimpleNamespace(__version__="4.13.0")
        modules_map = {"numpy": fake_np, "cv2": fake_cv2}

        with (
            patch("modules.installer.os.name", "nt"),
            patch("modules.installer.import_module", side_effect=lambda name: modules_map[name]),
            patch("modules.installer.test_mirrors") as mock_mirrors,
            patch("modules.installer.install_package") as mock_install,
            patch("modules.installer.add_runtime_search_paths") as mock_add_paths,
        ):
            loaded = start(config_obj=SimpleNamespace(mirrors={}))

        self.assertEqual(loaded, [fake_np, fake_cv2])
        mock_mirrors.assert_not_called()
        mock_install.assert_not_called()
        mock_add_paths.assert_not_called()

    def test_load_preinstalled_returns_none_when_cv2_missing(self):
        fake_np = SimpleNamespace(__version__="2.5.1")

        def fake_import(name):
            if name == "numpy":
                return fake_np
            raise ImportError("No module named 'cv2'")

        with patch("modules.installer.import_module", side_effect=fake_import):
            self.assertIsNone(load_preinstalled_modules())


if __name__ == "__main__":
    unittest.main()
