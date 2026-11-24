(opts) => {
    const skipBySig = (opts && opts.skipBySig) || {};
    const removeFloatingTips = Boolean(opts && opts.removeFloatingTips);
    const allowFixedFallback = !opts || opts.allowFixedFallback !== false;

    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
    const compact = (value) =>
        (value || '')
            .replace(/[\uE000-\uF8FF\u200B-\u200D\uFEFF]/g, '')
            .replace(/\s+/g, '')
            .replace(/,/g, '，')
            .replace(/\(/g, '（')
            .replace(/\)/g, '）')
            .replace(/[!！。.~～]+$/g, '')
            .trim();

    const isVisible = (el) => {
        if (!el || !el.isConnected) return false;
        let cur = el;
        while (cur && cur !== document.documentElement) {
            const style = getComputedStyle(cur);
            if (style.display === 'none' || style.visibility === 'hidden') return false;
            const opacity = Number.parseFloat(style.opacity || '1');
            if (!Number.isNaN(opacity) && opacity <= 0.05) return false;
            cur = cur.parentElement;
        }
        const rect = el.getBoundingClientRect();
        return (
            rect.width > 0 &&
            rect.height > 0 &&
            rect.bottom > 0 &&
            rect.right > 0 &&
            rect.top < window.innerHeight &&
            rect.left < window.innerWidth
        );
    };

    const isDisabled = (el) => {
        if (!el) return true;
        if (el.disabled) return true;
        const aria = (el.getAttribute && el.getAttribute('aria-disabled')) || '';
        if (aria.toLowerCase() === 'true') return true;
        const cls = typeof el.className === 'string' ? el.className : '';
        if (/(?:^|\s|-)(?:is-disabled|disabled)(?:\s|$|-)/i.test(cls)) return true;
        const style = getComputedStyle(el);
        if (style.pointerEvents === 'none') return true;
        return false;
    };

    // 1. 页面级安全验证守卫：只要当前存在可见验证码，立即放弃，不做任何点击。
    const pageVerifySelectors = [
        '.yidun_popup .yidun_modal',
        '.yidun_modal__title',
        '.yidun_panel',
        "[id^='tcaptcha_transform']",
        '#tcaptcha_iframe_dy',
        "iframe[id^='tcaptcha']",
        "[class*='geetest_panel']",
    ];
    for (const sel of pageVerifySelectors) {
        for (const node of document.querySelectorAll(sel)) {
            if (isVisible(node)) {
                return { dismissed: false, reason: 'verification_visible' };
            }
        }
    }

    const effectiveZIndex = (el) => {
        let cur = el;
        let best = 0;
        while (cur && cur !== document.documentElement) {
            const style = getComputedStyle(cur);
            if (style.position !== 'static') {
                const z = Number.parseInt(style.zIndex, 10);
                if (!Number.isNaN(z) && z > best) best = z;
            }
            cur = cur.parentElement;
        }
        return best;
    };

    const isOverlayLike = (el) => {
        let cur = el;
        while (cur && cur !== document.body && cur !== document.documentElement) {
            const style = getComputedStyle(cur);
            if (style.position === 'fixed') return true;
            if (style.position === 'absolute') {
                const z = Number.parseInt(style.zIndex, 10);
                if (!Number.isNaN(z) && z >= 10) return true;
            }
            const role = (cur.getAttribute && cur.getAttribute('role')) || '';
            const ariaModal = (cur.getAttribute && cur.getAttribute('aria-modal')) || '';
            if (role === 'dialog' || role === 'alertdialog' || ariaModal === 'true') {
                return true;
            }
            cur = cur.parentElement;
        }
        return false;
    };

    const isFullScreenBox = (rect) =>
        rect.width >= window.innerWidth * 0.85 && rect.height >= window.innerHeight * 0.85;

    const isMainUiContainer = (el) => {
        if (!el || el === document.body || el === document.documentElement || el.id === 'app') {
            return true;
        }
        const tag = el.tagName ? el.tagName.toLowerCase() : '';
        if (tag === 'header' || tag === 'nav' || tag === 'footer') return true;
        // 绝不把包含播放器或课程目录的主体容器当作弹窗
        const mainSelectors =
            'video, .videoArea, #vjs_container, .clearfix.video, .chapter-item, ' +
            '.child-info.hasvideo, .file-item, .knowledge-content';
        if (el.matches && el.matches(mainSelectors)) return true;
        if (el.querySelector && el.querySelector(mainSelectors)) return true;
        return false;
    };

    const isNarrowEdgeBar = (rect) => {
        // 排除顶部导航条与贴边悬浮小工具栏
        if (rect.height <= 90 && rect.width >= window.innerWidth * 0.65) return true;
        if (rect.width <= 110 && rect.height >= window.innerHeight * 0.5) return true;
        return false;
    };

    const unwrapBackdropToCard = (el) => {
        const rect = el.getBoundingClientRect();
        const cls = typeof el.className === 'string' ? el.className : '';
        const looksWrapper =
            isFullScreenBox(rect) ||
            /(?:wrapper|mask|overlay|backdrop|shade)/i.test(cls);
        if (!looksWrapper) return el;

        // 优先查找内层已知弹窗卡片
        const innerKnown = el.querySelector(
            '.el-dialog, .el-message-box, .ant-modal, .ant-modal-confirm, ' +
            '.ss2077-custom-dialog, [role="dialog"], [aria-modal="true"]'
        );
        if (innerKnown && isVisible(innerKnown) && !isMainUiContainer(innerKnown)) {
            return innerKnown;
        }

        // 否则查找内层尺寸适中、包含文字与交互元素的卡片
        const children = Array.from(el.children).filter((child) => {
            if (!isVisible(child) || isMainUiContainer(child)) return false;
            const r = child.getBoundingClientRect();
            return (
                r.width >= 160 &&
                r.height >= 80 &&
                (!isFullScreenBox(r) || child.children.length > 0)
            );
        });
        if (children.length === 1) {
            return unwrapBackdropToCard(children[0]);
        }
        const card = children.find((child) => {
            const r = child.getBoundingClientRect();
            return !isFullScreenBox(r) && !isNarrowEdgeBar(r);
        });
        return card ? unwrapBackdropToCard(card) : el;
    };

    // 2. 收集候选弹窗容器（已知弹窗选择器 + 通用 fixed 浮层降级发现）
    const candidateSet = new Set();

    const explicitDialogSelectors = [
        '.el-dialog__wrapper .el-dialog',
        '.el-overlay-dialog .el-dialog',
        '.el-dialog',
        '.el-message-box__wrapper .el-message-box',
        '.el-message-box',
        '.ant-modal-wrap .ant-modal',
        '.ant-modal',
        '.ant-modal-confirm',
        '.ss2077-custom-dialog',
        '.studytime-div',
        '.dialog-read',
        '.dialog-warn',
        '.dialog-tips',
        '.dialog-notice',
        '.hint_delete',
        '[role="dialog"]',
        '[role="alertdialog"]',
        '[aria-modal="true"]',
    ];
    for (const sel of explicitDialogSelectors) {
        for (const node of document.querySelectorAll(sel)) {
            if (!isVisible(node) || isMainUiContainer(node)) continue;
            const card = unwrapBackdropToCard(node);
            const rect = card.getBoundingClientRect();
            if (rect.width >= 140 && rect.height >= 70 && !isMainUiContainer(card)) {
                candidateSet.add(card);
            }
        }
    }

    const patternSelectors = [
        '[class*="dialog" i]',
        '[class*="modal" i]',
        '[class*="popup" i]',
        '[class*="popover" i]',
        '[class*="remind" i]',
        '[class*="notice" i]',
        '[class*="guide" i]',
    ];
    for (const sel of patternSelectors) {
        for (const node of document.querySelectorAll(sel)) {
            if (!isVisible(node) || isMainUiContainer(node) || !isOverlayLike(node)) {
                continue;
            }
            const card = unwrapBackdropToCard(node);
            const rect = card.getBoundingClientRect();
            if (
                rect.width >= 160 &&
                rect.height >= 80 &&
                !isNarrowEdgeBar(rect) &&
                !isMainUiContainer(card)
            ) {
                candidateSet.add(card);
            }
        }
    }

    // 通用 fixed 浮层扫描：仅在精准弹窗选择器未命中时降级触发，避免每轮全页无差别 getComputedStyle
    if (candidateSet.size === 0 && allowFixedFallback) {
        for (const node of document.querySelectorAll('div, section, aside')) {
            if (node.childElementCount === 0 || candidateSet.has(node)) continue;
            const style = getComputedStyle(node);
            if (style.position !== 'fixed') continue;
            if (!isVisible(node) || isMainUiContainer(node)) continue;
            const rect = node.getBoundingClientRect();
            if (rect.width < 160 || rect.height < 80 || isNarrowEdgeBar(rect)) continue;
            const card = unwrapBackdropToCard(node);
            if (!card || !isVisible(card) || isMainUiContainer(card)) continue;
            const cardRect = card.getBoundingClientRect();
            if (cardRect.width < 160 || cardRect.height < 80 || isNarrowEdgeBar(cardRect)) {
                continue;
            }
            candidateSet.add(card);
        }
    }

    // 嵌套去重：去掉包含内部独立弹窗卡片的全屏/遮罩壳，以及弹窗内部的细碎子块
    let candidates = Array.from(candidateSet);
    candidates = candidates.filter((el) => {
        for (const other of candidates) {
            if (other === el) continue;
            // 若 el 是其他候选卡片的祖先且 el 看起来是遮罩/壳层，则放弃 el
            if (el.contains(other)) {
                const elRect = el.getBoundingClientRect();
                const elCls = typeof el.className === 'string' ? el.className : '';
                if (
                    isFullScreenBox(elRect) ||
                    /(?:wrapper|mask|overlay|backdrop|shade)/i.test(elCls)
                ) {
                    return false;
                }
            }
            // 若 other 包含 el，且 other 不是全屏遮罩壳层，则保留外层完整弹窗卡片 other
            if (other.contains(el)) {
                const otherRect = other.getBoundingClientRect();
                const otherCls = typeof other.className === 'string' ? other.className : '';
                const otherIsWrapper =
                    isFullScreenBox(otherRect) ||
                    /(?:wrapper|mask|overlay|backdrop|shade)/i.test(otherCls);
                if (!otherIsWrapper) {
                    return false;
                }
            }
        }
        return true;
    });

    // 按有效 z-index 降序、DOM 靠后优先（最顶层弹窗优先）
    candidates.sort((a, b) => {
        const za = effectiveZIndex(a);
        const zb = effectiveZIndex(b);
        if (za !== zb) return zb - za;
        const pos = a.compareDocumentPosition(b);
        if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return 1;
        if (pos & Node.DOCUMENT_POSITION_PRECEDING) return -1;
        return 0;
    });

    // 3. 严格排除：验证码 / 弹题 / 随堂练习 / 考试交卷 / 登录 / 破坏性操作
    const protectedDomSelector = [
        '.yidun_popup',
        '.yidun_modal',
        '.yidun_panel',
        "[id^='tcaptcha']",
        "iframe[id^='tcaptcha']",
        "[class*='yidun' i]",
        "[class*='tcaptcha' i]",
        "[class*='captcha' i]",
        "[class*='geetest' i]",
        "[class*='nc-container' i]",
        '.topic-title',
        '.topic-item',
        '.question-body',
        '.ai-class-exercise-dialog',
        '.ques-list',
        '.dialog-test',
        '.el-scrollbar__view .number',
        'input[type="password"]',
    ].join(', ');

    const protectedTitleKeywords = [
        '安全验证', '人机验证', '行为验证', '滑动验证', '滑块验证',
        '图形验证', '点选验证', '拼图验证', '人脸识别', '人脸核验',
        '活体检测', '身份验证', '实名认证',
        '弹题', '随堂练习', '课中测验', '课中练习', '章节测试', '单元测试', '期末考试',
        '确认交卷', '提交试卷',
        '退出登录', '确认退出', '注销账号', '账号登录', '扫码登录', '手机登录', '登录',
    ];

    const protectedBodyKeywords = [
        '请完成安全验证', '安全验证', '人机验证', '行为验证',
        '拖动滑块', '拖动下方滑块', '请依次点击', '点击图中', '拼图验证',
        '人脸核验', '活体检测',
        '确认交卷', '提交试卷', '未做完', '还有题目', '暂未作答',
        '题目未完成', '试卷未完成', '答题卡',
        '确定退出登录', '确认退出登录', '扫码登录', '手机验证码登录',
        '重新考试', '清空学习记录',
    ];

    const extractTitle = (dialog) => {
        const titleSelectors = [
            '.el-dialog__title',
            '.el-message-box__title',
            '.ant-modal-title',
            '.ant-modal-confirm-title',
            '.ss2077-custom-title',
            'h1', 'h2', 'h3', 'h4',
            '[class*="title" i]',
            '[class*="header" i]',
        ];
        for (const sel of titleSelectors) {
            const el = dialog.querySelector(sel);
            if (!el || !isVisible(el)) continue;
            const txt = normalize(el.innerText || el.textContent);
            if (txt && txt.length <= 40) return txt;
        }
        const firstLine = (dialog.innerText || '')
            .split(/\r?\n/)
            .map((line) => line.trim())
            .find((line) => line.length > 0);
        return (firstLine || '').slice(0, 40);
    };

    const isProtectedDialog = (dialog, title, fullText) => {
        if (dialog.matches(protectedDomSelector) || dialog.querySelector(protectedDomSelector)) {
            return true;
        }
        // 多选/单选选项（>=2）通常是测验或答题弹窗，不误关
        const choiceInputs = dialog.querySelectorAll(
            '.el-radio, .el-checkbox, input[type="radio"], input[type="checkbox"]'
        );
        if (choiceInputs.length >= 2) return true;

        for (const iframe of dialog.querySelectorAll('iframe')) {
            const src = (iframe.getAttribute('src') || '').toLowerCase();
            if (/captcha|verify|login/.test(src)) return true;
        }

        if (protectedTitleKeywords.some((kw) => title.includes(kw))) return true;
        if (protectedBodyKeywords.some((kw) => fullText.includes(kw))) return true;
        return false;
    };

    // 4. 按钮文案优先级与关闭图标策略
    const primaryDismissLabels = [
        '下次再说',
        '不再提示',
        '不再提醒',
        '今日不再提示',
        '不再显示',
        '暂不绑定',
        '暂不关注',
        '暂不开启',
        '暂不评价',
        '暂不体验',
        '暂不需要',
        '暂不处理',
        '稍后再说',
        '以后再说',
        '残忍拒绝',
        '跳过引导',
        '跳过提示',
        '跳过',
        '我知道了',
        '知道了',
        '我已知晓',
        '已知晓',
        '我已阅读并同意',
        '已阅读并同意',
        '我已阅读',
        '已阅读',
        '同意并继续',
        '继续学习',
        '继续观看',
        '继续播放',
        '关闭提示',
        '关闭弹窗',
        '关闭',
        '好的',
        '明白',
        '已绑定，不再提示',
        '已绑定不再提示',
        '已关注，不再提示',
        '已关注不再提示',
        '暂不',
    ];

    const noticeContextCues = [
        '提醒', '提示', '须知', '必读', '公告', '通知', '引导', '说明',
        '承诺', '护眼', '夜间', '公众号', '绑定', '关注', '续播',
        '静音', '倍速', '播放', '调查', '问卷', '下载', '客户端', 'APP',
        '微信', '版本', '更新', '习惯分', '学分', '成绩', '挂机', '学习',
    ];
    const noticeOnlyLabels = ['确定', '确认', '同意', '取消'];

    const closeIconSelectors = [
        '.ss2077-custom-title > img.icon',
        '.iconfont.iconguanbi',
        '.iconguanbi',
        '.el-dialog__headerbtn',
        '.el-dialog__close',
        '.el-message-box__headerbtn',
        '.ant-modal-close',
        '.dialog-close',
        '.modal-close',
        '.popup-close',
        '.close-btn',
        '.closeBtn',
        '.close-icon',
        '.closeIcon',
        '.btn-close',
        '[aria-label="Close" i]',
        '[aria-label="关闭"]',
        '[title="关闭"]',
    ];

    const resolveClickable = (el, dialog) => {
        const closestBtn = el.closest
            ? el.closest('button, a, [role="button"], .el-button, .ant-btn')
            : null;
        if (closestBtn && dialog.contains(closestBtn) && isVisible(closestBtn)) {
            return closestBtn;
        }
        return el;
    };

    const findButtonByLabels = (dialog, labels, skippedSet) => {
        const nodes = Array.from(
            dialog.querySelectorAll(
                'button, a, [role="button"], .el-button, .ant-btn, ' +
                '[class*="btn" i], [class*="button" i], div, span'
            )
        );
        const matchesByLabel = new Map();
        for (const node of nodes) {
            if (!isVisible(node) || isDisabled(node)) continue;
            const raw = compact(node.innerText || node.textContent);
            if (!raw || raw.length > 20) continue;
            if (!labels.includes(raw) || skippedSet.has(raw)) continue;
            // 取最内层命中节点，避免选中整行容器
            const hasMatchingChild = Array.from(node.children).some(
                (child) => isVisible(child) && compact(child.innerText || child.textContent) === raw
            );
            if (hasMatchingChild) continue;
            const clickable = resolveClickable(node, dialog);
            if (isDisabled(clickable)) continue;
            if (!matchesByLabel.has(raw)) {
                matchesByLabel.set(raw, clickable);
            }
        }
        for (const label of labels) {
            if (matchesByLabel.has(label)) {
                return { el: matchesByLabel.get(label), label };
            }
        }
        return null;
    };

    const findCloseIcon = (dialog, skippedSet) => {
        const roots = [dialog];
        const parent = dialog.parentElement;
        if (parent && !isMainUiContainer(parent)) {
            const pCls = typeof parent.className === 'string' ? parent.className : '';
            if (/(?:ss2077-custom-modal|el-dialog__wrapper|ant-modal-wrap)/i.test(pCls)) {
                roots.push(parent);
            }
        }
        for (const root of roots) {
            for (const sel of closeIconSelectors) {
                const token = `icon:${sel}`;
                if (skippedSet.has(token)) continue;
                for (const icon of root.querySelectorAll(sel)) {
                    if (!isVisible(icon) || isDisabled(icon)) continue;
                    // 仅点击当前弹窗自身或直接壳层里的关闭图标，避免跨弹窗误点
                    if (root !== dialog && !dialog.contains(icon) && icon.parentElement !== root) {
                        continue;
                    }
                    const clickable = resolveClickable(icon, root);
                    if (!isDisabled(clickable)) {
                        return { el: clickable, label: token };
                    }
                }
            }
        }
        // 兜底：弹窗上半区的独立 ×/✕/✖ 关闭符号
        const glyphToken = 'icon:glyph-close';
        if (!skippedSet.has(glyphToken)) {
            const dlgRect = dialog.getBoundingClientRect();
            const glyphs = new Set(['×', '✕', '✖', '╳', 'X', 'x']);
            for (const node of dialog.querySelectorAll('button, a, span, div, i')) {
                if (!isVisible(node) || isDisabled(node)) continue;
                const txt = normalize(node.innerText || node.textContent);
                if (!glyphs.has(txt)) continue;
                const r = node.getBoundingClientRect();
                if (r.width > 56 || r.height > 56) continue;
                if (r.top > dlgRect.top + dlgRect.height * 0.45) continue;
                if (r.left < dlgRect.left + dlgRect.width * 0.5) continue;
                const clickable = resolveClickable(node, dialog);
                if (!isDisabled(clickable)) {
                    return { el: clickable, label: glyphToken };
                }
            }
        }
        return null;
    };

    for (const dialog of candidates) {
        const title = extractTitle(dialog);
        const fullText = normalize(dialog.innerText || dialog.textContent);
        if (isProtectedDialog(dialog, title, fullText)) {
            continue;
        }
        const sig = `${title}|${fullText.slice(0, 60)}`;
        const skippedSet = new Set(skipBySig[sig] || []);

        let action = findButtonByLabels(dialog, primaryDismissLabels, skippedSet);
        let strategy = 'button';

        if (!action && noticeContextCues.some((cue) => fullText.includes(cue))) {
            action = findButtonByLabels(dialog, noticeOnlyLabels, skippedSet);
            strategy = 'notice_button';
        }
        if (!action) {
            action = findCloseIcon(dialog, skippedSet);
            strategy = 'close_icon';
        }
        if (!action) continue;

        action.el.click();
        return {
            dismissed: true,
            sig,
            title: title || '课程弹窗',
            button: action.label,
            strategy,
        };
    }

    // 5. 已知旧版非模态浮层提示（无阻塞但会遮挡点击区域）
    if (removeFloatingTips) {
        const floatingSelectors = ['.exploreTip', '.ai-helper-Index2', '.aiMsg.once'];
        for (const sel of floatingSelectors) {
            const tip = document.querySelector(sel);
            if (tip && isVisible(tip)) {
                tip.remove();
                return {
                    dismissed: true,
                    sig: `floating:${sel}`,
                    title: '页面浮层提示',
                    button: sel,
                    strategy: 'remove_tip',
                };
            }
        }
    }

    return { dismissed: false, reason: 'none' };
}
