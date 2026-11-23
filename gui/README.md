# Autovisor GUI（Tauri 2 + 内置无感后端 + React）

为 [Autovisor-GUI](https://github.com/RRRRUDDDD/Autovisor-GUI) 打造的现代一体化桌面客户端，采用 **Tauri 2 (Rust) + 内置无感本地后端 + React / Tailwind** 架构：

- **开箱即用、零外部依赖感知**：双击主程序 `Autovisor GUI.exe` 即直接进入桌面应用，无需手动指定或调用外部命令行程序；
- **常驻内置本地后端**：Tauri 启动时在后台静默拉起内置于 `runtime/` 的 `autovisor-backend.exe`（本地回环随机端口 + 随机令牌鉴权 + JSON 握手 + SSE 实时状态流），结构化同步课程进度、完成列表、安全验证提醒与执行日志；
- **强进程树托管与防僵尸**：结合 `stdin` 生命周期监听与 Windows Job Object（`KILL_ON_JOB_CLOSE`），停止任务或退出窗口时优雅保存 Cookie、关闭浏览器并彻底回收进程树；
- **原生 CDP 登录与课程识别**：由 Rust 直接通过本机 Edge / Chrome 的 CDP 协议完成手机号 / 学号 / 扫码登录并自动识别全部课程队列；
- **完整配置与诊断面板**：播放倍速、时长限制、静音、自动验证码、AI 课程掌握度、多渠道消息推送（Windows 系统通知 / Server酱 / Qmsg酱 / Bark / Telegram）与推送测试、浏览器检查与历史日志浏览。

## 开发与构建

```powershell
# 1. 运行后端 / 前端 / Rust 单元测试
cd Autovisor
.venv\Scripts\python.exe -m unittest discover -s tests -q

cd Autovisor\gui
npm install
npm test

cd src-tauri
cargo test

# 2. 一键构建内置后端 + React 前端 + Tauri Release + 便携成品目录
cd ..
powershell -ExecutionPolicy Bypass -File .\build-portable.ps1
```

构建完成后，便携目录位于 `Autovisor/dist/Autovisor-GUI-1.0.0-windows-amd64/`：
- `Autovisor GUI.exe` — 一体化桌面主程序
- `runtime/` — 内置无感后端运行时（自动托管，无需手动操作）
- `userdata/` — 首次启动自动生成的用户配置、Cookie 与运行日志目录
