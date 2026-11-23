# encoding=utf-8
"""智慧树终端二维码登录与二维码图片渲染/导出模块。"""
from base64 import b64decode
import io
import json
import os
import threading
import time
import uuid

from PIL import Image, ImageOps

from modules.http_utils import create_retry_session
from modules.utils import filter_zhihuishu_cookies, get_runtime_path

QR_IMG_URL = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrImg"
QR_INFO_URL = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrInfo"
QR_LOGIN_SERVICE_URL = (
    "https://passport.zhihuishu.com/login"
    "?service=https://onlineweb.zhihuishu.com/onlinestuh5"
)

_DEFAULT_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://passport.zhihuishu.com/login",
}


def render_qr_unicode(img_bytes: bytes, size: int = 47) -> str:
    """使用 Unicode 半块字符渲染二维码，适合无 ANSI 颜色支持或 UTF-8 终端。"""
    img = Image.open(io.BytesIO(img_bytes))
    qr = ImageOps.grayscale(img.resize((size, size), Image.Resampling.NEAREST))
    chars = (" ", "▀", "▄", "█")
    col, row = qr.size

    def _cell(x: int, y: int) -> str:
        top_dark = qr.getpixel((x, y)) < 128
        below_dark = (y + 1 < row) and qr.getpixel((x, y + 1)) < 128
        return chars[int(top_dark) + int(below_dark) * 2]

    border = chars[0] * (col + 2)
    lines = [border]
    for y in range(0, row, 2):
        row_chars = "".join(_cell(x, y) for x in range(col))
        lines.append(f"{chars[0]}{row_chars}{chars[0]}")
    lines.append(border)
    return "\n".join(lines)


def render_qr_tty(img_bytes: bytes, size: int = 47) -> str:
    """使用 ANSI 背景色块渲染二维码，适合支持真彩/256 色的终端。"""
    img = Image.open(io.BytesIO(img_bytes))
    qr = ImageOps.grayscale(img.resize((size, size), Image.Resampling.NEAREST))
    white = "\033[0;37;47m  "
    black = "\033[0;37;40m  "
    reset = "\033[0m"
    col, row = qr.size

    border = white * (col + 2) + reset
    lines = [border]
    for y in range(row):
        row_cells = "".join(
            white if qr.getpixel((x, y)) > 128 else black for x in range(col)
        )
        lines.append(f"{white}{row_cells}{white}{reset}")
    lines.append(border)
    return "\n".join(lines)


def save_qr_image(img_bytes: bytes, save_dir: str) -> str:
    """将登录二维码 PNG 保存到指定目录，返回保存后的绝对路径。"""
    target_dir = os.path.abspath(os.path.expanduser(save_dir))
    os.makedirs(target_dir, exist_ok=True)
    filename = f"zhs-qr-{time.strftime('%Y%m%d-%H%M%S')}.png"
    target_path = os.path.join(target_dir, filename)
    with open(target_path, "wb") as file:
        file.write(img_bytes)
    return target_path


def display_qr_image(
    img_bytes: bytes,
    show_in_terminal: bool = True,
    ensure_unicode: bool = False,
    save_dir: str = "",
    logger=None,
) -> str | None:
    """展示并可选导出二维码图片，返回导出的图片路径（若未导出则返回 None）。"""
    saved_path = None
    if save_dir:
        saved_path = save_qr_image(img_bytes, save_dir)
        if logger:
            logger.info(f"登录二维码已保存至: {saved_path}", shift=True)
        else:
            print(f"登录二维码已保存至: {saved_path}")

    if show_in_terminal:
        rendered = (
            render_qr_unicode(img_bytes)
            if ensure_unicode
            else render_qr_tty(img_bytes)
        )
        print(rendered)
    elif not saved_path:
        img = Image.open(io.BytesIO(img_bytes))
        threading.Thread(target=img.show, daemon=True).start()

    if logger:
        logger.info("请使用智慧树 / 知到 App 扫描二维码完成登录...", shift=True)
    else:
        print("请使用智慧树 / 知到 App 扫描二维码完成登录...")
    return saved_path


def requests_cookies_to_playwright(cookie_jar) -> list[dict]:
    """将 requests.CookieJar 转换为 Playwright context.add_cookies 可用格式。"""
    raw_cookies = []
    for cookie in cookie_jar:
        name = getattr(cookie, "name", "")
        value = getattr(cookie, "value", "")
        domain = getattr(cookie, "domain", "") or ".zhihuishu.com"
        if not name:
            continue
        item = {
            "name": str(name),
            "value": str(value if value is not None else ""),
            "domain": str(domain),
            "path": str(getattr(cookie, "path", None) or "/"),
            "secure": bool(getattr(cookie, "secure", False)),
            "httpOnly": False,
        }
        expires = getattr(cookie, "expires", None)
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        raw_cookies.append(item)
    return filter_zhihuishu_cookies(raw_cookies)


def qr_login_for_cookies(
    *,
    show_in_terminal: bool = True,
    ensure_unicode: bool = False,
    save_dir: str = "",
    proxy: str = "",
    poll_interval: float = 1.0,
    max_refreshes: int = 3,
    qr_callback=None,
    logger=None,
    session=None,
    sleep_fn=time.sleep,
) -> list[dict]:
    """通过官方二维码登录接口获取智慧树登录 Cookies（Playwright 格式）。"""
    own_session = session is None
    client = session or create_retry_session(proxy=proxy, headers=_DEFAULT_HEADERS)
    try:
        for attempt in range(1, max_refreshes + 1):
            resp = client.get(QR_IMG_URL, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            qr_token = payload.get("qrToken")
            img_b64 = payload.get("img")
            if not qr_token or not img_b64:
                raise RuntimeError("获取登录二维码失败：接口未返回有效 qrToken 或图片数据")

            img_bytes = b64decode(img_b64)
            if qr_callback is not None:
                qr_callback(img_bytes)
            else:
                display_qr_image(
                    img_bytes,
                    show_in_terminal=show_in_terminal,
                    ensure_unicode=ensure_unicode,
                    save_dir=save_dir,
                    logger=logger,
                )

            scanned = False
            expired = False
            while True:
                sleep_fn(poll_interval)
                poll_resp = client.get(
                    QR_INFO_URL,
                    params={"qrToken": qr_token},
                    timeout=15,
                )
                poll_resp.raise_for_status()
                info = poll_resp.json()
                status = info.get("status")
                if status == -1:
                    continue
                if status == 0:
                    if not scanned:
                        scanned = True
                        if logger:
                            logger.info("二维码已扫描，请在手机上确认登录...", shift=True)
                    continue
                if status == 1:
                    once_password = info.get("oncePassword")
                    if not once_password:
                        raise RuntimeError("扫码确认成功，但未返回一次性登录凭证")
                    client.get(
                        QR_LOGIN_SERVICE_URL,
                        params={"pwd": once_password},
                        timeout=15,
                    )
                    cookies = requests_cookies_to_playwright(client.cookies)
                    if not cookies:
                        raise RuntimeError("扫码登录未获取到有效的智慧树 Cookies")
                    if logger:
                        logger.event("二维码登录", 结果="成功", Cookie条数=len(cookies))
                    return cookies
                if status == 2:
                    expired = True
                    if logger:
                        logger.warn(
                            f"二维码已过期，正在刷新 ({attempt}/{max_refreshes})...",
                            shift=True,
                        )
                    break
                if status == 3:
                    raise RuntimeError("已在手机端取消二维码登录")
                raise RuntimeError(f"未知的二维码登录状态: {status} ({info.get('msg') or ''})")

            if not expired:
                break

        raise RuntimeError(f"二维码连续过期 {max_refreshes} 次，扫码登录已中止")
    finally:
        if own_session:
            client.close()


def _write_cookies_atomic(cookie_path: str, cookies: list[dict]) -> None:
    target = os.path.abspath(cookie_path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{target}.tmp.{os.getpid()}"
    payload = json.dumps(cookies, ensure_ascii=False, indent=2)
    with open(tmp_path, "w", encoding="utf-8") as file:
        file.write(payload)
    try:
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, target)


class QrLoginManager:
    """面向桌面端 HTTP API 的无浏览器二维码登录会话管理器。"""

    def __init__(self, session_factory=None, ttl_secs: float = 300.0):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._session_factory = session_factory
        self._ttl_secs = max(30.0, float(ttl_secs))

    def _create_session(self, proxy: str = ""):
        if self._session_factory is not None:
            return self._session_factory(proxy)
        return create_retry_session(proxy=proxy, headers=_DEFAULT_HEADERS)

    def _cleanup_expired_locked(self) -> None:
        now = time.monotonic()
        expired_ids = [
            sid
            for sid, item in self._sessions.items()
            if now - item["created_at"] > self._ttl_secs
        ]
        for sid in expired_ids:
            entry = self._sessions.pop(sid, None)
            if entry is not None:
                try:
                    entry["client"].close()
                except Exception:
                    pass

    def _pop_and_close(self, session_id: str) -> bool:
        with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        try:
            entry["client"].close()
        except Exception:
            pass
        return True

    def start(self, proxy: str = "", save_dir: str = "") -> dict:
        with self._lock:
            self._cleanup_expired_locked()
        client = self._create_session(proxy=proxy)
        try:
            resp = client.get(QR_IMG_URL, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            qr_token = str(payload.get("qrToken") or "").strip()
            img_b64 = str(payload.get("img") or "").strip()
            if not qr_token or not img_b64:
                raise RuntimeError("获取登录二维码失败：接口未返回有效 qrToken 或图片数据")
            img_bytes = b64decode(img_b64)
            saved_path = save_qr_image(img_bytes, save_dir) if save_dir else ""
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise

        session_id = uuid.uuid4().hex
        with self._lock:
            self._sessions[session_id] = {
                "client": client,
                "qr_token": qr_token,
                "img_bytes": img_bytes,
                "created_at": time.monotonic(),
            }
        return {
            "sessionId": session_id,
            "imageBase64": img_b64,
            "savedPath": saved_path,
        }

    def poll(self, session_id: str, cookie_path: str | None = None) -> dict:
        sid = str(session_id or "").strip()
        if not sid:
            raise KeyError("缺少扫码会话 ID")
        with self._lock:
            self._cleanup_expired_locked()
            entry = self._sessions.get(sid)
        if entry is None:
            raise KeyError("扫码会话不存在或已过期，请刷新二维码")

        client = entry["client"]
        qr_token = entry["qr_token"]
        poll_resp = client.get(
            QR_INFO_URL,
            params={"qrToken": qr_token},
            timeout=15,
        )
        poll_resp.raise_for_status()
        info = poll_resp.json()
        status = info.get("status")

        if status == -1:
            return {"status": "waiting"}
        if status == 0:
            return {"status": "scanned"}
        if status == 1:
            once_password = info.get("oncePassword")
            if not once_password:
                self._pop_and_close(sid)
                raise RuntimeError("扫码确认成功，但未返回一次性登录凭证")
            try:
                client.get(
                    QR_LOGIN_SERVICE_URL,
                    params={"pwd": once_password},
                    timeout=15,
                )
                cookies = requests_cookies_to_playwright(client.cookies)
                if not cookies:
                    raise RuntimeError("扫码登录未获取到有效的智慧树 Cookies")
                target_path = cookie_path or get_runtime_path("data", "cookies.json")
                _write_cookies_atomic(target_path, cookies)
                return {"status": "confirmed", "cookieCount": len(cookies)}
            finally:
                self._pop_and_close(sid)
        if status == 2:
            self._pop_and_close(sid)
            return {"status": "expired"}
        if status == 3:
            self._pop_and_close(sid)
            return {"status": "cancelled"}

        raise RuntimeError(f"未知的二维码登录状态: {status} ({info.get('msg') or ''})")

    def save_current_image(self, session_id: str, save_dir: str) -> str:
        sid = str(session_id or "").strip()
        target_dir = str(save_dir or "").strip()
        if not sid:
            raise KeyError("缺少扫码会话 ID")
        if not target_dir:
            raise ValueError("请提供有效的保存目录")
        with self._lock:
            self._cleanup_expired_locked()
            entry = self._sessions.get(sid)
            img_bytes = entry["img_bytes"] if entry is not None else None
        if img_bytes is None:
            raise KeyError("扫码会话不存在或已过期，请刷新二维码")
        return save_qr_image(img_bytes, target_dir)

    def cancel(self, session_id: str) -> bool:
        sid = str(session_id or "").strip()
        if not sid:
            return False
        return self._pop_and_close(sid)

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._sessions.values())
            self._sessions.clear()
        for entry in entries:
            try:
                entry["client"].close()
            except Exception:
                pass
