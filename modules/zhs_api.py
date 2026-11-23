# encoding=utf-8
"""智慧树接口通用协议层: 表驱动加密查询、CAS SSO 换票与三态会话校验.

本模块只承载协议细节, 业务接口 (考试/作业/AI 课程) 在各自模块中组合使用.
所有请求字段与加密参数的依据见 docs/api_contracts.md.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote, urljoin

import requests

from modules import zhidao_crypto
from modules.http_utils import create_retry_session

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://ai-smart-course-student-pro.zhihuishu.com",
    "Referer": "https://ai-smart-course-student-pro.zhihuishu.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

PASSPORT_ORIGIN = "https://passport.zhihuishu.com"
EXAM_SSO_SERVICE = (
    "https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/student/"
    "getStudentHomework"
)
LOGIN_INFO_SERVICE = (
    "https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/getLoginUserInfo"
)
CAS_LOGIN = f"{PASSPORT_ORIGIN}/cas/login"
LOGIN_INFO_URL = LOGIN_INFO_SERVICE
CAS_TICKET_PARAM = "ticket="

REQUEST_TIMEOUT = 20
GET_TIMEOUT = 20


class ZhsApiError(RuntimeError):
    """智慧树接口返回异常或响应不可解析."""


class CaptchaRequiredError(ZhsApiError):
    """接口要求滑块/点选验证 (知到接口 code == -12)."""


class SessionExpiredError(ZhsApiError):
    """CASTGC/CAS 票据失效, 需要用户重新登录."""


class SessionStatus(Enum):
    OK = "OK"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class QueryStrategy:
    """一类智慧树加密接口的报文策略.

    ok_field / ok_value 为 None 时只校验 HTTP 2xx (无返回体接口).
    """

    key: bytes
    set_timestamp: bool = True
    content_type: str = "form"
    ok_field: str | None = "code"
    ok_value: object = 0
    captcha_on_minus_12: bool = False
    raw_response: bool = False


STRATEGIES: dict[str, QueryStrategy] = {
    # 首页/课程列表: Autovisor 新增, zhs 的 STRATEGIES 中不存在.
    "home": QueryStrategy(zhidao_crypto.HOME_KEY),
    "zhidao": QueryStrategy(
        zhidao_crypto.VIDEO_KEY, captcha_on_minus_12=True
    ),
    "ai_exam": QueryStrategy(zhidao_crypto.EXAM_KEY),
    "ai_exam_submit": QueryStrategy(
        zhidao_crypto.EXAM_KEY, ok_field=None, raw_response=True
    ),
    "ai_task": QueryStrategy(
        zhidao_crypto.AI_KEY, content_type="json", ok_value=200
    ),
    "homework": QueryStrategy(
        zhidao_crypto.EXAM_KEY, set_timestamp=False, ok_field="status", ok_value="200"
    ),
    "zhidao_exam": QueryStrategy(
        zhidao_crypto.EXAM_KEY, set_timestamp=False, ok_field="status", ok_value="200"
    ),
}


def get_strategy(name: str) -> QueryStrategy:
    try:
        return STRATEGIES[name]
    except KeyError as exc:
        raise ValueError(f"未知的智慧树接口策略: {name}") from exc


def build_session(cookies, proxy: str = "", user_agent: str = "") -> requests.Session:
    """用浏览器导出的 Cookie 构造带代理/重试的 requests 会话."""
    session = create_retry_session(proxy, headers=dict(DEFAULT_HEADERS))
    if user_agent:
        session.headers["User-Agent"] = user_agent
    if isinstance(cookies, dict):
        cookies = [{"name": name, "value": value} for name, value in cookies.items()]
    for cookie in cookies or []:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(name, value)
    try:
        raw = session.cookies.get("CASLOGC")
        if raw:
            uuid = json.loads(unquote(raw)).get("uuid")
            if uuid:
                session.cookies.set(f"exitRecod_{uuid}", "2")
    except (ValueError, TypeError):
        pass
    return session


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ZhsApiClient:
    """带 Cookie/代理/重试的智慧树协议客户端."""

    def __init__(self, cookies, proxy: str = "", user_agent: str = ""):
        self.proxy = proxy
        self.session = build_session(cookies, proxy, user_agent)
        self.lock = threading.RLock()
        self._exam_sso_ready = False

    def reset_exam_sso(self) -> None:
        self._exam_sso_ready = False

    # ------------------------------------------------------------------ query
    def query(
        self,
        strategy_name: str,
        url: str,
        data: dict,
        method: str = "POST",
        *,
        extra_fields: dict | None = None,
    ) -> dict:
        """按命名策略加密提交并校验返回码.

        extra_fields 不参与 secretStr 加密, 作为普通字段一并提交
        (例如知到考试 saveStudentAnswer 的 source=1).
        """
        return self.execute(
            get_strategy(strategy_name),
            url,
            data,
            method=method,
            extra_fields=extra_fields,
        )

    def execute(
        self,
        strategy: QueryStrategy,
        url: str,
        data: dict,
        *,
        method: str = "POST",
        extra_fields: dict | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> dict:
        payload = dict(data)
        timestamp = None
        if strategy.set_timestamp:
            timestamp = int(time.time()) * 1000
            payload["dateFormate"] = timestamp
        form: dict = {
            "secretStr": zhidao_crypto.encrypt(
                json.dumps(payload, ensure_ascii=False), key=strategy.key
            )
        }
        if timestamp is not None:
            form["dateFormate"] = timestamp
        if extra_fields:
            form.update(extra_fields)

        method = (method or "POST").upper()
        with self.lock:
            if method == "GET":
                response = self.session.get(url, params=form, timeout=timeout)
            elif strategy.content_type == "json":
                response = self.session.post(
                    url,
                    data=json.dumps(form),
                    headers={"Content-Type": "application/json;charset=UTF-8"},
                    timeout=timeout,
                )
            else:
                response = self.session.post(url, data=form, timeout=timeout)

        response.raise_for_status()
        if strategy.raw_response:
            try:
                parsed = response.json()
            except (ValueError, AttributeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        try:
            ret = response.json()
        except ValueError as exc:
            text = getattr(response, "text", "") or ""
            raise ZhsApiError(f"接口返回非 JSON: {str(text)[:120]}") from exc
        if not isinstance(ret, dict):
            raise ZhsApiError(f"接口返回结构异常: {str(ret)[:120]}")

        code = _as_int(ret.get("code"))
        if strategy.captcha_on_minus_12 and code == -12:
            raise CaptchaRequiredError(
                f"接口要求验证码(code=-12): {ret.get('message') or ret}"
            )
        if strategy.ok_field is not None:
            expected = strategy.ok_value
            actual = ret.get(strategy.ok_field)
            if str(actual) != str(expected):
                raise ZhsApiError(
                    f"{strategy.ok_field}={actual} msg={ret.get('message') or ret}"
                )
        return ret

    # ----------------------------------------------------------------- cas sso
    def _cas_login(self, service: str) -> tuple[str | None, int]:
        """请求 CAS 登录入口, 返回 (service 票据地址, HTTP 状态码)."""
        url = f"{CAS_LOGIN}?service={service}"
        with self.lock:
            response = self.session.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT)
            status = int(getattr(response, "status_code", 0) or 0)
            headers = getattr(response, "headers", {}) or {}
            location = headers.get("Location") or headers.get("location") or ""
            if status == 302 and location and CAS_TICKET_PARAM in location:
                target = urljoin(PASSPORT_ORIGIN, location)
                self.session.get(target, allow_redirects=True, timeout=REQUEST_TIMEOUT)
                return target, status
        return None, status

    def ensure_exam_sso(self, force: bool = False) -> None:
        """确保 studentexam-api 会话可用: 通过 CAS 换票种下会话 Cookie.

        已换票则直接返回; force=True 时会重置状态重新换票.
        """
        with self.lock:
            if self._exam_sso_ready and not force:
                return
            ticket_url, _ = self._cas_login(EXAM_SSO_SERVICE)
            if not ticket_url:
                self._exam_sso_ready = False
                raise SessionExpiredError("CAS 未签发考试会话票据, 请重新登录智慧树.")
            self._exam_sso_ready = True

    def verify_session_status(self) -> SessionStatus:
        """用 CAS + getLoginUserInfo 判断登录态: OK / EXPIRED / UNAVAILABLE.

        HTTP 5xx 或连接异常视为服务不可用, 不判定为掉线, 也不会清空本地 Cookie.
        """
        try:
            ticket_url, cas_status = self._cas_login(LOGIN_INFO_SERVICE)
        except requests.exceptions.RequestException:
            return SessionStatus.UNAVAILABLE
        if cas_status >= 500:
            return SessionStatus.UNAVAILABLE
        if not ticket_url:
            return SessionStatus.EXPIRED

        try:
            with self.lock:
                response = self.session.get(
                    f"{LOGIN_INFO_URL}?time={int(time.time() * 1000)}",
                    timeout=GET_TIMEOUT,
                )
        except requests.exceptions.RequestException:
            return SessionStatus.UNAVAILABLE

        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 500:
            return SessionStatus.UNAVAILABLE
        if status >= 400:
            return SessionStatus.EXPIRED
        try:
            data = response.json()
        except ValueError:
            return SessionStatus.EXPIRED
        if not isinstance(data, dict):
            return SessionStatus.EXPIRED
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        if result.get("uuid") or result.get("username"):
            return SessionStatus.OK
        return SessionStatus.EXPIRED