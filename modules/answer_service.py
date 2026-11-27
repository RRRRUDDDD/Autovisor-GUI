# encoding=utf-8
"""跨答题引擎共享的外部题库懒加载器与 LLM 熔断/多级回退服务。"""
from __future__ import annotations

from collections.abc import Callable

from modules import llm
from modules.logger import Logger
from modules.question_bank import QuestionBankClient, QuestionBankResult

logger = Logger()


class QuestionBankHelper:
    """统一管理 QuestionBankClient 的懒加载、作用域校验、查询降级、统计输出与资源关闭。"""

    def __init__(self, client=None, *, proxy: str = "", scope: str = ""):
        self.client = client
        self.proxy = str(proxy or "")
        self.scope = str(scope or "").strip().lower()
        self.ready = client is not None
        self.owns_client = False

    def get(self):
        if self.ready:
            return self.client
        self.ready = True
        try:
            self.client = QuestionBankClient.from_config(proxy=self.proxy)
            self.owns_client = self.client is not None
        except Exception as exc:
            logger.debug(f"初始化外部题库失败: {exc}")
            self.client = None
            self.owns_client = False
        return self.client

    def allows_scope(self, bank=None) -> bool:
        target = self.client if bank is None else bank
        if target is None or not self.scope:
            return True
        allows_fn = getattr(target, "allows_scope", None)
        if callable(allows_fn):
            try:
                return bool(allows_fn(self.scope))
            except Exception as exc:
                logger.debug(f"外部题库作用域判断异常: {exc}")
                return False
        return True

    def query_result(
        self, title: str, options: list[str], question_type
    ) -> QuestionBankResult | None:
        if (
            type(self).query is not QuestionBankHelper.query
            or "query" in self.__dict__
        ):
            try:
                answer = self.query(title, options, question_type) or ""
            except Exception as exc:
                logger.debug(f"外部题库查询降级: {exc}")
                return None
            return QuestionBankResult(answer=str(answer), ai=False) if answer else None
        bank = self.get()
        if bank is None or not bank.enabled():
            return None
        if not self.allows_scope(bank):
            return None
        try:
            query_result_fn = getattr(bank, "query_result", None)
            if callable(query_result_fn):
                if self.scope:
                    return query_result_fn(
                        title, options, question_type, scope=self.scope
                    )
                return query_result_fn(title, options, question_type)
            answer = bank.query(title, options, question_type) or ""
            if not answer:
                return None
            return QuestionBankResult(answer=str(answer), ai=False)
        except Exception as exc:
            logger.debug(f"外部题库查询降级: {exc}")
            return None

    def query(self, title: str, options: list[str], question_type) -> str:
        result = self.query_result(title, options, question_type)
        return result.answer if result is not None else ""

    def log_stats(self) -> None:
        if self.client is None:
            return
        try:
            self.client.log_stats()
        except Exception as exc:
            logger.debug(f"外部题库统计输出失败: {exc}")

    def close(self) -> None:
        if self.owns_client and self.client is not None:
            close_fn = getattr(self.client, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
            self.owns_client = False

    def __enter__(self) -> QuestionBankHelper:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class LlmCircuitBreaker:
    """记录自定义大模型不可重试故障（401/402 等），避免整卷逐题重复超时。"""

    def __init__(self) -> None:
        self.disabled = False
        self.warned = False

    def trip(
        self,
        exc: BaseException,
        *,
        warn_message: str | None = None,
        event_name: str | None = None,
    ) -> None:
        self.disabled = True
        if not self.warned:
            self.warned = True
            msg = warn_message or (
                f"自定义大模型不可用({exc}), 本试卷后续题目将跳过 LLM 直接降级."
            )
            logger.warn(msg, shift=True)
            if event_name:
                logger.event(
                    event_name,
                    结果="LLM 已禁用",
                    错误=logger.summarize_exception(exc),
                )

    def reset(self) -> None:
        self.disabled = False
        self.warned = False

    def run_fallback_chain(
        self,
        providers: list[tuple[str, Callable[[], object], bool]],
        *,
        validate: Callable[[object], object] | None = None,
        warn_message_factory: Callable[[BaseException], str] | None = None,
        fail_log_prefix: str = "答题",
    ) -> tuple[str, object]:
        """按优先级依次调用答题提供方；遇到 NonRetryableLLMError 时对受控 LLM 触发熔断。

        providers 元素格式: (provider_name, invoke_fn, is_custom_llm)
        返回 (命中提供方名, 有效结果)；全部失败抛出 ValueError。
        """
        for name, invoke_fn, is_custom_llm in providers:
            if is_custom_llm and self.disabled:
                continue
            try:
                raw = invoke_fn()
            except llm.NonRetryableLLMError as exc:
                if is_custom_llm:
                    warn_msg = (
                        warn_message_factory(exc)
                        if callable(warn_message_factory)
                        else None
                    )
                    self.trip(exc, warn_message=warn_msg)
                logger.debug(f"{name} {fail_log_prefix}快速失败: {exc}")
                continue
            except Exception as exc:
                logger.debug(f"{name} {fail_log_prefix}失败: {exc}")
                continue
            result = validate(raw) if callable(validate) else raw
            if result:
                return name, result
            logger.debug(f"{name} 返回结果无效, 继续兜底.")
        raise ValueError(f"AI 未给出有效的{fail_log_prefix}结果")
