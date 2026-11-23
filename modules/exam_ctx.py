# encoding=utf-8
"""智慧树 AI 课程计分考试的接口版实现.

流程: 取卷 -> 逐题作答 -> 保存答案 -> 交卷 -> 回读正确答案写回题库.
未命中题库时优先用智慧树 AI 生成答案, 失败再退到 data/llm.json 配置的自定义大模型,
最后随机选, 因此第一轮即使有错题, 后续轮次也能命中题库.
"""
from __future__ import annotations

import asyncio
import ast
import json
import random
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import unquote

import requests

from modules import llm, zhidao_crypto, zhs_api
from modules.answer_store import AiQuestionStore
from modules.http_utils import build_requests_proxies
from modules.logger import Logger
from modules.ppt_extractor import PptExtractor
from modules.utils import get_runtime_path
from modules.zhs_api import (  # noqa: F401  兼容旧的导入路径
    CaptchaRequiredError,
    SessionExpiredError,
    SessionStatus,
    build_session,
)

logger = Logger()

EXAM_BASE = "https://studentexamtest.zhihuishu.com/gateway/t/v1"
KNOWLEDGE_RESOURCE_LIST = (
    "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/resources/list-knowledge-resource"
)
EXAM_OPEN = f"{EXAM_BASE}/exam/user/openExam"
EXAM_USED_TIME = f"{EXAM_BASE}/exam/user/updateUserUsedTime"
EXAM_SHEET = f"{EXAM_BASE}/exam/user/getExamSheetInfo"
EXAM_QUESTION = f"{EXAM_BASE}/question/getExamQuestionInfo"
EXAM_SAVE_ANSWER = f"{EXAM_BASE}/answer/saveAnswer"
EXAM_SUBMIT = f"{EXAM_BASE}/exam/user/submit"
AI_EXAM_PAPER = "https://kg-ai-run.zhihuishu.com/run/gateway/t/stu/exam/questions-paper"
ZHIDAO_AI_CHAT = (
    "https://dmb-chat.zhihuishu.com/gateway/t/v1/completions/chat/stream"
)
ZHIDAO_AI_HEADERS = {
    "service-code": "SI_POLYMAS",
    "Accept": "text/event-stream",
    "Content-Type": "application/json",
}

QUESTION_TYPE_NAMES = {1: "单选题", 2: "多选题", 14: "判断题"}
MAX_EXAM_ATTEMPTS = 6
DEFAULT_PROMPT_CHAR_LIMIT = 28000
MATERIAL_TRUNCATED_MARK = "\n…[参考资料已截断]"


def is_flagged(value) -> bool:
    return str(value).strip().lower() in {"1", "true"}


def as_number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _brief_html(text: str | None, limit: int = 60) -> str:
    """去除 HTML 标签并折叠空白，供答题日志展示题干与选项摘要。"""
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    plain = " ".join(plain.split())
    if len(plain) <= limit:
        return plain
    return f"{plain[:limit]}..."


def _format_answer_choices(content: dict, answer_ids: list) -> str:
    options = content.get("optionVos") or []
    by_id = {
        str(opt.get("id")): _brief_html(opt.get("content", ""), 36)
        for opt in options
        if "id" in opt
    }
    parts = []
    for aid in answer_ids:
        key = str(aid)
        label = by_id.get(key)
        parts.append(f"{label}({key})" if label else key)
    return "; ".join(parts) or str(answer_ids)


# 兼容旧名称: 智慧树接口异常统一由 modules.zhs_api.ZhsApiError 表示.
ExamApiError = zhs_api.ZhsApiError


def parse_answer_ids(text: str | None) -> list:
    """从 ```answer`` 代码块或裸 JSON/列表文本里解析出选项 id."""
    raw = (text or "").strip()
    if not raw:
        return []
    block = re.search(r"```answer\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    candidates = [block.group(1).strip()] if block else []
    candidates.append(raw)
    for candidate in candidates:
        data = None
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            try:
                data = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                match = re.search(r"\[.*\]", candidate, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except (ValueError, TypeError):
                        try:
                            data = ast.literal_eval(match.group(0))
                        except (ValueError, SyntaxError):
                            data = None
        if not isinstance(data, list):
            continue
        ids = []
        for item in data:
            if isinstance(item, dict) and item.get("id") is not None:
                ids.append(item["id"])
            elif isinstance(item, (int, str)) and str(item).strip():
                ids.append(item)
        if ids:
            return ids
    return []


def parse_review_exam_url(url: str | None) -> dict:
    """从浏览器里的复习/模拟考试页提取接口所需参数."""
    match = re.search(
        r"/studentReviewTestOrExam/(\d+)/\d+/\d+/(\d+)/([^/?#]+)", url or ""
    )
    if not match:
        return {}
    params = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", url or ""))
    paper_id = params.get("paperId") or params.get("examPaperId") or ""
    if not paper_id:
        return {}
    return {
        "examTestId": match.group(1),
        "courseId": match.group(2),
        "nodeName": unquote(match.group(3)),
        "paperId": paper_id,
    }


def select_random_answers(choices: list[dict], count: int) -> list:
    count = min(max(count, 1), len(choices))
    picked = random.sample(choices, count)
    return [choice["id"] for choice in picked]


def _trim_reference_materials(materials: list[dict], budget: int) -> str:
    """按字符预算裁剪参考资料, 超出预算的末尾内容直接截断, 保留可读提示."""
    if budget <= 0:
        return ""
    header = "参考资料：\n"
    kept: list[str] = []
    remaining = budget - len(header)
    for item in materials:
        name = str(item.get("name", ""))
        content = str(item.get("content", ""))
        prefix = f"```{name}\n"
        suffix = "\n```"
        block = prefix + content + suffix
        if len(block) <= remaining:
            kept.append(block)
            remaining -= len(block) + 1
            continue
        inner_budget = remaining - len(prefix) - len(suffix) - len(MATERIAL_TRUNCATED_MARK)
        if inner_budget > 0:
            kept.append(prefix + content[:inner_budget] + MATERIAL_TRUNCATED_MARK + suffix)
        break
    if not kept:
        return ""
    return header + "\n".join(kept) + "\n"


def build_answer_prompt(
    question_type: int,
    question: str,
    choices: list[dict],
    reference_materials: list[dict] | None = None,
    extra: dict | None = None,
    max_chars: int | None = None,
) -> str:
    extra = extra or {}
    type_name = QUESTION_TYPE_NAMES.get(question_type, "客观题")
    background = (
        f"假设你是一名学生，正在学习《{extra.get('courseName', '未知课程')}》，"
        "需要严格按照考试要求完成一道题目，否则无法及格。\n"
    )
    theme = f"当前主题：{extra.get('theme', '')}。\n" if extra.get("theme") else ""
    knowledge = (
        f"考察知识点：{extra.get('knowledgePoint', '')}。\n"
        if extra.get("knowledgePoint")
        else ""
    )
    requirement = f"""本题为{type_name}，请从选项中选择{'最合适的答案' if question_type == 1 else '所有正确的答案'}，
回答放到 markdown 代码块中，例如：
```answer
[{{"id": 652308395, "content": "<p>仅涉及心、肺、肝、肾及脑相关疾病</p>"}}]
```
答案必须是 JSON 列表，单选也要是列表，否则视为无效答案。代码块外可以简短解释依据。
"""
    body = (
        background
        + theme
        + knowledge
        + requirement
        + f"现在，请听题：\n\n{question}\n\n"
        + f"选项如下：```choices\n{json.dumps(choices, ensure_ascii=False, indent=4)}\n```\n"
    )
    materials = ""
    if reference_materials:
        try:
            limit = int(max_chars) if max_chars else DEFAULT_PROMPT_CHAR_LIMIT
        except (TypeError, ValueError):
            limit = DEFAULT_PROMPT_CHAR_LIMIT
        materials = _trim_reference_materials(reference_materials, limit - len(body))
    return materials + body


class AnswerBank:
    """按课程保存题库: 底层写入 SQLite, 同时保留 data.json 快照兼容旧版本."""

    def __init__(self, course_id, base_dir: str | Path | None = None, store=None):
        self.course_id = str(course_id)
        explicit_dir = base_dir is not None
        self.base_dir = Path(
            base_dir
            if base_dir is not None
            else get_runtime_path("data", "exam_answers", self.course_id)
        )
        self.exam_test_id = None
        self.items: dict[str, dict] = {}
        if store is not None:
            self.store = store
        elif explicit_dir:
            # 显式目录通常来自测试: 数据库与旧 JSON 都隔离在该目录内
            self.store = AiQuestionStore(
                self.base_dir / "questions_bank.db", data_root=self.base_dir
            )
            self.store.migrate_course_json_dir(self.course_id, self.base_dir)
        else:
            self.store = AiQuestionStore()

    def _path(self, name: str) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir / name

    @staticmethod
    def _key(question_id, version: int) -> str:
        return str(question_id) if version == 1 else f"{question_id}_{version}"

    def load(self, exam_test_id) -> None:
        self.exam_test_id = exam_test_id
        merged: dict[str, dict] = {}
        if self.base_dir.is_dir():
            for file in self.base_dir.glob("*.json"):
                if file.name == "data.json":
                    continue
                try:
                    merged.update(json.loads(file.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        try:
            merged.update(
                json.loads(self._path("data.json").read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError):
            pass
        try:
            current = json.loads(
                self._path(f"{exam_test_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            current = {}
        merged.update(current)
        self.items = {}
        for key, value in merged.items():
            if not isinstance(value, dict):
                continue
            value.setdefault("version", 1)
            self.items[key] = value

    def get(self, question_id, version: int = 1) -> dict | None:
        return self.items.get(self._key(question_id, version))

    def set(self, question_id, version: int, answer: dict) -> None:
        record = {
            "version": version,
            "question": answer.get("question", ""),
            "answer": answer.get("answer", ""),
            "answer_content": answer.get("answer_content", ""),
            "questionDict": answer.get("questionDict", {}),
        }
        self.items[self._key(question_id, version)] = record
        self.store.upsert_ai_question(
            self.course_id,
            self.exam_test_id or "",
            str(question_id),
            version,
            question_plain=record["question"],
            question_raw=record["question"],
            answer=record["answer"],
            answer_content=record["answer_content"],
            question_dict=record["questionDict"],
        )
        self.save()

    def save(self, exam_test_id=None) -> None:
        if exam_test_id is not None:
            self.exam_test_id = exam_test_id
        merged_path = self._path("data.json")
        merged_path.write_text(
            json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if exam_test_id is not None:
            self._path(f"{exam_test_id}.json").write_text(
                json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def search_in_course(self, question: str, exclude_exam_id=None) -> dict | None:
        """跨 exam_id 检索同课程答案 (精确 -> 宽松 -> 模糊)."""
        return self.store.search_in_course(
            self.course_id, question, exclude_exam_id=exclude_exam_id
        )


class ExamApi(zhs_api.ZhsApiClient):
    """带 Cookie/代理/重试的智慧树考试接口客户端 (复用 zhs_api 策略层)."""

    def __init__(self, cookies, proxy: str = "", user_agent: str = ""):
        super().__init__(cookies, proxy, user_agent)

    def query(
        self,
        url: str,
        data: dict,
        key: bytes = zhidao_crypto.EXAM_KEY,
        ok_code: int = 0,
        method: str = "POST",
        content_type: str = "form",
        set_timestamp: bool = True,
        extra_fields: dict | None = None,
    ) -> dict:
        """兼容旧方法签名: 按调用参数临时构造策略后走统一 query 流程."""
        strategy = zhs_api.QueryStrategy(
            key=key,
            set_timestamp=set_timestamp,
            content_type=content_type,
            ok_field="code",
            ok_value=ok_code,
        )
        return self.execute(strategy, url, data, method=method, extra_fields=extra_fields)

    def query_strategy(
        self, strategy_name: str, url: str, data: dict, method: str = "POST", **kwargs
    ) -> dict:
        """按命名策略提交 (供知到作业/考试/题库等新接口使用)."""
        return zhs_api.ZhsApiClient.query(self, strategy_name, url, data, method, **kwargs)

    def query_paper(self, course_id, class_id, knowledge_id) -> dict | None:
        data = {
            "scMapId": course_id,
            "courseId": class_id,
            "classId": class_id,
            "knowledgeId": knowledge_id,
        }
        try:
            ret = self.query(
                AI_EXAM_PAPER,
                data,
                key=zhidao_crypto.AI_KEY,
                ok_code=200,
                content_type="json",
            )
        except Exception as exc:
            logger.log_exception("查询考试试卷失败.", exc)
            return None
        return ret.get("data") or None

    def list_knowledge_resources(
        self, course_id, class_id, knowledge_id
    ) -> list[dict]:
        """读取知识点关联资源 (PPT 课件等); 失败时返回空列表."""
        data = {
            "courseId": course_id,
            "classId": class_id,
            "knowledgeId": knowledge_id,
        }
        try:
            ret = self.query_strategy("ai_task", KNOWLEDGE_RESOURCE_LIST, data)
        except Exception as exc:
            logger.log_exception("查询知识点课件资源失败.", exc)
            return []
        payload = ret.get("data") if isinstance(ret, dict) else None
        resources = (
            payload.get("resourceList") if isinstance(payload, dict) else None
        )
        return resources if isinstance(resources, list) else []


class ZhidaoAnswerer:
    """智慧树自带 AI 答题通道(大明白 dmb-chat 流式接口, 优先级高于自定义大模型)."""

    def __init__(self, session: requests.Session, lock=None, timeout: int = 60):
        self.session = session
        self.lock = lock
        self.timeout = timeout

    @staticmethod
    def _parse_sse_payload(payload: dict) -> tuple[str, str | None, bool]:
        """解析单条 JSON 事件, 返回 (增量文本, 最终完整答案, 是否结束)."""
        code = payload.get("code")
        if code is not None and str(code) not in ("0", "200"):
            msg = payload.get("message") or payload.get("msg") or str(payload)
            raise ExamApiError(f"智慧树 AI 接口返回异常(code={code}): {msg}")

        # 新版 dmb-chat SSE 格式: {"messageType":"TEXT","message":"...","end":false/true,"context":{"answer":"..."}}
        if "message" in payload or "end" in payload or "context" in payload:
            chunk = payload.get("message") if isinstance(payload.get("message"), str) else ""
            is_end = bool(payload.get("end"))
            final_answer = None
            ctx = payload.get("context")
            if is_end and isinstance(ctx, dict) and isinstance(ctx.get("answer"), str) and ctx["answer"].strip():
                final_answer = ctx["answer"]
            return chunk, final_answer, is_end

        # 兼容 OpenAI 风格响应结构
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return "", msg["content"], True
            delta = first.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                return delta["content"], None, bool(first.get("finish_reason"))
        return "", None, False

    @classmethod
    def _consume_response(cls, response) -> str:
        chunks: list[str] = []
        final_answer: str | None = None
        parsed_any = False

        if hasattr(response, "iter_lines"):
            raw_lines = response.iter_lines()
        else:
            raw_lines = (getattr(response, "text", "") or "").splitlines()

        for raw in raw_lines:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            parsed_any = True
            chunk, final, is_end = cls._parse_sse_payload(payload)
            if chunk:
                chunks.append(chunk)
            if final:
                final_answer = final
            if is_end:
                break

        content = (final_answer or "".join(chunks)).strip()
        if not content:
            if not parsed_any:
                raise ValueError("智慧树 AI 返回格式无法解析或为空")
            raise ValueError("智慧树 AI 未返回有效回答内容")
        return content

    def chat(self, prompt: str) -> str:
        data = {
            "terminalType": 1,
            "question": prompt,
            "relationType": 1,
            "timestamp": int(time.time() * 1000),
        }
        url, body = zhidao_crypto.sign_zhidao_ai(ZHIDAO_AI_CHAT, data)
        post_kwargs = {
            "json": body,
            "headers": dict(ZHIDAO_AI_HEADERS),
            "timeout": self.timeout,
        }
        if isinstance(self.session, requests.Session):
            post_kwargs["stream"] = True
        with (self.lock or nullcontext()):
            response = self.session.post(url, **post_kwargs)
        try:
            response.raise_for_status()
            return self._consume_response(response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def answer(self, prompt: str) -> list:
        return parse_answer_ids(self.chat(prompt))


class LlmAnswerer:
    """复用 data/llm.json 的通用大模型通道（支持 OpenAI/Claude/Gemini/Azure/Ollama 等）, 作为智慧树 AI 失败后的兜底."""

    def __init__(self, proxy: str = ""):
        self.proxy = proxy
        self.proxies = build_requests_proxies(proxy)

    def answer(self, prompt: str) -> list:
        if not llm.enabled():
            raise ValueError("未配置可用的大模型接口")
        content = llm.chat_sync(prompt, json_mode=False, proxy=self.proxy, stream=True)
        return parse_answer_ids(content)


DeepSeekAnswerer = LlmAnswerer


class ExamCtx:
    def __init__(
        self,
        api: ExamApi,
        course_id,
        knowledge_id,
        exam_test_id,
        exam_paper_id,
        op_extra: dict | None = None,
        answer_bank: AnswerBank | None = None,
    ):
        self.api = api
        self.course_id = course_id
        self.knowledge_id = knowledge_id
        self.exam_test_id = exam_test_id
        self.exam_paper_id = exam_paper_id
        self.op_extra = op_extra or {}
        self.answer_bank = answer_bank or AnswerBank(course_id)
        self.reference_materials: list[dict] = []
        self.sheet_content: list | None = None
        self._stopped = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self.zhidao_answerer = ZhidaoAnswerer(
            api.session, lock=getattr(api, "lock", None)
        )
        self.deepseek_answerer = DeepSeekAnswerer(getattr(api, "proxy", ""))
        self._last_ai_provider = ""
        self._llm_disabled = False
        self._llm_warned = False

    def open_exam(self, tried: int = 0) -> None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "courseId": self.course_id,
        }
        try:
            self.api.query(EXAM_OPEN, data)
        except Exception as exc:
            if tried < 3:
                logger.warn(f"打开考试失败, 重试第 {tried + 1} 次: {exc}")
                return self.open_exam(tried + 1)
            raise
        self._heartbeat = threading.Thread(target=self._update_used_time, daemon=True)
        self._heartbeat.start()

    def _update_used_time(self, heartbeat_seconds: int = 10) -> None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "heartbeatTime": heartbeat_seconds,
        }
        while not self._stopped.is_set():
            try:
                self.api.query(EXAM_USED_TIME, data)
            except Exception as exc:
                logger.debug(f"考试计时心跳失败: {exc}")
            self._stopped.wait(heartbeat_seconds)

    def get_sheet_content(self, tried: int = 0) -> list:
        if self.sheet_content is not None:
            return self.sheet_content
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
        }
        try:
            ret = self.api.query(EXAM_SHEET, data, method="GET")
            self.sheet_content = ret["data"]["partSheetVos"][0]["questionSheetVos"]
        except Exception as exc:
            if tried < 3:
                logger.warn(f"读取试卷失败, 重试第 {tried + 1} 次: {exc}")
                return self.get_sheet_content(tried + 1)
            raise
        return self.sheet_content

    def get_question_content(self, question_id, version: int, tried: int = 0) -> dict | None:
        data = {
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "questionId": question_id,
            "version": version,
        }
        try:
            ret = self.api.query(EXAM_QUESTION, data, method="GET")
            return ret.get("data")
        except Exception as exc:
            if tried < 3:
                logger.debug(f"读取题目失败, 重试第 {tried + 1} 次: {exc}")
                return self.get_question_content(question_id, version, tried + 1)
            logger.error(f"读取题目失败: {question_id} {exc}")
            return None

    def save_answer(self, question_id, answers: list) -> bool:
        if not answers:
            return False
        data = {
            "recruitId": self.course_id,
            "examTestId": self.exam_test_id,
            "examPaperId": self.exam_paper_id,
            "questionId": question_id,
            "dataVos": None,
            "answer": "#@#".join(str(answer) for answer in answers),
        }
        try:
            self.api.query(EXAM_SAVE_ANSWER, data)
        except Exception as exc:
            logger.error(f"保存答案失败: {question_id} {exc}")
            return False
        return True

    def submit_exam(self, tried: int = 0) -> None:
        data = {
            "examTestId": self.exam_test_id,
            "courseId": self.course_id,
            "courseType": 8,
            "examPaperId": self.exam_paper_id,
            "aiKnlowledgeId": self.knowledge_id,
        }
        try:
            self.api.query(EXAM_SUBMIT, data)
        except Exception:
            if tried < 3:
                logger.warn(f"交卷失败, 重试第 {tried + 1} 次.")
                return self.submit_exam(tried + 1)
            raise
        finally:
            self._stopped.set()

    def _generate_answer(self, question_type: int, question: dict, choices: list[dict]) -> list:
        prompt = build_answer_prompt(
            question_type,
            question.get("content", ""),
            choices,
            self.reference_materials,
            self.op_extra,
            max_chars=self.op_extra.get("promptMaxChars"),
        )
        for name, answerer in (
            ("智慧树AI", self.zhidao_answerer),
            (llm.provider_label(), self.deepseek_answerer),
        ):
            if name != "智慧树AI" and self._llm_disabled:
                continue
            try:
                ids = answerer.answer(prompt)
            except llm.NonRetryableLLMError as exc:
                if name != "智慧树AI":
                    self._llm_disabled = True
                    if not self._llm_warned:
                        self._llm_warned = True
                        logger.warn(
                            f"自定义大模型不可用({exc}), 本试卷后续题目将跳过 LLM 直接降级.",
                            shift=True,
                        )
                logger.debug(f"{name} 答题快速失败: {exc}")
                continue
            except Exception as exc:
                logger.debug(f"{name} 答题失败: {exc}")
                continue
            by_text = {str(choice["id"]): choice["id"] for choice in choices}
            ids = [by_text[str(item)] for item in ids if str(item) in by_text]
            if ids:
                self._last_ai_provider = name
                logger.debug(f"{name} 给出有效选项: {ids}")
                return ids
            logger.debug(f"{name} 返回的答案不在选项内, 继续兜底.")
        raise ValueError("AI 未给出可用的选项 id")

    def get_question_answer(self, question: dict) -> tuple[list, str]:
        question_id = question.get("id") or question.get("questionId")
        if question_id is None:
            raise ValueError("题目缺少 id")
        question_type = question.get("questionType")
        version = question.get("version", 1)
        choices = [
            {"id": option["id"], "content": option.get("content", "")}
            for option in question.get("optionVos") or []
            if "id" in option
        ]
        valid_ids = {str(choice["id"]) for choice in choices}

        cached = self.answer_bank.get(question_id, version)
        if cached and cached.get("answer"):
            cached_ids = [
                item for item in str(cached["answer"]).split("#@#") if item
            ]
            if not choices or all(item in valid_ids for item in cached_ids):
                time.sleep(random.uniform(1.5, 3.0))
                return cached_ids, "题库"

        if not choices:
            raise ValueError(f"题目 {question_id} 没有可选项")
        if len(choices) < 2:
            return [choices[0]["id"]], "唯一选项"

        try:
            return self._generate_answer(question_type, question, choices), "AI"
        except Exception as exc:
            logger.warn(f"题目 {question_id} AI 作答失败, 改为随机: {exc}")
        time.sleep(random.uniform(1.5, 3.0))
        if question_type == 2:
            return select_random_answers(choices, 2), "随机"
        return select_random_answers(choices, 1), "随机"

    def start(self, reference_materials: list[dict] | None = None) -> tuple[bool, int, int]:
        self.reference_materials = reference_materials or []
        try:
            self.answer_bank.load(self.exam_test_id)
            self.open_exam()
            sheet = self.get_sheet_content()
            if not sheet:
                raise ValueError("试卷没有题目")

            total = len(sheet)
            logger.info(f"开始接口考试作答: 试卷 {self.exam_paper_id}, 共 {total} 题.")
            logger.event("接口考试开始", 试卷=self.exam_paper_id, 题目=total)
            for index, item in enumerate(sheet, 1):
                content = self.get_question_content(
                    item["questionId"], item.get("version", 1)
                )
                if not content:
                    continue
                content["version"] = item.get("version", 1)
                self._last_ai_provider = ""
                try:
                    answers, source = self.get_question_answer(content)
                except Exception as exc:
                    logger.error(f"题目 {item['questionId']} 未作答: {exc}")
                    logger.event(
                        "接口答题",
                        序号=f"{index}/{total}",
                        题目=item["questionId"],
                        结果="未作答",
                        错误=logger.summarize_exception(exc),
                    )
                    continue
                saved = self.save_answer(item["questionId"], answers)
                source_label = (
                    f"AI:{self._last_ai_provider}"
                    if source == "AI" and self._last_ai_provider
                    else source
                )
                q_brief = _brief_html(content.get("content", ""), 48)
                ans_text = _format_answer_choices(content, answers)
                logger.info(
                    f"  [接口答题 {index}/{total}] 题{item['questionId']}({source_label}): "
                    f"{q_brief} -> {ans_text}" + ("" if saved else " [保存失败]")
                )
                logger.event(
                    "接口答题",
                    序号=f"{index}/{total}",
                    题目=item["questionId"],
                    题干=_brief_html(content.get("content", ""), 80),
                    方式=source_label,
                    答案=ans_text,
                    选项ID=",".join(str(a) for a in answers),
                    保存成功=saved,
                )

            self.submit_exam()
            logger.info(f"接口考试试卷 {self.exam_paper_id} 已提交, 正在核对成绩并更新题库...")

            correct_questions = 0
            for item in sheet:
                version = item.get("version", 1)
                content = self.get_question_content(item["questionId"], version)
                if not content:
                    continue
                result = content.get("userAnswerVo")
                if isinstance(result, list):
                    result = result[0] if result else None
                if not isinstance(result, dict):
                    logger.warn(f"题目 {item['questionId']} 没有作答结果.")
                    continue
                correct = [
                    option
                    for option in content.get("optionVos") or []
                    if is_flagged(option.get("isCorrect"))
                ]
                correct_preview = "; ".join(
                    _brief_html(opt.get("content", ""), 36) or str(opt.get("id"))
                    for opt in correct
                )
                if is_flagged(result.get("isCorrect")):
                    correct_questions += 1
                    logger.debug(f"题目 {item['questionId']} 回答正确.")
                else:
                    logger.info(
                        f"题目 {item['questionId']} 答错, 题库已更新"
                        + (f" (正确答案: {correct_preview})." if correct_preview else ".")
                    )
                if not correct:
                    continue
                self.answer_bank.set(
                    item["questionId"],
                    version,
                    {
                        "question": content.get("content", ""),
                        "answer": "#@#".join(str(option["id"]) for option in correct),
                        "answer_content": "\n".join(
                            option.get("content", "") for option in correct
                        ),
                        "questionDict": content,
                    },
                )
            self.answer_bank.save(self.exam_test_id)
            success = correct_questions == total
            logger.info(
                f"接口考试完成: 试卷 {self.exam_paper_id}, 得分 {correct_questions}/{total}"
                + (" (全对)" if success else "")
            )
            logger.event(
                "接口考试结果",
                试卷=self.exam_paper_id,
                得分=f"{correct_questions}/{total}",
                全对=success,
            )
            return success, correct_questions, total
        finally:
            self._stopped.set()


class ExamRunner:
    """按知识点循环取卷、答题、交卷, 直到掌握度达标或达到尝试上限."""

    def __init__(
        self,
        course_id,
        class_id,
        api: ExamApi,
        target: int = 100,
        max_attempts: int = MAX_EXAM_ATTEMPTS,
        op_extra: dict | None = None,
        bank: AnswerBank | None = None,
    ):
        self.course_id = course_id
        self.class_id = class_id
        self.api = api
        self.target = target
        self.max_attempts = max_attempts
        self.op_extra = op_extra or {}
        self.bank = bank or AnswerBank(course_id)
        self.extractor = PptExtractor(
            proxy=getattr(api, "proxy", ""),
            session=getattr(api, "session", None),
        )
        self._materials: dict[str, list[dict]] = {}

    def run(self, nodes: list[dict], deadline: float | None = None) -> dict:
        summary = {"done": [], "skipped": [], "failed": []}
        for index, node in enumerate(nodes, 1):
            if deadline is not None and time.monotonic() >= deadline:
                logger.warn("接口考试达到时限, 停止剩余知识点.")
                break
            if node.get("freeExam"):
                summary["skipped"].append(node.get("name") or node.get("nodeUid"))
                continue
            knowledge_id = node.get("nodeUid")
            if not knowledge_id:
                continue
            name = node.get("name") or knowledge_id
            extra = {
                **self.op_extra,
                "theme": node.get("module") or "",
                "knowledgePoint": name,
            }
            logger.event(
                "知识点考试",
                序号=f"{index}/{len(nodes)}",
                知识点=name,
            )
            result = self._run_node(knowledge_id, name, extra, deadline)
            if result == "done":
                summary["done"].append(name)
            elif result == "skipped":
                summary["skipped"].append(name)
            else:
                summary["failed"].append(name)
        logger.info(
            f"接口考试阶段汇总: 达标 {len(summary['done'])} 个, "
            f"跳过 {len(summary['skipped'])} 个, 未完成 {len(summary['failed'])} 个."
        )
        return summary

    def _collect_materials(self, knowledge_id, name: str) -> list[dict]:
        """按知识点缓存 PPT 参考资料, 多轮重考只下载解析一次."""
        if knowledge_id in self._materials:
            return self._materials[knowledge_id]
        resources = self.api.list_knowledge_resources(
            self.course_id, self.class_id, knowledge_id
        )
        materials = self.extractor.collect(
            resources,
            course_name=str(self.op_extra.get("courseName") or ""),
        )
        self._materials[knowledge_id] = materials
        if materials:
            logger.info(
                f"知识点「{name}」已注入 {len(materials)} 份 PPT 参考资料 "
                f"({sum(len(item['content']) for item in materials)} 字)."
            )
        return materials

    def _run_node(self, knowledge_id, name: str, extra: dict, deadline) -> str:
        for attempt in range(1, self.max_attempts + 1):
            if deadline is not None and time.monotonic() >= deadline:
                return "failed"
            paper = self.api.query_paper(self.course_id, self.class_id, knowledge_id)
            if not paper or not paper.get("paperId"):
                logger.info(f"知识点「{name}」没有可用的考试试卷.")
                return "skipped"
            mastery = as_number(paper.get("masteryScore"))
            if mastery >= self.target:
                logger.info(f"知识点「{name}」掌握度已达 {mastery:g}, 跳过.")
                return "done"
            if mastery < 30 and attempt > 4:
                logger.warn(f"知识点「{name}」掌握度始终低于 30, 放弃.")
                return "failed"
            logger.info(
                f"知识点「{name}」第 {attempt}/{self.max_attempts} 轮考试, "
                f"当前掌握度 {mastery:g}."
            )
            reference_materials = self._collect_materials(knowledge_id, name)
            ctx = ExamCtx(
                api=self.api,
                course_id=self.course_id,
                knowledge_id=knowledge_id,
                exam_test_id=paper["examTestId"],
                exam_paper_id=paper["paperId"],
                op_extra=extra,
                answer_bank=self.bank,
            )
            try:
                success, correct, total = ctx.start(
                    reference_materials=reference_materials
                )
            except Exception as exc:
                logger.log_exception(f"知识点「{name}」考试异常.", exc)
                return "failed"
            logger.info(
                f"知识点「{name}」第 {attempt}/{self.max_attempts} 轮结束: "
                f"得分 {correct}/{total}" + (" (全对)" if success else "")
            )
            logger.event(
                "知识点考试结果",
                知识点=name,
                轮次=attempt,
                得分=f"{correct}/{total}",
                全对=success,
            )
            time.sleep(random.uniform(0.8, 1.6))
        return "failed"


async def answer_review_exam_page(page, config=None) -> bool:
    """把当前复习/模拟考试页接管为接口作答.

    返回 True 表示已接管(无论接口是否成功, 避免 UI 流程再交一次卷);
    返回 False 表示页面参数不足, 交给原有 UI 流程处理.
    """
    info = parse_review_exam_url(getattr(page, "url", "") or "")
    if not info.get("examTestId") or not info.get("paperId"):
        logger.warn("复习考试页缺少 examTestId/paperId, 无法接口作答.")
        return False
    try:
        cookies = await page.context.cookies()
        user_agent = await page.evaluate("navigator.userAgent")
    except Exception as exc:
        logger.log_exception("读取浏览器 Cookie 失败, 无法接口作答.", exc)
        return False
    api = ExamApi(
        cookies,
        proxy=getattr(config, "proxy", "") if config is not None else "",
        user_agent=user_agent,
    )
    ctx = ExamCtx(
        api,
        info.get("courseId") or "review",
        0,
        info["examTestId"],
        info["paperId"],
        op_extra={"knowledgePoint": info.get("nodeName") or "", "来源": "复习考试"},
    )
    logger.event("复习考试接口作答", 试卷=info["paperId"], 考试=info["examTestId"])
    try:
        success, correct, total = await asyncio.to_thread(ctx.start)
    except Exception as exc:
        logger.log_exception("复习考试接口作答失败.", exc)
        logger.warn("复习考试未能通过接口完成, 请手动检查并交卷.")
        return True
    logger.info(
        f"复习/模拟考试接口作答完成: 得分 {correct}/{total}"
        + (" (全对)" if success else "")
    )
    logger.event("复习考试结果", 得分=f"{correct}/{total}", 全对=success)
    return True