# encoding=utf-8
"""宿主 stdin 管道看门狗与内置依赖预热（防止僵尸进程与 Windows Loader Lock 死锁）。"""
from __future__ import annotations

import os
import sys

from modules.backend.http_routes import BackendHTTPServer


def _watch_parent_stdin_windows(server: BackendHTTPServer) -> bool:
    """Windows 下通过 Win32 PeekNamedPipe/ReadFile 监听父进程 stdin 管道 EOF。

    不能使用 sys.stdin.buffer.read(1) 或 os.read(0, 1)：阻塞态 UCRT _read(0)
    会长期持有 C 运行时 stdin 文件锁，导致工作线程首次加载 numpy/OpenBLAS DLL
    时与 Windows Loader Lock / GIL 发生永久死锁。
    """
    import ctypes
    from ctypes import wintypes

    STD_INPUT_HANDLE = -10
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_BROKEN_PIPE = 109
    ERROR_INVALID_HANDLE = 6

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    handle = kernel32.GetStdHandle(wintypes.DWORD(STD_INPUT_HANDLE))
    if not handle or handle == INVALID_HANDLE_VALUE:
        return False

    buf = ctypes.create_string_buffer(64)
    bytes_read = wintypes.DWORD(0)
    total_avail = wintypes.DWORD(0)
    bytes_left = wintypes.DWORD(0)

    while not server.shutdown_event.is_set():
        ok = kernel32.PeekNamedPipe(
            handle,
            None,
            0,
            None,
            ctypes.byref(total_avail),
            ctypes.byref(bytes_left),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in (ERROR_BROKEN_PIPE, ERROR_INVALID_HANDLE):
                return True
            # 非管道句柄（如 NUL 或普通文件重定向）不触发自动退出
            return False
        if total_avail.value > 0:
            to_read = min(total_avail.value, len(buf))
            read_ok = kernel32.ReadFile(
                handle, buf, to_read, ctypes.byref(bytes_read), None
            )
            if not read_ok or bytes_read.value == 0:
                return True
        else:
            server.shutdown_event.wait(0.25)
    return False


def _watch_parent_stdin(server: BackendHTTPServer):
    """宿主关闭 stdin 管道（正常退出或崩溃）时自动优雅退出，防止僵尸进程。"""
    should_shutdown = True
    try:
        if os.name == "nt":
            should_shutdown = _watch_parent_stdin_windows(server)
        else:
            while not server.shutdown_event.is_set():
                data = sys.stdin.buffer.read(1)
                if not data:
                    break
    except Exception:
        pass
    finally:
        if should_shutdown and not server.shutdown_event.is_set():
            server.graceful_shutdown(stop_http=True)


def _preload_builtin_deps():
    """在主线程（启动子线程与管道监听前）预热内置 numpy / cv2，消除首次任务延迟与 DLL 锁竞争。"""
    for mod_name in ("numpy", "cv2"):
        try:
            __import__(mod_name)
        except Exception:
            pass
