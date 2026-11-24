# encoding=utf-8
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

from backend_server import TaskManager


class TaskManagerUnitTests(unittest.TestCase):
    def test_structured_events_update_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = TaskManager(tmp)
            try:
                with mgr._lock:
                    mgr._state["status"] = "running"

                mgr.on_logger_event("section", {"title": "课程 2/5"})
                mgr.on_logger_event(
                    "context",
                    {
                        "fields": {
                            "课程索引": 2,
                            "课程总数": 5,
                            "目录类型": "融合共享课",
                            "课程名称": "高等数学",
                        }
                    },
                )
                mgr.on_logger_event(
                    "event",
                    {"name": "课时开始", "fields": {"标题": "1.1 函数与极限", "模式": "学习"}},
                )
                mgr.on_progress("当前进度", 63.5, 11.2)
                mgr.on_logger_event(
                    "event",
                    {
                        "name": "课时已完成",
                        "fields": {"标题": "1.1 函数与极限", "模式": "学习", "已学分钟": 12.5},
                    },
                )
                mgr.on_logger_event(
                    "event",
                    {
                        "name": "人工介入",
                        "fields": {"状态": "需要", "原因": "检测到安全验证，请手动完成"},
                    },
                )

                snap = mgr.snapshot()
                self.assertEqual(snap["courseIndex"], 2)
                self.assertEqual(snap["courseTotal"], 5)
                self.assertEqual(snap["catalogType"], "融合共享课")
                self.assertEqual(snap["currentCourse"], "高等数学")
                self.assertEqual(snap["currentLesson"], "1.1 函数与极限")
                self.assertEqual(snap["progressDesc"], "当前进度")
                self.assertAlmostEqual(snap["lessonProgress"], 100.0)
                self.assertAlmostEqual(snap["courseElapsedMin"], 12.5)
                self.assertAlmostEqual(snap["remainingMinutes"], 11.2)
                self.assertEqual(len(snap["completedLessons"]), 1)
                self.assertEqual(snap["completedLessons"][0]["title"], "1.1 函数与极限")
                self.assertIn("安全验证", snap["needsAttention"])

                mgr.on_logger_event("event", {"name": "恢复播放", "fields": {"播放器时间": 10.0}})
                snap = mgr.snapshot()
                self.assertEqual(snap["needsAttention"], "")

                mgr.on_logger_event(
                    "event",
                    {"name": "安全验证", "fields": {"状态": "开始", "地址": "https://example.com"}},
                )
                self.assertIn("安全验证", mgr.snapshot()["needsAttention"])
                mgr.on_logger_event(
                    "event",
                    {"name": "人工介入", "fields": {"状态": "已解除", "原因": "安全验证已自动完成"}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["needsAttention"], "")

                mgr.on_logger_event(
                    "event",
                    {"name": "达到课程时限", "fields": {"时限分钟": 30, "复习模式": False}},
                )
                self.assertEqual(mgr.snapshot()["status"], "time_limit")
            finally:
                mgr.shutdown()

    def test_ai_smart_and_mastery_events_update_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = TaskManager(tmp)
            try:
                with mgr._lock:
                    mgr._state["status"] = "running"

                mgr.on_logger_event(
                    "context",
                    {"fields": {"子项目序号": "2/10", "子项目名称": "导数与微分", "模式": "学习"}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "导数与微分")
                self.assertEqual(snap["progressDesc"], "AI 智慧课子项目 2/10")

                mgr.on_logger_event(
                    "event",
                    {
                        "name": "资源开始",
                        "fields": {"标题": "2.1 导数概念", "类型": "视频", "阶段": "学习"},
                    },
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "2.1 导数概念 (视频)")
                self.assertEqual(snap["progressDesc"], "AI 智慧课必学资源")

                mgr.on_logger_event("section", {"title": "掌握度提升"})
                mgr.on_logger_event("event", {"name": "知识点状态", "fields": {"名称": "极限定义", "掌握度": 45}})
                snap = mgr.snapshot()
                self.assertEqual(snap["stage"], "掌握度提升")
                self.assertEqual(snap["currentLesson"], "极限定义")
                self.assertAlmostEqual(snap["lessonProgress"], 45.0)
                self.assertIn("45%", snap["progressDesc"])

                mgr.on_logger_event("event", {"name": "掌握度结果", "fields": {"轮次": 2, "掌握度": 85}})
                snap = mgr.snapshot()
                self.assertAlmostEqual(snap["lessonProgress"], 85.0)
                self.assertIn("第 2 轮", snap["progressDesc"])

                mgr.on_logger_event("event", {"name": "知识点完成", "fields": {"名称": "极限定义", "前": 45, "后": 100, "轮次": 1}})
                snap = mgr.snapshot()
                self.assertAlmostEqual(snap["lessonProgress"], 100.0)
                self.assertEqual(len(snap["completedLessons"]), 1)
                self.assertIn("极限定义", snap["completedLessons"][0]["title"])

                mgr.on_logger_event(
                    "context",
                    {"fields": {"课时名称": "1.2 极限复习"}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "1.2 极限复习")

                mgr.on_logger_event(
                    "event",
                    {
                        "name": "资源开始",
                        "fields": {"标题": "2.1 导数概念", "类型": "video", "阶段": "复习"},
                    },
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["progressDesc"], "AI 智慧课视频复习")

                mgr.on_logger_event(
                    "event",
                    {"name": "ai-smart 资源复习完成", "fields": {"标题": "2.1 导数概念", "类型": "video", "轮次": 1}},
                )
                snap = mgr.snapshot()
                self.assertEqual(len(snap["completedLessons"]), 2)
                self.assertIn("2.1 导数概念", snap["completedLessons"][-1]["title"])
            finally:
                mgr.shutdown()

    def test_exam_api_and_resource_completion_events_update_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = TaskManager(tmp)
            try:
                with mgr._lock:
                    mgr._state["status"] = "running"

                mgr.on_logger_event("section", {"title": "接口考试"})
                mgr.on_logger_event(
                    "event",
                    {"name": "接口考试范围", "fields": {"知识点": 5, "目标掌握度": 100}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["stage"], "接口考试")
                self.assertIn("5", snap["progressDesc"])
                self.assertAlmostEqual(snap["lessonProgress"], 0.0)

                mgr.on_logger_event(
                    "event",
                    {"name": "知识点考试", "fields": {"序号": "3/5", "知识点": "微积分基本定理"}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "微积分基本定理")
                self.assertEqual(snap["progressDesc"], "接口考试 3/5")
                self.assertAlmostEqual(snap["lessonProgress"], 40.0)

                mgr.on_logger_event(
                    "event",
                    {"name": "接口考试开始", "fields": {"试卷": "P100", "题目": 10}},
                )
                snap = mgr.snapshot()
                self.assertIn("10", snap["progressDesc"])
                self.assertAlmostEqual(snap["lessonProgress"], 0.0)

                mgr.on_logger_event(
                    "event",
                    {"name": "接口答题", "fields": {"序号": "5/10", "题目": 501}},
                )
                snap = mgr.snapshot()
                self.assertAlmostEqual(snap["lessonProgress"], 50.0)
                self.assertIn("5/10", snap["progressDesc"])

                mgr.on_logger_event(
                    "event",
                    {
                        "name": "知识点考试结果",
                        "fields": {"知识点": "微积分基本定理", "轮次": 1, "得分": "10/10", "全对": True},
                    },
                )
                snap = mgr.snapshot()
                self.assertAlmostEqual(snap["lessonProgress"], 100.0)
                self.assertEqual(len(snap["completedLessons"]), 1)
                self.assertEqual(
                    snap["completedLessons"][0]["title"],
                    "微积分基本定理（接口考试 10/10）",
                )

                mgr.on_logger_event(
                    "event",
                    {
                        "name": "资源补齐",
                        "fields": {"序号": "2/4", "名称": "第2章 课件", "补齐": 2, "已完成": 1, "失败": 0},
                    },
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "第2章 课件")
                self.assertEqual(snap["progressDesc"], "必学资源补齐 2/4")
                self.assertAlmostEqual(snap["lessonProgress"], 50.0)

                mgr.on_logger_event(
                    "event",
                    {"name": "知到作业", "fields": {"状态": "开始", "名称": "第一章单元测试", "序号": "1/3"}},
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["currentLesson"], "第一章单元测试")
                self.assertEqual(snap["progressDesc"], "知到作业 1/3")
                self.assertAlmostEqual(snap["lessonProgress"], 0.0)

                mgr.on_logger_event(
                    "event",
                    {
                        "name": "知到作业",
                        "fields": {
                            "状态": "完成",
                            "名称": "第一章单元测试",
                            "序号": "1/3",
                            "得分": 20.0,
                            "已交卷": True,
                        },
                    },
                )
                snap = mgr.snapshot()
                self.assertEqual(snap["progressDesc"], "知到作业完成 1/3")
                self.assertAlmostEqual(snap["lessonProgress"], 100.0 / 3, places=2)
                self.assertTrue(
                    any("第一章单元测试" in item["title"] for item in snap["completedLessons"])
                )

                # Deprecated modes must be rejected
                with self.assertRaises(ValueError):
                    mgr.start_task("fetch_courses")
                with self.assertRaises(ValueError):
                    mgr.start_task("test_notify")
            finally:
                mgr.shutdown()

    def test_debug_log_toggle_emits_debug_entries(self):
        from modules.logger import Logger

        with tempfile.TemporaryDirectory() as tmp:
            mgr = TaskManager(tmp)
            logger = Logger()
            prev_sink = logger._event_sink
            prev_emit = getattr(logger, "emit_debug", False)
            try:
                logger.set_event_sink(mgr.on_logger_event)
                with mgr._lock:
                    mgr._state["status"] = "running"
                    mgr._active_logger = logger

                mgr.set_debug_log(False)
                logger.debug("hidden debug message")
                self.assertEqual(len(mgr.snapshot()["logs"]), 0)

                mgr.set_debug_log(True)
                logger.debug("visible debug message")
                logs = mgr.snapshot()["logs"]
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0]["level"], "debug")
                self.assertEqual(logs[0]["message"], "visible debug message")
            finally:
                logger.emit_debug = prev_emit
                logger.set_event_sink(prev_sink)
                mgr.shutdown()

    def test_debug_log_enabled_before_task_start_persists_across_config_load(self):
        import time
        from unittest import mock
        from modules.logger import Logger

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            with open(os.path.join(tmp, "data", "mirrors.json"), "w", encoding="utf-8") as f:
                f.write('{"Official": "https://pypi.org"}')
            logger_inst = Logger()
            prev_emit = getattr(logger_inst, "emit_debug", False)
            prev_filename = logger_inst.filename
            prev_log_dir = logger_inst.log_dir
            prev_root = logger_inst.runtime_root
            prev_ctx = dict(logger_inst._context)
            prev_data_dir_env = os.environ.get("AUTOVISOR_DATA_DIR")
            os.environ["AUTOVISOR_DATA_DIR"] = tmp
            mgr = TaskManager(tmp)
            try:
                mgr.set_debug_log(True)
                self.assertTrue(mgr._debug_log_enabled)

                observed_emit_debug = []

                async def fake_check_browser(config, logger, cookie_path):
                    observed_emit_debug.append(logger.emit_debug)
                    logger.debug("debug during check_browser")
                    return 0

                with mock.patch("modules.diagnostics.check_browser", side_effect=fake_check_browser):
                    mgr.start_task("check_browser")
                    deadline = time.time() + 5.0
                    while time.time() < deadline and mgr.snapshot()["status"] == "running":
                        time.sleep(0.05)

                snap = mgr.snapshot()
                self.assertEqual(snap["status"], "completed", snap.get("errorMessage"))
                self.assertTrue(mgr._debug_log_enabled)
                self.assertEqual(observed_emit_debug, [True])
                self.assertTrue(
                    any(
                        entry["level"] == "debug" and "debug during check_browser" in entry["message"]
                        for entry in snap["logs"]
                    )
                )
            finally:
                mgr.shutdown()
                if prev_data_dir_env is None:
                    os.environ.pop("AUTOVISOR_DATA_DIR", None)
                else:
                    os.environ["AUTOVISOR_DATA_DIR"] = prev_data_dir_env
                logger_inst.emit_debug = prev_emit
                logger_inst.filename = prev_filename
                logger_inst.log_dir = prev_log_dir
                logger_inst.runtime_root = prev_root
                logger_inst._context.clear()
                logger_inst._context.update(prev_ctx)

    def test_solve_captcha_runs_on_manager_loop_and_closes_cdp_browser(self):
        from unittest import mock
        import backend_server

        with tempfile.TemporaryDirectory() as tmp:
            mgr = TaskManager(tmp)
            try:
                closed_calls = []

                class FakeBrowser:
                    contexts = []

                    async def close(self):
                        closed_calls.append(True)

                class FakeChromium:
                    async def connect_over_cdp(self, url):
                        return FakeBrowser()

                class FakePlaywrightCtx:
                    chromium = FakeChromium()

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc, tb):
                        return False

                with (
                    mock.patch.object(backend_server, "_load_captcha_modules", return_value=object()),
                    mock.patch("playwright.async_api.async_playwright", return_value=FakePlaywrightCtx()),
                ):
                    res = mgr.solve_captcha("http://127.0.0.1:9222", "https://passport.zhihuishu.com", 1000, 1)
                self.assertEqual(res, {"solved": False, "reason": "未找到登录页"})
                self.assertEqual(closed_calls, [True])
            finally:
                mgr.shutdown()


class BackendServerProcessTests(unittest.TestCase):
    def test_handshake_auth_health_sse_and_stdin_eof_exit(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(repo_root, "backend_server.py")
        token = "test-secret-token-123456"

        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["AUTOVISOR_BACKEND_TOKEN"] = token
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    script,
                    "--data-dir",
                    tmp,
                    "--resources-dir",
                    os.path.join(repo_root, "resources"),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
            try:
                line = proc.stdout.readline()
                self.assertTrue(line.strip(), "backend did not emit handshake line")
                handshake = json.loads(line)
                self.assertEqual(handshake["event"], "autovisor-backend-ready")
                self.assertEqual(handshake["version"], 1)
                port = int(handshake["port"])
                self.assertGreater(port, 0)

                base_url = f"http://127.0.0.1:{port}"

                # 1. Rejects missing token
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"{base_url}/api/health", timeout=5)
                self.assertEqual(ctx.exception.code, 401)

                # 2. Accepts valid token for /api/health
                req = urllib.request.Request(
                    f"{base_url}/api/health",
                    headers={"X-Autovisor-Token": token},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertEqual(health["taskStatus"], "idle")

                # 3. /api/state returns structured initial RunState
                req_state = urllib.request.Request(
                    f"{base_url}/api/state",
                    headers={"X-Autovisor-Token": token},
                )
                with urllib.request.urlopen(req_state, timeout=5) as resp:
                    state = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(state["status"], "idle")
                self.assertEqual(state["stage"], "就绪")

                # 4. /api/events sends initial SSE snapshot
                req_sse = urllib.request.Request(
                    f"{base_url}/api/events",
                    headers={"X-Autovisor-Token": token},
                )
                with urllib.request.urlopen(req_sse, timeout=5) as resp:
                    event_line = resp.readline().decode("utf-8").strip()
                    data_line = resp.readline().decode("utf-8").strip()
                self.assertEqual(event_line, "event: state")
                self.assertTrue(data_line.startswith("data: "))
                sse_state = json.loads(data_line[len("data: "):])
                self.assertEqual(sse_state["status"], "idle")

                # 4b. /api/notify/test & /api/llm/test & /api/llm/models validate payloads
                for endpoint, body in (
                    ("/api/notify/test", {"provider": ""}),
                    ("/api/llm/test", {"llm": {"apiBase": "invalid", "model": "x"}}),
                    ("/api/llm/models", {"llm": {"apiBase": "https://x.openai.azure.com", "apiFormat": "azure", "apiKey": "k"}}),
                ):
                    req_post = urllib.request.Request(
                        f"{base_url}{endpoint}",
                        data=json.dumps(body).encode("utf-8"),
                        headers={
                            "X-Autovisor-Token": token,
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as err_ctx:
                        urllib.request.urlopen(req_post, timeout=5)
                    self.assertEqual(err_ctx.exception.code, 400)
                    err_body = json.loads(err_ctx.exception.read().decode("utf-8"))
                    self.assertIn("error", err_body)

                # 4c. /api/cache/import -> /api/cache/stats -> /api/cache/clear
                import_payload = {
                    "data": {
                        "zhidao_questions": [
                            {
                                "course_id": "c-1",
                                "exam_id": "e-1",
                                "eid": "E1",
                                "content": "测试题干",
                                "options": [{"id": 1, "content": "选项A"}],
                                "correct_options": [1],
                            }
                        ],
                        "ai_questions": [
                            {
                                "course_id": "c-1",
                                "exam_id": "e-1",
                                "question_id": "q-1",
                                "question_plain": "AI 题干",
                                "answer": "A",
                            }
                        ],
                    }
                }
                req_import = urllib.request.Request(
                    f"{base_url}/api/cache/import",
                    data=json.dumps(import_payload).encode("utf-8"),
                    headers={
                        "X-Autovisor-Token": token,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_import, timeout=5) as resp:
                    imp_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(imp_body["ok"])
                self.assertEqual(imp_body["zhidao_questions"], 1)
                self.assertEqual(imp_body["ai_questions"], 1)

                req_clear = urllib.request.Request(
                    f"{base_url}/api/cache/clear",
                    data=json.dumps({}).encode("utf-8"),
                    headers={
                        "X-Autovisor-Token": token,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_clear, timeout=5) as resp:
                    clear_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(clear_body["ok"])
                self.assertEqual(clear_body["zhidao_questions"], 1)
                self.assertEqual(clear_body["ai_questions"], 1)

                req_stats = urllib.request.Request(
                    f"{base_url}/api/cache/stats",
                    headers={"X-Autovisor-Token": token},
                )
                with urllib.request.urlopen(req_stats, timeout=5) as resp:
                    stats_body = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(stats_body["ok"])
                self.assertEqual(stats_body["zhidao_questions"], 0)
                self.assertEqual(stats_body["ai_questions"], 0)

                # 5. Closing stdin triggers graceful shutdown
                proc.stdin.close()
                exit_code = proc.wait(timeout=8)
                self.assertEqual(exit_code, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

    def test_verify_deps_and_stdin_pipe_no_numpy_deadlock(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = os.path.join(repo_root, "backend_server.py")

        verify = subprocess.run(
            [sys.executable, script, "--verify-deps"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        payload = json.loads(verify.stdout.strip())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["numpy"])
        self.assertTrue(payload["cv2"])

    def test_cache_dir_isolation_running_conflict_and_invalid_proxy_status(self):
        import threading
        from unittest import mock
        import backend_server

        token = "inproc-test-token"
        with (
            tempfile.TemporaryDirectory() as isolated_dir,
            tempfile.TemporaryDirectory() as global_dir,
        ):
            class _FakeManager:
                def __init__(self, data_dir):
                    self.data_dir = data_dir
                    self.status = "idle"

                def snapshot(self):
                    return {"status": self.status}

                def shutdown(self):
                    pass

            mgr = _FakeManager(isolated_dir)
            server = backend_server.BackendHTTPServer(
                ("127.0.0.1", 0),
                backend_server.BackendRequestHandler,
                mgr,
                token,
                "1.0.0-test",
            )
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{port}"

            def _post(path, payload):
                req = urllib.request.Request(
                    f"{base_url}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "X-Autovisor-Token": token,
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                return urllib.request.urlopen(req, timeout=5)

            try:
                with mock.patch.dict(os.environ, {"AUTOVISOR_DATA_DIR": global_dir}):
                    # 1. Import / Export / Clear are isolated to manager.data_dir/data/questions_bank.db
                    import_payload = {
                        "data": {
                            "zhidao_questions": [
                                {
                                    "course_id": "iso-c1",
                                    "exam_id": "iso-e1",
                                    "eid": "ISO1",
                                    "content": "隔离测试题",
                                    "options": [{"id": 1, "content": "选项A"}],
                                    "correct_options": [1],
                                }
                            ]
                        }
                    }
                    with _post("/api/cache/import", import_payload) as resp:
                        imp = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(imp["ok"])
                    self.assertEqual(imp["zhidao_questions"], 1)

                    isolated_db = os.path.join(isolated_dir, "data", "questions_bank.db")
                    global_db = os.path.join(global_dir, "data", "questions_bank.db")
                    self.assertTrue(os.path.isfile(isolated_db))
                    self.assertFalse(os.path.exists(global_db))

                    with _post("/api/cache/export", {}) as resp:
                        exp = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(exp["ok"])
                    self.assertEqual(len(exp["data"]["zhidao_questions"]), 1)

                    # 2. Running task rejects /api/cache/clear and /api/cache/import with 409
                    mgr.status = "running"
                    with self.assertRaises(urllib.error.HTTPError) as clear_ctx:
                        _post("/api/cache/clear", {})
                    self.assertEqual(clear_ctx.exception.code, 409)

                    with self.assertRaises(urllib.error.HTTPError) as imp_ctx:
                        _post("/api/cache/import", import_payload)
                    self.assertEqual(imp_ctx.exception.code, 409)

                    mgr.status = "idle"
                    with _post("/api/cache/clear", {}) as resp:
                        cleared = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(cleared["ok"])
                    self.assertEqual(cleared["zhidao_questions"], 1)

                    # 3. /api/courses/fetch with invalid proxy returns 400 BAD_REQUEST
                    with self.assertRaises(urllib.error.HTTPError) as proxy_ctx:
                        _post(
                            "/api/courses/fetch",
                            {
                                "cookies": [{"name": "CASTGC", "value": "tgt-1"}],
                                "proxy": "ftp://bad",
                            },
                        )
                    self.assertEqual(proxy_ctx.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
