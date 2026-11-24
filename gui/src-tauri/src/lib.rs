mod backend;
mod browser_detect;
mod commands;
mod config;
mod error;
mod process_tree;
mod runtime;
mod window_close;
mod zhs;

use backend::BackendManager;
use runtime::{load_close_action, resolve_app_paths};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use window_close::{CloseDecision, CloseGate};
use zhs::ZhsSessionManager;

pub(crate) struct AppState {
    pub(crate) backend: Arc<BackendManager>,
    pub(crate) zhs_session: Arc<ZhsSessionManager>,
    pub(crate) close_gate: Arc<CloseGate>,
    pub(crate) exit_cleaned: Arc<AtomicBool>,
}

fn cleanup_before_exit(
    exit_cleaned: &AtomicBool,
    zhs_session: &ZhsSessionManager,
    backend: &BackendManager,
) {
    if !exit_cleaned.swap(true, Ordering::SeqCst) {
        zhs_session.cancel();
        backend.shutdown();
    }
}

pub(crate) fn spawn_cleanup_and_exit(
    app: AppHandle,
    exit_cleaned: Arc<AtomicBool>,
    zhs_session: Arc<ZhsSessionManager>,
    backend: Arc<BackendManager>,
) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    // 防止连续触发关闭窗口 + 托盘退出时第二个线程跳过清理抢先调用 app.exit(0)
    if exit_cleaned.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(move || {
        zhs_session.cancel();
        backend.shutdown();
        app.exit(0);
    });
}

pub(crate) fn data_dir() -> PathBuf {
    resolve_app_paths().data_dir
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn run() {
    let backend = Arc::new(BackendManager::new());
    let zhs_session = Arc::new(ZhsSessionManager::new());
    let close_gate = Arc::new(CloseGate::default());
    let exit_cleaned = Arc::new(AtomicBool::new(false));

    let backend_for_setup = Arc::clone(&backend);
    let backend_for_exit = Arc::clone(&backend);
    let backend_for_tray = Arc::clone(&backend);
    let backend_for_run = Arc::clone(&backend);
    let session_for_exit = Arc::clone(&zhs_session);
    let session_for_tray = Arc::clone(&zhs_session);
    let session_for_run = Arc::clone(&zhs_session);
    let gate_for_window = Arc::clone(&close_gate);
    let cleaned_for_exit = Arc::clone(&exit_cleaned);
    let cleaned_for_tray = Arc::clone(&exit_cleaned);
    let cleaned_for_run = Arc::clone(&exit_cleaned);

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .manage(AppState {
            backend,
            zhs_session,
            close_gate,
            exit_cleaned,
        })
        .setup(move |app| {
            // 应用启动时在后台静默拉起内置无感后端并建立 SSE 状态流
            backend_for_setup.start_async(app.handle().clone());

            let show_item = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "退出程序", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

            let mut tray_builder = TrayIconBuilder::new()
                .menu(&menu)
                .show_menu_on_left_click(false)
                .tooltip("知到 · 智慧树自动化学习助手")
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "quit" => {
                        spawn_cleanup_and_exit(
                            app.clone(),
                            Arc::clone(&cleaned_for_tray),
                            Arc::clone(&session_for_tray),
                            Arc::clone(&backend_for_tray),
                        );
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                });

            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }
            let _ = tray_builder.build(app);
            Ok(())
        })
        .on_window_event(move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let action = load_close_action();
                match gate_for_window.on_close_requested(action) {
                    CloseDecision::Allow => {
                        api.prevent_close();
                        let _ = window.hide();
                        spawn_cleanup_and_exit(
                            window.app_handle().clone(),
                            Arc::clone(&cleaned_for_exit),
                            Arc::clone(&session_for_exit),
                            Arc::clone(&backend_for_exit),
                        );
                    }
                    CloseDecision::Minimize => {
                        api.prevent_close();
                        let _ = window.minimize();
                    }
                    CloseDecision::Hide => {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                    CloseDecision::Prompt(id) => {
                        api.prevent_close();
                        let _ = window.emit_to("main", window_close::PROMPT_EVENT, id);
                        let app = window.app_handle().clone();
                        let gate = Arc::clone(&gate_for_window);
                        let cleaned = Arc::clone(&cleaned_for_exit);
                        let session = Arc::clone(&session_for_exit);
                        let backend = Arc::clone(&backend_for_exit);
                        std::thread::spawn(move || {
                            std::thread::sleep(window_close::PROMPT_ACK_TIMEOUT);
                            if gate.expire(id) {
                                spawn_cleanup_and_exit(app, cleaned, session, backend);
                            }
                        });
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::diagnostics::get_runtime_info,
            commands::diagnostics::restart_backend,
            commands::auth::search_schools,
            commands::auth::zhs_login,
            commands::auth::cancel_zhs_login,
            commands::auth::refresh_courses,
            commands::auth::logout_zhs,
            commands::auth::clear_saved_login,
            commands::system::set_close_behavior,
            commands::system::close_prompt_shown,
            commands::system::close_choice,
            commands::system::pick_browser_exe,
            commands::system::pick_cookie_file,
            commands::system::pick_directory,
            commands::config::load_config,
            commands::config::save_config,
            commands::tasks::get_run_state,
            commands::tasks::start_autovisor,
            commands::tasks::stop_autovisor,
            commands::tasks::cancel_shutdown,
            commands::tasks::run_diagnostic,
            commands::config::test_notification,
            commands::config::test_llm_connection,
            commands::config::test_question_bank,
            commands::config::export_question_cache,
            commands::config::import_question_cache,
            commands::config::clear_question_cache,
            commands::config::fetch_llm_models,
            commands::tasks::set_debug_log,
            commands::auth::start_qr_login,
            commands::auth::poll_qr_login,
            commands::auth::save_qr_image,
            commands::auth::cancel_qr_login,
            commands::diagnostics::check_for_update,
            commands::auth::clear_cookies,
            commands::diagnostics::list_log_files,
            commands::diagnostics::read_log_file,
            commands::system::open_path,
            commands::system::open_url,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Autovisor GUI");

    app.run(move |_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            cleanup_before_exit(&cleaned_for_run, &session_for_run, &backend_for_run);
        }
    });
}
