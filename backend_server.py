# encoding=utf-8
"""Autovisor 内置无感本地后端服务入口（供 Tauri 桌面端托管通信）。

设计特点：
- 仅使用 Python 标准库（http.server + asyncio），零额外依赖；
- 绑定 127.0.0.1:0 自动分配空闲端口，通过 stdout 输出单行 JSON 握手；
- 所有 /api/* 接口校验 X-Autovisor-Token 令牌；
- 通过 Logger / progress 结构化事件钩子维护 RunState，并通过 SSE 实时推送；
- 监听 stdin EOF，宿主进程退出时自动取消任务、关闭浏览器并优雅退出。
"""
import argparse
import json
import os
import secrets
import sys
import threading
import traceback

from modules.backend.http_routes import (  # noqa: F401
    BackendHTTPServer,
    BackendRequestHandler,
    _ALLOWED_ORIGIN_HOSTS,
    _LOOPBACK_HOSTS,
    _is_loopback_cdp_url,
    _is_valid_host_header,
    _is_valid_origin_header,
    _validate_user_path,
)
from modules.backend.state_reducer import (  # noqa: F401
    format_hms_local as _format_hms_local,
    parse_ratio as _parse_ratio,
)
from modules.backend.task_manager import (  # noqa: F401
    LOG_LIMIT,
    SSE_COALESCE_SECS,
    TaskManager,
    _load_captcha_modules,
    _now_secs,
    _pick_captcha_page,
    _solve_captcha_via_cdp,
)
from modules.backend.watchdog import (  # noqa: F401
    _preload_builtin_deps,
    _watch_parent_stdin,
    _watch_parent_stdin_windows,
)
from modules.shutdown import SHUTDOWN_OUTCOMES, ShutdownManager  # noqa: F401


def _parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="Autovisor Embedded Backend Service")
    parser.add_argument("--data-dir", default="", help="可写用户数据目录（config.ini / data / logs）")
    parser.add_argument("--resources-dir", default="", help="只读内置资源目录（stealth.min.js）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，固定仅允许回环地址")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示系统自动分配")
    parser.add_argument("--auth-stdin", action="store_true", help="从 stdin 首行 JSON 握手读取认证令牌")
    parser.add_argument("--allow-insecure-dev", action="store_true", help="仅限本地手动调试：允许无令牌免鉴权访问")
    parser.add_argument("--verify-deps", action="store_true", help="校验内置运行时依赖是否可直接导入并退出")
    args = parser.parse_args(argv)
    if not args.verify_deps and not args.data_dir:
        parser.error("the following arguments are required: --data-dir")
    return args


def main(argv=None) -> int:
    # 保留原始 stdout 专门用于输出干净的单行握手 JSON
    handshake_stdout = sys.stdout
    args = _parse_cli_args(argv)

    if args.verify_deps:
        sys.stdout = sys.stderr
        import Autovisor  # noqa: F401
        from modules import installer

        core_mods = ["requests", "PIL", "playwright.async_api"]
        for mod_name in core_mods:
            __import__(mod_name)

        installer.validate_python_version()
        loaded = installer.load_preinstalled_modules()
        if not loaded or len(loaded) < 2:
            for dep in ("numpy", "cv2"):
                try:
                    __import__(dep)
                except Exception:
                    sys.stderr.write(f"导入 {dep} 失败:\n{traceback.format_exc()}\n")
            sys.stderr.write("内置验证码依赖校验失败：未能直接导入 numpy / cv2\n")
            return 1
        np_mod, cv2_mod = loaded[0], loaded[1]
        arr = np_mod.zeros((8, 8, 3), dtype=np_mod.uint8)
        gray = cv2_mod.cvtColor(arr, cv2_mod.COLOR_BGR2GRAY)
        if gray.shape != (8, 8):
            sys.stderr.write(f"内置 OpenCV 基础运算校验失败：{gray.shape}\n")
            return 1
        handshake_stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "numpy": getattr(np_mod, "__version__", ""),
                    "cv2": getattr(cv2_mod, "__version__", ""),
                    "core": core_mods,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handshake_stdout.flush()
        return 0

    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    os.environ["AUTOVISOR_DATA_DIR"] = data_dir
    if args.resources_dir:
        os.environ["AUTOVISOR_RESOURCES_DIR"] = os.path.abspath(args.resources_dir)

    if args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("安全限制：内置后端仅允许绑定 127.0.0.1")

    token = ""
    if args.auth_stdin:
        if sys.stdin is None or sys.stdin.isatty():
            raise SystemExit("安全限制：--auth-stdin 需要通过管道传入首行 JSON 握手令牌")
        raw_auth = sys.stdin.buffer.readline(4096)
        if not raw_auth:
            raise SystemExit("安全限制：未从 stdin 收到认证令牌握手")
        try:
            auth_payload = json.loads(raw_auth.decode("utf-8"))
        except Exception as exc:
            raise SystemExit(f"安全限制：stdin 认证握手 JSON 无效: {exc}")
        if not isinstance(auth_payload, dict) or not str(auth_payload.get("authToken") or "").strip():
            raise SystemExit("安全限制：stdin 认证握手缺少非空 authToken")
        token = str(auth_payload["authToken"]).strip()
    else:
        token = os.environ.get("AUTOVISOR_BACKEND_TOKEN", "").strip()
        if not token and not args.allow_insecure_dev:
            token = secrets.token_urlsafe(32)

    # 将模块运行时的 print 输出重定向到 stderr，确保 stdout 只输出握手消息
    sys.stdout = sys.stderr

    # 在主线程预先加载内置 numpy / cv2，确保任务启动时 0 延迟且绝不与子线程发生 DLL 死锁
    _preload_builtin_deps()

    from modules.version import __version__

    manager = TaskManager(data_dir)
    server = BackendHTTPServer(
        (args.host, args.port),
        BackendRequestHandler,
        manager=manager,
        token=token,
        app_version=__version__,
        allow_insecure_dev=bool(args.allow_insecure_dev),
    )
    actual_port = server.server_address[1]

    # 启动宿主 stdin 监听线程（仅在非 TTY 管道托管模式下启用）
    if sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(
            target=_watch_parent_stdin, args=(server,), name="stdin-watchdog", daemon=True
        ).start()

    handshake = {
        "event": "autovisor-backend-ready",
        "version": 1,
        "port": actual_port,
        "pid": os.getpid(),
        "appVersion": __version__,
    }
    handshake_stdout.write(json.dumps(handshake, ensure_ascii=False) + "\n")
    handshake_stdout.flush()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.graceful_shutdown(stop_http=False)
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
