async (cfg) => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const isVisible = (el) => {
        if (!el) return false;
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
    };
    const waitFor = async (fn, timeoutMs = 5000, intervalMs = 150) => {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const res = fn();
            if (res) return res;
            await sleep(intervalMs);
        }
        return fn();
    };
    const setInputEl = (el, val) => {
        if (!el) return false;
        el.focus();
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && typeof desc.set === 'function') {
            desc.set.call(el, val);
        } else {
            el.value = val;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    };
    const setInput = (sel, val) => setInputEl(document.querySelector(sel), val);
    const checkTerms = () => {
        const cbs = Array.from(document.querySelectorAll('input.el-checkbox__original'));
        for (const cb of cbs) {
            if (!cb.checked) cb.click();
        }
    };
    const findNewTab = (id, keywords) => {
        const byId = id ? document.getElementById(id) : null;
        if (byId && isVisible(byId)) return byId;
        return Array.from(document.querySelectorAll('.login-tabs .el-tabs__item, .el-tabs__item'))
            .find((el) => isVisible(el) && keywords.some((k) => (el.innerText || '').trim().includes(k))) || null;
    };
    const clickNewSubmit = async () => {
        const btn = await waitFor(() => {
            const el = document.querySelector('.btn-block__grandient_login');
            if (!el || !isVisible(el)) return null;
            if (el.classList.contains('primary') || !el.classList.contains('info')) return el;
            return null;
        }, 2500, 120) || document.querySelector('.btn-block__grandient_login');
        if (btn && isVisible(btn)) {
            btn.click();
            return true;
        }
        return false;
    };

    const isNewLogin = location.hostname.includes('login.zhihuishu.com')
        || Boolean(document.querySelector('.login-tabs, .btn-block__grandient_login, input[name="mobile"], input[name="unid"]'));

    if (isNewLogin) {
        if (cfg.mode === 'qrcode') {
            const tab = findNewTab('tab-3', ['扫码']);
            if (tab) tab.click();
            return { ok: Boolean(tab), page: 'new', mode: 'qrcode' };
        }

        if (cfg.mode === 'student') {
            const tab = findNewTab('tab-2', ['学号']);
            if (tab) tab.click();
            const unidEl = await waitFor(() => {
                const el = document.querySelector('input[name="unid"]');
                return isVisible(el) ? el : null;
            }, 4000, 120);

            let schoolPicked = false;
            if (cfg.schoolName) {
                const wrapper = document.querySelector('.el-select__wrapper, .el-select');
                if (wrapper) wrapper.click();
                await sleep(200);
                const schInput = await waitFor(() => {
                    const el = document.querySelector('input.el-select__input');
                    return isVisible(el) ? el : null;
                }, 3000, 120);
                if (schInput) {
                    setInputEl(schInput, cfg.schoolName);
                    const opt = await waitFor(() => {
                        const items = Array.from(document.querySelectorAll('.el-select-dropdown__item')).filter(isVisible);
                        if (!items.length) return null;
                        return items.find((i) => (i.innerText || '').trim() === cfg.schoolName)
                            || items.find((i) => (i.innerText || '').trim().includes(cfg.schoolName))
                            || items[0];
                    }, 5000, 180);
                    if (opt) {
                        opt.click();
                        schoolPicked = true;
                        await sleep(250);
                    }
                }
            }

            const filledId = setInputEl(unidEl || document.querySelector('input[name="unid"]'), cfg.studentId);
            const pwdEl = Array.from(document.querySelectorAll('input[type="password"]')).find(isVisible)
                || document.querySelector('input[type="password"]');
            const filledPwd = setInputEl(pwdEl, cfg.password);
            checkTerms();
            await sleep(300);
            let submitted = false;
            if (schoolPicked && filledId && filledPwd && cfg.studentId && cfg.password) {
                submitted = await clickNewSubmit();
            }
            return { ok: filledId && filledPwd, page: 'new', mode: 'student', schoolPicked, submitted };
        }

        // phone / account mode on new login page
        const tab = findNewTab('tab-1.1', ['账号登录', '手机号']);
        if (tab && !tab.classList.contains('is-active')) {
            tab.click();
            await sleep(250);
        }
        const mobileEl = await waitFor(() => {
            const el = document.querySelector('input[name="mobile"]');
            return isVisible(el) ? el : null;
        }, 4000, 120);
        const filledUser = setInputEl(mobileEl || document.querySelector('input[name="mobile"]'), cfg.username);
        const pwdEl = Array.from(document.querySelectorAll('input[type="password"]')).find(isVisible)
            || document.querySelector('input[type="password"]');
        const filledPwd = setInputEl(pwdEl, cfg.password);
        checkTerms();
        await sleep(300);
        let submitted = false;
        if (filledUser && filledPwd && cfg.username && cfg.password) {
            submitted = await clickNewSubmit();
        }
        return { ok: filledUser && filledPwd, page: 'new', mode: 'phone', submitted };
    }

    // Legacy passport.zhihuishu.com fallback
    if (cfg.mode === 'qrcode') {
        const qrWall = document.querySelector('.qrcode-wall-warp');
        if (qrWall && isVisible(qrWall)) {
            return { ok: true, page: 'legacy', mode: 'qrcode' };
        }
        const tab = document.querySelector('#qrCodeLoginNew');
        if (tab && isVisible(tab)) tab.click();
        return { ok: true, page: 'legacy', mode: 'qrcode' };
    }

    if (cfg.mode === 'student') {
        const tab = document.querySelector('#qStudentID');
        if (tab) tab.click();
        await sleep(250);
        if (cfg.schoolId) {
            const hidden = document.querySelector('#clSchoolId');
            if (hidden) hidden.value = cfg.schoolId;
        }
        if (cfg.schoolName) {
            setInput('#quickSearch', cfg.schoolName);
            if (!cfg.schoolId) {
                try {
                    if (window.userindex && typeof window.userindex.selectSchoolByName === 'function') {
                        window.userindex.selectSchoolByName();
                    }
                } catch (e) {}
                await sleep(500);
                const firstItem = document.querySelector('#schoolListCode li');
                if (firstItem) firstItem.click();
            }
        }
        setInput('#clCode', cfg.studentId);
        setInput('#clPassword', cfg.password);
        checkTerms();
        await sleep(300);
        const btn = document.querySelector('.wall-sub-btn');
        if (btn && cfg.studentId && cfg.password) btn.click();
        return { ok: true, page: 'legacy', mode: 'student' };
    }

    const tab = document.querySelector('#qSignin');
    if (tab) tab.click();
    await sleep(200);
    setInput('#lUsername', cfg.username);
    setInput('#lPassword', cfg.password);
    checkTerms();
    await sleep(300);
    const btn = document.querySelector('.wall-sub-btn');
    if (btn && cfg.username && cfg.password) btn.click();
    return { ok: true, page: 'legacy', mode: 'phone' };
}
