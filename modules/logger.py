import os
import re
import sys
import threading
import time
import traceback

# 用于在记录日志来源时跳过日志系统自身的栈帧
_LOGGER_FILE = os.path.basename(__file__)
_LOG_FILE_PATTERN = re.compile(r"^Log(\d+)\.txt$", re.IGNORECASE)


# 单例模式日志器
class Logger:
    MAX_LOG_FILES = 50
    _instance = None
    _lock = threading.Lock()  # 线程安全锁

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(Logger, cls).__new__(cls)
                cls._instance._init()
        return cls._instance

    def _init(self):
        self.runtime_root = self.get_runtime_root()
        self.log_dir = os.path.join(self.runtime_root, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.log_dir, 0o700)
        self._write_lock = threading.Lock()
        self._throttled = {}
        self._context = {}
        self._event_sink = None
        self.console_debug = False
        self.emit_debug = False
        self.filename = ""
        self._open_new_log_file()

    def _open_new_log_file(self):
        os.makedirs(self.log_dir, exist_ok=True)
        existing_logs = []
        max_index = 0
        try:
            entries = os.listdir(self.log_dir)
        except OSError:
            entries = []
        for name in entries:
            match = _LOG_FILE_PATTERN.match(name)
            if match:
                idx = int(match.group(1))
                if idx > max_index:
                    max_index = idx
                full_path = os.path.join(self.log_dir, name)
                if os.path.isfile(full_path):
                    existing_logs.append((idx, full_path))

        new_index = max_index + 1
        while os.path.exists(os.path.join(self.log_dir, f"Log{new_index}.txt")):
            new_index += 1
        self.filename = os.path.join(self.log_dir, f"Log{new_index}.txt")
        with open(self.filename, "w", encoding="utf-8"):
            pass
        if os.name != "nt":
            os.chmod(self.filename, 0o600)

        max_files = max(1, int(getattr(self, "MAX_LOG_FILES", 50)))
        if len(existing_logs) >= max_files:
            def _sort_key(item):
                idx, path = item
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = 0.0
                return (mtime, idx)

            existing_logs.sort(key=_sort_key)
            excess = len(existing_logs) - (max_files - 1)
            for _, old_path in existing_logs[:excess]:
                if old_path == self.filename:
                    continue
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        return self.filename

    def start_new_run(self):
        """为长驻后端中的新一轮任务切换独立日志文件并重置上下文。"""
        with self._write_lock:
            self.runtime_root = self.get_runtime_root()
            self.log_dir = os.path.join(self.runtime_root, "logs")
            self._throttled.clear()
            self._context.clear()
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
        first_line = str(exc).splitlines()[0].strip()
        return f"{type(exc).__name__}: {first_line}"

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

    def write_log(self, msg, level=None, raw=False):
        """写入一条日志: [时间] [级别] [调用位置] 内容; 多行明细不带级别。"""
        if raw:
            record = msg
        elif level:
            record = (
                f"[{self._timestamp()}] [{level}] "
                f"[{self._source_location()}] {msg}"
            )
        else:
            record = f"[{self._timestamp()}] [{self._source_location()}] {msg}"
        with self._write_lock:
            with open(self.filename, "a", encoding="utf-8") as f:
                f.write(record)
                f.flush()

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
            self.write_log("\n".join(detail_lines) + "\n")
        state = self.context_text()
        if state:
            self.write_log(f"运行状态: {state}\n")
        self.write_log(f"{tb}\n")

    def save(self, inform=True):
        self._emit("log_file", path=self.filename)
        if inform:
            print(f"\n日志文件已保存至: {self.filename}")

    def debug(self, msg):
        self.write_log(f"{msg}\n", level="DEBUG")
        if getattr(self, "console_debug", False):
            print(f"[DEBUG] {msg}", flush=True)
        if getattr(self, "emit_debug", False):
            self._emit("log", level="debug", message=str(msg))

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
        self.write_log("\n", raw=True)
        self.write_log(f"===== {title} =====\n")
        print(f"\n===== {title} =====")
        self._emit("section", title=str(title))

    def event(self, name, interval=None, **fields):
        """记录关键事件(只写入文件), 字段以 key=value 形式附加; 传 interval 时按间隔折叠。"""
        if interval is not None:
            folded = self._throttle(f"event:{name}", interval)
            if folded is None:
                return
            if folded:
                fields["期间折叠"] = folded
        details = " ".join(
            f"{key}={value}"
            for key, value in fields.items()
            if value is not None and value != ""
        )
        line = name
        if details:
            line = f"{name} | {details}"
        self.write_log(f"{line}\n", level="EVENT")
        self._emit("event", name=str(name), fields=dict(fields))

    def context(self, **fields):
        """更新运行状态(课程、课时、阶段等), 异常日志会自动附带。"""
        updates = {key: value for key, value in fields.items() if value is not None}
        self._context.update(updates)
        if updates:
            self._emit("context", fields=updates)

    def clear_context(self, *keys):
        if not keys:
            self._context.clear()
            return
        for key in keys:
            self._context.pop(key, None)

    def context_text(self):
        return " ".join(f"{key}={value}" for key, value in self._context.items())

    def _throttle(self, key, interval):
        """允许写入时返回需要补记的折叠次数, 仍在间隔内返回 None。"""
        now = time.monotonic()
        last, folded = self._throttled.get(key, (None, 0))
        if last is not None and now - last < interval:
            self._throttled[key] = (last, folded + 1)
            return None
        self._throttled[key] = (now, 0)
        return folded

    def info(self, msg, shift=False):
        if shift:
            text = f"\r\n\033[32m[INFO]\033[0m {msg}"
        else:
            text = f"\r\033[32m[INFO]\033[0m {msg}"
        print(text.ljust(50))
        self.write_log(f"{msg}\n", level="INFO")
        self._emit("log", level="info", message=str(msg))

    def warn(self, msg, shift=False):
        if shift:
            text = f"\r\n\033[33m[WARN]\033[0m {msg}"
        else:
            text = f"\r\033[33m[WARN]\033[0m {msg}"
        print(text.ljust(50))
        self.write_log(f"{msg}\n", level="WARN")
        self._emit("log", level="warning", message=str(msg))

    def error(self, msg, shift=False, with_context=True):
        if shift:
            text = f"\r\n\033[31m[ERROR]\033[0m {msg}"
        else:
            text = f"\r\033[31m[ERROR]\033[0m {msg}"
        print(text.ljust(50))
        self.write_log(f"{msg}\n", level="ERROR")
        if with_context:
            state = self.context_text()
            if state:
                self.write_log(f"运行状态: {state}\n")
        self._emit("log", level="error", message=str(msg))
