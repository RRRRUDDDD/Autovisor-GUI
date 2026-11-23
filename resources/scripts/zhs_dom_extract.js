(() => {
  const courses = [];
  const seen = new Set();
  const isCourseUrl = (u) => {
    if (typeof u !== 'string') return false;
    const l = u.toLowerCase();
    return l.includes('studyvideoh5.zhihuishu.com')
      || l.includes('fusioncourseh5.zhihuishu.com')
      || l.includes('studywisdomh5.zhihuishu.com')
      || l.includes('wisdom-mooc.zhihuishu.com')
      || l.includes('ai-smart-course-student-pro.zhihuishu.com')
      || l.includes('hike.zhihuishu.com');
  };
  const normUrl = (u) => {
    let s = String(u || '').trim();
    if (s.startsWith('//')) s = 'https:' + s;
    else if (s.startsWith('http://')) s = 'https://' + s.slice(7);
    if (s.endsWith('#/')) s = s.slice(0, -2);
    else if (s.endsWith('#')) s = s.slice(0, -1);
    return s;
  };
  const genericActionTitles = new Set([
    '进入学习', '继续学习', '开始学习', '去学习', '立即学习', '查看详情', '进入课程', '开始上课'
  ]);
  const skipContainerKey = (k) => {
    const l = String(k || '').toLowerCase();
    return [
      'recommend', 'hotcourse', 'banner', 'advert', 'adlist', 'guesslike', 'popular',
      'endcourse', 'finishcourse', 'finishedcourse', 'overcourse', 'historycourse', 'pastcourse', 'closedcourse'
    ].some((bad) => l.includes(bad));
  };
  const pushCourse = (item) => {
    if (!item || !item.url) return;
    const url = normUrl(item.url);
    if (!isCourseUrl(url) || seen.has(url)) return;
    seen.add(url);
    courses.push({
      title: String(item.title || '').trim().replace(/\s+/g, ' '),
      url,
      teacher: String(item.teacher || '').trim(),
      school: String(item.school || '').trim(),
      progress: item.progress != null ? String(item.progress).trim() : '',
    });
  };

  // 1) Direct anchor links with enclosing course card fallback for title/metadata
  document.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || a.href || '';
    if (!isCourseUrl(href)) return;
    let title = (a.getAttribute('title') || a.innerText || '').trim().split('\n')[0].trim();
    let teacher = '';
    let school = '';
    let progress = '';
    const card = a.closest('.course-item, .course-card, .course-box, .my-course-item, .item-box, li, [class*="course"]');
    if (card) {
      if (!title || genericActionTitles.has(title) || title.length <= 1) {
        const titleEl = card.querySelector('.courseName, .course-name, .course-title, .title, h3, h4, [title]');
        if (titleEl) {
          title = (titleEl.getAttribute('title') || titleEl.innerText || '').trim().split('\n')[0].trim();
        }
      }
      const teacherEl = card.querySelector('.teacherName, .teacher-name, .teacher, [class*="teacher"]');
      if (teacherEl) teacher = (teacherEl.innerText || '').trim();
      const schoolEl = card.querySelector('.schoolName, .school-name, .school, [class*="school"]');
      if (schoolEl) school = (schoolEl.innerText || '').trim();
      const progEl = card.querySelector('.processNum, .progress-num, .percent, [class*="progress"], [class*="process"]');
      if (progEl) {
        const m = (progEl.innerText || '').match(/\d+(?:\.\d+)?%/);
        if (m) progress = m[0];
      }
    }
    if (genericActionTitles.has(title)) title = '';
    pushCourse({ title, url: href, teacher, school, progress });
  });

  // 2) Walk Vue 2 (__vue__) & Vue 3 (__vueParentComponent / __vue_app__) reactive state
  const visited = new Set();
  const scanVal = (val, depth = 0, parentKey = '') => {
    if (!val || depth > 6 || typeof val !== 'object') return;
    if (val instanceof Node || val instanceof Window) return;
    if (visited.has(val)) return;
    visited.add(val);
    if (Array.isArray(val)) {
      for (const entry of val.slice(0, 120)) scanVal(entry, depth + 1, parentKey);
      return;
    }

    const parentLower = String(parentKey || '').toLowerCase();
    const name = val.courseName || val.courseTitle || (parentLower.includes('course') ? (val.name || val.title || '') : '');
    const rawUrl = val.studyUrl || val.playUrl || val.pcUrl || val.courseUrl || val.linkUrl || val.videoUrl || val.jumpUrl || val.h5Url || val.newCourseUrl || val.learnUrl || val.studentUrl || '';
    const secret = val.secret || val.recruitAndCourseId || val.secretStr || '';
    let finalUrl = isCourseUrl(rawUrl) ? normUrl(rawUrl) : '';

    if (!finalUrl && typeof secret === 'string' && secret.trim().length >= 4 && !/\s/.test(secret.trim()) && (val.courseName || val.courseTitle || val.recruitAndCourseId || val.courseId || parentLower.includes('course'))) {
      const hint = [val.courseType, val.shareType, val.courseModel, val.typeName, val.courseTag, name, parentLower].join(' ').toLowerCase();
      const isFusion = val.fusionCourseFlag == 1 || val.isFusion == 1 || val.fusionFlag == 1 || val.courseType == 4 || hint.includes('fusion') || hint.includes('融合');
      finalUrl = isFusion
        ? ('https://fusioncourseh5.zhihuishu.com/stuStudy?recruitAndCourseId=' + secret.trim())
        : ('https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=' + secret.trim());
    }
    if (!finalUrl && (val.courseId || val.aiCourseId) && (val.classId || val.recruitId)) {
      const aiHint = [val.courseType, val.typeName, val.courseModel, parentLower].join(' ').toLowerCase();
      const isAi = Boolean(val.aiCourseId) || aiHint.includes('ai-smart') || aiHint.includes('aicourse') || aiHint.includes('ai智慧') || (aiHint.includes('ai') && !aiHint.includes('aided'));
      if (isAi) {
        finalUrl = 'https://ai-smart-course-student-pro.zhihuishu.com/learnPage?courseId=' + encodeURIComponent(String(val.courseId || val.aiCourseId).trim()) + '&classId=' + encodeURIComponent(String(val.classId || val.recruitId).trim());
      }
    }
    if (!finalUrl && val.courseId && (val.courseName || val.courseTitle || parentLower.includes('course'))) {
      const isHike = parentLower.includes('startinng') || parentLower.includes('hike') || parentLower.includes('aided') || parentLower.includes('schoolcourse') || parentLower.includes('creditcourse') || val.courseNature != null || val.aidedTeaching != null || String(val.pcUrl || '').toLowerCase().includes('hike');
      if (isHike) {
        finalUrl = 'https://hike.zhihuishu.com/aidedteaching/sourceLearning/sourceLearning?courseId=' + encodeURIComponent(String(val.courseId).trim());
      }
    }

    if (finalUrl) {
      pushCourse({
        title: name,
        url: finalUrl,
        teacher: val.teacherName || val.speakerName || val.mainTeacher || val.lecturerName || val.teacher || '',
        school: val.schoolName || val.collegeName || val.universityName || '',
        progress: val.progress ?? val.studyProcess ?? val.studyProgress ?? val.learnProgress ?? val.studyRate ?? val.finishPercent ?? '',
      });
    }

    let keys;
    try { keys = Object.keys(val); } catch (e) { return; }
    for (const k of keys.slice(0, 60)) {
      if (k.startsWith('_') || k.startsWith('$') || skipContainerKey(k)) continue;
      let child;
      try { child = val[k]; } catch (e) { continue; }
      if (child && typeof child === 'object') scanVal(child, depth + 1, k);
    }
  };

  const seenVm = new Set();
  const walkVue2 = (vm, depth = 0) => {
    if (!vm || seenVm.has(vm) || depth > 14) return;
    seenVm.add(vm);
    try { if (vm.$data) scanVal(vm.$data, 0, ''); } catch (e) {}
    try { if (vm.$props) scanVal(vm.$props, 0, ''); } catch (e) {}
    try { if (vm.$store && vm.$store.state) scanVal(vm.$store.state, 0, ''); } catch (e) {}
    if (Array.isArray(vm.$children)) {
      for (const child of vm.$children) walkVue2(child, depth + 1);
    }
  };
  const walkVue3 = (comp, depth = 0) => {
    if (!comp || seenVm.has(comp) || depth > 14) return;
    seenVm.add(comp);
    try { if (comp.setupState) scanVal(comp.setupState, 0, ''); } catch (e) {}
    try { if (comp.data) scanVal(comp.data, 0, ''); } catch (e) {}
    try { if (comp.props) scanVal(comp.props, 0, ''); } catch (e) {}
    try {
      const gp = comp.appContext && comp.appContext.config && comp.appContext.config.globalProperties;
      if (gp && gp.$store && gp.$store.state) scanVal(gp.$store.state, 0, '');
    } catch (e) {}
    try {
      if (comp.subTree && comp.subTree.component) walkVue3(comp.subTree.component, depth + 1);
    } catch (e) {}
  };

  const appRoot = document.querySelector('#main-app, #app');
  if (appRoot && appRoot.__vue__) walkVue2(appRoot.__vue__, 0);
  if (appRoot && appRoot.__vueParentComponent) walkVue3(appRoot.__vueParentComponent, 0);
  if (appRoot && appRoot.__vue_app__ && appRoot.__vue_app__._container && appRoot.__vue_app__._container._vnode && appRoot.__vue_app__._container._vnode.component) {
    walkVue3(appRoot.__vue_app__._container._vnode.component, 0);
  }
  document.querySelectorAll('*').forEach((el) => {
    if (el.__vue__) walkVue2(el.__vue__, 0);
    if (el.__vueParentComponent) walkVue3(el.__vueParentComponent, 0);
  });

  // 3) Visible user name fallback from page header
  let userName = '';
  for (const sel of ['.user-name', '.nick-name', '.account-name', '.header-user-name', '.stu-name', '.name-box']) {
    const el = document.querySelector(sel);
    const t = (el && el.innerText || '').trim();
    if (t && t.length <= 24) {
      userName = t;
      break;
    }
  }

  return { courses, userName };
})()
