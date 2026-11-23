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

- 知到作业：`getStudentHomework(flag=1/2)`、`saveCourseTran`、`doHomework`、`saveStudentAnswer`、`submit`、`lookHomework`、`getStuAnswerInfo`；题目位于 `rt.examBase.workExamParts[].questionDtos[]`（`eid`、`name`、`questionOptions[{id, content}]`、`questionType.id`）。
- 知到考试：`getStudentFinalExam(flag=1/0)`、`getSaveAnswerLockResult`、`getLoginSchoolInfo`、`doExam`、`saveStudentAnswer(source=1, examType=1)`、`submit`；`submit` 以 `rt.statu == "1"` 判成功。
- AI 综合考试：`taskList`、`start-pull-paper`、`openExam`、`getAnswerSheetInformation`（遍历 `partSheetVos`）、`saveBatchAnswer`、`submit`、`openExamDetail`（`isLookAnswer` / `isAllowShowDetail`）。
- 官方 AI 解析：`GET /api/v1/user/info` 取 `data.userId`；`POST /api/v1/question/analysis/thread/run` 解析 SSE `choices[0].message.content` 至 `stop == true`。

## 5. 变更维护约定

- 新接口一律优先在 `STRATEGIES` 中登记策略，业务模块只组合调用，不复制加解密代码。
- 新增字段时先补 `tests/test_zhs_api.py` 的 mock 用例，再更新本文件，并记录抓包日期。