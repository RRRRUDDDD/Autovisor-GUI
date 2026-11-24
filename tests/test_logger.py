import gzip
import os
import re
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from modules.logger import Logger, redact_sensitive


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LogFileTestCase(unittest.TestCase):
    """把日志重定向到临时文件, 便于检查真实写入格式。"""

    def setUp(self):
        self.logger = Logger()
        self.original_filename = self.logger.filename
        self.original_context = dict(self.logger._context)
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        handle.close()
        self.tmp_path = handle.name
        self.logger.filename = self.tmp_path
        self.logger._throttled.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        self.logger.filename = self.original_filename
        self.logger._context.clear()
        self.logger._context.update(self.original_context)
        self.logger._throttled.clear()
        os.unlink(self.tmp_path)

    def lines(self):
        self.logger.flush()
        with open(self.tmp_path, encoding="utf-8") as file:
            return file.read().splitlines()

    def text(self):
        return "\n".join(self.lines())


class DebugThrottledTests(LogFileTestCase):
    def setUp(self):
        super().setUp()
        self.written = []
        self.logger.write_log = lambda msg, **kwargs: self.written.append(msg)
        self.clock = FakeClock()
        patcher = mock.patch("modules.logger.time.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.logger.__dict__.pop, "write_log", None)

    def test_first_message_is_written_immediately(self):
        self.logger.debug_throttled("k", "miss")
        self.assertEqual(self.written, ["miss\n"])

    def test_repeats_within_interval_are_folded(self):
        self.logger.debug_throttled("k", "miss")
        for _ in range(3):
            self.clock.advance(5)
            self.logger.debug_throttled("k", "miss")
        self.assertEqual(self.written, ["miss\n"])

        self.clock.advance(60)
        self.logger.debug_throttled("k", "miss")
        self.assertEqual(
            self.written,
            ["miss\n", "miss (期间折叠 3 次)\n"],
        )

    def test_interval_window_starts_from_last_write(self):
        self.logger.debug_throttled("k", "miss")
        self.clock.advance(59)
        self.logger.debug_throttled("k", "miss")
        self.clock.advance(2)
        self.logger.debug_throttled("k", "miss")
        self.assertEqual(
            self.written,
            ["miss\n", "miss (期间折叠 1 次)\n"],
        )

    def test_keys_are_throttled_independently(self):
        self.logger.debug_throttled("a", "miss a")
        self.logger.debug_throttled("b", "miss b")
        self.clock.advance(1)
        self.logger.debug_throttled("a", "miss a")
        self.logger.debug_throttled("b", "miss b")
        self.assertEqual(
            self.written,
            ["miss a\n", "miss b\n"],
        )

    def test_custom_interval(self):
        self.logger.debug_throttled("k", "miss", interval=5)
        self.clock.advance(5)
        self.logger.debug_throttled("k", "miss", interval=5)
        self.assertEqual(self.written, ["miss\n", "miss\n"])


class LogRecordTests(LogFileTestCase):
    def test_record_prefix_order_is_time_level_source(self):
        self.logger.debug("hello")
        line = self.lines()[-1]
        self.assertRegex(
            line,
            r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[DEBUG\] "
            r"\[[\w.]*test_logger\.[\w_]+:\d+\] hello$",
        )

    def test_every_level_uses_the_same_prefix_order(self):
        self.logger.info("i", shift=True)
        self.logger.warn("w", shift=True)
        self.logger.error("e", shift=True)
        self.logger.event("ev", 键="值")
        prefixes = [
            r"\[INFO\] \[[\w.]*test_logger\.[\w_]+:\d+\] i$",
            r"\[WARN\] \[[\w.]*test_logger\.[\w_]+:\d+\] w$",
            r"\[ERROR\] \[[\w.]*test_logger\.[\w_]+:\d+\] e$",
            r"\[EVENT\] \[[\w.]*test_logger\.[\w_]+:\d+\] ev \| 键=值$",
        ]
        for line, expected in zip(self.lines(), prefixes):
            self.assertRegex(line, expected)

    def test_source_location_skips_logger_internals(self):
        self.logger.info("hello", shift=True)
        line = self.lines()[-1]
        self.assertNotIn("modules.logger.", line)
        self.assertIn("test_source_location_skips_logger_internals:", line)

    def test_event_renders_fields_and_skips_empty(self):
        self.logger.event("课时开始", 序号="1/3", 标题="第一章", 备注=None, 空="")
        line = self.lines()[-1]
        self.assertIn("[EVENT]", line)
        self.assertIn("课时开始 | 序号=1/3 标题=第一章", line)
        self.assertNotIn("备注", line)
        self.assertNotIn("空=", line)

    def test_event_interval_folds_repeats(self):
        clock = FakeClock()
        with mock.patch("modules.logger.time.monotonic", clock):
            self.logger.event("播放状态", interval=60, 进度="12%")
            for _ in range(4):
                clock.advance(1)
                self.logger.event("播放状态", interval=60, 进度="12%")
            clock.advance(60)
            self.logger.event("播放状态", interval=60, 进度="13%")
        self.assertIn("播放状态 | 进度=12%", self.text())
        self.assertIn("进度=13% 期间折叠=4", self.text())
        self.assertEqual(len([line for line in self.lines() if "播放状态" in line]), 2)

    def test_section_writes_banner(self):
        self.logger.section("运行日志")
        self.assertIn("===== 运行日志 =====", self.text())
        banner = [line for line in self.lines() if "===== 运行日志 =====" in line]
        self.assertEqual(len(banner), 1)
        self.assertRegex(banner[0], r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] ")

    def test_context_is_attached_to_exception_log(self):
        self.logger.context(course="高数", lesson="1.1")
        try:
            raise ValueError("boom")
        except ValueError as exc:
            self.logger.log_exception("课时失败.", exc)
        text = self.text()
        self.assertIn("异常类型: ValueError", text)
        self.assertIn("异常详情: boom", text)
        self.assertIn("运行状态: course=高数 lesson=1.1", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertEqual(text.count("运行状态:"), 1)

    def test_error_includes_run_state(self):
        self.logger.context(course="高数", lesson="1.1")
        self.logger.error("出错了")
        text = self.text()
        self.assertIn("[ERROR]", text)
        self.assertIn("出错了", text)
        self.assertIn("运行状态: course=高数 lesson=1.1", text)

    def test_error_without_context_has_no_state(self):
        self.logger.error("出错了")
        self.assertNotIn("运行状态:", self.text())

    def test_context_can_be_cleared(self):
        self.logger.context(course="高数", lesson="1.1")
        self.logger.clear_context("lesson")
        self.assertEqual(self.logger.context_text(), "course=高数")
        self.logger.clear_context()
        self.assertEqual(self.logger.context_text(), "")

    def test_no_context_line_without_context(self):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            self.logger.log_exception("课时失败.", exc)
        self.assertNotIn("运行状态:", self.text())


class SummarizeExceptionTests(unittest.TestCase):
    def test_uses_first_line_only(self):
        exc = RuntimeError("first\nsecond")
        self.assertEqual(Logger.summarize_exception(exc), "RuntimeError: first")

    def test_source_location_of_unknown_frame_is_safe(self):
        self.assertTrue(re.match(r"^.+$", Logger._source_location()))


class LogRotationTests(unittest.TestCase):
    def test_rotates_and_prunes_oldest_logs_beyond_max_log_files(self):
        logger = Logger()
        orig_log_dir = logger.log_dir
        orig_filename = logger.filename
        orig_max_in_dict = "MAX_LOG_FILES" in logger.__dict__
        orig_max_val = logger.__dict__.get("MAX_LOG_FILES")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                logger.log_dir = tmp
                logger.configure(max_files=3)

                # Create Log2.txt and Log5.txt (gap in numbering)
                p2 = os.path.join(tmp, "Log2.txt")
                p5 = os.path.join(tmp, "Log5.txt")
                with open(p2, "w", encoding="utf-8") as f:
                    f.write("old 2")
                with open(p5, "w", encoding="utf-8") as f:
                    f.write("old 5")
                os.utime(p2, (1000, 1000))
                os.utime(p5, (2000, 2000))

                # Next file uses max_index + 1 -> Log6.txt
                p6 = logger._open_new_log_file()
                self.assertEqual(os.path.basename(p6), "Log6.txt")
                self.assertEqual(sorted(os.listdir(tmp)), ["Log2.txt", "Log5.txt", "Log6.txt"])
                os.utime(p6, (3000, 3000))

                # Opening a 4th log prunes the oldest (Log2.txt) keeping MAX_LOG_FILES = 3
                p7 = logger._open_new_log_file()
                self.assertEqual(os.path.basename(p7), "Log7.txt")
                self.assertEqual(sorted(os.listdir(tmp)), ["Log5.txt", "Log6.txt", "Log7.txt"])
        finally:
            if orig_max_in_dict:
                logger.__dict__["MAX_LOG_FILES"] = orig_max_val
            else:
                logger.__dict__.pop("MAX_LOG_FILES", None)
            logger.log_dir = orig_log_dir
            logger.filename = orig_filename


class LoggerInitTests(unittest.TestCase):
    ATTRS = (
        "MAX_LOG_SIZE_MB",
        "LOG_RETENTION_DAYS",
        "LOG_COMPRESS_AFTER_DAYS",
        "MAX_LOG_FILES",
    )

    def test_init_does_not_maintain_existing_logs(self):
        logger = Logger()
        saved = (logger.filename, logger.log_dir, logger.runtime_root)
        saved_class = {name: getattr(Logger, name) for name in self.ATTRS}

        def restore():
            logger.filename, logger.log_dir, logger.runtime_root = saved
            for name, value in saved_class.items():
                setattr(Logger, name, value)

        self.addCleanup(restore)
        with mock.patch.object(Logger, "_maintain_logs") as maintain, mock.patch.object(
            Logger, "_open_new_log_file", return_value="Log1.txt"
        ) as opener:
            logger._init()
        maintain.assert_not_called()
        opener.assert_called_once_with(prune=False)

    def test_init_does_not_prune_below_user_max_files_before_config_loaded(self):
        logger = Logger()
        saved = (logger.filename, logger.log_dir, logger.runtime_root)
        saved_class = {name: getattr(Logger, name) for name in self.ATTRS}

        def restore():
            logger.filename, logger.log_dir, logger.runtime_root = saved
            for name, value in saved_class.items():
                setattr(Logger, name, value)

        self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "logs")
            os.makedirs(log_dir, exist_ok=True)
            for idx in range(1, 211):
                with open(os.path.join(log_dir, f"Log{idx}.txt"), "w", encoding="utf-8") as f:
                    f.write("old")

            Logger.MAX_LOG_FILES = 200
            with mock.patch.object(Logger, "get_runtime_root", return_value=tmp):
                logger._init()

            # 初始化阶段绝不按默认 200 裁剪：原有 210 个旧日志 + 1 个新日志 = 211
            self.assertEqual(len(os.listdir(log_dir)), 211)

            cfg = SimpleNamespace(
                log_max_size_mb=10,
                log_retention_days=0,
                log_max_files=300,
                log_compress_after_days=0,
            )
            logger.configure_from_config(cfg)
            self.assertEqual(len(os.listdir(log_dir)), 211)


class LogProductionTests(unittest.TestCase):
    """Phase 1.5: 体积轮转、保留期清理、gzip 归档与脱敏。"""

    ATTRS = (
        "MAX_LOG_SIZE_MB",
        "LOG_RETENTION_DAYS",
        "LOG_COMPRESS_AFTER_DAYS",
        "MAX_LOG_FILES",
    )

    def setUp(self):
        self.logger = Logger()
        self.saved = {
            name: self.logger.__dict__[name]
            for name in self.ATTRS
            if name in self.logger.__dict__
        }
        self.added = set()
        self.orig_dir = self.logger.log_dir
        self.orig_filename = self.logger.filename
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.logger.log_dir = self.tmp
        self.logger.filename = os.path.join(self.tmp, "Log1.txt")
        with open(self.logger.filename, "w", encoding="utf-8"):
            pass
        self.addCleanup(self._restore)

    def _restore(self):
        for name in self.ATTRS:
            if name in self.saved:
                self.logger.__dict__[name] = self.saved[name]
            else:
                self.logger.__dict__.pop(name, None)
        self.logger.log_dir = self.orig_dir
        self.logger.filename = self.orig_filename
        self._tmp.cleanup()

    def _set(self, name, value):
        if name not in self.logger.__dict__:
            self.added.add(name)
        setattr(self.logger, name, value)

    def _files(self):
        return sorted(os.listdir(self.tmp))

    def test_write_beyond_size_limit_switches_to_next_log_file(self):
        self._set("MAX_LOG_SIZE_MB", 0.0005)  # ~524 bytes
        self._set("MAX_LOG_FILES", 50)
        for index in range(30):
            self.logger.write_log(f"测试轮转第{index}行 " + "x" * 80 + "\n")
        files = self._files()
        self.assertGreaterEqual(len(files), 2)
        self.assertNotEqual(os.path.basename(self.logger.filename), "Log1.txt")
        self.assertTrue(os.path.isfile(self.logger.filename))

    def test_maintain_removes_logs_older_than_retention(self):
        self._set("LOG_RETENTION_DAYS", 14.0)
        self._set("LOG_COMPRESS_AFTER_DAYS", 0.0)
        old = os.path.join(self.tmp, "Log1.txt")
        recent = os.path.join(self.tmp, "Log2.txt")
        with open(old, "w", encoding="utf-8") as file:
            file.write("old")
        with open(recent, "w", encoding="utf-8") as file:
            file.write("recent")
        now = time.time()
        os.utime(old, (now - 20 * 86400, now - 20 * 86400))
        os.utime(recent, (now - 3600, now - 3600))
        self.logger.filename = os.path.join(self.tmp, "Log3.txt")
        with open(self.logger.filename, "w", encoding="utf-8"):
            pass

        self.logger._maintain_logs()

        files = self._files()
        self.assertNotIn("Log1.txt", files)
        self.assertIn("Log2.txt", files)
        self.assertIn("Log3.txt", files)

    def test_maintain_compresses_old_logs_with_matching_content(self):
        self._set("LOG_RETENTION_DAYS", 14.0)
        self._set("LOG_COMPRESS_AFTER_DAYS", 3.0)
        old = os.path.join(self.tmp, "Log1.txt")
        with open(old, "w", encoding="utf-8") as file:
            file.write("需要归档的内容")
        now = time.time()
        os.utime(old, (now - 5 * 86400, now - 5 * 86400))
        recent = os.path.join(self.tmp, "Log2.txt")
        with open(recent, "w", encoding="utf-8") as file:
            file.write("keep")
        self.logger.filename = os.path.join(self.tmp, "Log3.txt")
        with open(self.logger.filename, "w", encoding="utf-8"):
            pass

        self.logger._maintain_logs()

        files = self._files()
        self.assertNotIn("Log1.txt", files)
        self.assertIn("Log1.txt.gz", files)
        with gzip.open(os.path.join(self.tmp, "Log1.txt.gz"), "rt", encoding="utf-8") as file:
            self.assertEqual(file.read(), "需要归档的内容")
        self.assertIn("Log2.txt", files)

    def test_maintain_purges_expired_logs_before_compressing(self):
        self._set("LOG_RETENTION_DAYS", 2.0)
        self._set("LOG_COMPRESS_AFTER_DAYS", 1.0)
        expired = os.path.join(self.tmp, "Log2.txt")
        with open(expired, "w", encoding="utf-8") as file:
            file.write("超过保留期但同样超过压缩期")
        now = time.time()
        os.utime(expired, (now - 5 * 86400, now - 5 * 86400))

        self.logger._maintain_logs()

        files = self._files()
        self.assertNotIn("Log2.txt", files)
        # 先删后压: 不应留下刚刚生成的 .gz 归档
        self.assertNotIn("Log2.txt.gz", files)

    def test_configure_from_config_applies_user_retention_immediately(self):
        within_user_window = os.path.join(self.tmp, "Log2.txt")
        with open(within_user_window, "w", encoding="utf-8") as file:
            file.write("20 天前")
        beyond_user_window = os.path.join(self.tmp, "Log3.txt")
        with open(beyond_user_window, "w", encoding="utf-8") as file:
            file.write("40 天前")
        now = time.time()
        os.utime(within_user_window, (now - 20 * 86400, now - 20 * 86400))
        os.utime(beyond_user_window, (now - 40 * 86400, now - 40 * 86400))

        saved = {name: getattr(Logger, name) for name in self.ATTRS}

        def restore():
            for name, value in saved.items():
                setattr(Logger, name, value)

        self.addCleanup(restore)
        config = SimpleNamespace(
            log_max_size_mb=10,
            log_retention_days=30,
            log_max_files=200,
            log_compress_after_days=3,
        )

        self.logger.configure_from_config(config)

        files = self._files()
        # 20 天超过默认 14 天但未超过用户配置的 30 天: 必须保留(压缩归档)
        self.assertIn("Log2.txt.gz", files)
        # 40 天超过用户配置的 30 天: 立即清理
        self.assertNotIn("Log3.txt", files)
        self.assertNotIn("Log3.txt.gz", files)

    def test_redaction_covers_keys_cookies_proxy_and_url_query(self):
        self.logger.write_log("Authorization: Bearer sk-abcdefgh12345678\n")
        self.logger.write_log("Cookie: CASTGC=abc; CASLOGC=def\n")
        self.logger.write_log("https://api.day.app/secret-key/通知\n")
        self.logger.write_log("代理 http://user:pass@127.0.0.1:7890\n")
        self.logger.event(
            "配置",
            api_key="sk-1234567890abcdef",
            url="https://example.com/x?token=secret-token&key=secret-key",
        )
        with open(self.logger.filename, encoding="utf-8") as file:
            text = file.read()
        for secret in (
            "sk-abcdefgh12345678",
            "CASTGC=abc",
            "CASLOGC=def",
            "secret-key",
            "user:pass@",
            "secret-token",
            "sk-1234567890abcdef",
        ):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        # Verify persistent handle uses block buffering (not line buffering=1)
        self.logger.info("buffered-check")
        self.assertIsNotNone(self.logger._file)
        self.assertFalse(self.logger._file.line_buffering)

    def test_redact_sensitive_helper_masks_provider_keys(self):
        text = redact_sensitive(
            "AIzaSyD-1234567890abcdefg gsk_123456789012 xai-123456789012"
        )
        self.assertNotIn("AIzaSyD-1234567890abcdefg", text)
        self.assertNotIn("gsk_123456789012", text)
        self.assertNotIn("xai-123456789012", text)

    def test_concurrent_writes_do_not_lose_lines(self):
        self._set("MAX_LOG_SIZE_MB", 0.0)
        total = 0
        lines_per_thread = 50
        workers = 8

        def writer(index):
            for line in range(lines_per_thread):
                self.logger.write_log(f"线程{index} 行{line}\n")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.logger.flush()
        with open(self.logger.filename, encoding="utf-8") as file:
            content = file.read().splitlines()
        total = workers * lines_per_thread
        self.assertEqual(len(content), total)
        self.assertEqual(len(set(content)), total)

    def test_all_channels_redact_sensitive_and_empty_exception_safe(self):
        from modules.redaction import redact_argv, redact_notification

        # 1. summarize_exception handles empty exception message without IndexError
        self.assertEqual(Logger.summarize_exception(ValueError()), "ValueError")
        self.assertEqual(Logger.summarize_exception(RuntimeError("")), "RuntimeError")

        # 2. info / warn / error / debug / event / context redact BEFORE reaching event_sink (SSE)
        emitted = []
        prev_sink = self.logger._event_sink
        prev_emit_debug = self.logger.emit_debug
        try:
            self.logger.emit_debug = True
            self.logger.set_event_sink(lambda kind, payload: emitted.append((kind, payload)))
            self.logger.info("proxy http://alice:supersecret@127.0.0.1:7890")
            self.logger.warn("key=sk-1234567890abcdef")
            self.logger.error("phone 13812345678", with_context=False)
            self.logger.debug("Authorization: Bearer topsecretvalue")
            self.logger.event("ev", url="https://example.com?token=mysecret")
            self.logger.context(proxy="http://bob:pw1234@127.0.0.1:8080")
        finally:
            self.logger.set_event_sink(prev_sink)
            self.logger.emit_debug = prev_emit_debug
            self.logger.clear_context()

        dumped = repr(emitted)
        for leak in (
            "alice:supersecret",
            "sk-1234567890abcdef",
            "13812345678",
            "topsecretvalue",
            "mysecret",
            "bob:pw1234",
        ):
            self.assertNotIn(leak, dumped)

        # 3. configure() mutates instance attributes, not Logger class constants
        orig_class_max = Logger.MAX_LOG_FILES
        self.logger.configure(max_files=77)
        self.assertEqual(self.logger.MAX_LOG_FILES, 77)
        self.assertEqual(Logger.MAX_LOG_FILES, orig_class_max)

        # 4. Persistent handle reused across writes without reopening
        self.logger.write_log("persist-1\n", level="INFO")
        fh1 = self.logger._file
        self.logger.write_log("persist-2\n", level="INFO")
        fh2 = self.logger._file
        self.assertIsNotNone(fh1)
        self.assertIs(fh1, fh2)

        # 5. redact_argv masks proxy password
        argv_str = redact_argv(["--proxy", "http://u:secretpass@127.0.0.1:7890", "--debug"])
        self.assertNotIn("secretpass", argv_str)
        self.assertIn("--debug", argv_str)

        # 6. Shared notifier redaction does NOT swallow subsequent lines after cookie/container
        multi = "cookie: CASTGC=abc123\n第二行正常进度通知\ntoken: {\"a\": 1}\n第三行依然保留"
        redacted_multi = redact_notification(multi)
        self.assertNotIn("CASTGC=abc123", redacted_multi)
        self.assertIn("第二行正常进度通知", redacted_multi)
        self.assertIn("第三行依然保留", redacted_multi)


if __name__ == "__main__":
    unittest.main()
