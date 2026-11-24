# encoding=utf-8
"""内置本地 HTTP / SSE 服务端、请求安全校验与路由处理器。"""
from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import PurePath
import queue
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse

from modules.backend.task_manager import TaskManager

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "tauri.localhost"})


def _is_valid_host_header(host: str | None) -> bool:
    raw = str(host or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(f"http://{raw}")
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _is_valid_origin_header(origin: str | None) -> bool:
    raw = str(origin or "").strip()
    if not raw:
        return True
    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    hostname = (parsed.hostname or "").lower()
    if scheme == "tauri":
        return hostname == "localhost"
    if scheme in ("http", "https"):
        return hostname in _ALLOWED_ORIGIN_HOSTS
    return False


def _is_loopback_cdp_url(cdp_url: str) -> bool:
    raw = str(cdp_url or "").strip()
    if not raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme not in ("ws", "wss", "http", "https"):
        return False
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _validate_user_path(raw: str, *, expect_dir: bool) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("路径不能为空")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("路径包含非法控制字符")
    if text.startswith("\\\\") or text.startswith("//"):
        raise ValueError("禁止使用网络 UNC 路径")
    if ".." in PurePath(text).parts:
        raise ValueError("路径禁止包含目录遍历片段 (..)")
    resolved = os.path.realpath(os.path.abspath(text))
    if expect_dir:
        if not os.path.isdir(resolved):
            raise ValueError(f"目录不存在或无效: {text}")
    else:
        if not os.path.isfile(resolved):
            raise ValueError(f"文件不存在或不是普通文件: {text}")
    return resolved


def _serialize_course(c) -> dict:
    return {
        "title": c.title,
        "url": c.url,
        "courseType": c.course_type,
        "teacher": c.teacher,
        "school": c.school,
        "progress": c.progress,
    }


class BackendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        handler_cls,
        manager: TaskManager,
        token: str,
        app_version: str,
        *,
        allow_insecure_dev: bool = False,
    ):
        self.manager = manager
        self.token = token
        self.allow_insecure_dev = bool(allow_insecure_dev)
        self.app_version = app_version
        self.shutdown_event = threading.Event()
        self._shutdown_once_lock = threading.Lock()
        self._shutdown_done = False
        self._shutdown_complete = threading.Event()
        self._qr_manager_lock = threading.Lock()
        self._qr_manager = None
        self._question_store_lock = threading.Lock()
        self._question_store = None
        self._question_store_db_path = ""
        super().__init__(addr, handler_cls)

    def get_question_store(self):
        data_root = os.path.join(self.manager.data_dir, "data")
        db_path = os.path.join(data_root, "questions_bank.db")
        with self._question_store_lock:
            if (
                self._question_store is None
                or self._question_store_db_path != db_path
            ):
                from modules.answer_store import AiQuestionStore

                self._question_store = AiQuestionStore(
                    db_path=db_path,
                    data_root=data_root,
                )
                self._question_store_db_path = db_path
            return self._question_store

    def get_qr_manager(self):
        with self._qr_manager_lock:
            if self._qr_manager is None:
                from modules.qr_login import QrLoginManager

                self._qr_manager = QrLoginManager()
            return self._qr_manager

    def close_qr_manager(self):
        with self._qr_manager_lock:
            mgr = self._qr_manager
            self._qr_manager = None
        if mgr is not None:
            try:
                mgr.close_all()
            except Exception:
                pass

    def graceful_shutdown(self, *, stop_http: bool = True):
        """幂等统一关停：标记退出、清理二维码会话、关闭任务管理器与事件循环，并按需停止 HTTP 监听。"""
        with self._shutdown_once_lock:
            if self._shutdown_done:
                already_started = True
            else:
                self._shutdown_done = True
                already_started = False
        if already_started:
            self._shutdown_complete.wait(timeout=8.0)
            return
        try:
            self.shutdown_event.set()
            self.close_qr_manager()
            try:
                self.manager.shutdown()
            except Exception:
                pass
            if stop_http:
                try:
                    self.shutdown()
                except Exception:
                    pass
        finally:
            self._shutdown_complete.set()


class BackendRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    def _drain_request_body(self, max_bytes: int = 64 * 1024) -> None:
        """提前拒绝 POST 请求前读取小体积剩余请求体，避免 Windows loopback 关闭时触发 TCP RST (WinError 10053)。"""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            return
        if 0 < length <= max_bytes:
            try:
                self.rfile.read(length)
            except Exception:
                pass

    def _validate_request_origin(self) -> bool:
        if not _is_valid_host_header(self.headers.get("Host")):
            self._drain_request_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden host"})
            return False
        if not _is_valid_origin_header(self.headers.get("Origin")):
            self._drain_request_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden origin"})
            return False
        return True

    def _authorized(self) -> bool:
        expected = getattr(self.server, "token", "") or ""
        if not expected:
            return bool(getattr(self.server, "allow_insecure_dev", False))
        provided = self.headers.get("X-Autovisor-Token", "")
        if not provided:
            return False
        return hmac.compare_digest(provided, expected)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes: int = 2 * 1024 * 1024) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if length > max_bytes:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _question_store(self):
        return self.server.get_question_store()

    # ---- GET 路由 ----

    def do_GET(self):
        if not self._validate_request_origin():
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        handler = _GET_ROUTES.get(path)
        if handler is not None:
            handler(self)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def _get_health(self):
        snap = self.server.manager.snapshot()
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "version": self.server.app_version,
                "pid": os.getpid(),
                "taskStatus": snap["status"],
            },
        )

    def _get_state(self):
        self._send_json(HTTPStatus.OK, self.server.manager.snapshot())

    def _get_cache_stats(self):
        query = parse_qs(urlparse(self.path).query)
        course_id = (query.get("courseId") or query.get("course_id") or [""])[0]
        try:
            stats = self._question_store().stats(course_id.strip() or None)
            self._send_json(HTTPStatus.OK, {"ok": True, **stats})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (sqlite3.Error, Exception) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_sse(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.server.manager.subscribe()
        try:
            while not self.server.shutdown_event.is_set():
                try:
                    item = q.get(timeout=15.0)
                    if isinstance(item, tuple) and len(item) == 2:
                        event_name, data = item
                    else:
                        event_name, data = "state", item
                    payload = json.dumps(data, ensure_ascii=False)
                    chunk = f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.server.manager.unsubscribe(q)

    # ---- POST 路由 ----

    def do_POST(self):
        if not self._validate_request_origin():
            return
        if not self._authorized():
            self._drain_request_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        path = urlparse(self.path).path
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except (TypeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "无效的 Content-Length"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            if not (path == "/api/shutdown" and content_length <= 0):
                self._drain_request_body()
                self._send_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "Content-Type 必须为 application/json"},
                )
                return

        try:
            body_limit = _POST_BODY_LIMITS.get(path, 2 * 1024 * 1024)
            body = self._read_json_body(max_bytes=body_limit)
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"无效请求体: {exc}"})
            return

        handler = _POST_ROUTES.get(path)
        if handler is not None:
            handler(self, body)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"})

    def _post_tasks_start(self, body: dict):
        mode = str(body.get("mode") or "study").strip()
        target = str(body.get("target") or "").strip()
        if mode == "import_cookies":
            try:
                target = _validate_user_path(target, expect_dir=False)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
        try:
            state = self.server.manager.start_task(mode, target)
            self._send_json(HTTPStatus.OK, state)
        except (ValueError, RuntimeError) as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})

    def _post_tasks_stop(self, _body: dict):
        state = self.server.manager.stop_task()
        self._send_json(HTTPStatus.OK, state)

    def _post_tasks_cancel_shutdown(self, _body: dict):
        try:
            state = self.server.manager.cancel_shutdown()
            self._send_json(HTTPStatus.OK, state)
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": str(exc).strip() or "取消关机失败，请重试"},
            )

    def _post_tasks_debug(self, body: dict):
        enabled = bool(body.get("enabled"))
        effective = self.server.manager.set_debug_log(enabled)
        self._send_json(HTTPStatus.OK, {"ok": True, "enabled": effective})

    def _post_notify_test(self, body: dict):
        from modules.notifier import test_notification_config

        provider = str(body.get("provider") or "").strip()
        url = str(body.get("url") or "").strip()
        tg_chat_id = str(body.get("tgChatId") or body.get("tg_chat_id") or "").strip()
        proxy = str(body.get("proxy") or "").strip()
        try:
            msg = test_notification_config(
                provider=provider,
                url=url,
                tg_chat_id=tg_chat_id,
                proxy=proxy,
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "message": msg})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_cache_export(self, body: dict):
        course_id = str(body.get("courseId") or body.get("course_id") or "").strip()
        try:
            payload = self._question_store().export_data(course_id or None)
            self._send_json(HTTPStatus.OK, {"ok": True, "data": payload})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (sqlite3.Error, Exception) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _post_cache_import(self, body: dict):
        if self.server.manager.snapshot().get("status") in ("running", "stopping"):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "任务运行中，请先停止当前学习任务后再导入题库缓存"},
            )
            return
        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        try:
            counts = self._question_store().import_data(payload)
            self._send_json(HTTPStatus.OK, {"ok": True, **counts})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (sqlite3.Error, Exception) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _post_cache_clear(self, body: dict):
        if self.server.manager.snapshot().get("status") in ("running", "stopping"):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"error": "任务运行中，请先停止当前学习任务后再清理题库缓存"},
            )
            return
        course_id = str(body.get("courseId") or body.get("course_id") or "").strip()
        try:
            counts = self._question_store().clear(course_id or None)
            self._send_json(HTTPStatus.OK, {"ok": True, **counts})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (sqlite3.Error, Exception) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _post_question_bank_test(self, body: dict):
        from modules.llm import question_bank_conf
        from modules.question_bank import DEFAULT_QUERY_URL, QuestionBankClient

        try:
            conf = question_bank_conf()
            token = str(body.get("token") or conf.get("token") or "").strip()
            query_url = str(body.get("url") or conf.get("url") or DEFAULT_QUERY_URL).strip()
            proxy = str(body.get("proxy") or "").strip()
            client = QuestionBankClient(token=token, query_url=query_url, proxy=proxy)
            try:
                result = client.probe()
            finally:
                close_fn = getattr(client, "close", None)
                if callable(close_fn):
                    close_fn()
            self._send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_llm_test(self, body: dict):
        from modules.llm import test_connection

        llm_cfg = body.get("llm") if isinstance(body.get("llm"), dict) else body
        proxy = str(body.get("proxy") or "").strip()
        try:
            msg = test_connection(llm_cfg, proxy=proxy)
            self._send_json(HTTPStatus.OK, {"ok": True, "message": msg})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_llm_models(self, body: dict):
        from modules.llm import fetch_models

        llm_cfg = body.get("llm") if isinstance(body.get("llm"), dict) else body
        proxy = str(body.get("proxy") or "").strip()
        try:
            models = fetch_models(llm_cfg, proxy=proxy)
            self._send_json(HTTPStatus.OK, {"ok": True, "models": models})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_qr_start(self, body: dict):
        proxy = str(body.get("proxy") or "").strip()
        save_dir = str(body.get("saveDir") or "").strip()
        if save_dir:
            try:
                save_dir = _validate_user_path(save_dir, expect_dir=True)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
        try:
            result = self.server.get_qr_manager().start(proxy=proxy, save_dir=save_dir)
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_qr_poll(self, body: dict):
        session_id = str(body.get("sessionId") or "").strip()
        cookie_path = os.path.join(self.server.manager.data_dir, "data", "cookies.json")
        try:
            result = self.server.get_qr_manager().poll(
                session_id, cookie_path=cookie_path
            )
            self._send_json(HTTPStatus.OK, {"ok": True, **result})
        except (KeyError, ValueError) as exc:
            msg = exc.args[0] if exc.args else str(exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(msg)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_qr_save(self, body: dict):
        session_id = str(body.get("sessionId") or "").strip()
        save_dir = str(body.get("saveDir") or "").strip()
        try:
            save_dir = _validate_user_path(save_dir, expect_dir=True)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        try:
            saved_path = self.server.get_qr_manager().save_current_image(
                session_id, save_dir
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "savedPath": saved_path})
        except (KeyError, ValueError) as exc:
            msg = exc.args[0] if exc.args else str(exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(msg)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_qr_cancel(self, body: dict):
        session_id = str(body.get("sessionId") or "").strip()
        cancelled = self.server.get_qr_manager().cancel(session_id)
        self._send_json(HTTPStatus.OK, {"ok": True, "cancelled": cancelled})

    def _post_courses_fetch(self, body: dict):
        from modules.course_fetcher import fetch_courses_via_api

        proxy = str(body.get("proxy") or "").strip()
        raw_cookies = body.get("cookies")
        if raw_cookies is None:
            cookie_path = os.path.join(
                self.server.manager.data_dir, "data", "cookies.json"
            )
            try:
                with open(cookie_path, "r", encoding="utf-8") as file:
                    raw_cookies = json.load(file)
            except OSError:
                raw_cookies = []
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"本地 Cookie 文件格式无效: {exc}"},
                )
                return
        if not isinstance(raw_cookies, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "cookies 参数格式无效"})
            return
        try:
            fetched = fetch_courses_via_api(raw_cookies, proxy=proxy)
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sessionValid": fetched.session_valid,
                    "courses": [_serialize_course(c) for c in fetched.courses],
                    "displayName": fetched.display_name,
                    "schoolName": fetched.school_name,
                    "sources": fetched.sources,
                    "cookies": fetched.cookies,
                },
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_courses_extract(self, body: dict):
        from modules.course_fetcher import (
            dedupe_courses,
            extract_courses_from_captured,
            fetch_courses_via_api,
        )

        responses = body.get("responses")
        if responses is None:
            responses = []
        elif not isinstance(responses, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "responses 参数格式无效"})
            return

        dom_courses = body.get("domCourses")
        if dom_courses is None:
            dom_courses = []
        elif not isinstance(dom_courses, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "domCourses 参数格式无效"})
            return

        extra_courses = body.get("courses")
        if extra_courses is None:
            extra_courses = []
        elif not isinstance(extra_courses, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "courses 参数格式无效"})
            return

        raw_cookies = body.get("cookies")
        if raw_cookies is not None and not isinstance(raw_cookies, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "cookies 参数格式无效"})
            return

        dom_user_name = str(body.get("domUserName") or "").strip()
        proxy = str(body.get("proxy") or "").strip()
        fetch_api = (
            bool(body.get("fetchApi"))
            if "fetchApi" in body
            else bool(raw_cookies)
        )

        try:
            captured_courses, display_name, school_name = extract_courses_from_captured(
                responses,
                dom_courses,
                dom_user_name=dom_user_name,
                extra_courses=extra_courses,
            )
            session_valid = bool(captured_courses)
            refreshed_cookies: list[dict] = []
            combined_courses = list(captured_courses)

            if fetch_api and raw_cookies:
                fetched = fetch_courses_via_api(raw_cookies, proxy=proxy)
                session_valid = bool(fetched.session_valid or session_valid)
                refreshed_cookies = list(fetched.cookies or [])
                if fetched.display_name:
                    display_name = fetched.display_name
                if fetched.school_name:
                    school_name = fetched.school_name
                combined_courses = dedupe_courses(
                    list(fetched.courses) + captured_courses
                )

            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "sessionValid": session_valid,
                    "courses": [_serialize_course(c) for c in combined_courses],
                    "displayName": display_name,
                    "schoolName": school_name,
                    "cookies": refreshed_cookies,
                },
            )
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_update_check(self, body: dict):
        from modules import updater

        proxy = str(body.get("proxy") or "").strip()
        current_version = str(body.get("currentVersion") or "").strip()
        try:
            kwargs = {"proxy": proxy}
            if current_version:
                kwargs["current_version"] = current_version
            info = updater.check_update_info(**kwargs)
            self._send_json(HTTPStatus.OK, {"ok": True, **info})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

    def _post_captcha_solve(self, body: dict):
        cdp_url = str(body.get("cdpUrl") or "").strip()
        page_url = str(body.get("pageUrl") or "").strip()
        if not _is_loopback_cdp_url(cdp_url):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "cdpUrl 无效（仅允许本机回环地址）"},
            )
            return
        try:
            appear_ms = int(body.get("appearMs") or 6000)
        except (TypeError, ValueError):
            appear_ms = 6000
        try:
            attempts = int(body.get("maxAttempts") or 2)
        except (TypeError, ValueError):
            attempts = 2
        appear_ms = max(1000, min(appear_ms, 20000))
        attempts = max(1, min(attempts, 4))
        try:
            result = self.server.manager.solve_captcha(
                cdp_url, page_url, appear_ms, attempts
            )
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"自动验证失败: {type(exc).__name__}: {exc}"},
            )
            return
        self._send_json(HTTPStatus.OK, {"ok": True, **result})

    def _post_shutdown(self, _body: dict):
        self._send_json(HTTPStatus.OK, {"ok": True})
        threading.Thread(target=self._trigger_shutdown, daemon=True).start()

    def _trigger_shutdown(self):
        self.server.graceful_shutdown(stop_http=True)


_GET_ROUTES = {
    "/api/health": BackendRequestHandler._get_health,
    "/api/state": BackendRequestHandler._get_state,
    "/api/cache/stats": BackendRequestHandler._get_cache_stats,
    "/api/events": BackendRequestHandler._handle_sse,
}

_POST_BODY_LIMITS = {
    "/api/cache/import": 20 * 1024 * 1024,
    "/api/courses/extract": 10 * 1024 * 1024,
}

_POST_ROUTES = {
    "/api/tasks/start": BackendRequestHandler._post_tasks_start,
    "/api/tasks/stop": BackendRequestHandler._post_tasks_stop,
    "/api/tasks/cancel-shutdown": BackendRequestHandler._post_tasks_cancel_shutdown,
    "/api/tasks/debug": BackendRequestHandler._post_tasks_debug,
    "/api/notify/test": BackendRequestHandler._post_notify_test,
    "/api/cache/export": BackendRequestHandler._post_cache_export,
    "/api/cache/import": BackendRequestHandler._post_cache_import,
    "/api/cache/clear": BackendRequestHandler._post_cache_clear,
    "/api/question-bank/test": BackendRequestHandler._post_question_bank_test,
    "/api/llm/test": BackendRequestHandler._post_llm_test,
    "/api/llm/models": BackendRequestHandler._post_llm_models,
    "/api/qr-login/start": BackendRequestHandler._post_qr_start,
    "/api/qr-login/poll": BackendRequestHandler._post_qr_poll,
    "/api/qr-login/save": BackendRequestHandler._post_qr_save,
    "/api/qr-login/cancel": BackendRequestHandler._post_qr_cancel,
    "/api/courses/fetch": BackendRequestHandler._post_courses_fetch,
    "/api/courses/extract": BackendRequestHandler._post_courses_extract,
    "/api/update/check": BackendRequestHandler._post_update_check,
    "/api/captcha/solve": BackendRequestHandler._post_captcha_solve,
    "/api/shutdown": BackendRequestHandler._post_shutdown,
}
