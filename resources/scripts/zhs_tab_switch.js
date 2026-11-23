(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const keywords = ['共享课', '学分课', '翻转', '校内', '融合', 'AI', '智慧', '慕课', '在学', '进行中'];
  const avoid = ['已完结', '已结束', '历史', '退出', '设置', '消息', '帮助', '认证', '选课', '下载', '客户端', '协议', '隐私'];
  const isVisible = (el) => {
    if (!el || typeof el.getBoundingClientRect !== 'function') return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 12 && r.height > 12;
  };
  const seenText = new Set();
  const candidates = Array.from(
    document.querySelectorAll('.el-tabs__item, .tab-item, .nav-item, [role="tab"], li, a, button, span, div')
  ).filter((el) => {
    if (!isVisible(el)) return false;
    if (el.children && el.children.length > 2) return false;
    const txt = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!txt || txt.length > 16 || seenText.has(txt)) return false;
    if (avoid.some((a) => txt.includes(a))) return false;
    if (!keywords.some((k) => txt.includes(k))) return false;
    seenText.add(txt);
    return true;
  });

  const clickNextPages = async () => {
    for (let p = 0; p < 4; p++) {
      const nextBtn = Array.from(
        document.querySelectorAll('button.btn-next, .el-pagination .btn-next, [aria-label*="下一页"], a, button, li')
      ).find((el) => {
        if (!isVisible(el)) return false;
        if (el.disabled || el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true') return false;
        if (el.matches('button.btn-next, .el-pagination .btn-next')) return true;
        const t = (el.innerText || '').trim();
        return t === '下一页' || t === '加载更多' || t === '查看更多';
      });
      if (!nextBtn) break;
      try {
        nextBtn.click();
        await sleep(800);
      } catch (e) {
        break;
      }
    }
  };

  await clickNextPages();
  for (const tab of candidates.slice(0, 8)) {
    try {
      tab.click();
      await sleep(950);
      await clickNextPages();
    } catch (e) {}
  }
  if (candidates.length > 0) {
    try {
      candidates[0].click();
    } catch (e) {}
  }
  return candidates.length;
})()
