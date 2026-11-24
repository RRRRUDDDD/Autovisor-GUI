# Autovisor 跨端 IPC 与配置契约文档（Single Source of Truth）

> 生效版本：Phase 3（方案 A：Rust `config_ini.rs` 为配置权威源，Python `course_fetcher.py` 为课程解析权威源，`resources/llm_providers.json` 为大模型厂商预设权威源）。
> 适用范围：React WebView (`gui/src`) ↔ Tauri Rust 宿主 (`gui/src-tauri/src`) ↔ Python 本地无感后端 (`backend_server.py`)。

---

## 1. 配置权威源与持久化文件契约

### 1.1 方案 A 权威源分工

- **桌面端读写权威源**：`gui/src-tauri/src/config_ini.rs`（`AutovisorConfig`、`LlmConfig`、`CourseItem`）。未启动 Python 后端时即可独立读写。
- **Python 运行时消费端**：`modules/configs.py` 与 `modules/llm.py`，严格对齐 Rust 的布尔解析（`true`/`1`/`yes` 与 `false`/`0`/`no`）、数值类型与上下界、`CONFIG_TEMPLATE` 键集。
- **前端视图规范化层**：`gui/src/lib/configModel.js`，负责表单校验与与 Rust `camelCase` 字段映射。
- **共享静态目录**：`resources/llm_providers.json`，由 Rust (`include_str!`)、React (ES JSON import)、Python (`modules/paths.py:get_resource_path`) 三端共享。

### 1.2 磁盘文件与向后兼容约束

| 文件路径（相对 `data_dir`） | 格式 | 读写方 | 向后兼容约定 |
| --- | --- | --- | --- |
| `config.ini` | INI (UTF-8 / GBK 兼容读取，UTF-8 CRLF 写出) | Rust 写，Rust/Python 读 | 过滤 `\r`/`\n`/`\0` 防 INI 注入；`password` 不做 `.trim()` 保留首尾空格；缺失段时报错不覆写 |
| `data/llm.json` | JSON (`snake_case`) | Rust 写，Rust/Python 读 | 兼容旧版 4 字段（`api_key`/`api_base`/`model`/`auto_submit`），缺失 `provider`/`api_format` 按 `resources/llm_providers.json` 自动推断 |
| `data/gui_courses.json` | JSON (`CourseItem[]`, `camelCase`) | Rust 读写 | 与 `config.ini [course-url]` 合并保留用户手动取消勾选与自定义标题 |
| `data/mirrors.json` | JSON (`{ name: url }`) | Rust 写，Rust/Python 读 | 默认从内置资源初始化 |
| `data/cookies.json` | JSON (`PlaywrightCookie[]`) | Rust/Python 原子写 | Unix 强制 `0o600`；加载时过滤过期与非 `*.zhihuishu.com` 域名 |
| `data/gui_prefs.json` | JSON (`GuiPrefs`) | Rust 锁内事务式读写 (`update_prefs`) | 损坏时备份为 `.corrupt.<ts>` 并报错，禁止静默清空；不再持久化或向 WebView 暴露明文密码（仅返回 `hasSavedPhonePassword` / `hasSavedStudentPassword`） |
| `data/credentials.json` | 加密凭据存储（Windows DPAPI / macOS Keychain / Linux Secret Service，无密钥链时降级 `0o600` 文件） | Rust `credentials.rs` | 首次加载自动迁移旧版 `gui_prefs.json` 明文密码并清除原明文字段；登录时可通过 `LoginRequest.useSavedPassword` 在 Rust 侧解密使用 |
| `data/questions_bank.db` | SQLite (WAL, `synchronous=NORMAL`) | Python `AiQuestionStore` | 统一题库单点数据源；首次自动迁移旧 `data/answers.db` / `data/answer_cache.json` / `data/exams/*/answers.json` |

### 1.3 `AutovisorConfig` JSON Schema（Tauri IPC `camelCase`）

| 字段 | 类型 | 默认值 | `config.ini` 对应段/键 | 边界 / 规则 |
| --- | --- | --- | --- | --- |
| `username` | `string` | `""` | `[user-account] username` | 渲染时 strip `\r\n\0` + trim |
| `password` | `string` | `""` | `[user-account] password` | 渲染时 strip `\r\n\0`，**保留首尾空格** |
| `qrLogin` / `qrTerminal` / `qrUnicode` | `boolean` | `false` | `[user-account] qrLogin/qrTerminal/qrUnicode` | `true/1/yes` ↔ `false/0/no` |
| `qrSaveDir` | `string` | `""` | `[user-account] qrSaveDir` | 目录路径 |
| `driver` | `"Edge" \| "Chrome" \| "Chromium"` | `"Edge"` | `[browser-option] driver` | 大小写不敏感归一化 |
| `exePath` | `string` | `""` | `[browser-option] EXE_PATH` | 留空自动探测本机浏览器 |
| `proxy` | `string` | `""` | `[browser-option] proxy` | 仅允许 `http://` `https://` `socks5://` `socks5h://` |
| `attachExistingChrome` | `boolean` | `false` | `[browser-option] attachExistingChrome` | 连接已开启调试端口的 Chrome |
| `cdpUrl` | `string` | `"http://127.0.0.1:9222"` | `[browser-option] cdpUrl` | 远程调试地址 |
| `enableAutoCaptcha` | `boolean` | `true` | `[script-option] enableAutoCaptcha` | 自动滑块/点选验证 |
| `enableHideWindow` | `boolean` | `true` | `[script-option] enableHideWindow` | 启动后隐藏浏览器窗口 |
| `shutdownOnComplete` | `boolean` | `false` | `[script-option] shutdownOnComplete` | 正常完成/达限时倒计时关机 |
| `enableDebugLog` | `boolean` | `false` | `[script-option] enableDebugLog` | 实时输出 DEBUG 日志 |
| `logMaxSizeMb` | `integer` | `10` | `[script-option] log_max_size_mb` | `>= 1` |
| `logRetentionDays` | `integer` | `14` | `[script-option] log_retention_days` | `>= 0`（`0` 不按天清理） |
| `logMaxFiles` | `integer` | `200` | `[script-option] log_max_files` | `>= 1` |
| `logCompressAfterDays` | `integer` | `3` | `[script-option] log_compress_after_days` | `>= 0`（`0` 不压缩） |
| `notificationProvider` | `"" \| "Windows" \| "ServerChan" \| "Qmsg" \| "Bark" \| "Telegram"` | `"Windows"` | `[notification] provider` | 显式留空禁用；兼容旧 `barkUrl` |
| `notificationUrl` | `string` | `""` | `[notification] url` | 远程推送地址 |
| `notificationTgChatId` | `string` | `""` | `[notification] tg_chat_id` | Telegram Chat ID |
| `notifyOnVerify` / `notifyOnFinish` / `notifyOnError` | `boolean` | `true` | `[notification] notifyOn*` | 事件通知开关 |
| `limitMaxTime` | `number` | `0.0` | `[course-option] limitMaxTime` | `>= 0`（分钟，`0` 不限） |
| `limitSpeed` | `number` | `1.0` | `[course-option] limitSpeed` | `[0.5, 1.8]`，1 位小数 |
| `soundOff` | `boolean` | `true` | `[course-option] soundOff` | 静音播放 |
| `reviewMode` | `boolean` | `false` | `[course-option] reviewMode` | 复习模式 / 习惯分 |
| `enableExamApi` | `boolean` | `true` | `[course-option] enableExamApi` | AI 智慧课接口考试 |
| `enableReviewExamApi` | `boolean` | `false` | `[course-option] enableReviewExamApi` | 复习/模拟考接口作答 |
| `enableZhidaoQuiz` | `boolean` | `true` | `[course-option] enableZhidaoQuiz` | 课中弹题智慧树 AI |
| `enableMastery` | `boolean` | `true` | `[course-option] enableMastery` | 知识点掌握度刷题 |
| `masteryTarget` | `number` | `100.0` | `[course-option] masteryTarget` | `[1, 100]` |
| `enablePptReference` | `boolean` | `true` | `[course-option] enablePptReference` | PPT 课件文本参考 |
| `enablePopupIntercept` | `boolean` | `true` | `[course-option] enablePopupIntercept` | 弹题响应只读拦截 |
| `enableHomeworkApi` | `boolean` | `true` | `[course-option] enableHomeworkApi` | 知到作业加密 API |
| `homeworkThreshold` | `number` | `100.0` | `[course-option] homeworkThreshold` | `[0, 100]` |
| `homeworkMaxRedo` | `integer` | `3` | `[course-option] homeworkMaxRedo` | `>= 0` |
| `enableFormalExamApi` | `boolean` | `true` | `[course-option] enableFormalExamApi` | 知到正式考试加密 API |
| `forceRedoAnswered` | `boolean` | `false` | `[course-option] forceRedoAnswered` | 强制重做已答题目 |
| `llm` | `LlmConfig` | 见下 | `data/llm.json` | 12 字段对象 |
| `courses` | `CourseItem[]` | `[]` | `[course-url]` + `data/gui_courses.json` | 已勾选的 https URL 写入 INI |
| `mirrors` | `Record<string, string>` | 4 镜像源 | `data/mirrors.json` | PyPI 镜像表 |

### 1.4 `LlmConfig` 字段映射（IPC `camelCase` ↔ 磁盘 `snake_case`）

`provider`, `apiFormat`/`api_format`, `apiKey`/`api_key`, `apiBase`/`api_base`, `model`, `visionModel`/`vision_model`, `apiVersion`/`api_version`, `autoSubmit`/`auto_submit`, `streamTimeout`/`stream_timeout` (`>0`, 默认 `120`), `streamIdleTimeout`/`stream_idle_timeout` (`>0` 且 `<= streamTimeout`, 默认 `30`), `questionBankToken`/`question_bank_token`, `questionBankUrl`/`question_bank_url` (默认 `https://tk.enncy.cn/query`)。

---

## 2. Tauri Commands 契约（共 39 个）

全部注册于 `gui/src-tauri/src/lib.rs` 的 `tauri::generate_handler![...]`，前端通过 `gui/src/lib/tauriApi.js` 调用（`withGlobalTauri: false`，仅走 `@tauri-apps/api/core`）。

### 2.1 运行时、后端与窗口生命周期（8 个）

| # | Command | 参数 | 返回值 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `get_runtime_info` | `refreshBrowsers?: boolean` | `RuntimeInfo` | 返回路径、版本、已探测浏览器、Cookie/会话/记住登录态、关闭行为、后端状态 |
| 2 | `restart_backend` | 无 | `Result<RuntimeInfo, String>` | 重启托管 Python 后端并重建 SSE |
| 3 | `set_close_behavior` | `behavior: "ask" \| "minimize" \| "tray" \| "exit"` | `Result<RuntimeInfo, String>` | 持久化窗口关闭偏好 |
| 4 | `close_prompt_shown` | `promptId: number` | `boolean` | 确认前端已弹出关闭确认框，防止超时强退 |
| 5 | `close_choice` | `action: "cancel" \| "minimize" \| "tray" \| "exit"`, `remember: boolean` | `Result<RuntimeInfo, String>` | 执行用户选择的关闭动作 |
| 6 | `check_for_update` | `proxy: string` | `Result<UpdateInfo, String>` | 代理转发至 Python `/api/update/check` |
| 7 | `open_path` | `target: string` | `Result<(), String>` | 仅允许 `data_dir` / `logs` 内目录或 `.log/.txt/.ini/.json`，拒绝 UNC 与可执行文件 |
| 8 | `open_url` | `url: string` | `Result<(), String>` | 严格 `url::Url` 校验，仅允许 `http`/`https`，Windows 走 `ShellExecuteW` |

### 2.2 登录、会话与课程同步（9 个）

| # | Command | 参数 | 返回值 | 说明 |
| --- | --- | --- | --- | --- |
| 9 | `search_schools` | `query: string`, `proxy?: string` | `Result<SchoolItem[], String>` | 查询并缓存智慧树学校列表 |
| 10 | `zhs_login` | `request: LoginRequest` | `Result<SessionResult, String>` | CDP 浏览器登录 / Cookie 刷新并调用 Python 提取课程 |
| 11 | `cancel_zhs_login` | 无 | `()` | 取消进行中的 CDP 登录会话并清理浏览器进程树 |
| 12 | `refresh_courses` | 无 | `Result<SessionResult, String>` | Cookie 模式快速同步课程列表 |
| 13 | `logout_zhs` | 无 | `Result<RuntimeInfo, String>` | 取消登录、删除 `cookies.json` 并清空会话 |
| 14 | `clear_saved_login` | `mode?: "phone" \| "student"` | `Result<RuntimeInfo, String>` | 清除记住的手机号/学号凭据（省略清除全部） |
| 15 | `clear_cookies` | 无 | `Result<RuntimeInfo, String>` | 删除 `cookies.json` 并清空会话信息 |
| 16 | `start_qr_login` | `proxy: string`, `saveDir?: string` | `Result<QrStartResp, String>` | 转发 Python `/api/qr-login/start` |
| 17 | `poll_qr_login` | `sessionId: string` | `Result<QrPollResp, String>` | 转发 Python `/api/qr-login/poll` |
| 18 | `save_qr_image` | `sessionId: string`, `saveDir: string` | `Result<{ ok, savedPath }, String>` | 转发 Python `/api/qr-login/save` |
| 19 | `cancel_qr_login` | `sessionId: string` | `Result<(), String>` | 转发 Python `/api/qr-login/cancel` |

> 注：以上登录与扫码类共 11 个（#9~#19）。

### 2.3 配置与文件选择器（5 个）

| # | Command | 参数 | 返回值 | 说明 |
| --- | --- | --- | --- | --- |
| 20 | `load_config` | 无 | `Result<AutovisorConfig, String>` | 读取 `config.ini` + `gui_courses.json` + `mirrors.json` + `llm.json` |
| 21 | `save_config` | `config: AutovisorConfig` | `Result<AutovisorConfig, String>` | 原子写入配置文件并返回内存规范化副本 |
| 22 | `pick_browser_exe` | 无 | `Option<string>` | 系统文件选择对话框（浏览器可执行文件） |
| 23 | `pick_cookie_file` | 无 | `Option<string>` | 系统文件选择对话框（Cookie JSON） |
| 24 | `pick_directory` | 无 | `Option<string>` | 系统文件夹选择对话框 |

### 2.4 任务控制、日志与诊断（8 个）

| # | Command | 参数 | 返回值 | 说明 |
| --- | --- | --- | --- | --- |
| 25 | `get_run_state` | 无 | `RunState` | 获取当前任务快照与日志环形缓存 |
| 26 | `start_autovisor` | 无 | `Result<RunState, String>` | 校验至少勾选 1 门课后触发 Python `/api/tasks/start` (`mode="study"`) |
| 27 | `stop_autovisor` | 无 | `Result<RunState, String>` | 触发 Python `/api/tasks/stop` |
| 28 | `cancel_shutdown` | 无 | `Result<RunState, String>` | 取消自动关机倒计时（`/api/tasks/cancel-shutdown`） |
| 29 | `run_diagnostic` | `kind: "check_browser" \| "check_course" \| "import_cookies"`, `target: string` | `Result<RunState, String>` | 启动诊断或 Cookie 导入任务 |
| 30 | `set_debug_log` | `enabled: boolean` | `Result<boolean, String>` | 动态切换运行中调试日志输出（`/api/tasks/debug`） |
| 31 | `list_log_files` | 无 | `LogFileItem[]` | 列出 `logs/` 目录下历史日志文件 |
| 32 | `read_log_file` | `path: string` | `Result<string, String>` | 仅允许读取 `logs/` 目录内文件尾部 128KB |

### 2.5 通知、大模型与题库缓存（7 个）

| # | Command | 参数 | 返回值 | 说明 |
| --- | --- | --- | --- | --- |
| 33 | `test_notification` | `provider`, `url`, `tgChatId`, `proxy` | `Result<string, String>` | 转发 `/api/notify/test` |
| 34 | `test_llm_connection` | `llm: LlmConfig`, `proxy: string` | `Result<string, String>` | 转发 `/api/llm/test` |
| 35 | `fetch_llm_models` | `llm: LlmConfig`, `proxy: string` | `Result<string[], String>` | 转发 `/api/llm/models` |
| 36 | `test_question_bank` | `token`, `url`, `proxy` | `Result<string, String>` | 转发 `/api/question-bank/test` |
| 37 | `export_question_cache` | `courseId?: string` | `Result<Option<string>, String>` | 异步弹窗保存 `/api/cache/export` 返回的 JSON |
| 38 | `import_question_cache` | 无 | `Result<Option<ImportResult>, String>` | 异步弹窗读取 JSON 并提交 `/api/cache/import`（返回 `zhidao_questions` / `ai_questions`） |
| 39 | `clear_question_cache` | `courseId?: string` | `Result<ClearResult, String>` | 转发 `/api/cache/clear` |

---

## 3. Tauri Events 契约（Rust → WebView）

| 事件名 | 载荷类型 | 触发时机 |
| --- | --- | --- |
| `autovisor-state` | `RunState`（全量快照）或 `{ kind: "state", state }` / `{ kind: "log", entries }` 增量载荷 | 后端 SSE 推送任务进度、状态跃迁、增量日志或命令返回最新状态 |
| `autovisor-backend-status` | `BackendStatus` (`{ ready, starting, port, pid, appVersion, lastError }`) | 后端启动、握手成功、异常断开或重启 |
| `autovisor-login-status` | `LoginStatusEvent` (`{ phase, message, needsAction }`) | `zhs_login` / `refresh_courses` 期间阶段提示（`launching`/`waiting_user`/`verifying`/`fetching_courses`/`done`/`error`） |
| `close-requested` | `number` (`promptId`) | 用户点击主窗口关闭按钮且偏好为 `ask` 时发往 `main` 窗口 |

---

## 4. Python 本地无感后端 HTTP / SSE 契约（`backend_server.py`）

### 4.1 启动握手与安全边界

- **监听地址**：强制仅允许 `127.0.0.1` 或 `localhost`（动态端口 `0`）。
- **认证传递**：宿主以 `--auth-stdin` 启动并通过 `stdin` 首行写入 `{"authToken": "<64-hex CSPRNG>"}\n`；若未提供且未指定 `--allow-insecure-dev`，后端自动生成随机 Token（Fail-Closed）。
- **就绪握手**：`stdout` 仅输出一行 JSON：`{"event":"autovisor-backend-ready","version":1,"port":<port>,"pid":<pid>,"appVersion":"<ver>"}`；其余输出全部重定向至 `stderr`。
- **请求校验**：
  - `Host` 仅允许 `127.0.0.1[:port]` / `localhost[:port]` / `[::1][:port]`（否则 `403`）。
  - 若携带 `Origin`，仅允许 `http(s)://(127.0.0.1|localhost|::1|tauri.localhost)` 或 `tauri://localhost`（否则 `403`）。
  - 请求头必须携带 `X-Autovisor-Token`（恒定时间 `hmac.compare_digest` 校验，否则 `401`）。
  - `POST` 必须携带 `Content-Type: application/json`（`/api/shutdown` 空体除外，否则 `415`）。
  - 请求体大小限制：默认 `2 MiB`，`/api/courses/extract` 为 `10 MiB`，`/api/cache/import` 为 `20 MiB`。

### 4.2 GET / SSE 端点

| 方法与路径 | 查询参数 | 成功响应 (`200`) | 错误码 |
| --- | --- | --- | --- |
| `GET /api/health` | 无 | `{ ok: true, version, pid, taskStatus }` | `401`, `403` |
| `GET /api/state` | 无 | `RunState` 完整快照（含 `revision`, `status`, `logs[]`, `completedLessons[]`, `shutdown` 等） | `401`, `403` |
| `GET /api/cache/stats` | `courseId?` | `{ ok: true, zhidao_questions, ai_questions, ... }` | `400`, `500` |
| `GET /api/events` | 无 | `text/event-stream`：建立连接先发 1 帧 `event: snapshot`，后续 150ms 合并窗口发 `event: state`（不含 `logs`）与 `event: log`（`{ revision, entries[] }`），15s 空闲发 `: keepalive` | `401`, `403` |

### 4.3 POST 端点

| 路径 | 请求体 JSON | 成功响应 (`200`) | 错误码 |
| --- | --- | --- | --- |
| `/api/tasks/start` | `{ mode: "study"\|"check_browser"\|"check_course"\|"import_cookies", target?: string }` | `RunState` | `400`（非法路径/参数）, `409`（任务运行或停止中） |
| `/api/tasks/stop` | `{}` | `RunState`（进入 `stopping` 直至清理完毕） | `401`, `403` |
| `/api/tasks/cancel-shutdown` | `{}` | `RunState` | `502` |
| `/api/tasks/debug` | `{ enabled: boolean }` | `{ ok: true, enabled: boolean }` | `400` |
| `/api/notify/test` | `{ provider, url, tgChatId, proxy }` | `{ ok: true, message }` | `400`, `502` |
| `/api/llm/test` | `{ llm: LlmConfig, proxy }` | `{ ok: true, message }` | `400`, `502` |
| `/api/llm/models` | `{ llm: LlmConfig, proxy }` | `{ ok: true, models: string[] }` | `400`, `502` |
| `/api/question-bank/test` | `{ token, url, proxy }` | `{ ok: boolean, message, ... }` | `400`, `502` |
| `/api/cache/export` | `{ courseId?: string }` | `{ ok: true, data: ExportPayload }` | `400`, `500` |
| `/api/cache/import` | `{ data: ExportPayload }` 或直接 `ExportPayload` | `{ ok: true, zhidao_questions, ai_questions }` | `400`, `409`, `500` |
| `/api/cache/clear` | `{ courseId?: string }` | `{ ok: true, zhidao_questions, ai_questions }` | `400`, `409`, `500` |
| `/api/qr-login/start` | `{ proxy?, saveDir? }` | `{ ok: true, sessionId, imageDataUrl, ... }` | `400`, `502` |
| `/api/qr-login/poll` | `{ sessionId }` | `{ ok: true, status, ... }` | `400`, `502` |
| `/api/qr-login/save` | `{ sessionId, saveDir }` | `{ ok: true, savedPath }` | `400`, `502` |
| `/api/qr-login/cancel` | `{ sessionId }` | `{ ok: true, cancelled: boolean }` | `400` |
| `/api/courses/fetch` | `{ cookies?: PlaywrightCookie[], proxy? }` | `{ ok: true, sessionValid, courses[], displayName, schoolName, sources, cookies[] }` | `400`, `502` |
| `/api/courses/extract` | `{ responses?: [{url, body}], domCourses?: [], domUserName?: string, courses?: [], cookies?: [], fetchApi?: boolean, proxy?: string }` | `{ ok: true, sessionValid, courses[], displayName, schoolName, cookies[] }` | `400`, `502` |
| `/api/update/check` | `{ proxy?, currentVersion? }` | `{ ok: true, hasUpdate, latestVersion, ... }` | `502` |
| `/api/captcha/solve` | `{ cdpUrl, pageUrl?, appearMs?, maxAttempts? }`（`cdpUrl` 仅允许 loopback） | `{ ok: true, solved, reason, ... }` | `400`, `502` |
| `/api/shutdown` | `{}`（可空体） | `{ ok: true }`，异步执行幂等 `graceful_shutdown()` | `401`, `403` |
