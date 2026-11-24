import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, Eye, EyeOff, Loader2, Search, X } from 'lucide-react';
import Input from '../ui/Input';
import Label from '../ui/Label';
import { tauriApi } from '../../lib/tauriApi';

export default function StudentLoginForm({
  schoolQuery,
  onChangeSchoolQuery,
  selectedSchool,
  onSelectSchool,
  studentId,
  onChangeStudentId,
  password,
  onChangePassword,
  showPassword,
  onToggleShowPassword,
  rememberCredentials,
  onChangeRemember,
  hasSavedStudent,
  effectiveProxy,
  loading,
  clearingSaved,
  mountedRef,
  onClearRemembered,
  passwordPlaceholder = '请输入密码',
}) {
  const [schoolOptions, setSchoolOptions] = useState([]);
  const [schoolLoading, setSchoolLoading] = useState(false);
  const [schoolDropdownOpen, setSchoolDropdownOpen] = useState(false);
  const schoolBoxRef = useRef(null);

  useEffect(() => {
    const onPointerDown = (e) => {
      if (schoolBoxRef.current && !schoolBoxRef.current.contains(e.target)) {
        setSchoolDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, []);

  useEffect(() => {
    if (!schoolDropdownOpen) return undefined;
    let cancelled = false;
    setSchoolLoading(true);
    const timer = setTimeout(async () => {
      try {
        const list = await tauriApi.searchSchools(schoolQuery.trim(), effectiveProxy);
        if (!cancelled && mountedRef.current) {
          setSchoolOptions(Array.isArray(list) ? list : []);
        }
      } catch {
        if (!cancelled && mountedRef.current) {
          setSchoolOptions([]);
        }
      } finally {
        if (!cancelled && mountedRef.current) {
          setSchoolLoading(false);
        }
      }
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [schoolQuery, schoolDropdownOpen, effectiveProxy, mountedRef]);

  return (
    <>
      <div className="space-y-1.5" ref={schoolBoxRef}>
        <Label htmlFor="login-school">学校</Label>
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-on-surface-variant"
            aria-hidden="true"
          />
          <Input
            id="login-school"
            type="text"
            autoComplete="off"
            placeholder="输入学校名称关键字搜索（如：清华大学）"
            value={schoolQuery}
            disabled={loading}
            onFocus={() => setSchoolDropdownOpen(true)}
            onChange={(e) => {
              onChangeSchoolQuery(e.target.value);
              onSelectSchool(null);
              setSchoolDropdownOpen(true);
            }}
            className="pl-10 pr-9"
          />
          {schoolQuery && !loading && (
            <button
              type="button"
              onClick={() => {
                onChangeSchoolQuery('');
                onSelectSchool(null);
                setSchoolDropdownOpen(true);
              }}
              aria-label="清空学校"
              className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-on-surface-variant hover:text-on-surface"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}

          {schoolDropdownOpen && (
            <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 max-h-52 overflow-y-auto rounded-2xl bg-surface-container-high p-1.5 shadow-elevation-3">
              {schoolLoading ? (
                <div className="flex items-center justify-center gap-2 py-4 text-xs text-on-surface-variant">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                  正在检索智慧树高校库...
                </div>
              ) : schoolOptions.length === 0 ? (
                <p className="px-3 py-3 text-center text-xs text-on-surface-variant">
                  未找到匹配学校，可直接输入完整校名继续登录
                </p>
              ) : (
                schoolOptions.map((item) => {
                  const picked = selectedSchool?.schoolId === item.schoolId;
                  return (
                    <button
                      key={item.schoolId}
                      type="button"
                      onClick={() => {
                        onSelectSchool(item);
                        onChangeSchoolQuery(item.name);
                        setSchoolDropdownOpen(false);
                      }}
                      className={`flex w-full items-center justify-between rounded-xl px-3.5 py-2.5 text-left text-[13px] transition-colors ${
                        picked
                          ? 'bg-secondary-container font-medium text-on-secondary-container'
                          : 'text-on-surface hover:bg-on-surface/[0.06]'
                      }`}
                    >
                      <span className="truncate">{item.name}</span>
                      {picked && (
                        <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />
                      )}
                    </button>
                  );
                })
              )}
            </div>
          )}
        </div>
        {selectedSchool?.schoolId && (
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-success">
            <CheckCircle2 className="h-3.5 w-3.5" />
            已绑定学校：{selectedSchool.name}（ID: {selectedSchool.schoolId}）
          </p>
        )}
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="login-student-id">大学学号</Label>
        <Input
          id="login-student-id"
          type="text"
          autoComplete="username"
          placeholder="请输入学号"
          value={studentId}
          disabled={loading}
          onChange={(e) => onChangeStudentId(e.target.value)}
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="login-student-password">密码</Label>
        <div className="relative">
          <Input
            id="login-student-password"
            type={showPassword ? 'text' : 'password'}
            autoComplete="current-password"
            placeholder={passwordPlaceholder}
            value={password}
            disabled={loading}
            onChange={(e) => onChangePassword(e.target.value)}
            className="pr-11"
          />
          <button
            type="button"
            onClick={onToggleShowPassword}
            disabled={loading}
            aria-label={showPassword ? '隐藏密码' : '显示密码'}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-on-surface-variant hover:text-on-surface"
          >
            {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5">
        <label className="flex cursor-pointer items-center gap-2.5 text-xs text-on-surface-variant">
          <input
            id="login-remember-student-credentials"
            type="checkbox"
            checked={rememberCredentials}
            disabled={loading}
            onChange={(e) => onChangeRemember(e.target.checked)}
            className="h-4 w-4 rounded accent-primary"
          />
          <span>记住学校、学号和密码（下次打开自动填充）</span>
        </label>
        {hasSavedStudent && (
          <button
            type="button"
            disabled={loading || clearingSaved}
            onClick={onClearRemembered}
            className="rounded-full px-2 py-0.5 text-[11px] font-medium text-on-surface-variant hover:bg-on-surface/[0.06] hover:text-error disabled:opacity-50"
          >
            清除已记住
          </button>
        )}
      </div>
    </>
  );
}
