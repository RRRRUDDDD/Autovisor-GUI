# Autovisor 重构与优化执行计划（Execution Plan）

> **基线版本**：GUI `v1.1.0` / 引擎 `3.20.0`（commit `b199495`，2026-11-24）  
> **适用仓库**：`E:\Downloads\9999999999999\Autovisor`  
> **使用方式**：本文档为自包含执行计划，供后续会话按阶段增量实施。每完成一个任务项，请勾选进度表、运行对应验证命令并按建议粒度提交。

---

## 0. 全局验证命令与约束

### 标准回归命令（每个 Phase 结束必跑）

```powershell
# 1. Python 依赖校验与单元测试（仓库根目录）
uv run --no-sync python backend_server.py --verify-deps
uv run --no-sync python -m unittest discover -s tests -v

# 2. React 前端单测与生产构建（gui/ 目录）
npm --prefix gui test
npm --prefix gui run build

# 3. Rust 编译与单元测试（gui/src-tauri/ 目录）
cargo test --manifest-path gui/src-tauri/Cargo.toml
```

### 执行约束
- **保持向后兼容**：不得破坏现有 `config.ini`、`data/llm.json`、`data/cookies.json`、`data/gui_prefs.json`、`data/answers.db` 的旧用户数据迁移与读取。
- **小步提交**：每个 Task 保持独立可回滚，Commit 遵循仓库既有中文 Conventional Commits 风格（如 `fix(security): ...`、`refactor(llm): ...`、`perf(sse): ...`）。
- **行号说明**：下文行号以基线 `b199495` 为准，修改前请以函数/符号名定位最新位置。

---

## 进度总览（Checklist）

### Phase 1：止血修复（安全 / 丢数据 / 必现 Bug / 发布门禁）
- [x] **T1.1** 安全加固：修复 `open_url` 命令注入、`open_path` 越权、关闭 `withGlobalTauri` 与收紧 CSP
- [x] **T1.2** 安全加固：修复 Windows Toast PowerShell CR 注入绕过
- [x] **T1.3** 安全加固：本地后端 Fail-Closed 鉴权、CSPRNG Token + `stdin` 传参、防 CSRF/SSRF
- [x] **T1.4** 数据安全：修复 `zhs_session.rs` / `runtime.rs` 瞬态读失败清空用户配置
- [x] **T1.5** 数据安全：修复 `answer_store.py` 并发锁、`BEGIN IMMEDIATE`、`synchronous=NORMAL` 与桥接丢答案
- [x] **T1.6** 数据安全：修复 `utils.py:save_cookies` FD 双重关闭与 Rust 非原子临时文件覆盖
- [x] **T1.7** 核心 Bug：修复 `quiz_page.py` 多选题无法取消错选 & `ai_smart_mastery.py` 监听器泄漏/`time.sleep`/提前中止 & `zhidao_homework_api` 未判分满分误判
- [x] **T1.8** 前端 Bug：修复题库导入契约错配、`onClearSavedLogin` 未连线、二维码切 Tab 僵尸轮询、8 个裸数字框替换 `NumberInput`
- [x] **T1.9** 发布链路：修复 `build-release.yml` 版本校验、`codesign` 吞错与 `build-portable.ps1` 二进制优先级反转

### Phase 2：并发稳定性与性能治理
- [x] **T2.1** 任务取消竞态收口：`backend_server.py` `run_id` 代际隔离、`stopping` 状态与 `to_thread` 取消信号
- [x] **T2.2** Rust 进程与锁健壮性：`backend.rs` / `zhs_session.rs` 生命周期锁、去掉 21 处 `.lock().unwrap()`、卸载主线程 5s 退出阻塞
- [x] **T2.3** 端到端 SSE 增量/节流推送 + React 渲染隔离（`ProgressView` 1Hz 计时器、`ExecutionLog` memo、`App.jsx` 状态拆分）
- [x] **T2.4** `llm.py` 流式 `<think>` 感知提前截断、HTTP Session 连接池与跨线程关流修复
- [x] **T2.5** `logger.py` 持久文件句柄、全通道脱敏、锁保护与 `summarize_exception` 空串修复
- [x] **T2.6** 异步阻塞与资源泄漏治理：OpenCV 验证码迁入 `to_thread`、`exam_ctx`/`zhs_api` 锁拆分与 Session 关闭、`Autovisor.py` 任务跟踪、`task_monitor` 取消处理、`updater.py` 去除静默 TLS 降级

### Phase 3：跨端单一真相源（SSOT）与领域模块去重
- [x] **T3.1** 课程解析下沉 Python：删掉 `zhs_session.rs` ~550 行重复解析，统一走 Python 后端接口
- [x] **T3.2** 统一配置与 LLM 厂商真相源：对齐 7 处三端漂移，补齐 `docs/ipc_contracts.md`
- [x] **T3.3** 抽离答题公共基础设施：`text_utils.py`、`answer_parser.py`、`answer_service.py`、共享 Playwright 测验 DOM 辅助
- [x] **T3.4** 抽离验证码公共层：`captcha_common.py` + `captcha_router.py`，打破三文件循环导入并走代理下载
- [x] **T3.5** `AnswerBank` / `AnswerCache` 收敛为 SQLite 单一数据源（取消逐题全量写 JSON）

### Phase 4：巨型文件拆分、凭据加密与工程护栏
- [x] **T4.1** 拆分 Python 巨型入口：`Autovisor.py`、`backend_server.py`、`llm.py`、`exam_ctx.py`、`course_fetcher.py`、`popups.py`
- [x] **T4.2** 拆分 Rust 模块：`zhs_session.rs`、`config_ini.rs`、`lib.rs`（并切换异步 `reqwest::Client`）
- [x] **T4.3** 拆分 React 巨型组件：`SettingsView.jsx`、`LoginView.jsx`、`WorkspaceView.jsx`、`App.jsx`（含修复错位日志控件）
- [x] **T4.4** 凭据持久化加密（DPAPI / `keyring`）+ 停止在 `get_runtime_info` 暴露明文密码
- [ ] **T4.5** 测试覆盖补盲（`ai_smart_mastery`、`quiz_page`、前端核心组件）+ 构建脚本去重 + CI 增加 `clippy`/ESLint

---

## Phase 1：止血修复（高确定性、低风险、高收益）

### T1.1 安全加固：修复 `open_url` / `open_path` 与 Tauri CSP
- **目标**：消除 Windows `cmd.exe` 命令注入与任意本地程序启动风险。
- **涉及文件**：
  - `gui/src-tauri/src/lib.rs`（`open_path` L666-693、`open_url` L695-722）
  - `gui/src-tauri/tauri.conf.json`（L14、L28）
  - `gui/src-tauri/Cargo.toml`（如需引入 `url` 或 `tauri-plugin-opener`）
- **改动方案**：
  1. `open_url`：使用 `url::Url::parse(trimmed)` 校验，仅允许 `scheme() == "http" || scheme() == "https"` 且不含控制字符/换行；Windows 侧彻底移除 `Command::new("cmd").args(["/C", "start", ...])`，改为 `tauri-plugin-opener`（或直接调用 `windows-sys` `ShellExecuteW`，确认 `&` 查询参数可正常打开）。
  2. `open_path`：对 `target` 做 `fs::canonicalize()`，验证必须位于允许根目录内（`data_dir()`、`logs` 目录、`config.ini` 所在目录），拒绝 UNC 路径与可执行文件扩展名（`.exe/.bat/.cmd/.ps1/.vbs/.msi/.com/.scr`）；若目标是文件且在 Windows 下需定位，优先 `explorer /select,<path>` 或只允许打开 `.log`/`.txt`/`.ini`/`.json`/目录。
  3. `tauri.conf.json`：将 `"withGlobalTauri"` 改为 `false`（确认 `gui/src/lib/tauriApi.js` 不依赖 `window.__TAURI__`）；CSP 补充 `object-src 'none'; base-uri 'none'; frame-ancestors 'none'`。
- **验收**：Rust 单测覆盖含 `&calc.exe`、`file://`、`\\evil\share`、越界 `C:\Windows\System32\cmd.exe`、合法 `https://studyvideoh5.zhihuishu.com/learnPage?a=1&b=2`；`npm --prefix gui test` 与 `cargo test` 通过。

### T1.2 安全加固：修复 Windows Toast PowerShell 注入
- **目标**：彻底消除动态字符串拼接到 PowerShell `-Command` 脚本的注入面。
- **涉及文件**：`modules/notifier.py`（`Windows._build_script` L372-395、`_run_powershell` L398-410、`_send` L414-420）、`tests/test_notifier.py`
- **改动方案**：
  1. 将 PowerShell 脚本改为**纯静态常量**，从 `$env:AUTOVISOR_TOAST_TEXT` 读取消息并用 `$xml.CreateTextNode($env:AUTOVISOR_TOAST_TEXT)` 挂载到 `<text>` 节点（无需手工 XML 拼接与正则过滤 `'@`）。
  2. `_run_powershell` 通过 `env={**os.environ, "AUTOVISOR_TOAST_TEXT": message}` 传参。
- **验收**：在 `tests/test_notifier.py` 新增含 `\r'@; calc.exe; @'\n`、`&<>`、多行 traceback 的单测，确认静态脚本内容不含原始 message 文本。

### T1.3 安全加固：本地后端 Fail-Closed 鉴权与防 CSRF/SSRF
- **目标**：封堵本地 HTTP 服务免鉴权、弱 Token、网页 CSRF 及非本机 CDP SSRF。
- **涉及文件**：
  - `gui/src-tauri/src/backend.rs`（`spawn_and_connect` L283、`generate_token` L825-837）
  - `backend_server.py`（`_authorized` L1051-1056、`do_GET`/`do_POST`、`main` L1623、`/api/captcha/solve` L1396-1401、`/api/tasks/start` L885-888、`/api/qr-login/*` L1278-1318）
  - `tests/test_backend_server.py`
- **改动方案**：
  1. `backend.rs`：改用 `getrandom`（32 字节 hex）生成 Token；仍可兼容通过已存在的 `stdin` 首行 JSON 握手（如 `{"authToken": "..."}`）或保留命令行/stdin 传参替代 `AUTOVISOR_BACKEND_TOKEN` 环境变量（注意同步更新 `_watch_parent_stdin` 先读首行握手再监控 EOF）。
  2. `backend_server.py`：
     - 启动时若未收到 Token 且未显式指定 `--allow-insecure-dev`（仅本地手动调试），改为 `secrets.token_urlsafe(32)` 自动生成（Fail-Closed，绝不 `if not expected: return True`）。
     - `_authorized()` 使用 `hmac.compare_digest(provided, expected)`。
     - 校验 `Host` 仅允许 `127.0.0.1[:port]` / `localhost[:port]` / `[::1][:port]`；若存在 `Origin` 头则拒绝非 Tauri/loopback 来源；`POST` 请求要求 `Content-Type` 为 `application/json`（除无 body 端点如 `/api/shutdown`）。
     - `/api/captcha/solve` 解析 `cdpUrl` 的 `urlparse(cdp_url).hostname`，仅放行 `{"127.0.0.1", "localhost", "::1"}`。
     - 校验 `/api/tasks/start` (`import_cookies`) 和 `/api/qr-login/*` 的文件路径为普通文件/合法目录，拒绝目录遍历。
- **验收**：更新 `tests/test_backend_server.py`，新增无 Token 401、错 Host 403、恶意 Origin 403、非 loopback `cdpUrl` 400 单测。

### T1.4 数据安全：防止瞬态读失败清空用户配置与偏好
- **目标**：杜绝 `unwrap_or_default()` 导致的配置静默清空。
- **涉及文件**：
  - `gui/src-tauri/src/zhs_session.rs`（`finalize_session_result` L1913-1925）
  - `gui/src-tauri/src/runtime.rs`（`load_prefs_from_path` L202-207、`load_prefs`/`save_prefs` L214-248）
  - `gui/src-tauri/src/config_ini.rs`（`load_config_from_dir`）
- **改动方案**：
  1. `load_config_from_dir` 区分「文件不存在（正常初始化默认）」与「存在但读取/解析失败（返回 `Err`）」。
  2. `finalize_session_result` 改为 `let mut cfg = load_config_from_dir(base_dir)?;`，禁止 `.unwrap_or_default()`。
  3. `runtime.rs` 新增持有 `prefs_lock()` 的事务式辅助函数 `update_prefs<F>(f: F) -> Result<GuiPrefs, String>`，避免 `load_prefs()` + 修改 + `save_prefs()` 在锁外竞态互相覆盖；若 `gui_prefs.json` 存在但损坏，先备份为 `.corrupt.<ts>` 再报错或提示，避免直接静默写空。
- **验收**：Rust 单测模拟 `config.ini` 不可读/损坏场景，断言 `finalize_session_result` 返回 `Err` 且不覆写原文件。

### T1.5 数据安全：修复 `answer_store.py` 并发、事务与桥接丢答案
- **目标**：消除 SQLite 并发写异常、逐行 fsync 慢写与桥接静默丢失历史答案。
- **涉及文件**：`modules/answer_store.py`（`_connect`/`_write` L178-231、`save_options_by_id` L528-562、`_migrate_*`/`import_data` L927-1048、`_import_zhidao_item` L1120-1161）、`tests/test_answer_store.py`
- **改动方案**：
  1. `_connect()`：每次连接均执行 `PRAGMA synchronous=NORMAL` 和 `PRAGMA busy_timeout=5000`；设置 `isolation_level=None`（autocommit 模式）。
  2. `_write()`：上下文管理器内部先获取 `self._lock`，再显式执行 `BEGIN IMMEDIATE`，正常退出 `COMMIT`，异常 `ROLLBACK`。
  3. 抽出公共辅助 `_merge_zhidao_rows(primary, secondary)`：合并 `eid`、`correct_options`（非空优先）、`wrong_options`（去重合并并按 `WRONG_COMBO_LIMIT` 截断）、`ai_analysis`（非空优先）。在 `save_options_by_id()` 删除 `bridge` 前与 `_import_zhidao_item()` 中共用该合并函数。
  4. `_migrate_course_dir`、`_migrate_mastery`、`import_data` 改为单事务批量写入。
- **验收**：在 `tests/test_answer_store.py` 新增：
  - `eid` 行先有 `correct_options`/`wrong_options`/`ai_analysis`，随后 `save_options_by_id` 桥接后全部字段完整保留；
  - 多线程并发写入无 `OperationalError: database is locked` / `IntegrityError`；
  - 导入超长 `wrong_options` 正确受 `WRONG_COMBO_LIMIT` 限制。

### T1.6 数据安全：修复 `save_cookies` FD 双关与 Rust 原子写冲突
- **目标**：消除跨线程误关 FD 与多线程写临时文件碰撞。
- **涉及文件**：
  - `modules/utils.py`（`save_cookies` L68-88）
  - `gui/src-tauri/src/config_ini.rs`（`write_file_atomic` L1037-1055）
  - `gui/src-tauri/src/zhs_session.rs`（`save_cookies_atomic` L663-676）
- **改动方案**：
  1. `utils.py:save_cookies`：在 `with os.fdopen(fd, "w", encoding="utf-8") as file:` 进入前保存/置空 `fd = None`（注意仅当 `os.fdopen` 成功返回后置 `None`），`except` 仅在 `fd is not None` 时调用 `os.close(fd)`。
  2. Rust：抽公共原子写函数（带 `CONFIG_IO_LOCK: Mutex<()>`），使用 `PID + AtomicU64` 唯一临时文件名或引入 `tempfile::NamedTempFile::persist`（Windows 底层调用 `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`，避免先 `remove_file` 再 `rename` 的非原子窗口），Unix 下统一 `chmod 0o600`。
- **验收**：`tests/test_cookie_persistence.py` 新增模拟 `os.replace` 抛 `PermissionError` 断言不二次 `os.close`；Rust 并发写单测无冲突。

### T1.7 领域核心必现 Bug 修复
- **目标**：修复已核实的 4 个高影响功能缺陷。
- **涉及文件**：
  - `modules/quiz_page.py`（`JS_CLICK` L62-77、`_click`、`_answer_questions_once` L439-450）
  - `modules/ai_smart_mastery.py`（`run_mastery` L814 & L918、`run_course_mastery` L1137-1159）
  - `modules/zhidao_homework_api.py`（`extract_score` L526-541）
  - `modules/homework.py`（`_save_or_submit` L432-442 & L801-802）
- **改动方案**：
  1. `quiz_page.py`：`JS_CLICK` 增加第 3 参数 `wantChecked`（默认 `true`），改为 `if (el.type === "checkbox" && el.checked === wantChecked) return true;`；`_answer_questions_once` 对 `need` 传 `wantChecked=True`，对 `wrong` 传 `wantChecked=False`。
  2. `ai_smart_mastery.py`：
     - `run_mastery` 用命名 `handler` 绑定 `page.on("response", handler)`，用 `pending_tasks: set[asyncio.Task]` 追踪异步回调，包裹 `try...finally` 执行 `page.remove_listener("response", handler)` 并取消未完成任务。
     - L918 `time.sleep(0.5)` 改为 `await asyncio.sleep(0.5)`。
     - L1137-1159 当节点 `final >= target` 时同样 `improved += 1`。
  3. `zhidao_homework_api.py`：`extract_score()` 仅读取实际得分字段（`"score"`、`"examScore"`、`"userScore"`），移除 `"totalScore"` 回退。
  4. `homework.py`：`_save_or_submit()` 返回 `bool` 表示真实点击成功，L802 改为 `stats["submitted"] = bool(submitted)`。
- **验收**：为 4 处缺陷分别补齐针对性单元测试并全部通过。

### T1.8 前端高优先级 Bug 修复
- **目标**：修复题库导入结果丢显示、清除记住密码无效、二维码切 Tab 僵尸轮询、数字框清空变 0 与 URL 校验缺锚点。
- **涉及文件**：
  - `gui/src/components/SettingsView.jsx`（L305-332、L731-817、L134-143）
  - `gui/src/components/DesktopParity.test.jsx`（L191、L244-247）
  - `gui/src/App.jsx`（L718-731、L150-181、L545-582）
  - `gui/src/components/LoginView.jsx`（L318-429、L536-560、L646-659）
  - `gui/src/lib/configModel.js`（L4-5、L123-133）
- **改动方案**：
  1. `SettingsView.jsx:handleImportCache` 改为读取 `result.zhidao_questions` 与 `result.ai_questions`（同步修正 `DesktopParity.test.jsx` mock）。
  2. `App.jsx` 给 `LoginView` 传入 `onClearSavedLogin={handleClearSavedLogin}`（调用 `tauriApi.clearSavedLogin(mode)` 并 `setRuntime(rt)`）；`LoginView.jsx:handleClearRemembered` 补上 `catch (err) { setError(...); }`。
  3. `LoginView.jsx` 增加 `qrAttemptIdRef` 与 `modeRef`：`startInAppQrLogin` 返回时若 `!mountedRef.current || qrAttemptIdRef.current !== attempt || modeRef.current !== 'qrcode'` 立即调用 `tauriApi.cancelQrLogin(res.sessionId)` 并退出；`cancelActiveQrSession` 递增 `qrAttemptIdRef.current`。
  4. `SettingsView.jsx` L731-817 全部 8 个数值框替换为现有 `NumberInput` 组件，并通过 validity 状态阻止非法中间态触发自动保存。
  5. `SettingsView.jsx:handleOpenLog` 增加 `logReadSeqRef` 防止慢请求覆盖新选中的日志。
  6. `configModel.js`：`isValidProxyUrl` / `isValidLlmApiBase` 改用 `new URL(trimmed)` 校验协议、非空 `hostname`、无空白字符与有效端口。
- **验收**：更新并新增 Vitest 单测覆盖上述 6 项修复，`npm --prefix gui test && npm --prefix gui run build` 通过。

### T1.9 发布流水线与便携打包脚本加固
- **目标**：封堵版本不一致发布、Apple Silicon 损坏签名与旧二进制打包风险。
- **涉及文件**：
  - `.github/workflows/build-release.yml`（L131-158、L292-296、L335、L403-407）
  - `gui/build-portable.ps1`（L51-55、L83-86）
  - `pyproject.toml`（固定 pyinstaller 版本或加入 `[dependency-groups] build`）
- **改动方案**：
  1. 抽前置 `validate-version` job（所有平台构建 job 均 `needs: [validate-version, test]`），强校验 `modules/version.py` (`__gui_version__`)、`gui/src-tauri/tauri.conf.json`、`gui/src-tauri/Cargo.toml`、`gui/package.json` 与 git tag 完全一致。
  2. `build-release.yml` L335 移除 `codesign ... || true`，改为失败即中断并紧接 `codesign --verify --deep --strict "$APP_BUNDLE"`。
  3. `gui/build-portable.ps1` 统一按 `Autovisor GUI.exe` 优先检查与拷贝（或严格匹配 `tauri.conf.json` 的 `mainBinaryName`）。
  4. 锁定 CI 与打包脚本中的 `pyinstaller` 版本（如 `pyinstaller==6.x.y`）。
- **验收**：静态检查 YAML 语法；本地跑 `pwsh -NoProfile -Command "& { ... }"` 验证版本校验脚本与便携脚本路径解析逻辑。

---

## Phase 2：并发稳定性与性能治理

### T2.1 `backend_server.py` 任务取消代际隔离与统一关停生命周期
- **涉及文件**：`backend_server.py`（`TaskManager` L645-1009、`solve_captcha` L765-780、`shutdown` L749-763、`main` L1663-1666）、`tests/test_backend_server.py`
- **改动方案**：
  1. 增加 `status = "stopping"`；`start_task()` 拒绝在 `self._active_future is not None and not self._active_future.done()` 时启动新任务；`_execute_task` 的 `finally` 块仅在 `self._run_id == run_id` 时清理 `logger`/`progress` sink 与状态。
  2. 为 `asyncio.to_thread` 同步工作器（`ExamRunner`、`ZhidaoHomeworkWorker`、`ZhidaoExamWorker`）透传 `cancel_event: threading.Event`，每题循环检查提前退出。
  3. `solve_captcha()` 超时捕获后立即 `fut.cancel()` 并在 `_solve_captcha_via_cdp` 内用 `asyncio.wait_for` 确保释放 CDP 资源。
  4. 合并 `/api/shutdown`、`_watch_parent_stdin`、`main() finally` 为单一幂等 `BackendHTTPServer.graceful_shutdown()`（确保关闭 `qr_manager`、cancel 残留任务、join `_loop_thread` 并关闭 `_loop`）。
  5. `/api/question-bank/test` 补 `try/except`，统一错误码：`ValueError -> 400`，本地 SQLite 错误 `-> 500`，并在 `BackendHTTPServer` 上复用 `AiQuestionStore` 实例。

### T2.2 Rust `BackendManager` 与 `ZhsSessionManager` 并发安全与去主线程阻塞
- **涉及文件**：`gui/src-tauri/src/backend.rs`、`gui/src-tauri/src/zhs_session.rs`、`gui/src-tauri/src/lib.rs`、`gui/src-tauri/src/process_tree.rs`
- **改动方案**：
  1. 新增 `fn lock_unpoisoned<T>(m: &Mutex<T>) -> MutexGuard<'_, T>` 替换 `backend.rs`（16 处）与 `zhs_session.rs`（5 处）的全部 `.lock().unwrap()`。
  2. `BackendManager` 增加 `lifecycle_lock: Mutex<()>` 串行化 `spawn_and_connect`/`restart`/`terminate_current_process`；错误路径清理校验 `generation`；`handle_sse_disconnect` 先安全终止并回收旧子进程；`ensure_ready` 用 `Condvar` 替代 120ms 忙等；`drain_stderr` 改为追加/轮转而非重启截断。
  3. `zhs_session.rs`：`launch_cdp_browser` 在 `job.assign` 失败分支调用 `child.kill(); child.wait();`；`ZhsSessionManager` 增加 `session_id: u64` 代际校验，防止前一次登录 `finish()` 误杀后一次登录的浏览器。
  4. `process_tree.rs`（Unix）：先 `try_wait()` 再检查进程组存活，避免对已退出的僵尸进程空等 350ms + `SIGKILL`。
  5. `lib.rs`：`export_question_cache`/`import_question_cache` 的文件对话框与磁盘读写移入 `spawn_blocking`（或 `AsyncFileDialog`）；`on_window_event` 与托盘退出将 `cleanup_before_exit` 放到独立线程执行完成后再 `app.exit(0)`，消除 5 秒 UI 假死；删除 `start_autovisor` 中无意义的读后原样写回与 `save_config_to_dir` 的冗余重读。

### T2.3 端到端 SSE 增量/节流推送 + React 渲染隔离
- **涉及文件**：`backend_server.py`（L224-291、L1453-1460）、`gui/src-tauri/src/backend.rs`（L744-755）、`gui/src/App.jsx`、`gui/src/components/ProgressView.jsx`、`gui/src/components/ExecutionLog.jsx`、`gui/src/components/WorkspaceView.jsx`
- **改动方案**：
  1. **后端 SSE 优化**：SSE 建立时发送 1 次全量快照（`event: snapshot`）；运行期间改为发轻量增量事件（`event: log`、`event: state`）或在 150ms 合并窗口内节流广播，且 `json.dumps` 移到 `self._lock` 外执行。
  2. **Rust 透传**：支持转发增量事件或维持兼容的合并快照结构。
  3. **React 渲染优化**：
     - `ProgressView.jsx` 将 1Hz `nowTick` 计时器下沉到独立叶组件 `ElapsedTimer`；hoist `const EMPTY_LOGS = [];`；直接传 `onOpenLogFile={onOpenPath}` 恢复 `memo(ExecutionLog)` 生效。
     - `App.jsx` 将高频变化的 `runState` 移入独立 `useSyncExternalStore` / Context，使 `SettingsView` / `WorkspaceView` 仅订阅 `taskRunning` / `shutdownScheduled` 布尔值，并用 `React.memo` 包裹各视图；仅在 `running -> non-running` 跃迁时调用 `getRuntimeInfo()`，修复 `import_cookies` 双次递增 `cookieImportState.nonce`。
     - `WorkspaceView.jsx` L389 用 `useMemo` 预建 `courseId -> index` `Map` 消除 $O(N^2)$ `findIndex`。

### T2.4 `llm.py` 流式可靠性与连接池优化
- **涉及文件**：`modules/llm.py`（L403-406、L620、L808、L868-1058、L1183-1206、L1410-1459）、`modules/quiz_page.py`（L582-729）、`tests/test_llm.py`
- **改动方案**：
  1. **修复 `<think>` 误截断**：流式消费中增量追踪是否处于 `<think>...</think>` 内部，**仅对思考块之外的正文**做 `_has_aim_block` 提前结束判断；`_strip_think_tags` 兼容未闭合的截断 `<think>...`。
  2. **修复 $O(N^2)$ 拼接与关流竞争**：改为 `parts: list[str]` 累加 + 增量尾窗匹配；使用可复用线程安全 `requests.Session`（或切换 `httpx.Client`），避免每次请求新建连接池；对 `to_indexes()` 的 `list[int]` 路径补充 `0 <= idx < len(options)` 越界校验，收紧短选项子串误匹配，`_parse_quiz_questions` 改用 `json.JSONDecoder().raw_decode()`。
  3. **`quiz_page.py` 降本**：优先提取测验主容器文本而非全页 `body[:60000]`，并对同一页面签名缓存已求解答案避免重试/复核重复调 LLM。

### T2.5 `logger.py` I/O 性能、线程安全与全通道脱敏
- **涉及文件**：`modules/logger.py`、`Autovisor.py`（L1866）、`tests/test_logger.py`
- **改动方案**：
  1. 在 `info`/`warn`/`error`/`debug`/`event`/`context` 入口统一执行 `redact_sensitive()`，确保控制台与 SSE/HTTP `/api/state` 同等脱敏；脱敏 `Autovisor.py` 打印的 `sys.argv`（如 `--proxy` 密码）。
  2. 保持单一持久化文件句柄 `self._file` + 增量字节计数，仅在 `WARN`/`ERROR`/`EVENT` 或轮转/关闭时 flush，避免每行 `open/write/flush/close/getsize`。
  3. `_context`、`_throttled`、`configure()` 全部纳入 `self._write_lock` 保护，配置字段改为实例属性而非修改类属性。
  4. 修复 `summarize_exception()` 在 `str(exc) == ""` 时的 `IndexError`。
  5. 合并 `notifier.py` 与 `logger.py` 脱敏逻辑到共享 `modules/redaction.py`，预编译正则并修复 `notifier.py` L78/L83 贪婪 `re.S` `.*` 误删多行正文的问题。

### T2.6 异步阻塞、HTTP 会话泄漏与安全收口
- **涉及文件**：`modules/slider.py`、`modules/tencent_slider.py`、`modules/click_captcha.py`、`modules/zhs_api.py`、`modules/exam_ctx.py`、`modules/homework.py`、`modules/question_bank.py`、`modules/course_runner.py`、`modules/tasks.py`、`modules/updater.py`、`Autovisor.py`
- **改动方案**：
  1. 验证码 OpenCV/PIL 图像处理（`progress_img`、`locate_gap`、`find_click_point`）统一用 `await asyncio.to_thread(...)` 执行；`window_control.py` 的 Win32 `EnumWindows` 同样放入 `to_thread`。
  2. `ZhsApiClient`、`QuestionBankClient`、`Notifier` 实现 `close()` + 上下文管理并在全部调用点用 `with`/`finally` 关闭；将 `ZhsApiClient.lock` 缩小到仅保护 Cookie/SSO 状态，给 `ZhidaoAnswerer`/`AiAnalysisApi` 分配独立 Session（克隆 Cookie），解除 60s AI 阻塞 10s 考试心跳的问题。
  3. `exam_ctx.py`：`AiExamBase._on_answered()` 统一统计 `_failed_saves`（覆盖 `AiHomeworkCtx`）；非幂等 `submit_exam` 移出 `urllib3.Retry(POST)` 并消除零退避递归重试风暴；`open_exam` 防重复拉起心跳线程并在退出时 join。
  4. `Autovisor.py`：`try:` 紧贴 `create_browser_session()` 之后；用 `set[asyncio.Task]` 追踪 `_on_new_page` 任务并在 `finally` 中解绑监听与取消；`tasks.py:task_monitor` 增加 `if task.cancelled(): continue`；`course_runner.py` 用单次 `evaluate_all` 批量拉取目录状态。
  5. `updater.py`：`is_trusted_url()` 强校验仓库路径前缀 `/{REPO}/releases/`；移除静默 `verify=False` 自动降级；为 `_API_RATE_LIMIT_UNTIL` 加 `threading.Lock`。
  6. `question_bank.py`：记录日志前对 URL/异常中的 `token=` 脱敏。

---

## Phase 3：跨端单一真相源（SSOT）与领域模块去重

> [!IMPORTANT]
> **Phase 3 前置架构决策（建议下个对话开始前确认，若无异议默认采用推荐方案）**
> - **配置真相源选择**：
>   - **推荐方案 A（Rust `config_ini` 为权威源）**：保持 GUI 未启动 Python 后端时即可秒开并读写设置的优势；对外提供明确 JSON Schema，并统一修复 Python `configs.py` 与前端 `configModel.js` 对齐其解析规则（布尔值、数值类型、模板键、校验规则）。
>   - **备选方案 B（Python `/api/config` 为权威源）**：彻底删除 `config_ini.rs` 1,364 行，但 GUI 启动需等待 Python 后端就绪后才能加载设置页。

### T3.1 课程解析下沉 Python（消除 Rust ~550 行重复）
- **涉及文件**：`gui/src-tauri/src/zhs_session.rs`（L816-1372）、`modules/course_fetcher.py`、`backend_server.py`
- **改动方案**：
  1. 在 `backend_server.py` 暴露内部接口 `/api/courses/extract`（接收 Rust CDP 捕获的原始 JSON 响应列表 / Cookie），直接调用 `course_fetcher.py` 的解析、hostname 校验、去重与分类逻辑。
  2. 删除 `zhs_session.rs` 中重复的 16 字段课程解析、泛型标题表、AI/Fusion/Hike 分类（含 `"startinng"` 拼写错误）与进度格式化逻辑。
  3. 修复 `course_fetcher.py` L948-1003「未先尝试直拉 API 就强制等 6.2s」的问题，及 `append_courses_to_config` 先 `normalize_course_url` 再去重。

### T3.2 统一三端配置与 LLM 厂商契约
- **涉及文件**：`gui/src-tauri/src/config_ini.rs`、`modules/configs.py`、`modules/llm.py`、`gui/src/lib/configModel.js`、`gui/src/lib/llmProviders.js`、`docs/ipc_contracts.md`（新建）
- **改动方案**：
  1. 解决 7 处已确认漂移：统一布尔解析（`true`/`1`/`yes`）、日志参数数值类型、泛型标题表、课程 URL 类型识别（补齐 `/dohomework/`、`/doexam/`、`/testdetail/`）、`CONFIG_TEMPLATE` 缺失键、决定 `forceRedoAnswered` 是否在 GUI 暴露开关。
  2. 修复 `config_ini.rs` INI 渲染过滤 `\r`/`\n`（防 INI 注入）与不对密码 `.trim()`；合并 `LlmConfig`/`LlmDiskConfig` 重复定义；`configs.py` 对 `_reload_if_changed()` 增加 2 秒节流避免热路径高频 `stat()`；统一三处路径解析到 `modules/paths.py`。
  3. 将 LLM 厂商预设表收敛为一份共享 JSON（打包进资源或构建期同步校验），并编写 `docs/ipc_contracts.md` 固化全部 39 个 Tauri Command、事件载荷与 Python `/api/*` 契约。

### T3.3 抽离 5 套答题引擎公共基础设施
- **涉及文件**：新建 `modules/text_utils.py`、`modules/answer_parser.py`、`modules/answer_service.py`、`modules/quiz_dom.py`；精简 `answer_store.py`、`ai_smart_mastery.py`、`exam_ctx.py`、`zhidao_homework_worker.py`、`homework.py`、`quiz_page.py`、`ai_smart.py`
- **改动方案**：
  1. `text_utils.py`：统一 `normalize_text`、`compact_text`、`brief`、`strip_think_tags`、`cache_key`、`loose_key`、`similarity`、`parse_percent`。
  2. `answer_parser.py`：统一 ```` ```answer ```` 块提取、JSON/字母/填空解析与 `QuestionType` 枚举。
  3. `answer_service.py`：统一 `QuestionBankClient` 懒加载/查询统计、`LlmCircuitBreaker`（`_llm_disabled`/`_llm_warned`）与多级回退策略。
  4. `quiz_dom.py`：抽离共享的 Playwright 按钮点击、弹窗确认与选项状态探测；全库替换 `playwright._impl._errors` 私有导入为 `playwright.async_api`；`ExamApi` 从继承 `ZhsApiClient` 改为组合，消除 LSP 违规。

### T3.4 抽离验证码公共层与打破循环导入
- **涉及文件**：新建 `modules/captcha_common.py`、`modules/captcha_router.py`；重构 `modules/slider.py`、`modules/tencent_slider.py`、`modules/click_captcha.py`
- **改动方案**：
  1. 公共层统一 `_iter_targets`、`_visible_locator`、代理感知的图片下载（修复 `slider.py:62` 与 `tencent_slider.py:415` 绕过代理）、坐标换算与人类拖拽轨迹。
  2. 消除 `slider.py` 中的 `global cv2, np` 全局变量与三文件互相懒导入；为 `click_captcha.py` 补充 Linux 字体（`DejaVuSans-Bold` / `NotoSans`）回退。

### T3.5 缓存层收口为 SQLite 单一真相源
- **涉及文件**：`modules/exam_ctx.py`（`AnswerBank`）、`modules/ai_smart_mastery.py`（`AnswerCache`）、`modules/answer_store.py`
- **改动方案**：让 `AnswerBank` / `AnswerCache` 读写均直连 `AiQuestionStore`（SQLite），取消每答一题就全量重写 JSON 文件的做法；保留 JSON 仅用于显式导入/导出接口与旧数据一次性迁移。

---

## Phase 4：巨型文件拆分、凭据加密与工程护栏

### T4.1 Python 巨型模块拆解
- `Autovisor.py` → `modules/runtime_bootstrap.py`（与 `backend_server.py` 共享）、`modules/auth_flow.py`、`modules/stages/ai_smart_stage.py`、`modules/stages/zhidao_assessment.py`（合并作业/考试 180 行重复循环）；用 `RunContext` 替换模块级全局状态，删除死代码（`event_loop_verify`/`event_loop_answer`/`ZHS_COOKIE_URLS`/`init_page`）。
- `backend_server.py` → `modules/backend/{task_manager,state_reducer,http_routes,watchdog}.py`（335 行中文 `if/elif` 改为事件常量分发）。
- `llm.py` → `modules/llm_config.py`、`modules/llm_client.py`（`test_connection` 复用 `_chat_sync` 协议适配器）；将 `answer_with_llm` 移至测验模块。
- `exam_ctx.py` → `modules/exam_api.py`、`modules/zhidao_ai.py`、`modules/ai_exam_runner.py`。
- `popups.py` → 580 行内联 JS 抽出至 `resources/scripts/dismiss_popups.js` 并优化每 1.5s 全页 `getComputedStyle` 扫描（仅在精准弹窗选择器未命中时降级触发）。
- `course_fetcher.py` → 303 行 `fetch_courses_via_api` 拆为 4 个数据源子函数。

### T4.2 Rust 模块拆解与异步 HTTP 化
- `zhs_session.rs` → `src/zhs/{mod,cdp,cookies,schools}.rs` + `resources/scripts/zhs_login_autofill.js`（`include_str!`）。
- `config_ini.rs` → `src/config/{model,ini,llm,storage}.rs`。
- `lib.rs` → `src/commands/{auth,config,tasks,diagnostics,system}.rs`，引入 `thiserror` `AppError` 替换裸 `String` 错误；`Cargo.toml` 为 `reqwest` 启用异步客户端替代 ~20 处 `spawn_blocking`，Release Profile 开启 `lto = "thin"`；`runtime.rs` 移除硬编码 UTC+8（改传 epoch 由前端格式化）并修复 `read_log_tail` UTF-8 截断边界。

### T4.3 React 前端组件拆解与 UX 清理
- `SettingsView.jsx`（1,864 行）→ 拆为 8 个独立设置卡片子组件，并把误放在「答题增强与题库」卡片中的日志轮转控件移回「历史运行日志」卡片，消除调试日志双次 `setConfig`。
- `LoginView.jsx`（1,234 行）→ `PhoneLoginForm`、`StudentLoginForm`、`QrLoginPanel`（+ `useQrLogin` hook）、`LoginEnvBar`。
- `WorkspaceView.jsx`（951 行）→ `CourseQueuePanel`、`ManualCourseAddForm`、`StudyParamsSidebar`。
- `App.jsx`（1,078 行）→ 抽出 `useAutoSaveConfig`、`useRunStateStream`、`useUpdateChecker` hooks，去除硬编码版本回退字串（改由 Vite `define` 从 `package.json` 注入）。

### T4.4 凭据加密与隐私加固
- 在 Rust 层引入 Windows DPAPI / `keyring` 加密存储记住的手机号/学号密码（Unix 文件设 `0o600`），`get_runtime_info` 改为只返回 `hasSavedPhonePassword` / `hasSavedStudentPassword` 布尔值，不再向 WebView 周期性下发明文密码。

### T4.5 测试补盲、构建去重与 CI 门禁
- Python 新增独立单测套件：`tests/test_ai_smart_mastery.py`、`tests/test_quiz_page.py`、`tests/test_zhidao_exam_api.py`。
- 前端新增单测：`NumberInput.test.jsx`、`llmProviders.test.js`、`LoginView.test.jsx`（二维码状态机 + 清除记住）、`ProgressView.test.jsx`、`ExecutionLog.test.jsx`、`WorkspaceView.test.jsx`。
- 抽出 `scripts/build_common.py` 去重 `build.py` 与 `build_backend.py`（缺少 `python3.dll` / 资源时 fail fast）；清理 `pyproject.toml` 空的 `captcha = []` extra；在 `desktop-ci.yml` 合并 `linux-tests.yml` 并加入 `cargo clippy -- -D warnings` 与 ESLint 检查。

---

## 下个对话快速启动提示词（按 Phase 逐个执行，可直接复制）

### Phase 1（止血修复：安全 / 丢数据 / 必现 Bug / 发布门禁）
> 请阅读 `Autovisor/docs/refactor_plan.md`，按顺序执行 **Phase 1（T1.1 ~ T1.9）**。先检查当前代码最新位置（不要盲信文档行号），每完成一个任务项就补齐对应单测、勾选 plan 中的 checkbox，全部完成后运行文档第 0 节的三端回归测试并汇总改动与测试结果。

### Phase 2（并发稳定性与性能治理）
> 请阅读 `Autovisor/docs/refactor_plan.md`，确认 Phase 1 已完成后，按顺序执行 **Phase 2（T2.1 ~ T2.6）**。重点注意 `T2.3` 的 SSE 增量/节流推送前后端协议兼容，每完成一个任务项跑对应单测并更新 checkbox，全部完成后执行三端全量回归测试并汇报结果。

### Phase 3（跨端单一真相源与领域模块去重）
> 请阅读 `Autovisor/docs/refactor_plan.md`，确认 Phase 1~2 已完成后，采用推荐的 **方案 A（Rust `config_ini` 为配置权威源）**，按顺序执行 **Phase 3（T3.1 ~ T3.5）**。注意保持旧版 `config.ini` / `llm.json` / `cookies.json` / `answers.db` 兼容，每完成一个任务项更新 checkbox 并验证对应测试，最后补齐 `docs/ipc_contracts.md` 并跑完三端回归测试。

### Phase 4（巨型文件拆分、凭据加密与工程护栏）
> 请阅读 `Autovisor/docs/refactor_plan.md`，确认 Phase 1~3 已完成后，按顺序执行 **Phase 4（T4.1 ~ T4.5）**。拆分巨型文件时保持对外导出符号与既有行为不变，优先完成结构拆分再补齐 `T4.5` 的单测盲区与 CI 门禁，每项完成后更新 checkbox，最后运行三端全量测试、前端构建以及 `cargo clippy` 验证。
