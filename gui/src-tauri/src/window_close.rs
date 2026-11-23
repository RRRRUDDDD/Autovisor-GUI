//! 点击窗口右上角关闭按钮时的状态机与策略。
//!
//! 策略说明：
//! - 偏好为 `ask`（每次询问）时，阻止默认关闭并通知前端弹出选择框；
//! - 若前端在 `PROMPT_ACK_TIMEOUT` 内未确认收到（页面仍在加载、白屏或异常），
//!   自动兜底退出，避免用户被关不掉的窗口困住；
//! - 弹窗显示期间用户再次点击关闭按钮时，直接退出程序。

use std::sync::Mutex;
use std::time::Duration;

pub const PROMPT_ACK_TIMEOUT: Duration = Duration::from_secs(3);
pub const PROMPT_EVENT: &str = "close-requested";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CloseAction {
    #[default]
    Ask,
    Minimize,
    Tray,
    Exit,
}

impl CloseAction {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "ask" => Some(CloseAction::Ask),
            "minimize" => Some(CloseAction::Minimize),
            "tray" => Some(CloseAction::Tray),
            "exit" => Some(CloseAction::Exit),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            CloseAction::Ask => "ask",
            CloseAction::Minimize => "minimize",
            CloseAction::Tray => "tray",
            CloseAction::Exit => "exit",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseDecision {
    /// 允许窗口关闭，随后触发退出并清理后台进程。
    Allow,
    /// 最小化到任务栏。
    Minimize,
    /// 隐藏到系统托盘。
    Hide,
    /// 保持窗口打开并通知前端展示序号为 `u64` 的关闭确认弹窗。
    Prompt(u64),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PromptState {
    Idle,
    Waiting(u64),
    Shown(u64),
}

pub struct CloseGate {
    state: Mutex<(PromptState, u64)>,
}

impl Default for CloseGate {
    fn default() -> Self {
        CloseGate {
            state: Mutex::new((PromptState::Idle, 0)),
        }
    }
}

impl CloseGate {
    pub fn on_close_requested(&self, action: CloseAction) -> CloseDecision {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        let (state, next_id) = &mut *guard;
        match action {
            CloseAction::Minimize => CloseDecision::Minimize,
            CloseAction::Tray => CloseDecision::Hide,
            CloseAction::Exit => {
                *state = PromptState::Idle;
                CloseDecision::Allow
            }
            CloseAction::Ask if *state != PromptState::Idle => {
                // 弹窗打开或等待期间再次点击关闭，直接退出。
                *state = PromptState::Idle;
                CloseDecision::Allow
            }
            CloseAction::Ask => {
                *next_id += 1;
                *state = PromptState::Waiting(*next_id);
                CloseDecision::Prompt(*next_id)
            }
        }
    }

    /// 前端确认弹窗 `id` 已展示，取消超时兜底退出并等待用户选择。
    pub fn acknowledge(&self, id: u64) -> bool {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if guard.0 == PromptState::Waiting(id) {
            guard.0 = PromptState::Shown(id);
            true
        } else {
            false
        }
    }

    /// 若弹窗 `id` 超时仍未被前端确认，重置状态并返回 true 通知宿主退出。
    pub fn expire(&self, id: u64) -> bool {
        let mut guard = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if guard.0 == PromptState::Waiting(id) {
            guard.0 = PromptState::Idle;
            true
        } else {
            false
        }
    }

    /// 用户已完成选择或取消，下次点击关闭按钮重新按策略处理。
    pub fn resolve(&self) {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).0 = PromptState::Idle;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn action_names_are_exact() {
        for name in ["ask", "minimize", "tray", "exit"] {
            assert_eq!(CloseAction::parse(name).unwrap().as_str(), name);
        }
        for invalid in ["", "Tray", " tray", "quit", "hide"] {
            assert!(CloseAction::parse(invalid).is_none());
        }
    }

    #[test]
    fn remembered_actions_do_not_prompt() {
        let gate = CloseGate::default();
        assert_eq!(
            gate.on_close_requested(CloseAction::Minimize),
            CloseDecision::Minimize
        );
        assert_eq!(
            gate.on_close_requested(CloseAction::Tray),
            CloseDecision::Hide
        );
        assert_eq!(
            gate.on_close_requested(CloseAction::Exit),
            CloseDecision::Allow
        );
    }

    #[test]
    fn unacknowledged_prompt_expires_and_acknowledged_one_waits() {
        let gate = CloseGate::default();
        let CloseDecision::Prompt(first) = gate.on_close_requested(CloseAction::Ask) else {
            panic!("expected a prompt");
        };
        assert!(gate.expire(first), "a silent page must not trap the user");
        assert!(!gate.acknowledge(first), "a late acknowledgement is ignored");

        let CloseDecision::Prompt(second) = gate.on_close_requested(CloseAction::Ask) else {
            panic!("expected a prompt");
        };
        assert_ne!(first, second);
        assert!(!gate.acknowledge(first));
        assert!(gate.acknowledge(second));
        assert!(!gate.expire(second), "the user is choosing");
        gate.resolve();
        assert!(matches!(
            gate.on_close_requested(CloseAction::Ask),
            CloseDecision::Prompt(_)
        ));
    }

    #[test]
    fn closing_again_during_a_prompt_exits() {
        let gate = CloseGate::default();
        let CloseDecision::Prompt(id) = gate.on_close_requested(CloseAction::Ask) else {
            panic!("expected a prompt");
        };
        assert!(gate.acknowledge(id));
        assert_eq!(
            gate.on_close_requested(CloseAction::Ask),
            CloseDecision::Allow
        );
        assert!(!gate.expire(id));
        assert!(matches!(
            gate.on_close_requested(CloseAction::Ask),
            CloseDecision::Prompt(_)
        ));
    }
}
