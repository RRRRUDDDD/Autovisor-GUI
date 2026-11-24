import { logLevel, redactSensitive } from './logPresentation';

/**
 * 根据运行状态（errorMessage、近期错误/警告日志、任务模式与阶段）
 * 将任务执行异常归类为面向用户的可读诊断信息与可执行排查步骤。
 */

function collectSignalText(runState) {
  const parts = [];
  if (runState?.errorMessage) {
    parts.push(String(runState.errorMessage));
  }
  const logs = Array.isArray(runState?.logs) ? runState.logs : [];
  // 取最近 25 条中的 error / warning 文本，避免整段历史日志干扰当前根因判断
  const recent = logs.slice(-25);
  for (let i = recent.length - 1; i >= 0; i -= 1) {
    const entry = recent[i];
    const lv = logLevel(entry?.level);
    if (lv === 'error' || lv === 'warning') {
      const msg = String(entry?.message || '').trim();
      if (msg) parts.push(msg);
    }
  }
  return parts.join('\n');
}

function pickRawDetail(runState) {
  const err = String(runState?.errorMessage || '').trim();
  if (err && !/^任务异常结束（退出码\s*-?\d+），请查看日志$/.test(err)) {
    return redactSensitive(err);
  }
  const logs = Array.isArray(runState?.logs) ? runState.logs : [];
  for (let i = logs.length - 1; i >= 0; i -= 1) {
    const lv = logLevel(logs[i]?.level);
    if (lv === 'error' || lv === 'warning') {
      const msg = String(logs[i]?.message || '').trim();
      if (msg) return redactSensitive(msg);
    }
  }
  return redactSensitive(err || '未捕获到详细错误信息，请查看完整日志文件。');
}

const ERROR_RULES = [
  {
    category: 'llm',
    badge: '大模型 / 答题配置异常',
    severity: 'warning',
    match: (text) =>
      /nonretryablellmerror|未配置.*大模型|未配置视觉模型|insufficient_quota|api\s*key|rate\s*limit|invalid_api_key|model.*not found|大模型不可用|视觉模型请求失败/i.test(
        text
      ),
    build: () => ({
      title: '大模型或视觉模型接口不可用',
      summary:
        'AI 答题请求的 API Key 鉴权失败、账户余额/配额不足、模型 ID 不存在，或尚未配置支持读图的视觉模型。',
      steps: [
        '前往「系统设置 → 大模型智能答题」检查 API Key、API Base 与模型名称，并点击「测试模型连接」。',
        '如果作业/考试包含图片题，请额外填写支持视觉读图的「视觉模型」（如 gpt-4o、Qwen2.5-VL、glm-4v）。',
        '确认服务商账户有可用额度，且网络（或代理）能正常访问所填 API 地址。',
      ],
      actions: ['settings', 'restart', 'open_log'],
    }),
  },
  {
    category: 'auth',
    badge: '登录态失效',
    severity: 'error',
    match: (text, runState) =>
      runState?.mode === 'import_cookies' ||
      /sessionexpired|cookie|cookies\s*已失效|未签发.*票据|请重新登录|未完成登录|登录超时|登录失败|账号或密码|401|unauthorized|forbidden|未登录/i.test(
        text
      ),
    build: (_text, runState) => ({
      title:
        runState?.mode === 'import_cookies'
          ? '导入智慧树 Cookie 失败'
          : '智慧树登录状态已失效或被挤下线',
      summary:
        runState?.mode === 'import_cookies'
          ? '所选 JSON 文件格式不正确，或不包含有效的智慧树域名凭证。'
          : '本地保存的登录 Cookie 已过期、账号在别处登录导致掉线，或未能完成页面登录校验。',
      steps:
        runState?.mode === 'import_cookies'
          ? [
              '确认导出的 Cookie 为标准 JSON 数组，且包含 zhihuishu.com 域下的登录凭证（如 CASTGC / CASLOGC）。',
              '如果导入困难，建议直接返回登录页使用「扫码登录」或「手机号 / 学号登录」。',
            ]
          : [
              '点击下方「重新登录」，使用扫码或手机号/学号重新获取有效登录态。',
              '若经常长时间后台挂机，建议在登录页勾选「记住信息」以便中途掉线时自动重登。',
              '确认没有在其他设备或浏览器同时登录同一智慧树账号导致互踢。',
            ],
      actions:
        runState?.mode === 'import_cookies'
          ? ['settings', 'open_log']
          : ['relogin', 'settings', 'open_log'],
    }),
  },
  {
    category: 'captcha',
    badge: '安全验证受阻',
    severity: 'warning',
    match: (text) =>
      /captcharequired|code=-12|人机验证|安全验证|滑块|点选验证|验证码/i.test(text),
    build: () => ({
      title: '触发智慧树安全验证，自动识别未通过或等待超时',
      summary:
        '平台在当前请求或播放过程中弹出了滑块/点选验证，自动过验证未成功或人工等待超时导致任务中断。',
      steps: [
        '在「自动学习 → 学习参数」中确认已开启「自动跳过验证码」。',
        '若持续触发验证，可暂时关闭「隐藏浏览器窗口」，重新运行并在弹出浏览器时手动完成一次验证。',
        '适当降低播放倍速或缩短单门课程时长限制，可减少频繁触发风控的概率。',
      ],
      actions: ['courses', 'restart', 'open_log'],
    }),
  },
  {
    category: 'browser',
    badge: '浏览器异常',
    severity: 'error',
    match: (text) =>
      /browsertype\.launch|targetclosederror|executable doesn't exist|connect_over_cdp|cdp|浏览器启动失败|未检测到.*浏览器|浏览器已关闭|browser has been closed|page has been closed|找不到.*浏览器|msedge|chromium/i.test(
        text
      ),
    build: (text) => {
      const closedManually = /targetclosederror|浏览器已关闭|browser has been closed|page has been closed/i.test(
        text
      );
      return {
        title: closedManually
          ? '学习浏览器窗口已关闭或意外退出'
          : '无法启动或连接本地浏览器',
        summary: closedManually
          ? '任务运行期间受控的浏览器窗口被关闭、崩溃，或调试连接断开。'
          : '未能找到可用的 Edge / Chrome 可执行文件，或开启的 CDP 远程调试端点不可连接。',
        steps: closedManually
          ? [
              '如果是误关闭了学习浏览器窗口，直接点击「再次运行」即可从当前进度继续。',
              '如果频繁闪退，可前往「系统设置 → 浏览器与网络」切换浏览器内核（如 Chrome / Edge）后点击「检测浏览器与登录状态」。',
            ]
          : [
              '前往「系统设置 → 浏览器与网络」，点击「重新扫描」选择已安装的 Edge / Chrome，或手动指定可执行路径。',
              '点击「检测浏览器与登录状态」验证浏览器能否正常拉起。',
              '若开启了「连接已打开的 Chrome（CDP 调试）」，请确认端口地址正确且目标 Chrome 已带调试参数启动，否则请关闭该开关。',
            ],
        actions: ['settings', 'restart', 'open_log'],
      };
    },
  },
  {
    category: 'network',
    badge: '网络 / 代理异常',
    severity: 'error',
    match: (text) =>
      /proxyerror|sslerror|connectionerror|connecttimeout|readtimeout|err_proxy|err_connection|err_internet|err_name_not_resolved|err_timed_out|err_tunnel|remote end closed|connection aborted|502|503|504|代理|网络连接/i.test(
        text
      ),
    build: () => ({
      title: '网络连接或代理访问智慧树失败',
      summary:
        '请求智慧树页面或接口时出现超时、连接被拒绝、SSL 握手异常，或当前配置的代理服务器不可达。',
      steps: [
        '检查本机网络是否通畅，尝试在普通浏览器打开智慧树网站确认平台未维护。',
        '如果配置了代理，前往「系统设置 → 浏览器与网络」核对代理地址和端口，或清空代理改为直连。',
        '若为智慧树服务器短时波动，等待 1–2 分钟后点击「再次运行」。',
      ],
      actions: ['settings', 'restart', 'open_log'],
    }),
  },
  {
    category: 'config',
    badge: '配置 / 环境异常',
    severity: 'error',
    match: (text, runState) =>
      runState?.exitCode === 2 ||
      /configerror|配置无效|配置文件|依赖文件缺失|filenotfounderror|unicodedecodeerror|镜像配置|未检测到有效课程链接|未检测到有效网址/i.test(
        text
      ),
    build: (text) => {
      const noCourses = /未检测到有效课程链接|未检测到有效网址/i.test(text);
      return {
        title: noCourses ? '没有可学习的课程链接' : '配置文件或本地运行环境异常',
        summary: noCourses
          ? '当前课程队列中没有勾选任何有效的智慧树课程链接，因此任务无法开始。'
          : 'config.ini、镜像配置等文件格式有误或缺少必要组件，导致学习引擎未能初始化。',
        steps: noCourses
          ? [
              '点击「返回课程队列」，勾选至少一门需要学习的课程。',
              '如果列表为空，点击「刷新课程」重新自动识别，或使用「手动补充链接」添加。',
            ]
          : [
              '前往「系统设置 → 运行环境与版本」打开数据目录，检查 config.ini 是否有格式错误或编码问题。',
              '核对最近修改的代理、API 地址或参数是否为合法值。',
              '若提示依赖文件缺失，请重新完整解压发行包后再启动程序。',
            ],
        actions: noCourses ? ['courses'] : ['settings', 'open_log'],
      };
    },
  },
  {
    category: 'course_progress',
    badge: '课程进度 / 目录异常',
    severity: 'warning',
    match: (text) =>
      /课程目录加载超时|课程目录中没有可播放的视频课时|本轮因课时进度未确认而停止|目录尚未恢复当前课时|课程未确认完成|掌握度刷取失败|任务链接处理失败|未找到题目|翻页停滞|timeouterror/i.test(
        text
      ),
    build: (text) => {
      const noVideo = /课程目录中没有可播放的视频课时/i.test(text);
      const unconfirmed = /课时进度未确认|课程未确认完成|目录尚未恢复当前课时/i.test(text);
      return {
        title: noVideo
          ? '未在课程目录中找到可播放的视频课时'
          : unconfirmed
            ? '课时学习进度未能在平台侧确认'
            : '课程页面加载或目录识别超时',
        summary: noVideo
          ? '该课程可能已结课、为纯资料/纯考试课程，或页面目录结构未完全加载。'
          : unconfirmed
            ? '视频播放或切课过程中，平台长时间未回写完成进度，为防止空挂机程序已主动中止。'
            : '课程页响应较慢、被弹窗遮挡，或当前课程形态与预期目录结构不一致。',
        steps: [
          '返回「自动学习」点击「刷新课程」，确认该课程仍在进行中且未过期/结课。',
          '点击该课程右侧的「只读检测（听诊器图标）」排查课程目录能否正常识别。',
          '建议暂时关闭「隐藏浏览器窗口」后点击「再次运行」，观察浏览器中是否有弹窗、切课提示或播放异常。',
        ],
        actions: ['courses', 'restart', 'open_log'],
      };
    },
  },
];

/**
 * 诊断任务异常，无异常时返回 null。
 */
export function diagnoseRunError(runState) {
  if (!runState) return null;
  const isError = runState.status === 'error' || Boolean(String(runState.errorMessage || '').trim());
  if (!isError) return null;

  const signalText = collectSignalText(runState);
  const rawDetail = pickRawDetail(runState);

  for (const rule of ERROR_RULES) {
    if (rule.match(signalText, runState)) {
      const built = rule.build(signalText, runState);
      return {
        category: rule.category,
        badge: rule.badge,
        severity: rule.severity,
        title: built.title,
        summary: built.summary,
        steps: built.steps,
        actions: built.actions,
        rawDetail,
      };
    }
  }

  return {
    category: 'unknown',
    badge: '运行异常',
    severity: 'error',
    title: '任务执行过程中发生未归类异常',
    summary:
      '程序在运行过程中遇到了非预期的页面状态、接口返回或运行错误，任务已自动停止以保护当前学习状态。',
    steps: [
      '点击「再次运行」确认是否为智慧树平台偶发波动导致。',
      '在下方执行日志中切换筛选为「仅错误」，定位首次报错行及所在课程/阶段。',
      '若反复出现，可打开日志文件或点击「复制日志」获取已自动脱敏的日志用于进一步排查。',
    ],
    actions: ['restart', 'settings', 'open_log'],
    rawDetail,
  };
}

/**
 * 诊断运行中需要人工介入的提示（验证码 / 手动登录 / 手动切课），返回清晰的处理指引。
 */
export function diagnoseAttention(message) {
  const text = String(message || '').trim();
  if (!text) return null;

  if (/验证|滑块|点选|captcha/i.test(text)) {
    return {
      title: '需要处理安全验证',
      message: text,
      hint: '浏览器窗口已自动前置，请完成滑块或点选验证；通过后程序会自动恢复学习，无需重启任务。',
    };
  }
  if (/登录/i.test(text)) {
    return {
      title: '需要在浏览器中完成登录',
      message: text,
      hint: '请在弹出的浏览器窗口中完成智慧树账号登录；登录成功后程序会自动保存凭证并继续学习。',
    };
  }
  if (/切课|课时未选中/i.test(text)) {
    return {
      title: '需要手动点选一下目标课时',
      message: text,
      hint: '自动切课被页面遮挡或延迟，请在浏览器中手动点击对应课时；选中后程序会自动接管播放。',
    };
  }
  return {
    title: '需要留意浏览器窗口',
    message: text,
    hint: '请切换到浏览器窗口查看当前提示并处理，完成后程序会自动继续。',
  };
}
