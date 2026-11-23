# encoding=utf-8
import configparser
import json
import os
import re
import sys


from modules.http_utils import normalize_proxy_url


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, config_path=None, mirrors_path=None):
        self.config_path = config_path
        self._config_mtime: float | None = None
        self._override_limit_max_time = None
        self._override_limit_speed = None
        self.proxy = ""
        self.qr_login = False
        self.qr_terminal = False
        self.qr_unicode = False
        self.qr_save_dir = ""
        self.notification_provider = "Windows"
        self.notification_url = ""
        self.notification_tg_chat_id = ""
        self.notify_on_verify = True
        self.notify_on_finish = True
        self.notify_on_error = True
        self.shutdownOnComplete = False
        self.enableDebugLog = False
        self.reviewMode = False
        if config_path:
            self._config = configparser.ConfigParser()
            # 用户常量
            self._read_config()
            self.driver = self.get_driver()
            self.username = self._config.get('user-account', 'username', raw=True)
            self.password = self._config.get('user-account', 'password', raw=True)
            self.qr_login = self.get_bool_field('user-account', 'qrLogin', fallback=False)
            self.qr_terminal = self.get_bool_field('user-account', 'qrTerminal', fallback=False)
            self.qr_unicode = self.get_bool_field('user-account', 'qrUnicode', fallback=False)
            self.qr_save_dir = self._config.get('user-account', 'qrSaveDir', raw=True, fallback='').strip()
            # 浏览器选项
            self.exe_path = self._config.get('browser-option', 'EXE_PATH', raw=True)
            self.attach_existing_chrome = self.get_bool_field('browser-option', 'attachExistingChrome', fallback=False)
            self.cdp_url = self._config.get('browser-option', 'cdpUrl', raw=True, fallback='http://127.0.0.1:9222').strip()
            raw_proxy = self._config.get('browser-option', 'proxy', raw=True, fallback='').strip()
            try:
                self.proxy = normalize_proxy_url(raw_proxy)
            except ValueError as error:
                raise ConfigError(str(error)) from error
            # 脚本选项
            self.enableAutoCaptcha = self.get_bool_field('script-option', 'enableAutoCaptcha')
            self.enableHideWindow = self.get_bool_field('script-option', 'enableHideWindow', fallback=True)
            self.shutdownOnComplete = self.get_bool_field('script-option', 'shutdownOnComplete', fallback=False)
            self.enableDebugLog = self.get_bool_field('script-option', 'enableDebugLog', fallback=False)
            # 日志生产化选项(体积轮转/保留期/文件数/压缩归档)
            self.log_max_size_mb = self._safe_get_float('script-option', 'log_max_size_mb', 10.0)
            self.log_retention_days = self._safe_get_float('script-option', 'log_retention_days', 14.0)
            self.log_max_files = int(self._safe_get_float('script-option', 'log_max_files', 200.0))
            self.log_compress_after_days = self._safe_get_float('script-option', 'log_compress_after_days', 3.0)
            # 远程消息推送选项(可选配置段；未显式配置时默认启用 Windows 系统通知)
            has_provider_option = self._config.has_option('notification', 'provider')
            raw_provider = self._config.get('notification', 'provider', raw=True, fallback='').strip()
            self.notification_url = self._config.get('notification', 'url', raw=True, fallback='').strip()
            self.notification_tg_chat_id = self._config.get('notification', 'tg_chat_id', raw=True, fallback='').strip()
            if not raw_provider and not self.notification_url:
                legacy_bark = self._config.get('notification', 'barkUrl', raw=True, fallback='').strip()
                if legacy_bark:
                    raw_provider = 'Bark'
                    self.notification_url = legacy_bark
            if not raw_provider and not has_provider_option:
                raw_provider = 'Windows'
            self.notification_provider = raw_provider
            self.notify_on_verify = self.get_bool_field('notification', 'notifyOnVerify', fallback=True)
            self.notify_on_finish = self.get_bool_field('notification', 'notifyOnFinish', fallback=True)
            self.notify_on_error = self.get_bool_field('notification', 'notifyOnError', fallback=True)
            # 课程选项
            self.soundOff = self.get_bool_field('course-option', 'soundOff')
            # 复习模式(刷习惯分): 忽略已完成状态重播视频, 配合 limitMaxTime 可循环刷满时长
            self.reviewMode = self.get_bool_field('course-option', 'reviewMode', fallback=False)
            # AI 智慧课: 视频刷完后是否继续把知识点掌握度刷满
            self.enableMastery = self.get_bool_field('course-option', 'enableMastery', fallback=True)
            # AI 智慧课: 是否先用接口版 ExamCtx 刷计分考试并交卷
            self.enableExamApi = self.get_bool_field('course-option', 'enableExamApi', fallback=True)
            # 复习/模拟考试页: 是否用接口直接作答交卷(默认关闭, 正式考试勿开)
            self.enableReviewExamApi = self.get_bool_field('course-option', 'enableReviewExamApi', fallback=False)
            # 课中弹题: 是否优先用智慧树 AI 作答, 失败再回退自定义大模型
            self.enableZhidaoQuiz = self.get_bool_field('course-option', 'enableZhidaoQuiz', fallback=True)
            # 知到作业: 命中作业页时优先走加密 API 明文作答, 失败回退视觉模型
            self.enableHomeworkApi = self.get_bool_field('course-option', 'enableHomeworkApi', fallback=True)
            # 知到正式考试: 命中考试页时由加密 API 接管 (默认开启)
            self.enableFormalExamApi = self.get_bool_field('course-option', 'enableFormalExamApi', fallback=True)
            # AI 智慧课: 自动提取知识点 PPT 文本作为答题参考 (默认开启)
            self.enablePptReference = self.get_bool_field('course-option', 'enablePptReference', fallback=True)
            # 播放页: 只读拦截课中弹题接口, 直接取服务端正确答案 (默认开启)
            self.enablePopupIntercept = self.get_bool_field('course-option', 'enablePopupIntercept', fallback=True)
            # 知到作业: 达标得分率与单份最大重做次数 (0=使用默认轮次)
            self.homeworkThreshold = self._safe_get_float('course-option', 'homeworkThreshold', 100.0)
            self.homeworkMaxRedo = int(self._safe_get_float('course-option', 'homeworkMaxRedo', 3.0))
            self.course_match_rule = re.compile("https://[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]")
            self.course_urls = self.get_course_urls()
        # 登录
        self.login_url = "https://passport.zhihuishu.com/login"
        self.block_js = '''return document.getElementsByClassName("yidun_jigsaw")[0].src'''
        self.bg_js = '''return document.getElementsByClassName("yidun_bg-img")[0].src'''
        # 弹窗
        self.pop_js = (
            '(() => {'
            ' const icons = Array.from(document.getElementsByClassName("iconfont iconguanbi"));'
            ' const target = icons.find(el => el && el.offsetWidth > 0 && el.offsetHeight > 0) || icons[0];'
            ' if (target) target.click();'
            '})();'
        )

        self.mirrors = self._read_mirrors(mirrors_path)

    def _read_config(self) -> None:
        if not os.path.isfile(self.config_path):
            raise ConfigError(f"未找到配置文件: {self.config_path}")
        self._config = configparser.ConfigParser()
        try:
            self._config.read(self.config_path, encoding='utf-8-sig')
        except UnicodeDecodeError:
            self._config.read(self.config_path, encoding='gbk')
        required_sections = {
            "user-account", "browser-option", "script-option", "course-option", "course-url"
        }
        missing_sections = required_sections - set(self._config.sections())
        if missing_sections:
            raise ConfigError(
                f"配置文件缺少必要配置段: {', '.join(sorted(missing_sections))}"
            )
        try:
            self._config_mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._config_mtime = None

    def _reload_if_changed(self) -> None:
        if not self.config_path:
            return
        try:
            current_mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._read_config()
            return
        if self._config_mtime is None or current_mtime != self._config_mtime:
            self._read_config()

    def _read_mirrors(self, mirrors_path=None) -> dict:
        if not mirrors_path:
            if self.config_path:
                base_dir = os.path.dirname(self.config_path)
            elif getattr(sys, "frozen", False):
                base_dir = os.path.dirname(sys.executable)
            else:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            mirrors_path = os.path.join(base_dir, "data", "mirrors.json")
        if not os.path.isfile(mirrors_path):
            raise ConfigError(f"未找到镜像配置文件: {mirrors_path}")
        try:
            with open(mirrors_path, "r", encoding="utf-8-sig") as file:
                mirrors = json.load(file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigError(f"镜像配置文件无效: {mirrors_path}") from error
        if not isinstance(mirrors, dict):
            raise ConfigError("镜像配置文件必须是 JSON 对象")
        mirrors = {str(name): str(url).strip() for name, url in mirrors.items() if str(url).strip()}
        if not mirrors:
            raise ConfigError("镜像配置文件至少需要配置一个镜像源")
        return mirrors

    def get_driver(self) -> str:
        driver = self._config.get('browser-option', 'driver', raw=True)
        if not driver:
            driver = "edge"
        return driver.lower()

    def get_bool_field(self, section: str, option: str, fallback: bool = False) -> bool:
        field = self._config.get(section, option, raw=True, fallback=str(fallback)).strip().lower()
        if field == "true":
            return True
        else:
            return False

    def get_course_urls(self) -> list:
        course_urls = []
        _options = self._config.options("course-url")
        for _option in _options:
            course_url = self._config.get("course-url", _option, raw=True)
            matched = re.findall(self.course_match_rule, course_url)
            if not matched:
                stripped = course_url.strip()
                if stripped:
                    from modules.logger import Logger

                    Logger().warn(f"无效课程网址，已忽略: {stripped}")
                continue
            course_urls.append(course_url)
        return course_urls

    # @property修饰器可设置属性
    # 这样写可实时响应配置变化
    def _safe_get_float(self, section: str, option: str, default: float = 0.0) -> float:
        try:
            value = self._config.get(section, option, raw=True, fallback='').strip()
            if not value:
                return default
            return float(value)
        except (ValueError, configparser.Error):
            return default

    def apply_cli_overrides(self, args) -> None:
        """按 命令行 > 配置文件 的优先级应用临时运行参数覆盖。"""
        if getattr(args, "course", None):
            urls = []
            for raw in args.course:
                url = str(raw or "").strip()
                if not url:
                    continue
                if not self.course_match_rule.fullmatch(url):
                    raise ConfigError(f"命令行提供的课程链接无效: {url}")
                if url not in urls:
                    urls.append(url)
            if urls:
                self.course_urls = urls
        if getattr(args, "speed", None) is not None:
            speed = float(args.speed)
            self._override_limit_speed = min(max(speed, 0.5), 1.8)
        if getattr(args, "limit", None) is not None:
            limit = float(args.limit)
            if limit < 0:
                raise ConfigError("命令行参数 --limit 不能为负数")
            self._override_limit_max_time = limit
        if getattr(args, "proxy", None):
            try:
                self.proxy = normalize_proxy_url(args.proxy)
            except ValueError as error:
                raise ConfigError(str(error)) from error
        if getattr(args, "hide_window", False):
            self.enableHideWindow = True
        if getattr(args, "review", False):
            self.reviewMode = True
        if getattr(args, "qr_login", False):
            self.qr_login = True
        if getattr(args, "qr_terminal", False):
            self.qr_login = True
            self.qr_terminal = True
        if getattr(args, "qr_unicode", False):
            self.qr_login = True
            self.qr_terminal = True
            self.qr_unicode = True
        if getattr(args, "qr_save_dir", None):
            self.qr_save_dir = str(args.qr_save_dir).strip()

    @property
    def limitMaxTime(self) -> float:
        if self._override_limit_max_time is not None:
            return self._override_limit_max_time
        self._reload_if_changed()
        return self._safe_get_float('course-option', 'limitMaxTime', 0.0)

    @property
    def limitSpeed(self) -> float:
        if self._override_limit_speed is not None:
            return self._override_limit_speed
        self._reload_if_changed()
        speed = self._safe_get_float('course-option', 'limitSpeed', 1.0)
        return min(max(speed, 0.5), 1.8)

    @property
    def masteryTarget(self) -> float:
        self._reload_if_changed()
        target = self._safe_get_float('course-option', 'masteryTarget', 100.0)
        if target <= 0:
            return 100.0
        return min(max(target, 1.0), 100.0)


# 以下模板用于启动时自动生成缺失的配置文件, 与 *.example 保持一致。
CONFIG_TEMPLATE = """[user-account]
;配置账号密码，留空则打开网页后需手动登录
;留空时首次运行会在终端引导输入一次并自动保存到本文件, 之后即可全自动登录
username =
password =
;可选: 启用终端二维码登录(默认:False)
qrLogin = False
qrTerminal = False
qrUnicode = False
qrSaveDir =

[browser-option]
;配置浏览器,可选 Edge 或 Chrome
driver = Edge
;指定浏览器所在路径,不填则使用默认路径
EXE_PATH =
;可选: 配置代理服务器(支持 http:// / https:// / socks5://, 留空直连)
proxy =

[script-option]
;是否自动过验证码(登录滑块[易盾/腾讯] / 课中点选, 默认:True)
enableAutoCaptcha = True
;是否自动隐藏浏览器窗口(默认:True)
enableHideWindow = True
;刷课任务完成后自动关机(默认:False, 仅正常完成或达到课程时限时倒计时60秒关机, 手动停止/出错不关机)
shutdownOnComplete = False
;单份日志体积上限/MB, 超限自动切换到下一个日志文件 (默认:10)
log_max_size_mb = 10
;日志保留天数, 启动时清理超期文件 (默认:14, 0=不按天清理)
log_retention_days = 14
;日志文件数上限(含 .txt.gz), 超出后从旧到新删除 (默认:200)
log_max_files = 200
;超过该天数的日志启动时 gzip 压缩归档 (默认:3, 0=不压缩)
log_compress_after_days = 3

[notification]
;通知服务商（默认:Windows，留空表示禁用通知）。可选值:
;Windows    - Windows 系统通知（Toast，仅 Windows 可用，无需下方参数）
;ServerChan - Server酱 (https://sct.ftqq.com)
;Qmsg       - Qmsg酱 (https://qmsg.zendee.cn)
;Bark       - Bark (iOS, https://github.com/Finb/Bark)
;Telegram   - Telegram Bot
provider = Windows
;推送地址（ServerChan/Qmsg/Bark/Telegram 需要）:
;ServerChan: https://sctapi.ftqq.com/<SendKey>.send
;Qmsg:       https://qmsg.zendee.cn/send/<key>
;Bark:       https://api.day.app/<key>/
;Telegram:   https://api.telegram.org/bot<token>/sendMessage
url =
;Telegram 聊天 ID（仅 Telegram 需要）
tg_chat_id =
notifyOnVerify = True
notifyOnFinish = True
notifyOnError = True

[course-option]
;限制每门课程刷课时长/min (不限制:0)
limitMaxTime = 0
;限定播放倍速 (最高:1.8)
limitSpeed = 1.0
;设置是否静音播放 (默认:True)
soundOff = True
;复习模式/刷习惯分: 忽略已完成状态重播视频, 设置 limitMaxTime 时循环刷满时长 (默认:False)
reviewMode = False
;AI智慧课: 视频刷完后是否继续把知识点掌握度(题目)刷满 (默认:True)
enableMastery = True
;AI智慧课: 是否先用接口版 ExamCtx 刷计分考试并交卷 (默认:True)
enableExamApi = True
;复习/模拟考试页: 是否捕获新页面并用接口直接作答交卷 (默认:False, 正式考试勿开)
enableReviewExamApi = False
;课中弹题: 是否优先用智慧树 AI 作答, 失败回退自定义大模型 (默认:True)
enableZhidaoQuiz = True
;知到作业: 命中作业页时优先走加密 API 明文作答, 失败回退视觉模型 (默认:True)
enableHomeworkApi = True
;知到正式考试: 命中考试页时由加密 API 接管, 含自动交卷 (默认:True)
enableFormalExamApi = True
;AI智慧课: 自动提取知识点 PPT 课件文本作为答题参考 (默认:True)
enablePptReference = True
;播放页: 只读拦截课中弹题接口, 直接取服务端正确答案 (默认:True)
enablePopupIntercept = True
;知到作业达标得分率 0-100, 低于该值自动重做 (默认:100)
homeworkThreshold = 100
;知到作业单份最大重做次数 (默认:3, 0=使用默认轮次)
homeworkMaxRedo = 3
;掌握度目标百分比 (1-100, 默认:100)
masteryTarget = 100

[course-url]
;配置要学习的课程链接,支持智慧共享课、翻转课、融合共享课、AI慕课(wisdom-mooc)、AI智慧课
;可继续添加多个课程链接
URL1 =
URL2 =
URL3 =
URL4 =
URLn =
"""

LLM_TEMPLATE = {
    "provider": "deepseek",
    "api_format": "openai",
    "api_key": "",
    "api_base": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "vision_model": "",
    "api_version": "",
    "auto_submit": False,
    "stream_timeout": 120,
    "stream_idle_timeout": 30,
    "question_bank_token": "",
    "question_bank_url": "https://tk.enncy.cn/query",
}

DEFAULT_MIRRORS = {
    "Huawei": "https://mirrors.huaweicloud.com/repository/pypi",
    "Aliyun": "https://mirrors.aliyun.com/pypi",
    "Tsinghua": "https://pypi.tuna.tsinghua.edu.cn",
    "Official": "https://pypi.org",
}


def _runtime_root():
    override = os.environ.get("AUTOVISOR_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundled_resource_file(filename: str) -> str | None:
    res_override = os.environ.get("AUTOVISOR_RESOURCES_DIR", "").strip()
    candidates = []
    if res_override:
        base = os.path.abspath(res_override)
        candidates.append(os.path.join(base, filename))
        candidates.append(os.path.join(base, "resources", filename))
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "resources", filename))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def ensure_config_files(config_path: str) -> list:
    """启动时补齐缺失的配置文件, 只创建、绝不覆盖已有文件。

    config.ini 按传入路径生成; data/llm.json 与 data/mirrors.json 固定生成在
    运行根目录的 data/ 下, 与程序运行时的读取位置保持一致。
    返回本次新建文件的路径列表, 由调用方负责提示用户。
    """
    created = []

    if not os.path.exists(config_path):
        parent = os.path.dirname(os.path.abspath(config_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(config_path, "w", encoding="utf-8", newline="\n") as file:
            file.write(CONFIG_TEMPLATE)
        created.append(config_path)

    data_dir = os.path.join(_runtime_root(), "data")
    os.makedirs(data_dir, exist_ok=True)
    for name, content in (
        ("llm.json", LLM_TEMPLATE),
        ("mirrors.json", DEFAULT_MIRRORS),
    ):
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            continue
        if name == "mirrors.json":
            bundled = _bundled_resource_file("mirrors.json")
            if bundled:
                with open(bundled, "r", encoding="utf-8-sig") as src, open(
                    path, "w", encoding="utf-8", newline="\n"
                ) as dst:
                    dst.write(src.read())
                created.append(path)
                continue
        with open(path, "w", encoding="utf-8") as file:
            json.dump(content, file, ensure_ascii=False, indent=2)
            file.write("\n")
        created.append(path)
    return created
