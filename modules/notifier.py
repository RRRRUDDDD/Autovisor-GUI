# encoding=utf-8
"""通知服务模块，用于向外部服务或系统发送通知消息。

支持多种通知服务：
- Windows: Windows 系统通知（Toast，通过 PowerShell 5.1 调用 WinRT 接口）
- ServerChan: Server酱
- Qmsg: Qmsg酱
- Bark: Bark (iOS)
- Telegram: Telegram Bot
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import configparser
import os
import re
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional
from urllib.parse import urlparse, urlsplit
from xml.sax.saxutils import escape

import requests

from modules.http_utils import build_requests_proxies
from modules.redaction import (
    mask_secret,
    redact_notification as redact,
    register_config,
    register_secret,
)

NOTIFICATION_TIMEOUT = (5, 10)
_ORIGINAL_REQUESTS_POST = requests.post


class NotificationService(ABC):
    """通知服务基类，定义通知服务的公共接口和实现。"""

    CONFIG_PATH = "config.ini"

    def __init__(self, *, proxy: str = "", logger=None, session=None):
        self.name = self.__class__.__name__
        self.url = ""
        self.tg_chat_id = ""
        self._conf: Optional[Dict[str, str]] = None
        self.disabled = False
        self.proxy = str(proxy or "").strip()
        self._logger = logger
        self._session = session
        self._owns_session = False
        self._session_lock = threading.Lock()
        self.last_error = ""

    def close(self) -> None:
        with self._session_lock:
            if self._owns_session and self._session is not None:
                close_fn = getattr(self._session, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass
                self._session = None
                self._owns_session = False

    def __enter__(self) -> "NotificationService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _log_info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(redact(message))

    def _log_error(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warn(redact(message), shift=True)

    def config_set(self, config: Dict[str, str]) -> None:
        """设置通知服务的配置。"""
        register_config(config)
        self._conf = dict(config)

    def _load_config_from_file(self) -> Optional[Dict[str, str]]:
        """从配置文件中加载通知服务的配置。"""
        try:
            config = configparser.ConfigParser()
            config.read(self.CONFIG_PATH, encoding="utf8")
            section = dict(config["notification"])
            register_config(section)
            return section
        except (KeyError, FileNotFoundError):
            self._log_info("未找到notification配置，已忽略外部通知功能")
            self.disabled = True
            return None

    def init_notification(self) -> None:
        """初始化通知服务，加载配置并进行必要的设置。"""
        if not self._conf:
            self._conf = self._load_config_from_file()

        if not self.disabled and self._conf:
            self._init_service()

    @abstractmethod
    def _init_service(self) -> None:
        """初始化特定的通知服务，由子类实现。"""
        pass

    @abstractmethod
    def _send(self, message: str) -> bool:
        """发送通知消息，由子类实现。"""
        pass

    def send(self, message: str, *, attempts: int = 2) -> bool:
        """发送通知消息的公共接口；失败自动重试，返回是否至少一次确认成功。"""
        if self.disabled:
            return True
        self.last_error = ""
        text = redact(message)
        for _ in range(max(1, attempts)):
            if self._send(text):
                self.last_error = ""
                return True
        return False

    def _post(self, provider: str, ok: Optional[Callable[[dict], bool]] = None, **kwargs) -> bool:
        """HTTP 通知服务共用的发送路径：POST 后校验状态码与 JSON 响应。"""
        req_kwargs = dict(kwargs)
        req_kwargs.setdefault("timeout", NOTIFICATION_TIMEOUT)
        if self.proxy and "proxies" not in req_kwargs:
            req_kwargs["proxies"] = build_requests_proxies(self.proxy)
        try:
            session = self._session
            if session is None and requests.post is not _ORIGINAL_REQUESTS_POST:
                response = requests.post(self.url, **req_kwargs)
            else:
                if session is None:
                    with self._session_lock:
                        if self._session is None:
                            self._session = requests.Session()
                            self._owns_session = True
                        session = self._session
                response = session.post(self.url, **req_kwargs)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            self.last_error = f"{provider}通知发送失败: {redact(exc)}"
            self._log_error(self.last_error)
            return False
        except ValueError as exc:
            self.last_error = f"{provider}返回数据解析失败: {redact(exc)}"
            self._log_error(self.last_error)
            return False
        except Exception as exc:
            self.last_error = f"{provider}通知发送失败: {type(exc).__name__}: {redact(exc)}"
            self._log_error(self.last_error)
            return False
        if ok is not None and not ok(result):
            self.last_error = f"{provider}通知发送失败: {redact(result)}"
            self._log_error(self.last_error)
            return False
        self._log_info(f"{provider}通知发送成功")
        return True


class DefaultNotification(NotificationService):
    """默认通知服务，当未配置任何通知服务时使用。"""

    def _init_service(self) -> None:
        pass

    def _send(self, message: str) -> bool:
        return True

    def get_notification_from_config(self) -> NotificationService:
        """根据配置创建具体的通知服务实例。"""
        if not self._conf:
            self._conf = self._load_config_from_file()

        if self.disabled:
            return self

        try:
            provider_name = str(self._conf.get("provider") or "").strip()
            if not provider_name:
                raise KeyError("未指定通知服务提供商")

            # 获取对应的通知服务类（仅注册表白名单内的类名可实例化）
            provider_class = PROVIDER_REGISTRY.get(provider_name)
            if not provider_class:
                self.last_error = f"未找到名为 {provider_name} 的通知服务提供商"
                self._log_error(self.last_error)
                self.disabled = True
                return self

            service = provider_class(
                proxy=self.proxy,
                logger=self._logger,
                session=self._session,
            )
            service.config_set(self._conf)
            return service
        except KeyError:
            self.disabled = True
            self._log_info("未找到外部通知配置，已忽略外部通知功能")
            return self


class ServerChan(NotificationService):
    """Server酱通知服务。"""

    def _init_service(self) -> None:
        if not self._conf or not str(self._conf.get("url") or "").strip():
            self.disabled = True
            self._log_info("未找到Server酱url配置，已忽略该通知服务")
            return

        self.url = str(self._conf["url"]).strip()
        self._log_info("已初始化Server酱通知服务")

    def _send(self, message: str) -> bool:
        # text 与 desp 同时携带消息，兼容两个版本的Server酱
        return self._post(
            "Server酱",
            json={"text": message, "desp": message},
            headers={"Content-Type": "application/json;charset=utf-8"},
        )


class Qmsg(NotificationService):
    """Qmsg酱通知服务。"""

    def _init_service(self) -> None:
        if not self._conf or not str(self._conf.get("url") or "").strip():
            self.disabled = True
            self._log_info("未找到Qmsg酱url配置，已忽略该通知服务")
            return

        self.url = str(self._conf["url"]).strip()
        self._log_info("已初始化Qmsg酱通知服务")

    def _send(self, message: str) -> bool:
        return self._post(
            "Qmsg酱",
            params={"msg": message},
            headers={"Content-Type": "application/json;charset=utf-8"},
        )


class Bark(NotificationService):
    """Bark 通知服务。"""

    def _init_service(self) -> None:
        if not self._conf or not str(self._conf.get("url") or "").strip():
            self.disabled = True
            self._log_info("未找到Bark的url配置，已忽略该通知服务")
            return

        self.url = str(self._conf["url"]).strip()
        self._log_info("已初始化Bark通知服务")

    def _send(self, message: str) -> bool:
        return self._post("Bark", params={"body": message})


class Telegram(NotificationService):
    """通过 Telegram Bot 发送通知。"""

    def _init_service(self) -> None:
        if (
            not self._conf
            or not str(self._conf.get("url") or "").strip()
            or not str(self._conf.get("tg_chat_id") or "").strip()
        ):
            self.disabled = True
            self._log_info("未找到Telegram的url或tg_chat_id配置，已忽略该通知服务")
            return
        self.tg_chat_id = str(self._conf["tg_chat_id"]).strip()
        self.url = str(self._conf["url"]).strip()
        self._log_info("已初始化Telegram通知服务")

    def _send(self, message: str) -> bool:
        # parse_mode=HTML 要求正文先转义，防止消息里的 <>& 被当作标签解析
        return self._post(
            "Telegram",
            data={
                "chat_id": self.tg_chat_id,
                "text": escape(message),
                "parse_mode": "HTML",
            },
            ok=lambda result: bool(isinstance(result, dict) and result.get("ok")),
        )


class Windows(NotificationService):
    """Windows 系统通知（Toast），通过 Windows PowerShell 5.1 调用 WinRT 接口发送。

    注意：必须使用 Windows PowerShell 5.1 (powershell.exe)，
    PowerShell 7 (pwsh) 没有 WinRT 投影，无法加载 Toast 类型。
    """

    _TOAST_SCRIPT = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
        "<text></text></binding></visual></toast>'); "
        "$textNode = $xml.GetElementsByTagName('text').Item(0); "
        "$textNode.AppendChild($xml.CreateTextNode($env:AUTOVISOR_TOAST_TEXT)) | Out-Null; "
        "$toast = New-Object Windows.UI.Notifications.ToastNotification($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'Microsoft.Windows.Explorer').Show($toast)"
    )

    def _init_service(self, *, probe: bool = True) -> None:
        if sys.platform != "win32":
            self.disabled = True
            self._log_info("当前系统不是 Windows，已忽略Windows系统通知服务")
            return

        if probe:
            # 预先探测 PowerShell 能否成功弹出 Toast（同时作为启用确认）
            ok, err = self._run_powershell(self._build_script(), "Windows 系统通知已启用")
            if not ok:
                self.disabled = True
                self.last_error = f"Windows系统通知不可用，已忽略该通知服务: {err}"
                self._log_error(self.last_error)
                return

        self._log_info("已初始化Windows系统通知服务")

    @staticmethod
    def _build_script(_message: str = "") -> str:
        """返回静态 PowerShell Toast 脚本，消息正文统一经由环境变量传递，绝不拼接进脚本。"""
        return Windows._TOAST_SCRIPT

    @staticmethod
    def _run_powershell(script: str, message: str = "") -> tuple[bool, str]:
        """执行 PowerShell 脚本，通过环境变量 AUTOVISOR_TOAST_TEXT 安全传参。"""
        try:
            env = dict(os.environ)
            env["AUTOVISOR_TOAST_TEXT"] = str(message).replace("\x00", "")
            kwargs = {"capture_output": True, "timeout": 15, "env": env}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                **kwargs,
            )
            if result.returncode == 0:
                return True, ""
            return False, result.stderr.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            return False, str(exc)

    def _send(self, message: str) -> bool:
        ok, err = self._run_powershell(self._build_script(), message)
        if ok:
            self._log_info("Windows系统通知发送成功")
        else:
            self.last_error = f"Windows系统通知发送失败: {err}"
            self._log_error(self.last_error)
        return ok


# provider 白名单：get_notification_from_config 只接受这些类名，避免 globals() 查找误实例化无关对象。
PROVIDER_REGISTRY: Dict[str, type[NotificationService]] = {
    provider.__name__: provider for provider in (ServerChan, Qmsg, Bark, Telegram, Windows)
}

PROVIDER_LABELS: Dict[str, str] = {
    "Windows": "Windows 系统通知",
    "ServerChan": "Server酱",
    "Qmsg": "Qmsg酱",
    "Bark": "Bark",
    "Telegram": "Telegram",
}

# 默认通知工厂别名
Notification = DefaultNotification


def normalize_provider(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value in PROVIDER_REGISTRY:
        return value
    lowered = value.lower()
    for name in PROVIDER_REGISTRY:
        if name.lower() == lowered:
            return name
    return ""


class Notifier:
    """Autovisor 通知封装器，内部直接委托给 NotificationService。"""

    def __init__(
        self,
        provider: str = "",
        url: str = "",
        tg_chat_id: str = "",
        proxy: str = "",
        notify_on_verify: bool = True,
        notify_on_finish: bool = True,
        notify_on_error: bool = True,
        logger=None,
        session=None,
    ):
        self.provider = normalize_provider(provider)
        self.url = str(url or "").strip()
        self.tg_chat_id = str(tg_chat_id or "").strip()
        self.proxy = str(proxy or "").strip()
        self.notify_on_verify = bool(notify_on_verify)
        self.notify_on_finish = bool(notify_on_finish)
        self.notify_on_error = bool(notify_on_error)
        self.logger = logger
        self._session = session
        self._service: Optional[NotificationService] = None
        self._service_lock = threading.Lock()

        register_config(
            {
                "provider": self.provider,
                "url": self.url,
                "tg_chat_id": self.tg_chat_id,
            }
        )

    @classmethod
    def from_config(cls, config, logger=None) -> "Notifier":
        return cls(
            provider=getattr(config, "notification_provider", ""),
            url=getattr(config, "notification_url", ""),
            tg_chat_id=getattr(config, "notification_tg_chat_id", ""),
            proxy=getattr(config, "proxy", ""),
            notify_on_verify=getattr(config, "notify_on_verify", True),
            notify_on_finish=getattr(config, "notify_on_finish", True),
            notify_on_error=getattr(config, "notify_on_error", True),
            logger=logger,
        )

    @property
    def enabled(self) -> bool:
        if not self.provider:
            return False
        if self.provider == "Windows":
            return sys.platform == "win32"
        if self.provider == "Telegram":
            return bool(self.url and self.tg_chat_id)
        return bool(self.url)

    def configured_channels(self) -> list[str]:
        return [self.provider] if self.enabled else []

    def _build_service(self) -> NotificationService:
        with self._service_lock:
            if self._service is not None:
                return self._service
            factory = Notification(proxy=self.proxy, logger=self.logger, session=self._session)
            factory.config_set(
                {
                    "provider": self.provider,
                    "url": self.url,
                    "tg_chat_id": self.tg_chat_id,
                }
            )
            service = factory.get_notification_from_config()
            if isinstance(service, Windows):
                # 避免每次初始化都多弹一条“已启用”测试弹窗，由实际 send() 发送
                service._init_service(probe=False)
            else:
                service.init_notification()
            self._service = service
            return service

    @staticmethod
    def _compose_message(title: str, content: str) -> str:
        t = str(title or "").strip()
        c = str(content or "").strip()
        if t and c:
            return f"{t}\n{c}"
        return c or t

    def send(self, title: str, content: str) -> dict[str, tuple[bool, str]]:
        """同步向已配置通知服务发送消息，返回 {provider: (ok, detail)}。"""
        if not self.enabled:
            return {}
        service = self._build_service()
        if service.disabled:
            detail = service.last_error or "通知服务未启用或配置不完整"
            return {self.provider: (False, detail)}

        message = self._compose_message(title, content)
        ok = service.send(message)
        detail = "ok" if ok else (service.last_error or "通知服务未确认送达")

        if self.logger:
            if ok:
                self.logger.event("消息推送", 渠道=self.provider, 结果="成功", 标题=title)
            else:
                self.logger.event("消息推送", 渠道=self.provider, 结果="失败", 原因=detail)
        return {self.provider: (ok, detail)}

    async def send_async(self, title: str, content: str) -> dict[str, tuple[bool, str]]:
        if not self.enabled:
            return {}
        return await asyncio.to_thread(self.send, title, content)

    async def notify_verify(self, detail: str = "检测到安全验证，需要人工处理") -> dict:
        if not self.enabled or not self.notify_on_verify:
            return {}
        return await self.send_async("Autovisor GUI 需要人工处理", detail)

    async def notify_finish(self, detail: str) -> dict:
        if not self.enabled or not self.notify_on_finish:
            return {}
        return await self.send_async("Autovisor GUI 学习任务结束", detail)

    async def notify_error(self, detail: str) -> dict:
        if not self.enabled or not self.notify_on_error:
            return {}
        return await self.send_async("Autovisor GUI 运行异常", detail)

    def close(self) -> None:
        if self._service is not None:
            close_fn = getattr(self._service, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass
            self._service = None

    def __enter__(self) -> "Notifier":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def notify_error_sync(self, detail: str) -> dict:
        if not self.enabled or not self.notify_on_error:
            return {}
        return self.send("Autovisor GUI 运行异常", detail)


def test_notification_config(
    provider: str,
    url: str = "",
    tg_chat_id: str = "",
    proxy: str = "",
) -> str:
    """校验通知配置并发送一条测试通知，成功返回中文提示，失败抛出 ValueError/RuntimeError。"""
    raw_provider = str(provider or "").strip()
    if not raw_provider:
        raise ValueError("请先选择通知服务")
    normalized = normalize_provider(raw_provider)
    if not normalized:
        raise ValueError(f"不支持的通知服务: {raw_provider}")

    url_str = str(url or "").strip()
    chat_id_str = str(tg_chat_id or "").strip()
    proxy_str = str(proxy or "").strip()

    if normalized != "Windows":
        if not (url_str.startswith("http://") or url_str.startswith("https://")):
            raise ValueError("请先填写有效的通知 URL（以 http:// 或 https:// 开头）")
        if normalized == "Telegram" and not chat_id_str:
            raise ValueError("使用 Telegram 通知时必须填写 Chat ID")

    with Notifier(
        provider=normalized,
        url=url_str,
        tg_chat_id=chat_id_str,
        proxy=proxy_str,
    ) as notifier:
        if not notifier.enabled:
            raise ValueError("当前环境不支持或未正确配置所选通知服务")

        results = notifier.send(
            "Autovisor GUI 推送测试",
            "这是一条来自 Autovisor GUI 桌面端的测试通知，推送通道工作正常。",
        )
    ok, detail = results.get(normalized, (False, "通知服务未确认送达"))
    label = PROVIDER_LABELS.get(normalized, normalized)
    if not ok:
        raise RuntimeError(detail or f"{label}通知发送失败")
    return f"{label}通知发送成功"
