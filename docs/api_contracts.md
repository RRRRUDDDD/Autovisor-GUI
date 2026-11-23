# 智慧树接口契约速查（Autovisor 实际调用）

> 抓包/联调日期：2026-11。本文件只记录 Autovisor 实际调用的域名、密钥名、查询策略与字段，不含任何账号、Cookie 或密钥值。接口字段来自公开请求观察与 `zhs/docs/api/openapi.yaml` 的字段说明，代码为独立实现。

## 1. 加密查询策略表

实现：`modules/zhs_api.py`（`QueryStrategy` / `STRATEGIES`），密钥常量：`modules/zhidao_crypto.py`（`KEYS`）。

| 策略名 | 密钥名 | `dateFormate` | 成功判定 | `content_type` | 特殊行为 |
| --- | --- | --- | --- | --- | --- |
| `home` | `HOME` | 是 | `code == 0` | form | 首页/课程列表（Autovisor 新增） |
| `zhidao` | `VIDEO` | 是 | `code == 0` | form | `code == -12` 抛 `CaptchaRequiredError` |
| `ai_exam` | `EXAM` | 是 | `code == 0` | form | AI 作业/考试（`studentexamtest`） |
| `ai_exam_submit` | `EXAM` | 是 | 仅 HTTP 2xx（无返回体） | form | AI 综合考试 `submit` |
| `ai_task` | `AI` | 是 | `code == 200` | json | `kg-ai-run` / `kg-run-student` |
| `homework` | `EXAM` | 否 | `status == "200"` | form | `studentexam-api`（知到作业/通用保存） |
| `zhidao_exam` | `EXAM` | 否 | `status == "200"` | form | `taurusexam-api`（知到考试） |

请求体：`secretStr` 为 AES-CBC(JSON) 密文；需要时顶层附带 `dateFormate`。`extra_fields` 作为明文字段并入表单（知到考试 `saveStudentAnswer` 的 `source=1`）。

## 2. CAS SSO 与三态登录校验

- 换票入口：`https://passport.zhihuishu.com/cas/login?service=<service>`，禁止自动重定向。
- `studentexam-api` 会话 service：`https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/student/getStudentHomework`。
- `302 + Location 含 ticket=` → 跟随 Location 种 Cookie，客户端缓存换票状态；`200` 或无 ticket → `SessionExpiredError`（`CASTGC` 失效）。
- 登录态校验：
  - 先经 CAS 为 `https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/getLoginUserInfo` 换票；
  - 再 `GET .../getLoginUserInfo?time=<ms>`；
  - `result.uuid` / `result.username` 非空 → `OK`；
  - HTTP 5xx 或连接异常 → `UNAVAILABLE`（不清 Cookie、不判定掉线）；
  - CAS 未签发票据、4xx、无用户信息 → `EXPIRED`。
- 枚举：`modules/zhs_api.SessionStatus`。

## 3. 已接入域名一览

| 域名 | 用途 | 策略 |
| --- | --- | --- |
| `studentexamtest.zhihuishu.com/gateway/t/v1` | AI 智慧课考试接口 | `ai_exam` / `ai_exam_submit` |
| `kg-ai-run.zhihuishu.com/run/gateway/t` | AI 课程试卷、资源、开考 | `ai_task` |
| `kg-run-student.zhihuishu.com/student/gateway/t` | AI 课程任务列表 | `ai_task` |
| `studentexam-api.zhihuishu.com/studentExam/gateway/t/v1` | 知到作业 / 考试保存答案 | `homework` |
| `taurusexam-api.zhihuishu.com` | 知到正式考试 | `zhidao_exam` |
| `ai-course-assistant-api.zhihuishu.com` | 官方 AI 解析（明文 JSON/SSE） | 无（不加密） |
| `onlineservice-api.zhihuishu.com` | 登录用户信息 | 无（CAS 票据） |
| `passport.zhihuishu.com` | CAS SSO | 无 |

## 4. 关键请求/响应字段（摘要）

### 4.1 知到作业（`studentexam-api`，`homework` 策略，`status == "200"`）

| 路径 | 请求体（明文 JSON） |
| --- | --- |
| `/student/getStudentHomework` | `{recruitId: str, courseId: int, flag: 1|2, pageSize: 100, pageNum: 0}`；响应 `rt.studentHomeworkList[]`（`id` = studentExamId、`examId`）与 `rt.next`（`-1` 结束） |
| `/student/saveCourseTran` | `{recruitId: str, examId: str, description: "", courseId: int}` |
| `/student/doHomework` | `{recruitId: str, examId: str, studentExamId: str, schoolId: str, courseId: str}` |
| `/answer/saveStudentAnswer` | `{stuExamAnswer: "<JSON 字符串>", recruitId: str}`；元素字段见下 |
| `/answer/submit` | `{recruitId: str, examId: str, stuExamId: str, achieveCount: "5"}`（字符串） |
| `/student/lookHomework` | `{recruitId: str, studentExamId: str, examId: str, schoolId: str, courseId: str}` |
| `/answer/getStuAnswerInfo` | `{recruitId: str, stuExamId: str, examId: str, schoolId: str, courseId: str, questionIds: "101,102"}`（逗号字符串） |

`stuExamAnswer` 元素：`{examId, recruitId, stuExamId, eid, schoolId, deviceId: "", examType, fromType: 3, answer, dataIds: "", questionType}`。
题目位于 `rt.examBase.workExamParts[].questionDtos[]`；`eid` 用于保存，`id`（lookHomework）用于查错与 AI 解析，两者不可混用。

### 4.2 知到正式考试（`taurusexam-api` + 复用 `homework` 保存）

| 路径 | 请求体 |
| --- | --- |
| `taurusExam/gateway/t/v1/student/getStudentFinalExam` | `{courseId: int, recruitId: str, flag: 1|0}` |
| `taurusExam/gateway/t/v1/student/doExam` | `{recruitId, examId, studentExamId, schoolId, courseId, deviceId: ""}` |
| `taurusExam/gateway/t/v1/answer/submit` | `{recruitId, examId, stuExamId, achieveCount: "5"}`；`rt.statu == "1"` 判成功 |
| `studentExam/gateway/t/v1/answer/getSaveAnswerLockResult` | `{examId: str, recruitId: str}`；`rt.state == 1` 表示锁定 |
| `studentExam/gateway/t/v1/exam/getLoginSchoolInfo` | `{}`；`rt.schoolId`，null 时按空串处理 |
| `studentExam/gateway/t/v1/answer/saveStudentAnswer` | 与作业相同，元素 `examType = 1`，表单额外明文 `source=1` |

### 4.3 AI 综合考试与官方解析

- AI 综合考试：`taskList`、`start-pull-paper`、`openExam`、`getAnswerSheetInformation`（遍历 `partSheetVos`）、`saveBatchAnswer`、`submit`（考试体 `{examTestId, recruitId, examPaperId}`）、`openExamDetail`（`isLookAnswer` / `isAllowShowDetail`）。
- 官方 AI 解析：`GET /api/v1/user/info` 取 `data.userId`；`POST /api/v1/question/analysis/thread/run` 请求体 `{courseId, recruitId, userRole: "STUDENT", userId: int, threadId: "", questionId: int, regenerate: false, runId: null}`，按 UTF-8 解析 SSE `choices[0].message.content` 至 `stop == true`。

## 5. 变更维护约定

- 新接口一律优先在 `STRATEGIES` 中登记策略，业务模块只组合调用，不复制加解密代码。
- 新增字段时先补 `tests/test_zhs_api.py` 的 mock 用例，再更新本文件，并记录抓包日期。