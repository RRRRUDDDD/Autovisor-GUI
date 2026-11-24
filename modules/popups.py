# encoding=utf-8
"""智慧树课程页通用非验证/非答题弹窗自动关闭模块。

背景
----
智慧树各版本播放页（旧版 studyvideoh5、智慧共享课 studywisdomh5、融合课
fusioncourseh5、AI 慕课 wisdom-mooc、翻转课 hike、AI 智慧课 ai-smart）在
进课、切课、播放过程中会不定时弹出各类提醒/提示/公告/引导/绑定弹窗，例如：
- 「课程提醒」（关注公众号并绑定智慧树账号：按钮「已绑定，不再提示」「下次再说」）
- 「学前必读」「学习须知」「课程须知」「诚信学习承诺书」「重要提示」「温馨提示」
- 习惯分/学习进度/防挂机/护眼/续播/新功能引导/问卷评价/APP 下载等浮层

本模块采用「已知弹窗容器 + 计算样式浮层降级发现 + 严格安全排除 + 优先级按钮/关闭图标」
的多层策略，并支持按钮点击未生效时自动换用备选按钮或关闭图标：
1. 绝不触碰安全验证 / 验证码 / 人机验证 / 人脸识别弹窗；
2. 绝不触碰课中弹题 / 随堂练习 / 课中测验 / 考试交卷确认弹窗；
3. 绝不触碰登录 / 退出登录 / 删除 / 重考等敏感或破坏性弹窗；
4. 绝不误点课程目录、视频播放器或页面常规导航控件。
"""
import asyncio
from functools import lru_cache
import time

from playwright.async_api import Page

from modules.logger import Logger
from modules.login import is_login_page
from modules.paths import get_resource_path
from modules.quiz_dom import TargetClosedError

logger = Logger()

# 后台轮询弹窗的间隔（秒）
POPUP_POLL_INTERVAL_SECONDS = 1.5
# 连续点击同一弹窗签名时，跳过已失败按钮标签的记忆时长（秒）
_RETRY_STATE_TTL_SECONDS = 30.0
# 后台轮询中全页 fixed 浮层降级扫描的间隔轮次（避免每 1.5s 全页 getComputedStyle）
_BACKGROUND_FALLBACK_EVERY_N_TICKS = 3


@lru_cache(maxsize=1)
def _load_dismiss_popup_script() -> str:
    path = get_resource_path("resources", "scripts", "dismiss_popups.js")
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


_DISMISS_ONE_POPUP_JS = _load_dismiss_popup_script()


def _should_skip_page(page: Page) -> bool:
    try:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed():
            return True
    except Exception:
        return True
    url = str(getattr(page, "url", "") or "")
    if url and is_login_page(url):
        return True
    return False


async def dismiss_course_popups(
    page: Page,
    *,
    max_rounds: int = 4,
    settle_ms: int = 300,
    remove_floating_tips: bool = False,
    retry_memory: dict[str, tuple[float, list[str]]] | None = None,
    allow_fixed_fallback: bool = True,
) -> list[dict]:
    """检测并关闭当前课程页上可见的非验证、非答题弹窗（支持连续级联弹窗）。

    当某个按钮点击后同一弹窗未消失时，会自动记录已尝试的按钮并在下一轮切换备选
    按钮（例如「已绑定，不再提示」无效时切换「下次再说」或关闭图标）。
    """
    if _should_skip_page(page):
        return []

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []

    now = time.monotonic()
    local_skips: dict[str, list[str]] = {}
    if retry_memory is not None:
        expired = [
            sig
            for sig, (ts, _) in retry_memory.items()
            if now - ts > _RETRY_STATE_TTL_SECONDS
        ]
        for sig in expired:
            retry_memory.pop(sig, None)
        for sig, (_, labels) in retry_memory.items():
            local_skips[sig] = list(labels)

    dismissed: list[dict] = []
    attempted_by_sig: dict[str, list[str]] = {}

    for _ in range(max(1, max_rounds)):
        if _should_skip_page(page):
            break
        try:
            result = await page.evaluate(
                _DISMISS_ONE_POPUP_JS,
                {
                    "skipBySig": local_skips,
                    "removeFloatingTips": remove_floating_tips,
                    "allowFixedFallback": allow_fixed_fallback,
                },
            )
        except TargetClosedError:
            raise
        except Exception as exc:
            logger.debug_throttled(
                "popups:dismiss",
                f"课程弹窗检测跳过: {logger.summarize_exception(exc)}",
            )
            break

        if not isinstance(result, dict) or not result.get("dismissed"):
            # 前一轮点过的弹窗如果已经消失，则清理它的失败按钮记忆，避免后续新课时同款弹窗被跳过
            if retry_memory is not None:
                for prev_sig, prev_btns in attempted_by_sig.items():
                    if len(prev_btns) <= 1:
                        retry_memory.pop(prev_sig, None)
            break

        sig = str(result.get("sig") or "")
        button = str(result.get("button") or "")
        title = str(result.get("title") or "课程弹窗")
        strategy = str(result.get("strategy") or "button")

        dismissed.append(
            {
                "sig": sig,
                "title": title,
                "button": button,
                "strategy": strategy,
            }
        )

        # 若本轮点完后同一弹窗下一轮仍未关闭，自动跳过该按钮改试备选按钮/关闭图标
        if sig and button:
            sig_attempts = attempted_by_sig.setdefault(sig, [])
            if button not in sig_attempts:
                sig_attempts.append(button)
            labels = local_skips.setdefault(sig, [])
            if button not in labels:
                labels.append(button)
            # 只有同一弹窗在同一轮次内需要第二次尝试时，才把失败按钮写入跨轮记忆
            if retry_memory is not None and len(sig_attempts) >= 2:
                retry_memory[sig] = (time.monotonic(), list(sig_attempts[:-1]))

        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)

    return dismissed


async def dismiss_popups(
    page: Page,
    popups_enabled: asyncio.Event | None = None,
    interval_seconds: float = POPUP_POLL_INTERVAL_SECONDS,
) -> None:
    """后台持续监听并自动关闭课程页内的各类提醒/提示/引导弹窗。"""
    await page.wait_for_load_state("domcontentloaded")
    retry_memory: dict[str, tuple[float, list[str]]] = {}
    tick = 0
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if popups_enabled is not None and not popups_enabled.is_set():
                continue
            allow_fallback = (tick % _BACKGROUND_FALLBACK_EVERY_N_TICKS) == 0
            tick += 1
            closed = await dismiss_course_popups(
                page,
                max_rounds=3,
                settle_ms=250,
                remove_floating_tips=False,
                retry_memory=retry_memory,
                allow_fixed_fallback=allow_fallback,
            )
            for item in closed:
                title = item["title"]
                button = item["button"]
                logger.info(f"已自动关闭课程弹窗: {title} ({button})")
                logger.event(
                    "关闭弹窗",
                    标题=title,
                    操作=button,
                    方式=item["strategy"],
                )
        except TargetClosedError:
            logger.debug("浏览器已关闭, 课程弹窗清理模块停止运行.")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug_throttled(
                "dismiss_popups",
                f"课程弹窗清理模块轮询跳过: {logger.summarize_exception(exc)}",
            )
            continue
