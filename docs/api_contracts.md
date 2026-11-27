# 智慧树接口契约速查（Autovisor 实际调用）

> 抓包/联调日期：2026-11（最近核对：2026-11-27）。本文件只记录 Autovisor 实际调用的域名、密钥名、查询策略与字段，不含任何账号、Cookie、设备指纹或密钥值。接口字段来自公开请求观察与 `zhs/docs/api/operations/` 的核对说明，代码为独立实现。

## 1. 加密查询策略表

实现：`modules/zhs_api.py`（`QueryStrategy` / `STRATEGIES`），密钥常量：`modules/zhidao_crypto.py`（`KEYS`）。

| 策略名 | 密钥名 | `dateFormate` | 成功判定 | `content_type` | 特殊行为 |
| --- | --- | --- | --- | --- | --- |
| `home` | `HOME` | 是 | `code == 0` | form | 首页/课程列表通用（Autovisor 新增） |
| `home_share` | `HOME` | 是 | `code == 200` | form | 共享课列表 `queryShareCourseInfo` |
| `home_ai` | `HOME` | 是 | 仅 HTTP 2xx（业务层校验 `status == "200"` 或 `code in (0, 200)`） | form | AI 智慧课列表 `queryStudentAICourseList` |
| `zhidao` | `VIDEO` | 是 | `code == 0` | form | `code == -12` 抛 `CaptchaRequiredError` |
| `ai_exam` | `EXAM` | 是 | `code == 0` | form | AI 作业/考试（`studentexamtest`） |
| `ai_exam_submit` | `EXAM` | 是 | 仅 HTTP 2xx（无返回体） | form | AI 综合考试 `submit`，`allow_retry=False`（非幂等禁重试） |
| `ai_task` | `AI` | 是 | `code == 200` | json | `kg-ai-run` / `kg-run-student` |
| `homework` | `EXAM` | 否 | `status == "200"` | form | `studentexam-api`（知到作业/通用保存/风控解锁）；`captcha_on_minus_12=True` |
| `zhidao_exam` | `EXAM` | 否 | `status == "200"` | form | `taurusexam-api`（知到正式考试）；`captcha_on_minus_12=True` |

请求与校验约定：
- 请求体：`secretStr` 为 AES-CBC(JSON) 密文；`set_timestamp=True` 时在明文 JSON 与顶层表单同时写入真实毫秒时间戳 `dateFormate = int(time.time() * 1000)`。`extra_fields` 作为明文字段并入表单（如知到正式考试 `saveStudentAnswer` 的 `source=1`）。
- 风控判定优先级：`ZhsApiClient.execute()` 对启用 `captcha_on_minus_12=True` 的策略（`zhidao` / `homework` / `zhidao_exam`），会在校验 `ok_field == ok_value`（如 `status == "200"`）**之前**先判定 `int(ret["code"]) == -12` 并抛出 `CaptchaRequiredError(code="-12", response=ret)`，防止 `doHomework` / `doExam` 返回 `status="200", code="-12"` 时被误判为成功或无题跳过。

## 2. 扫码登录、CAS SSO 与三态登录校验

### 2.1 扫码登录新域（`modules/qr_login.py`）

- 预热登录页：`GET https://passport.zhihuishu.com/login?service=https://onlineservice-api.zhihuishu.com/login/gologin`（获取二维码前先访问以种下初始会话 Cookie；请求头携带 `Referer: https://passport.zhihuishu.com/login`）。
- 获取二维码：`GET https://user.zhihuishu.com/zhsuser/qrCodeLogin/getLoginQrImg`（已由旧域 `passport.zhihuishu.com/qrCodeLogin/*` 迁移）：
  - 响应 JSON：`{img: "<base64 PNG，无 data: 前缀>", qrToken: "<32位十六进制轮询令牌>"}`。
- 轮询扫码状态：`GET https://user.zhihuishu.com/zhsuser/qrCodeLogin/getLoginQrInfo?qrToken=<qrToken>`：
  - 响应 JSON：`{status: -1|0|1|2|3, oncePassword?, uuid?, msg?}`；
  - `status` 枚举：`-1` 未扫码、`0` 已扫码等待手机确认、`1` 确认成功（返回 `oncePassword` 与 `uuid`）、`2` 二维码已过期（自动刷新，默认最多 `max_refreshes=3` 次）、`3` 用户取消；
  - 容错约束：轮询期间捕获瞬时网络/JSON 解析异常并继续重试，受总超时上限（默认 `timeout_secs=180`）控制。
- 一次性凭证换票：`GET https://passport.zhihuishu.com/login?service=https://onlineservice-api.zhihuishu.com/login/gologin&pwd=<oncePassword>`，跟随跳转后提取 `.zhihuishu.com` Cookie。

### 2.2 CAS SSO 与三态会话校验（`modules/zhs_api.py`）

- 换票入口：`GET https://passport.zhihuishu.com/cas/login?service=<service>`，禁止自动重定向。
- `studentexam-api` 会话 service：`https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/student/getStudentHomework`。
- 响应 `3xx`（`300 <= status < 400`）且 `Location` 含 `ticket=` → 跟随目标地址种下会话 Cookie；若目标响应 HTTP `>= 400` 视为换票失败；`200` 或无 `ticket=` → `SessionExpiredError`（`CASTGC` 失效）。
- 登录态校验（枚举 `modules.zhs_api.SessionStatus`）：
  - 先经 CAS 为 `https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/getLoginUserInfo` 换票；
  - 再 `GET .../getLoginUserInfo?time=<ms>`；
  - `result.uuid` / `result.username` 非空 → `OK`；
  - HTTP 5xx 或连接异常 → `UNAVAILABLE`（`ensure_login` 保留本地 `cookies.json`，不误删凭证）；
  - CAS 未签发票据、HTTP 4xx、无用户信息 → `EXPIRED`。

## 3. 已接入域名一览

| 域名 | 用途 | 策略 |
| --- | --- | --- |
| `user.zhihuishu.com/zhsuser` | 扫码登录取码与轮询 | 无（明文 JSON） |
| `passport.zhihuishu.com` | 登录页预热、扫码换票、CAS SSO | 无 |
| `onlineservice-api.zhihuishu.com` | 登录用户信息校验 | 无（CAS 票据） |
| `studentexam-api.zhihuishu.com/studentExam/gateway/t/v1` | 知到作业 / 考试保存答案 / 风控解锁 `validateSlideToken` | `homework` |
| `taurusexam-api.zhihuishu.com/taurusExam/gateway/t/v1` | 知到正式考试（列表/开考/交卷） | `zhidao_exam` |
| `studyservice-api.zhihuishu.com/gateway/t/v1` | 共享课信息查询（`queryCourse`） | `zhidao` |
| `studentexamtest.zhihuishu.com/gateway/t/v1` | AI 智慧课考试接口 | `ai_exam` / `ai_exam_submit` |
| `kg-ai-run.zhihuishu.com/run/gateway/t` | AI 课程试卷、资源列表、资源完成、开考/详情 | `ai_task` |
| `kg-run-student.zhihuishu.com/student/gateway/t` | AI 课程任务列表（`taskList`） | `ai_task` |
| `studyresources.zhihuishu.com/studyResources/stuResouce` | Hike 翻转/智慧课资源树、最近文件、文档已读 | 无（CAS 票据） |
| `ai-course-assistant-api.zhihuishu.com` | 官方 AI 解析（明文 JSON/SSE） | 无（不加密） |
| `c.dun.163.com` | 易盾验证码纯 HTTP 三段式协议与 `fp` 被动采集 | 易盾自定义加密 / JSONP |

## 4. 关键请求/响应字段（摘要）

### 4.1 知到作业（`studentexam-api`，`homework` 策略，`status == "200"`，先判 `code == "-12"`）

| 路径 | 请求体（明文 JSON）与响应关键字段 |
| --- | --- |
| `/student/getStudentHomework` | `{recruitId: str, courseId: int, flag: 1\|2, pageSize: 100, pageNum: 0}`；响应 `rt.studentHomeworkList[]`（`id` = studentExamId、`examId`）与 `rt.next`（`-1` 结束） |
| `/student/saveCourseTran` | `{recruitId: str, examId: str, description: "", courseId: int}`（已提交作业 `state=4` 重做前重置状态） |
| `/student/doHomework` | `{recruitId: str, examId: str, studentExamId: str, schoolId: str, courseId: str}`；风控触发时仍返回 `status: "200"` 但附带字符串 `code: "-12"`，需先判 `-12` 并解锁后再重试 |
| `/answer/saveStudentAnswer` | `{stuExamAnswer: "<JSON 字符串>", recruitId: str}`；元素字段见下 |
| `/answer/submit` | `{recruitId: str, examId: str, stuExamId: str, achieveCount: "5"}`（字符串） |
| `/student/lookHomework` | `{recruitId: str, studentExamId: str, examId: str, schoolId: str, courseId: str}`；返回数字型 `id`（无 `eid`），可能隐藏 `correctOptions` |
| `/answer/getStuAnswerInfo` | `{recruitId: str, stuExamId: str, examId: str, schoolId: str, courseId: str, questionIds: "101,102"}`（逗号分隔字符串，非数组） |
| `/answer/validateSlideToken` | `{recruitId: str, token: str, checkType: 1, chapterId: str}`；解锁结果看布尔字段 `pass`（见下） |

补充约定：
- `stuExamAnswer` 元素：`{examId, recruitId, stuExamId, eid, schoolId, deviceId: "", examType, fromType: 3, answer, dataIds: "", questionType}`（作业 `examType=""`，正式考试 `examType=1`）。
- 题目位于 `rt.examBase.workExamParts[].questionDtos[]`；`eid`（`doHomework`）用于保存答案，数字 `id`（`lookHomework`）用于 `getStuAnswerInfo` 查错与官方 AI 解析，两者不可混用。
- **`getStuAnswerInfo` 真实字典结构与 `isCurrent`**（`modules/zhidao_homework_api.py`、`modules/zhidao_homework_worker.py`）：
  - 真实响应的 `rt` 为按题目数字 ID 索引的字典：`rt = {"<questionId>": {"questionId": ..., "answer": "101,102", "isCurrent": "1", ...}}`（不是列表）；解析层 `iter_answer_info_items()` 优先兼容包装列表键（`questionDtos` / `stuAnswerInfos` / `questions` / `answerInfos`），否则直接按 `{qid: entry_dict}` 展开。
  - 正确性字段以 **`isCurrent`** 为真实主字段（`"1"` / `1` / `True` 视为答对，`"0"` / `""` 视为未对），同时兼容 `isRight` / `isCorrect` / `correct`。
  - 当 `lookHomework` 隐藏 `correctOptions` 且判定答对时，回退拆分 `answer`（多选按 `,`、填空按 `/`）写入 `correct_options` 落库；首轮提交即达标也会执行一次复查落库供跨卷复用。
- **`validateSlideToken` 风控解锁契约**：
  - 与视频端 `validateSlideToken` 同名不同域/路径：此处位于 `studentexam-api` 域 `/studentExam/gateway/t/v1/answer/validateSlideToken`，使用 `homework` 策略（`EXAM` 密钥、不发 `dateFormate`）。
  - **`chapterId` 实际传 `examId`（字符串）**，不是数字型章节 ID；`checkType` 固定为整数 `1`；`token` 为易盾 `check` 返回的裸 `validate` 经二次组装后的 `CN31_{aes(裸validate + "::" + fp)}_v_i_1`。
  - 响应 `status == "200"` 仅代表 HTTP/网关成功，解锁是否通过以布尔字段 `pass` 为准（`pass == true` 后重试原 `doHomework`/`doExam` 请求；`pass == false` 静默回退浏览器/人工处理）。

### 4.2 知到正式考试（`taurusexam-api` + 复用 `homework` 保存与风控解锁）

| 路径 | 请求体与关键说明 |
| --- | --- |
| `taurusExam/gateway/t/v1/student/getStudentFinalExam` | `{courseId: int, recruitId: str, flag: 1\|0}`；仅以 `studentStartTime` 非空或 `state == 2` 判定已开考（不得把 `progressType == 2` 当已开考） |
| `taurusExam/gateway/t/v1/student/doExam` | `{recruitId, examId, studentExamId, schoolId, courseId, deviceId: ""}`；与 `doHomework` 一致，风控触发时返回 `status: "200"` + `code: "-12"`（`zhidao_exam` 策略先抛 `CaptchaRequiredError`） |
| `taurusExam/gateway/t/v1/answer/submit` | `{recruitId, courseId, examId, stuExamId, achieveCount: "5"}`；`rt.statu == "1"` 判成功 |
| `studentExam/gateway/t/v1/answer/getSaveAnswerLockResult` | `{examId: str, recruitId: str}`；`rt.state == 1` 表示已锁定，跳过避免冲突 |
| `studentExam/gateway/t/v1/exam/getLoginSchoolInfo` | `{}`；取 `rt.schoolId`，为 `null` 时按空串处理 |
| `studentExam/gateway/t/v1/answer/saveStudentAnswer` | 与作业相同，元素 `examType = 1`，表单额外附带明文 `source=1` |
| `studentExam/gateway/t/v1/answer/validateSlideToken` | 复用作业风控解锁接口，`chapterId = examId`、`checkType = 1` |

### 4.3 AI 智慧课（掌握度、资源完成、综合考试与官方解析）

- **知识点试卷查询**（`POST https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/exam/questions-paper`，`ai_task` 策略，`code == 200`）：
  - 请求体：`{scMapId: courseId, courseId: classId, classId: classId, knowledgeId: knowledgeId}`（服务端历史约定 `courseId` 实传 `classId`，`scMapId` 传课程 ID）；
  - 响应 `data`：`examTestId`、`paperId`、掌握度优先读取 **`highMasteryScore`**，仅当 `highMasteryScore is None` 时回退 `masteryScore`（`highMasteryScore == 0` 不得回退）。
- **免考标记 `freeExam`**：接口偶发下发 `""` / `None` / `"0"` / `"1"`，统一归一化为整数 `0` 或 `1`；`skip_ai_free_exam=True` 仅跳过免考节点的掌握度/作业考核，**不跳过**必学视频与文档资源学习。
- **非视频资源快速完成**（`POST https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/studyRecord/completed`，`ai_task` 策略，`code == 200`）：
  - 请求体：`{courseId: int, classId: int, knowledgeId: int, resourcesUid: int, watchUId: 1}`；
  - 仅用于未完成的必学非视频资源（文档/PPT/文本），视频资源严格走浏览器播放。
- **正式考试任务扫描 `FORMAL_TASK_LIST`**（`POST https://kg-run-student.zhihuishu.com/student/gateway/t/task/taskList`，`ai_task` 策略，`code == 200`）：
  - 请求体：`{courseId: <courseId>, taskType: 1, taskName: "", status: 0}`（必须携带 `courseId` 与空串 `taskName`）；
  - 响应解析兼容 `data` 为任务数组或包含 `taskList` / `list` / `records` / `tasks` 的对象，并显式过滤 `taskType == 1`（排除布尔值与非考试任务）。
- **AI 综合考试流程**：`taskList` → `start-pull-paper`（`{courseId, classId, examTestId, pageNumber: 1, pageSize: 10}`）→ `openExam` → `getAnswerSheetInformation`（遍历 `partSheetVos`）→ `saveBatchAnswer` → `submit`（`ai_exam_submit` 策略禁重试，请求体 `{examTestId, recruitId, examPaperId}`）→ `openExamDetail`（校验 `isLookAnswer` / `isAllowShowDetail`）。
  - 填空题（`questionType == 3`）作答复用 PPT 参考资料、课程/主题/知识点上下文与题库参考；回读时提取清洗后的标准/正确选项文本并用 `"/"` 拼接（不得拼接 `option["id"]`）。
- **官方 AI 解析**：`GET https://ai-course-assistant-api.zhihuishu.com/api/v1/user/info` 取 `data.userId`；`POST /api/v1/question/analysis/thread/run` 请求体 `{courseId, recruitId, userRole: "STUDENT", userId: int, threadId: "", questionId: int, regenerate: false, runId: null}`，按 UTF-8 解析 SSE `choices[0].message.content` 至 `stop == true`（`ZhidaoAnswerer` 结合 `_ThinkAwareAimTracker` 提前结束流并带总超时上限）。

### 4.4 Hike 翻转/智慧课资源接口（`studyresources.zhihuishu.com`，CAS 票据）

实现：`modules/hike_navigation.py`。

| 路径 | 请求参数与用途 |
| --- | --- |
| `GET /studyResources/stuResouce/queryResourceMenuTree` | `courseId=<cid>&_=<ms>`；获取课程资源树，解析目标视频 `fileId` 与未完成的非视频文档节点 |
| `GET /studyResources/stuResouce/queryLastStudyFile` | `courseId=<cid>&_=<ms>`；`queryResourceMenuTree` 不可用时的 `fileId` 兜底 |
| `GET /studyResources/stuResouce/stuViewFile` | `courseId=<cid>&fileId=<fid>`；将未完成的非视频文档节点标记已读 |

### 4.5 易盾验证码纯 HTTP 协议与 `fp` 被动采集（`c.dun.163.com`）

实现：`modules/yidun_fp.py`（纯内存会话态 + `redaction.register_secret` 脱敏，绝不落盘或打印明文）、`modules/yidun_http.py`。

- **被动采集 `fp`**：在 Playwright/CDP 中只读监听 `c.dun.163.com/api/v3/get` 请求的 `fp` 查询参数（并支持 `localStorage`/`sessionStorage` 兜底探测）；真实 IRisk `fp` 长度约 190 字符（校验范围 150–260），尾段为 `:13位毫秒时间戳`，不得伪造或改写。
- **三段式 JSONP 协议**（响应格式 `callback({...})`，需剥离回调外壳）：
  1. `GET /api/v2/getconf`：`zoneId=""`、`iv="5"`、`id=<captchaId>`、`loadVersion="2.5.0"`、`runEnv="10"` → 返回 `data.dt`、`data.irToken`（`error == 0`）。
  2. `GET /api/v3/get`：`zoneId="CN31"`、`iv="4"`、`dt`、`irToken`、`id=<captchaId>`、`fp`、`cb`、`width` → 返回 `data.bg[]`、`data.front`、`data.token`、`data.type`。题型完全由 `captchaId` 决定：
     - 滑块：`75f9f716460a422f89a628f50fd8cc2b`（`type=2`，`front` 为拼图 URL 列表，作业/视频 `width=400`）；
     - 点选：`4da3050565514a35a50541b0e1f54538`（`type=11`，`front` 为提示文案字符串，`width=320`，CSS 画布 320×160）。
  3. `GET /api/v3/check`：`zoneId="CN31"`、`iv="4"`、`dt`、`id`、`token`、`data`（JSON 序列化后 URL 编码）、`width`、`type`、`cb` → `data.result == true` 时返回裸 `data.validate`：
     - 滑块加密：`data = {d, m:"", p, f, ext}`，密文内为 `aes(明文 + crc32(明文))`；
     - 点选加密：`data = {d:"", m, p, ext}`，外层 `aes(明文)` **不拼 CRC32**，`p` / `ext` 明文由 `xor_encode(token, ...)` 生成后再各自追加 8 位 hex `crc32`；坐标以 CSS 像素（0–320 / 0–160）为准。
- **二次组装**：业务侧（`validateSlideToken`）不得提交裸 `validate`，必须组装为 `CN31_{aes(裸validate + "::" + fp)}_v_i_1`。

## 5. 变更维护约定

- 新接口一律优先在 `STRATEGIES` 中登记策略，业务模块只组合调用，不复制加解密代码。
- 新增字段时先补 `tests/test_zhs_api.py` 或对应模块单测的 mock 用例，再更新本文件，并记录抓包/核对日期。
