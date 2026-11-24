"""Offline unit & headless Chromium DOM tests for modules/popups.py."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import Error
from playwright.async_api import async_playwright

from modules.popups import dismiss_course_popups, dismiss_popups


class PopupUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_login_page(self):
        page = Mock()
        page.is_closed = Mock(return_value=False)
        page.url = "https://passport.zhihuishu.com/login?service=..."
        page.evaluate = AsyncMock()
        result = await dismiss_course_popups(page)
        self.assertEqual(result, [])
        page.evaluate.assert_not_awaited()

    async def test_skips_closed_page(self):
        page = Mock()
        page.is_closed = Mock(return_value=True)
        page.url = "https://studyvideoh5.zhihuishu.com/stuStudy"
        page.evaluate = AsyncMock()
        result = await dismiss_course_popups(page)
        self.assertEqual(result, [])
        page.evaluate.assert_not_awaited()


class PopupBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        executable = os.environ.get("AUTOVISOR_TEST_BROWSER")
        try:
            self.browser = await self.playwright.chromium.launch(
                executable_path=executable, headless=True
            )
        except Error as exc:
            await self.playwright.stop()
            if not executable and "Executable doesn't exist" in str(exc):
                self.skipTest(
                    "Install Chromium with: python -m playwright install chromium"
                )
            raise
        self.page = await self.browser.new_page(viewport={"width": 1280, "height": 720})

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_dismisses_course_reminder_wechat_popup_with_unknown_classes(self):
        """复现截图中的「课程提醒」(公众号绑定)弹窗，即使使用任意自定义类名也能自动关闭。"""
        await self.page.set_content(
            """
            <div class="videoArea"><video></video></div>
            <div class="clearfix video current_play"><span id="lessonOrder">1.1</span></div>
            <div id="overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2001;
                 display:flex;align-items:center;justify-content:center;">
              <div id="card" class="x-unknown-card" style="width:520px;height:360px;background:#fff;
                   border-radius:12px;padding:24px;">
                <div class="x-head" style="font-size:20px;font-weight:bold;">课程提醒</div>
                <p>如您在课程学习即将结束时未完成学习，或考试即将结束时未参加考试，该公众号会发送提醒，确保您不会错过截止时间。</p>
                <div>1.扫一扫，关注公众号</div>
                <div>2.绑定智慧树账号</div>
                <div>请一定完成绑定，否则收不到提醒哦</div>
                <div style="display:flex;gap:12px;margin-top:20px;">
                  <div id="btn-bound" class="x-primary" style="padding:8px 20px;">
                    <i>&#xe6ab;</i> 已绑定，不再提示
                  </div>
                  <div id="btn-later" class="x-secondary" style="padding:8px 20px;">下次再说</div>
                </div>
              </div>
            </div>
            <script>
              window.__clicked = [];
              document.getElementById('btn-bound').onclick = () => {
                window.__clicked.push('bound');
                document.getElementById('overlay').style.display = 'none';
              };
              document.getElementById('btn-later').onclick = () => {
                window.__clicked.push('later');
                document.getElementById('overlay').style.display = 'none';
              };
            </script>
            """
        )
        closed = await dismiss_course_popups(self.page, settle_ms=50)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["title"], "课程提醒")
        self.assertEqual(closed[0]["button"], "下次再说")
        self.assertEqual(await self.page.evaluate("window.__clicked"), ["later"])
        self.assertFalse(await self.page.locator("#overlay").is_visible())

    async def test_falls_back_to_alternate_button_when_first_button_does_not_close(self):
        """若首个按钮点击后弹窗仍未消失，下一轮自动切换备选按钮（带 iconfont PUA 前缀）。"""
        await self.page.set_content(
            """
            <div id="overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2001;
                 display:flex;align-items:center;justify-content:center;">
              <div style="width:480px;height:320px;background:#fff;padding:24px;">
                <div class="title">课程提醒</div>
                <div>请及时关注公众号并绑定智慧树账号</div>
                <button id="btn-bound"><span>&#xe612;</span>已绑定，不再提示</button>
                <button id="btn-later">下次再说</button>
              </div>
            </div>
            <script>
              window.__clicked = [];
              document.getElementById('btn-later').onclick = () => {
                window.__clicked.push('later-noop');
              };
              document.getElementById('btn-bound').onclick = () => {
                window.__clicked.push('bound-ok');
                document.getElementById('overlay').style.display = 'none';
              };
            </script>
            """
        )
        closed = await dismiss_course_popups(self.page, max_rounds=4, settle_ms=50)
        self.assertEqual([item["button"] for item in closed], ["下次再说", "已绑定，不再提示"])
        self.assertEqual(
            await self.page.evaluate("window.__clicked"),
            ["later-noop", "bound-ok"],
        )
        self.assertFalse(await self.page.locator("#overlay").is_visible())

    async def test_chained_popups_including_hidden_decoy_iconguanbi(self):
        """级联弹窗：学前必读(仅可见关闭图标，跳过隐藏的假图标) -> 课程提醒 -> 温馨提示。"""
        await self.page.set_content(
            """
            <!-- 隐藏的旧关闭图标，历史上 config.pop_js[0] 会误点这里 -->
            <div style="display:none;"><i class="iconfont iconguanbi" id="hidden-close"></i></div>

            <div class="el-dialog__wrapper" id="dlg1" style="position:fixed;inset:0;z-index:2000;
                 display:flex;align-items:center;justify-content:center;">
              <div class="el-dialog studytime-div" style="width:460px;height:260px;background:#fff;">
                <div class="el-dialog__header">
                  <span class="el-dialog__title">学前必读</span>
                  <i class="iconfont iconguanbi" id="real-close" style="display:inline-block;width:20px;height:20px;">×</i>
                </div>
                <div class="el-dialog__body">本课程平时成绩含随堂练习 10%，请按时完成学习。</div>
              </div>
            </div>

            <div class="el-dialog__wrapper" id="dlg2" style="display:none;position:fixed;inset:0;z-index:2002;
                 align-items:center;justify-content:center;">
              <div class="el-dialog" style="width:460px;height:260px;background:#fff;">
                <div class="el-dialog__title">课程提醒</div>
                <div>关注公众号接收考试提醒</div>
                <button id="dlg2-later">下次再说</button>
              </div>
            </div>

            <div class="ant-modal-wrap" id="dlg3" style="display:none;position:fixed;inset:0;z-index:2004;
                 align-items:center;justify-content:center;">
              <div class="ant-modal" style="width:420px;height:220px;background:#fff;">
                <div class="ant-modal-title">温馨提示</div>
                <div>建议开启护眼模式</div>
                <button id="dlg3-ok">我知道了！</button>
              </div>
            </div>

            <script>
              window.__hiddenClicked = false;
              document.getElementById('hidden-close').onclick = () => { window.__hiddenClicked = true; };
              document.getElementById('real-close').onclick = () => {
                document.getElementById('dlg1').style.display = 'none';
                document.getElementById('dlg2').style.display = 'flex';
              };
              document.getElementById('dlg2-later').onclick = () => {
                document.getElementById('dlg2').style.display = 'none';
                document.getElementById('dlg3').style.display = 'flex';
              };
              document.getElementById('dlg3-ok').onclick = () => {
                document.getElementById('dlg3').style.display = 'none';
              };
            </script>
            """
        )
        closed = await dismiss_course_popups(self.page, max_rounds=5, settle_ms=50)
        self.assertEqual(len(closed), 3)
        self.assertFalse(await self.page.evaluate("window.__hiddenClicked"))
        self.assertFalse(await self.page.locator("#dlg1").is_visible())
        self.assertFalse(await self.page.locator("#dlg2").is_visible())
        self.assertFalse(await self.page.locator("#dlg3").is_visible())

    async def test_skips_disabled_countdown_button_then_clicks_when_enabled(self):
        """带倒计时禁用的按钮在禁用期不误点，启用后立即关闭。"""
        await self.page.set_content(
            """
            <div class="el-dialog__wrapper" id="dlg" style="position:fixed;inset:0;z-index:2000;
                 display:flex;align-items:center;justify-content:center;">
              <div class="el-dialog" style="width:460px;height:240px;background:#fff;">
                <div class="el-dialog__title">诚信学习承诺书</div>
                <div>请认真阅读并遵守学习规范</div>
                <button id="agree" class="el-button is-disabled" disabled>我已阅读并同意(3s)</button>
              </div>
            </div>
            <script>
              document.getElementById('agree').onclick = () => {
                document.getElementById('dlg').style.display = 'none';
              };
            </script>
            """
        )
        closed_early = await dismiss_course_popups(self.page, settle_ms=30)
        self.assertEqual(closed_early, [])
        self.assertTrue(await self.page.locator("#dlg").is_visible())

        await self.page.evaluate(
            """() => {
                const btn = document.getElementById('agree');
                btn.disabled = false;
                btn.classList.remove('is-disabled');
                btn.textContent = '我已阅读并同意';
            }"""
        )
        closed_ready = await dismiss_course_popups(self.page, settle_ms=30)
        self.assertEqual(len(closed_ready), 1)
        self.assertEqual(closed_ready[0]["button"], "我已阅读并同意")
        self.assertFalse(await self.page.locator("#dlg").is_visible())

    async def test_never_touches_captcha_quiz_exam_login_or_main_ui(self):
        """安全底线：验证码、课中弹题、交卷确认、登录弹窗及页面常规控件绝不被误关。"""
        # 1) 易盾验证码存在时，整页都不应被点击
        await self.page.set_content(
            """
            <div class="yidun_popup" style="position:fixed;inset:0;z-index:3000;">
              <div class="yidun_modal" style="width:320px;height:240px;background:#fff;">
                <div class="yidun_modal__title">请完成安全验证</div>
                <button class="close-btn" id="cap-close">关闭</button>
              </div>
            </div>
            <div class="el-dialog__wrapper" id="notice" style="position:fixed;inset:0;z-index:2000;">
              <div class="el-dialog" style="width:420px;height:220px;background:#fff;">
                <div class="el-dialog__title">课程提醒</div>
                <button id="notice-later">下次再说</button>
              </div>
            </div>
            <script>
              window.__touched = 0;
              document.querySelectorAll('button').forEach(b => b.onclick = () => window.__touched++);
            </script>
            """
        )
        self.assertEqual(await dismiss_course_popups(self.page, settle_ms=30), [])
        self.assertEqual(await self.page.evaluate("window.__touched"), 0)

        # 2) 课中弹题 / 随堂练习 / 交卷确认 / 登录弹窗 / 常规导航
        await self.page.set_content(
            """
            <header style="position:fixed;top:0;left:0;width:1280px;height:60px;background:#123;">
              <button id="nav-close">关闭</button>
            </header>
            <div class="el-dialog__wrapper" style="position:fixed;inset:0;z-index:2000;">
              <div class="el-dialog" style="width:500px;height:300px;background:#fff;">
                <div class="topic-title">1. 以下说法正确的是？</div>
                <div class="topic-item">A. 选项一</div>
                <button class="el-dialog__headerbtn" id="quiz-close">×</button>
              </div>
            </div>
            <div class="el-dialog__wrapper" style="position:fixed;inset:0;z-index:2001;">
              <div class="el-dialog" style="width:400px;height:220px;background:#fff;">
                <div class="el-dialog__title">提示</div>
                <div>还有题目未做完，确认交卷吗？</div>
                <button id="exam-ok">确定</button>
                <button id="exam-cancel">取消</button>
              </div>
            </div>
            <div class="el-dialog__wrapper" style="position:fixed;inset:0;z-index:2002;">
              <div class="el-dialog" style="width:400px;height:220px;background:#fff;">
                <div class="el-dialog__title">账号登录</div>
                <input type="password" />
                <button id="login-close">关闭</button>
              </div>
            </div>
            <script>
              window.__touched = 0;
              document.querySelectorAll('button').forEach(b => b.onclick = () => window.__touched++);
            </script>
            """
        )
        self.assertEqual(await dismiss_course_popups(self.page, settle_ms=30), [])
        self.assertEqual(await self.page.evaluate("window.__touched"), 0)

    async def test_background_task_dismisses_delayed_popup_and_respects_pause(self):
        """后台 dismiss_popups 任务支持暂停/恢复，并自动清理延迟出现的弹窗。"""
        await self.page.set_content(
            """
            <div id="delayed" style="display:none;position:fixed;inset:0;z-index:2000;
                 align-items:center;justify-content:center;">
              <div style="width:420px;height:240px;background:#fff;">
                <div>课程提醒</div>
                <div>请绑定公众号</div>
                <button id="later">下次再说</button>
              </div>
            </div>
            <script>
              document.getElementById('later').onclick = () => {
                document.getElementById('delayed').style.display = 'none';
              };
            </script>
            """
        )
        enabled = asyncio.Event()  # 初始暂停
        task = asyncio.create_task(dismiss_popups(self.page, enabled, interval_seconds=0.08))
        try:
            await self.page.evaluate(
                "document.getElementById('delayed').style.display = 'flex';"
            )
            await asyncio.sleep(0.25)
            # 暂停状态下不应关闭
            self.assertTrue(await self.page.locator("#delayed").is_visible())

            enabled.set()
            for _ in range(20):
                if not await self.page.locator("#delayed").is_visible():
                    break
                await asyncio.sleep(0.08)
            self.assertFalse(await self.page.locator("#delayed").is_visible())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_course_runner_dismisses_blocking_reminder_before_lesson_click(self):
        """course_runner 切课前会自动清掉遮挡目录的「课程提醒」弹窗，不会卡死在等待手动切课。"""
        from modules.course_runner import CourseOutcome, run_course
        from modules.lesson_navigation import LEGACY_CATALOG

        await self.page.set_content(
            """
            <div class="clearfix video current_play" id="l1">
              <span id="lessonOrder">1.1 第一节</span>
              <span class="time_icofinish"></span>
            </div>
            <div class="clearfix video" id="l2" style="width:300px;height:40px;background:#eee;">
              <span id="lessonOrder">1.2 第二节</span>
              <span class="progress-num">10%</span>
            </div>
            <video></video>
            <div id="remind-mask" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2000;
                 display:flex;align-items:center;justify-content:center;">
              <div style="width:480px;height:300px;background:#fff;padding:20px;">
                <div>课程提醒</div>
                <div>关注公众号绑定智慧树账号</div>
                <button id="remind-later">下次再说</button>
              </div>
            </div>
            <script>
              document.getElementById('remind-later').onclick = () => {
                document.getElementById('remind-mask').style.display = 'none';
              };
              document.getElementById('l2').onclick = () => {
                document.querySelectorAll('.current_play').forEach(el => el.classList.remove('current_play'));
                document.getElementById('l2').classList.add('current_play');
              };
            </script>
            """
        )

        learned = []

        async def finish(_page, _start, _pause, lesson, *_args):
            learned.append(await lesson.get_attribute("id"))
            await lesson.evaluate(
                "el => el.insertAdjacentHTML('beforeend', '<span class=time_icofinish></span>')"
            )
            return 0.0, True, False

        with patch("modules.course_runner.learn_lesson", finish):
            outcome = await run_course(
                self.page,
                LEGACY_CATALOG,
                SimpleNamespace(limitMaxTime=0),
                Mock(),
                asyncio.Event(),
            )

        self.assertIs(outcome, CourseOutcome.COMPLETED)
        self.assertEqual(learned, ["l2"])
        self.assertFalse(await self.page.locator("#remind-mask").is_visible())


if __name__ == "__main__":
    unittest.main()
