import atexit
import gzip
import os
import re
import shutil
import sys
import threading
import time
import traceback

from modules.redaction import redact_sensitive, redact_value

# 用于在记录日志来源时跳过日志系统自身的栈帧
_LOGGER_FILE = os.path.basename(__file__)
_LOG_FILE_PATTERN = re.compile(r"^Log(\d+)\.txt(?:\.gz)?$", re.IGNORECASE)
_MB = 1024 * 1024
_DAY = 86400
_FLUSH_LEVELS = frozenset({"WARN", "ERROR", "EVENT"})


# 单例模式日志器
class Logger:
    MAX_LOG_FILES = 200
    MAX_LOG_SIZE_MB = 10.0
    LOG_RETENTION_DAYS = 14.0
    LOG_COMPRESS_AFTER_DAYS = 3.0
    _instance = None
    _lock = threading.Lock()  # 线程安全锁

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(Logger, cls).__new__(cls)
                cls._instance._init()
                atexit.register(cls._instance.close)
        return cls._instance

    def _init(self):
        self._write_lock = getattr(self, "_write_lock", None) or threading.RLock()
        with self._write_lock:
            self._close_file_locked()
            self.runtime_root = self.get_runtime_root()
            self.log_dir = os.path.join(self.runtime_root, "logs")
            os.makedirs(self.log_dir, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.log_dir, 0o700)
            self._throttled = {}
            self._context = {}
            self._event_sink = None
            self.console_debug = False
            self.emit_debug = False
            self._filename = ""
            self._file = None
            self._file_path = ""
            self._file_bytes = 0
            # 重置可能残留的实例配置属性，使初始化阶段遵循类默认常量
            for attr in (
                "MAX_LOG_SIZE_MB",
                "LOG_RETENTION_DAYS",
                "MAX_LOG_FILES",
                "LOG_COMPRESS_AFTER_DAYS",
            ):
                self.__dict__.pop(attr, None)
            self._open_new_log_file(prune=False)

    @property
    def filename(self) -> str:
        return self._filename

    @filename.setter
    def filename(self, path: str) -> None:
        lock = getattr(self, "_write_lock", None)
        if lock is None:
            self._filename = path or ""
            return
        with lock:
            new_path = path or ""
            if getattr(self, "_file_path", "") != new_path:
                self._close_file_locked()
            self._filename = new_path

    def _close_file_locked(self) -> None:
        fh = getattr(self, "_file", None)
        if fh is not None:
            try:
                fh.flush()
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass
        self._file = None
        self._file_path = ""
        self._file_bytes = 0

    def _ensure_file_locked(self):
        target = self._filename
        if not target:
            return None
        fh = getattr(self, "_file", None)
        if fh is not None and not fh.closed and self._file_path == target:
            return fh
        self._close_file_locked()
        fh = open(target, "a", encoding="utf-8")
        self._file = fh
        self._file_path = target
        try:
            self._file_bytes = fh.tell()
        except OSError:
            self._file_bytes = 0
        return fh

    def flush(self) -> None:
        with self._write_lock:
            fh = getattr(self, "_file", None)
            if fh is not None and not fh.closed:
                try:
                    fh.flush()
                except OSError:
                    pass

    def close(self) -> None:
        lock = getattr(self, "_write_lock", None)
        if lock is None:
            return
        with lock:
            self._close_file_locked()

    def configure(
        self,
        *,
        max_size_mb=None,
        retention_days=None,
        max_files=None,
        compress_after_days=None,
    ):
        """应用 config.ini 的日志策略到实例属性（受锁保护，不篡改类常量）。"""
        with self._write_lock:
            if max_size_mb is not None:
                self.MAX_LOG_SIZE_MB = max(0.0, float(max_size_mb))
            if retention_days is not None:
                self.LOG_RETENTION_DAYS = max(0.0, float(retention_days))
            if max_files is not None:
                self.MAX_LOG_FILES = max(1, int(max_files))
            if compress_after_days is not None:
                self.LOG_COMPRESS_AFTER_DAYS = max(0.0, float(compress_after_days))

    def configure_from_config(self, config) -> None:
        """从 Config 实例读取日志配置后立即按用户策略维护一次旧日志."""
        with self._write_lock:
            self.configure(
                max_size_mb=getattr(config, "log_max_size_mb", None),
                retention_days=getattr(config, "log_retention_days", None),
                max_files=getattr(config, "log_max_files", None),
                compress_after_days=getattr(config, "log_compress_after_days", None),
            )
            self._maintain_logs()
            self._prune_by_count()

    def _log_entries(self) -> list[tuple[int, str]]:
        try:
            names = os.listdir(self.log_dir)
        except OSError:
            return []
        entries = []
        for name in names:
            match = _LOG_FILE_PATTERN.match(name)
            if not match:
                continue
            path = os.path.join(self.log_dir, name)
            if os.path.isfile(path):
                entries.append((int(match.group(1)), path))
        return entries

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _compress_log(self, path: str) -> bool:
        gz_path = f"{path}.gz"
        if os.path.exists(gz_path):
            return False
        try:
            with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(path)
            return True
        except OSError as exc:
            self._remove_quietly(gz_path)
            self.write_log(f"日志压缩失败(文件可能被占用): {path}: {exc}\n", level="WARN")
            return False

    def _maintain_logs(self, now: float | None = None) -> None:
        """启动或加载配置时维护: 先清理超期日志, 再压缩旧日志."""
        with self._write_lock:
            now = time.time() if now is None else now
            compress_days = max(0.0, float(getattr(self, "LOG_COMPRESS_AFTER_DAYS", 0.0)))
            retention_days = max(0.0, float(getattr(self, "LOG_RETENTION_DAYS", 0.0)))

            if retention_days > 0:
                for _, path in self._log_entries():
                    if path == self.filename:
                        continue
                    if (now - self._mtime(path)) / _DAY >= retention_days:
                        self._remove_quietly(path)

            if compress_days > 0:
                for _, path in self._log_entries():
                    if path.lower().endswith(".gz") or path == self.filename:
                        continue
                    if (now - self._mtime(path)) / _DAY >= compress_days:
                        self._compress_log(path)

    def _prune_by_count(self) -> None:
        """按 MAX_LOG_FILES 删除最旧日志, 不影响当前正在写入的文件."""
        with self._write_lock:
            max_files = max(1, int(getattr(self, "MAX_LOG_FILES", 200)))
            entries = sorted(
                self._log_entries(), key=lambda item: (self._mtime(item[1]), item[0])
            )
            removable = [path for _, path in entries if path != self.filename]
            excess = len(entries) - max_files
            for path in removable[: max(0, excess)]:
                self._remove_quietly(path)

    def _open_new_log_file(self, *, prune: bool = True):
        with self._write_lock:
            self._close_file_locked()
            os.makedirs(self.log_dir, exist_ok=True)
            existing_logs = []
            max_index = 0
            for idx, full_path in self._log_entries():
                if idx > max_index:
                    max_index = idx
                existing_logs.append((idx, full_path))

            new_index = max_index + 1
            while os.path.exists(os.path.join(self.log_dir, f"Log{new_index}.txt")) or os.path.exists(
                os.path.join(self.log_dir, f"Log{new_index}.txt.gz")
            ):
                new_index += 1
            new_path = os.path.join(self.log_dir, f"Log{new_index}.txt")
            self._filename = new_path
            with open(new_path, "w", encoding="utf-8"):
                pass
            self._file = None
            self._file_path = ""
            self._file_bytes = 0
            if os.name != "nt":
                os.chmod(new_path, 0o600)

            if prune:
                max_files = max(1, int(getattr(self, "MAX_LOG_FILES", 200)))
                if len(existing_logs) >= max_files:
                    def _sort_key(item):
                        idx, path = item
                        return (self._mtime(path), idx)

                    existing_logs.sort(key=_sort_key)
                    excess = len(existing_logs) - (max_files - 1)
                    for _, old_path in existing_logs[:excess]:
                        if old_path == self._filename:
                            continue
                        self._remove_quietly(old_path)
            return self._filename

    def start_new_run(self):
        """为长驻后端中的新一轮任务切换独立日志文件并重置上下文。"""
        with self._write_lock:
            self._close_file_locked()
            self.runtime_root = self.get_runtime_root()
            self.log_dir = os.path.join(self.runtime_root, "logs")
            self._throttled.clear()
            self._context.clear()
            self._maintain_logs()
            return self._open_new_log_file()

    def set_event_sink(self, sink):
        """注册可选的结构化事件回调（供内置桌面后端使用，不影响 CLI）。"""
        self._event_sink = sink

    def _emit(self, kind, **payload):
        sink = self._event_sink
        if sink is None:
            return
        try:
            sink(kind, payload)
        except Exception:
            pass

    @staticmethod
    def get_runtime_root():
        override = os.environ.get("AUTOVISOR_DATA_DIR", "").strip()
        if override:
            return os.path.abspath(override)
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @staticmethod
    def summarize_exception(exc):
        lines = str(exc).splitlines()
        first_line = lines[0].strip() if lines else ""
        return f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__

    @staticmethod
    def _timestamp():
        now = time.time()
        millis = int(now % 1 * 1000)
        return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{millis:03d}"

    @staticmethod
    def _source_location():
        """返回调用日志的位置(模块.函数:行号), 跳过日志系统自身的栈帧。"""
        try:
            frame = sys._getframe(1)
        except (AttributeError, ValueError):
            return "?"
        while frame is not None and os.path.basename(frame.f_code.co_filename) == _LOGGER_FILE:
            frame = frame.f_back
        if frame is None:
            return "?"
        module = frame.f_globals.get("__name__", "?")
        return f"{module}.{frame.f_code.co_name}:{frame.f_lineno}"

    def write_log(self, msg, level=None, raw=False, *, redacted=False):
        """写入一条日志: [时间] [级别] [调用位置] 内容; 复用持久句柄与增量字节计数。"""
        if raw:
            record = msg
        elif level:
            record = (
                f"[{self._timestamp()}] [{level}] "
                f"[{self._source_location()}] {msg}"
            )
        else:
            record = f"[{self._timestamp()}] [{self._source_location()}] {msg}"
        if not redacted:
            record = redact_sensitive(record)
        record_bytes = len(record.encode("utf-8"))
        with self._write_lock:
            if not self._filename:
                return
            try:
                fh = self._ensure_file_locked()
                if fh is None:
                    return
                fh.write(record)
                if level in _FLUSH_LEVELS:
                    fh.flush()
                self._file_bytes += record_bytes
                size = self._file_bytes
            except OSError as exc:
                self._close_file_locked()
                print(f"[WARN] 日志写入失败: {exc}", file=sys.stderr)
                return
            max_bytes = float(getattr(self, "MAX_LOG_SIZE_MB", 0.0)) * _MB
            if max_bytes > 0 and size >= max_bytes:
                try:
                    self._open_new_log_file()
                except OSError as exc:
                    print(f"[WARN] 日志轮转失败: {exc}", file=sys.stderr)

    def log_exception(self, msg, exc=None, shift=False):
        """记录异常: 异常类型、详情、当前运行状态和完整堆栈。"""
        detail_lines = []
        if exc is not None:
            detail_lines = [
                f"异常类型: {type(exc).__name__}",
                f"异常详情: {exc}",
            ]
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        else:
            tb = traceback.format_exc()
        self.error(msg, shift=shift, with_context=False)
        if detail_lines:
            self.write_log("\n".join(detail_lines) + "\n", level="ERROR")
        state = self.context_text()
        if state:
            self.write_log(f"运行状态: {state}\n", level="ERROR")
        self.write_log(f"{tb}\n", level="ERROR")

    def save(self, inform=True):
        self.close()
        self._emit("log_file", path=self.filename)
        if inform:
            print(f"\n日志文件已保存至: {self.filename}")

    def debug(self, msg):
        safe = redact_sensitive(msg)
        self.write_log(f"{safe}\n", level="DEBUG", redacted=True)
        if getattr(self, "console_debug", False):
            print(f"[DEBUG] {safe}", flush=True)
        if getattr(self, "emit_debug", False):
            self._emit("log", level="debug", message=safe)

    def debug_throttled(self, key, msg, interval=60):
        """轮询类日志限时折叠: 同一 key 每 interval 秒最多写一条, 期间次数汇总到下一行。"""
        folded = self._throttle(key, interval)
        if folded is None:
            return
        if folded:
            msg = f"{msg} (期间折叠 {folded} 次)"
        self.debug(msg)

    def section(self, title):
        """写入阶段标题, 把日志按运行阶段分段, 便于定位问题发生的位置。"""
        safe_title = redact_sensitive(title)
        self.write_log("\n", raw=True, redacted=True)
        self.write_log(f"===== {safe_title} =====\n", redacted=True)
        print(f"\n===== {safe_title} =====")
        self._emit("section", title=safe_title)

    def event(self, name, interval=None, **fields):
        """记录关键事件(只写入文件), 字段以 key=value 形式附加; 传 interval 时按间隔折叠。"""
        if interval is not None:
            folded = self._throttle(f"event:{name}", interval)
            if folded is None:
                return
            if folded:
                fields["期间折叠"] = folded
        safe_name = redact_sensitive(name)
        safe_fields = {key: redact_value(val) for key, val in fields.items()}
        details = " ".join(
            f"{key}={value}"
            for key, value in safe_fields.items()
            if value is not None and value != ""
        )
        line = safe_name
        if details:
            line = f"{safe_name} | {details}"
        self.write_log(f"{line}\n", level="EVENT")
        self._emit("event", name=safe_name, fields=safe_fields)

    def context(self, **fields):
        """更新运行状态(课程、课时、阶段等), 异常日志会自动附带。"""
        updates = {
            key: redact_value(value)
            for key, value in fields.items()
            if value is not None
        }
        with self._write_lock:
            self._context.update(updates)
        if updates:
            self._emit("context", fields=updates)

    def clear_context(self, *keys):
        with self._write_lock:
            if not keys:
                self._context.clear()
                return
            for key in keys:
                self._context.pop(key, None)

    def context_text(self):
        with self._write_lock:
            return " ".join(f"{key}={value}" for key, value in self._context.items())

    def _throttle(self, key, interval):
        """允许写入时返回需要补记的折叠次数, 仍在间隔内返回 None。"""
        with self._write_lock:
            now = time.monotonic()
            last, folded = self._throttled.get(key, (None, 0))
            if last is not None and now - last < interval:
                self._throttled[key] = (last, folded + 1)
                return None
            self._throttled[key] = (now, 0)
            return folded

    def info(self, msg, shift=False):
        safe = redact_sensitive(msg)
        if shift:
            text = f"\r\n\033[32m[INFO]\033[0m {safe}"
        else:
            text = f"\r\033[32m[INFO]\033[0m {safe}"
        print(text.ljust(50))
        self.write_log(f"{safe}\n", level="INFO", redacted=True)
        self._emit("log", level="info", message=safe)

    def warn(self, msg, shift=False):
        safe = redact_sensitive(msg)
        if shift:
            text = f"\r\n\033[33m[WARN]\033[0m {safe}"
        else:
            text = f"\r\033[33m[WARN]\033[0m {safe}"
        print(text.ljust(50))
        self.write_log(f"{safe}\n", level="WARN", redacted=True)
        self._emit("log", level="warning", message=safe)

    def error(self, msg, shift=False, with_context=True):
        safe = redact_sensitive(msg)
        if shift:
            text = f"\r\n\033[31m[ERROR]\033[0m {safe}"
        else:
            text = f"\r\033[31m[ERROR]\033[0m {safe}"
        print(text.ljust(50))
        self.write_log(f"{safe}\n", level="ERROR", redacted=True)
        if with_context:
            state = self.context_text()
            if state:
                self.write_log(f"运行状态: {state}\n", level="ERROR")
        self._emit("log", level="error", message=safe)
