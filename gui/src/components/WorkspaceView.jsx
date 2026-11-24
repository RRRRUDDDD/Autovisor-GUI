import { memo, useMemo } from 'react';
import CourseQueuePanel from './workspace/CourseQueuePanel';
import StudyParamsSidebar from './workspace/StudyParamsSidebar';
import { isValidLlmApiBase, isValidProxyUrl } from '../lib/configModel';

const EMPTY_COURSES = [];

const WorkspaceView = ({
  config,
  onChangeConfig,
  runtime,
  dirty,
  saving,
  saveStatus,
  refreshingCourses = false,
  courseSyncNotice = null,
  starting,
  startError,
  runState,
  taskRunning: taskRunningProp,
  hasRunHistory: hasRunHistoryProp,
  onRefreshCourses,
  onRetrySave,
  onStartStudy,
  onReturnToProgress,
  onCheckCourse,
}) => {
  const courses = config.courses || EMPTY_COURSES;
  const enabledCount = useMemo(() => courses.filter((c) => c.enabled).length, [courses]);
  const taskRunning =
    taskRunningProp !== undefined
      ? Boolean(taskRunningProp)
      : runState?.status === 'running' || runState?.status === 'stopping';
  const hasRunHistory =
    hasRunHistoryProp !== undefined
      ? Boolean(hasRunHistoryProp)
      : Boolean(runState && runState.status !== 'idle');
  const proxyValid = isValidProxyUrl(config.proxy);
  const llmBaseValid = isValidLlmApiBase(config.llm?.apiBase);

  return (
    <div className="m3-workspace-grid grid grid-cols-1 gap-5 md:grid-cols-[minmax(0,1fr)_clamp(20.5rem,32vw,26rem)]">
      <CourseQueuePanel
        courses={courses}
        enabledCount={enabledCount}
        refreshingCourses={refreshingCourses}
        courseSyncNotice={courseSyncNotice}
        taskRunning={taskRunning}
        onChangeConfig={onChangeConfig}
        onRefreshCourses={onRefreshCourses}
        onCheckCourse={onCheckCourse}
      />

      <StudyParamsSidebar
        config={config}
        runtime={runtime}
        enabledCount={enabledCount}
        proxyValid={proxyValid}
        llmBaseValid={llmBaseValid}
        dirty={dirty}
        saving={saving}
        saveStatus={saveStatus}
        starting={starting}
        startError={startError}
        taskRunning={taskRunning}
        hasRunHistory={hasRunHistory}
        refreshingCourses={refreshingCourses}
        onChangeConfig={onChangeConfig}
        onRetrySave={onRetrySave}
        onStartStudy={onStartStudy}
        onReturnToProgress={onReturnToProgress}
      />
    </div>
  );
};

export default memo(WorkspaceView);
