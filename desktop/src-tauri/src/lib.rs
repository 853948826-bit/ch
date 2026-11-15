//! 赛博诊所桌面小精灵（Tauri 2）：透明置顶窗口 + 网页渲染层。
//! Rust 层负责：守护进程接口代理（令牌只在这里，不暴露给网页）、事件长轮询、透明区鼠标穿透、
//! 拖动跟手（原生线程移动窗口）、全局快捷键、右键菜单弹窗、音效读取、全屏/空闲检测。
//! 界面与动画全部在 ui/ 中，Windows 与 macOS 共用一套代码。
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{Emitter, Manager, PhysicalPosition, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const API: &str = "http://127.0.0.1:8765";
const MENU_W: f64 = 660.0;
const MENU_H: f64 = 570.0;
const COMMERCIAL: bool = cfg!(feature = "commercial");
const APP_NAME: &str = if COMMERCIAL { "AI Defense" } else { "Cyber Clinic" };
/// 网页注册好事件监听后才开始推送，避免首个皮肤/事件丢失
static READY: AtomicBool = AtomicBool::new(false);

#[tauri::command]
fn ui_ready() {
    READY.store(true, Ordering::SeqCst);
}

fn token() -> String {
    if demo_mode() {
        return std::fs::read_to_string("run/ai-defense-demo/token")
            .unwrap_or_default().trim().to_string();
    }
    dirs::home_dir()
        .map(|h| h.join(".cyberclinic").join("token"))
        .and_then(|p| std::fs::read_to_string(p).ok())
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

fn base() -> String {
    if demo_mode() {
        return "http://127.0.0.1:18765".into();
    }
    std::env::var("CLINIC_URL").unwrap_or_else(|_| API.to_string())
}

fn demo_mode() -> bool {
    COMMERCIAL && std::env::var("AI_DEFENSE_DEMO").as_deref() == Ok("1")
}

fn call(method: &str, path: &str, body: Option<Value>, timeout: u64) -> Result<Value, String> {
    let url = format!("{}{}", base(), path);
    let req = ureq::request(method, &url)
        .set("X-Clinic-Token", &token())
        .timeout(Duration::from_secs(timeout));
    let res = match body {
        Some(b) => req.send_json(b),
        None => req.call(),
    };
    match res {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        // 402/400 等业务错误也把 JSON 带回给界面（如“需要解锁”）
        Err(ureq::Error::Status(code, r)) => {
            let mut v = r.into_json::<Value>().unwrap_or_else(|_| json!({}));
            v["http_status"] = json!(code);
            Ok(v)
        }
        Err(e) => Err(format!("守护进程不可达：{e}")),
    }
}

/// 网页调用守护进程接口的唯一通道（异步命令，不阻塞界面线程）
#[tauri::command]
async fn api(method: String, path: String, body: Option<Value>) -> Result<Value, String> {
    call(&method, &path, body, 20)
}

/// 流式接口（如 /ask 边生成边显示）：守护进程逐行返回 JSON，每行转成 clinic://stream 事件 {sid, data}，结束时 {sid, end}
#[tauri::command]
async fn api_stream(app: tauri::AppHandle, path: String, body: Value, sid: u64) -> Result<(), String> {
    use std::io::BufRead;
    let res = ureq::post(&format!("{}{}", base(), path))
        .set("X-Clinic-Token", &token())
        .timeout(Duration::from_secs(90))
        .send_json(body)
        .map_err(|e| format!("守护进程不可达：{e}"))?;
    for line in std::io::BufReader::new(res.into_reader()).lines() {
        let Ok(line) = line else { break };
        if let Ok(v) = serde_json::from_str::<Value>(&line) {
            let _ = app.emit_to("sprite", "clinic://stream", json!({"sid": sid, "data": v}));
        }
    }
    let _ = app.emit_to("sprite", "clinic://stream", json!({"sid": sid, "end": true}));
    Ok(())
}

/// 命中遮罩：网页把当前画面的不透明区域按 cell 像素一格上报，Rust 据此决定鼠标是否穿透
#[derive(Default)]
struct Mask {
    cols: usize,
    rows: usize,
    cell: f64,
    bits: Vec<u8>,
}

#[tauri::command]
fn set_mask(state: tauri::State<Arc<Mutex<Mask>>>, cols: usize, rows: usize, cell: f64, bits: Vec<u8>) {
    *state.lock().unwrap() = Mask { cols, rows, cell, bits };
}

/// 拖动：鼠标按住角色移动时由原生线程让窗口跟随光标（不经网页往返，跟手不卡）
#[derive(Default)]
struct Drag {
    on: AtomicBool,
}

#[tauri::command]
fn drag_begin(app: tauri::AppHandle, drag: tauri::State<Arc<Drag>>, stage: [f64; 4]) {
    if drag.on.swap(true, Ordering::SeqCst) {
        return;
    }
    let drag = drag.inner().clone();
    std::thread::spawn(move || {
        let Some(w) = app.get_webview_window("sprite") else { return };
        let (Ok(c0), Ok(p0)) = (w.cursor_position(), w.outer_position()) else {
            drag.on.store(false, Ordering::SeqCst);
            return;
        };
        let off = (c0.x - p0.x as f64, c0.y - p0.y as f64);
        eprintln!("{} [drag] begin cursor=({:.0},{:.0}) win=({},{})", stamp(), c0.x, c0.y, p0.x, p0.y);
        let mut last = (p0.x, p0.y);
        while drag.on.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(7));
            #[cfg(windows)]
            unsafe {
                use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON};
                if (GetAsyncKeyState(VK_LBUTTON as i32) as u16 & 0x8000) == 0 {
                    break; // 鼠标在窗口外松开也能结束拖动
                }
            }
            let Ok(c) = w.cursor_position() else { continue };
            let (mut nx, mut ny) = (c.x - off.0, c.y - off.1);
            // 角色本体始终留在光标所在屏幕内
            if let Ok(Some(m)) = app.monitor_from_point(c.x, c.y) {
                let sf = m.scale_factor();
                let (mx, my) = (m.position().x as f64, m.position().y as f64);
                let (mw, mh) = (m.size().width as f64, m.size().height as f64);
                let [sx, sy, sw, sh] = stage.map(|v| v * sf);
                nx = nx.min(mx + mw - sx - sw).max(mx - sx);
                ny = ny.min(my + mh - sy - sh).max(my - sy);
            }
            let p = (nx.round() as i32, ny.round() as i32);
            if p != last {
                let _ = w.set_position(PhysicalPosition::new(p.0, p.1));
                last = p;
            }
        }
        drag.on.store(false, Ordering::SeqCst);
        eprintln!("{} [drag] end win=({},{})", stamp(), last.0, last.1);
        let _ = app.emit_to("sprite", "clinic://dragend", ());
    });
}

#[tauri::command]
fn drag_end(drag: tauri::State<Arc<Drag>>) {
    drag.on.store(false, Ordering::SeqCst);
}

/// 皮肤音效：只读皮肤包目录里的音频文件，原样字节返回给网页播放
#[tauri::command]
fn sound_data(path: String) -> Result<tauri::ipc::Response, String> {
    let p = std::path::Path::new(&path);
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    let in_skins = p.components().any(|c| c.as_os_str() == "skins");
    if !in_skins || !["wav", "mp3", "m4a", "aiff", "caf", "ogg"].contains(&ext.as_str()) {
        return Err("不是皮肤音效文件".into());
    }
    let meta = std::fs::metadata(p).map_err(|e| e.to_string())?;
    if meta.len() > 20 * 1024 * 1024 {
        return Err("音效文件过大".into());
    }
    std::fs::read(p).map(tauri::ipc::Response::new).map_err(|e| e.to_string())
}

/// 用户无操作秒数（睡眠判定）
#[tauri::command]
fn idle_seconds() -> f64 {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::SystemInformation::GetTickCount;
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetLastInputInfo, LASTINPUTINFO};
        let mut li = LASTINPUTINFO { cbSize: std::mem::size_of::<LASTINPUTINFO>() as u32, dwTime: 0 };
        if GetLastInputInfo(&mut li) != 0 {
            return GetTickCount().wrapping_sub(li.dwTime) as f64 / 1000.0;
        }
    }
    #[cfg(target_os = "macos")]
    {
        #[link(name = "ApplicationServices", kind = "framework")]
        extern "C" {
            fn CGEventSourceSecondsSinceLastEventType(state: i32, event_type: u32) -> f64;
        }
        // kCGEventSourceStateCombinedSessionState = 0, kCGAnyInputEventType = ~0
        return unsafe { CGEventSourceSecondsSinceLastEventType(0, u32::MAX) };
    }
    #[allow(unreachable_code)]
    0.0
}

/// 全屏/游戏/演示中（勿扰）：Windows 用系统通知状态，能识别 D3D 独占全屏
#[tauri::command]
fn busy_fullscreen() -> bool {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::UI::Shell::SHQueryUserNotificationState;
        let mut s: i32 = 0;
        if SHQueryUserNotificationState(&mut s) == 0 && (s == 2 || s == 3 || s == 4) {
            return true; // BUSY / RUNNING_D3D_FULL_SCREEN / PRESENTATION_MODE
        }
        return win_fg_fullscreen();
    }
    #[cfg(target_os = "macos")]
    return mac_fullscreen();
    #[allow(unreachable_code)]
    false
}

/// Windows：前台窗口（不是桌面、任务栏、本程序）完整盖住它所在的显示器——无边框全屏游戏/视频/全屏应用。
/// 系统“通知状态”接口对无边框全屏窗口不可靠，这里补一层几何判断。
#[cfg(windows)]
unsafe fn win_fg_fullscreen() -> bool {
    use windows_sys::Win32::Foundation::RECT;
    use windows_sys::Win32::Graphics::Gdi::{GetMonitorInfoW, MonitorFromWindow, MONITORINFO, MONITOR_DEFAULTTONEAREST};
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetClassNameW, GetForegroundWindow, GetShellWindow, GetWindowRect, GetWindowThreadProcessId};
    let h = GetForegroundWindow();
    if h.is_null() || h == GetShellWindow() {
        return false;
    }
    let mut pid = 0u32;
    GetWindowThreadProcessId(h, &mut pid);
    if pid == std::process::id() {
        return false;
    }
    let mut cls = [0u16; 64];
    let n = GetClassNameW(h, cls.as_mut_ptr(), cls.len() as i32) as usize;
    let cls = String::from_utf16_lossy(&cls[..n]);
    if ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"].contains(&cls.as_str()) {
        return false;
    }
    let mut r: RECT = std::mem::zeroed();
    if GetWindowRect(h, &mut r) == 0 {
        return false;
    }
    let mut mi: MONITORINFO = std::mem::zeroed();
    mi.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
    if GetMonitorInfoW(MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST), &mut mi) == 0 {
        return false;
    }
    let m = mi.rcMonitor;
    r.left <= m.left && r.top <= m.top && r.right >= m.right && r.bottom >= m.bottom
}

/// macOS：有别的程序的普通窗口完整盖住某块屏幕（全屏游戏、全屏视频、演示、全屏应用）。最大化但没盖住菜单栏的窗口不算。
#[cfg(target_os = "macos")]
fn mac_fullscreen() -> bool {
    use core_foundation::base::{CFType, TCFType};
    use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
    use core_foundation::number::CFNumber;
    use core_foundation::string::{CFString, CFStringRef};
    use core_graphics::display::CGDisplay;
    use core_graphics::geometry::{CGPoint, CGRect, CGSize};
    use core_graphics::window::*;
    extern "C" {
        fn CGRectMakeWithDictionaryRepresentation(d: CFDictionaryRef, r: *mut CGRect) -> bool;
    }
    let Some(arr) = copy_window_info(kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID) else {
        return false;
    };
    let screens: Vec<CGRect> = CGDisplay::active_displays().unwrap_or_default().into_iter().map(|d| CGDisplay::new(d).bounds()).collect();
    let me = std::process::id() as i64;
    let key = |k: CFStringRef| unsafe { CFString::wrap_under_get_rule(k) };
    for item in arr.iter() {
        let d: CFDictionary<CFString, CFType> = unsafe { CFDictionary::wrap_under_get_rule(*item as CFDictionaryRef) };
        let num = |k: CFStringRef| d.find(key(k)).and_then(|v| v.downcast::<CFNumber>()).and_then(|n| n.to_i64());
        if num(unsafe { kCGWindowLayer }) != Some(0) || num(unsafe { kCGWindowOwnerPID }) == Some(me) {
            continue;
        }
        let Some(b) = d.find(key(unsafe { kCGWindowBounds })) else { continue };
        let mut r = CGRect::new(&CGPoint::new(0.0, 0.0), &CGSize::new(0.0, 0.0));
        if !unsafe { CGRectMakeWithDictionaryRepresentation(b.as_CFTypeRef() as CFDictionaryRef, &mut r) } {
            continue;
        }
        let near = |a: f64, b: f64| (a - b).abs() < 1.5;
        if screens.iter().any(|s| near(r.origin.x, s.origin.x) && near(r.origin.y, s.origin.y)
            && near(r.size.width, s.size.width) && near(r.size.height, s.size.height)) {
            return true;
        }
    }
    false
}

/// 专注模式：不和用户的窗口抢置顶（false 时小精灵退回普通层级）
#[tauri::command]
fn set_on_top(app: tauri::AppHandle, on: bool) {
    if let Some(w) = app.get_webview_window("sprite") {
        let _ = w.set_always_on_top(on);
    }
}

/// 右键菜单：独立弹窗，贴着光标、按屏幕空间决定向左还是向右展开
#[tauri::command]
async fn open_menu(app: tauri::AppHandle, ctx: Value) -> Result<(), String> {
    let sprite = app.get_webview_window("sprite").ok_or("no sprite")?;
    let cur = sprite.cursor_position().map_err(|e| e.to_string())?;
    let mon = app
        .monitor_from_point(cur.x, cur.y)
        .ok()
        .flatten()
        .or_else(|| sprite.current_monitor().ok().flatten())
        .ok_or("no monitor")?;
    let sf = mon.scale_factor();
    let (mx, my) = (mon.position().x as f64, mon.position().y as f64);
    let (mw, mh) = (mon.size().width as f64, mon.size().height as f64);
    let (w, h) = (MENU_W * sf, MENU_H * sf);
    let rtl = cur.x > mx + mw / 2.0;
    let x = (if rtl { cur.x - w } else { cur.x }).max(mx).min(mx + mw - w);
    let y = (cur.y - 6.0 * sf).max(my).min(my + mh - h);
    let mut ctx = ctx;
    ctx["defenseAvailable"] = json!(COMMERCIAL);
    ctx["at"] = json!({"x": (cur.x - x) / sf, "y": (cur.y - y) / sf, "rtl": rtl});
    let pos = PhysicalPosition::new(x.round() as i32, y.round() as i32);
    if let Some(m) = app.get_webview_window("menu") {
        let _ = m.set_position(pos);
        m.emit_to("menu", "menu://open", ctx).map_err(|e| e.to_string())?;
    } else {
        // Windows 上在命令线程里创建 WebView 窗口会卡死：统一派发到主线程创建
        let script = format!("window.__MENU_CTX__ = {};", ctx);
        let app2 = app.clone();
        app.run_on_main_thread(move || {
            let built = WebviewWindowBuilder::new(&app2, "menu", WebviewUrl::App("menu.html".into()))
                .title(format!("{APP_NAME} 菜单"))
                .inner_size(MENU_W, MENU_H)
                .transparent(true)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .shadow(false)
                .visible(false)
                .initialization_script(&script)
                .build();
            match built {
                Ok(m) => {
                    let _ = m.set_position(pos);
                    eprintln!("{} [menu] 菜单窗口已创建", stamp());
                }
                Err(e) => eprintln!("{} [menu] 创建菜单窗口失败：{e}", stamp()),
            }
        })
        .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// 智能体设置窗口：普通带标题栏的窗口，已打开就提到前面。
/// 必须是异步命令：同步命令在主线程执行，Windows 上在主线程里同步创建 WebView 窗口会卡死。
#[tauri::command]
async fn open_settings(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("settings") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
        return Ok(());
    }
    let app2 = app.clone();
    app.run_on_main_thread(move || {
        let built = WebviewWindowBuilder::new(&app2, "settings", WebviewUrl::App("settings.html".into()))
            .title(format!("{APP_NAME} · 智能体设置"))
            .inner_size(600.0, 760.0)
            .resizable(true)
            .center()
            .build();
        if let Err(e) = built {
            eprintln!("{} [settings] 创建设置窗口失败：{e}", stamp());
        }
    })
    .map_err(|e| e.to_string())
}

#[tauri::command]
async fn open_defense(app: tauri::AppHandle, assistant: Option<bool>, journal: Option<bool>) -> Result<(), String> {
    if !COMMERCIAL {
        return Err("防护工作台需要商业版".into());
    }
    let handle = app.clone();
    app.run_on_main_thread(move || {
        let view = if journal.unwrap_or(false) { Some("journal") }
            else if assistant.unwrap_or(false) { Some("chat") } else { None };
        if let Err(error) = show_defense(&handle, view) {
            eprintln!("{} [defense] {error}", stamp());
            let _ = handle.emit_to("sprite", "defense://error", ());
        }
    }).map_err(|error| error.to_string())
}

fn show_defense(app: &tauri::AppHandle, view: Option<&str>) -> tauri::Result<()> {
    if let Some(window) = app.get_webview_window("defense") {
        let _ = window.unminimize();
        let _ = window.set_focus();
        if let Some(view) = view {
            let _ = window.emit("defense://view", view);
        }
        return Ok(());
    }
    let page = view.map_or_else(|| "defense/index.html".to_owned(), |view| format!("defense/index.html?view={view}"));
    WebviewWindowBuilder::new(app, "defense", WebviewUrl::App(page.into()))
        .title("AI Defense · 露米")
        .inner_size(1030.0, 740.0)
        .min_inner_size(620.0, 500.0)
        .center()
        .build()
        .map(|_| ())
}

/// 菜单渲染完成后再显示并取得焦点（失焦即关闭）
#[tauri::command]
fn menu_show(app: tauri::AppHandle) {
    eprintln!("{} [menu] 显示", stamp());
    if let Some(m) = app.get_webview_window("menu") {
        let _ = m.show();
        let _ = m.set_focus();
    }
}

#[tauri::command]
fn menu_hide(app: tauri::AppHandle) {
    if let Some(m) = app.get_webview_window("menu") {
        let _ = m.hide();
    }
}

/// 用系统默认程序打开守护进程生成的诊断日志（只允许 run/history 下的 html）
#[tauri::command]
fn open_report(path: String) -> Result<(), String> {
    let p = std::path::Path::new(&path);
    let ok = p.extension().and_then(|e| e.to_str()) == Some("html") && p.components().any(|c| c.as_os_str() == "history");
    if !ok || !p.exists() {
        return Err("日志路径无效".into());
    }
    #[cfg(windows)]
    let r = std::process::Command::new("explorer").arg(p).spawn();
    #[cfg(target_os = "macos")]
    let r = std::process::Command::new("open").arg(p).spawn();
    #[cfg(not(any(windows, target_os = "macos")))]
    let r = std::process::Command::new("xdg-open").arg(p).spawn();
    r.map(|_| ()).map_err(|e| e.to_string())
}

/// 网页端日志/异常写到进程标准错误（排障用）
#[tauri::command]
fn js_log(msg: String) {
    eprintln!("{} [ui] {msg}", stamp());
}

/// 日志时间戳（启动后的秒数，便于和外部采样对齐）
fn stamp() -> String {
    static T0: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
    format!("{:8.2}", T0.get_or_init(std::time::Instant::now).elapsed().as_secs_f64())
}

/// 自检模式（CLINIC_SELFTEST=1）：网页按脚本走一遍交互并把结果写日志
#[tauri::command]
fn selftest() -> bool {
    std::env::var("CLINIC_SELFTEST").map(|v| v == "1").unwrap_or(false)
}

#[tauri::command]
fn quit(app: tauri::AppHandle) {
    app.exit(0);
}

/// 守护进程意外退出时自动拉起：用启动器记下的同一个 Python（run/python.txt），在程序目录里重新启动。
/// 只在当前目录确实是赛博诊所目录时才这么做；返回是否已尝试。
fn revive_daemon() -> bool {
    #[cfg(target_os = "macos")]
    if !demo_mode() && std::path::Path::new("run/guardian.plist").exists() {
        return false; // launchd owns recovery for this installation.
    }
    let path = if demo_mode() { "run/ai-defense-demo/python.txt" } else { "run/python.txt" };
    let Ok(py) = std::fs::read_to_string(path) else { return false };
    if !std::path::Path::new("clinic/daemon.py").exists() {
        return false;
    }
    let mut cmd = std::process::Command::new(py.trim());
    cmd.args(["-m", if demo_mode() { "clinic.daemon" } else { "clinic.guardian" }])
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    if demo_mode() {
        cmd.arg("--defense-demo");
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0000_0008 | 0x0000_0200 | 0x0800_0000); // DETACHED | NEW_GROUP | NO_WINDOW
    }
    match cmd.spawn() {
        Ok(mut c) => {
            eprintln!("{} [daemon] 守护进程不可达，已重新拉起 pid={}", stamp(), c.id());
            std::thread::spawn(move || { let _ = c.wait(); }); // 回收子进程，避免守护进程退出后留下僵尸
            true
        }
        Err(e) => {
            eprintln!("{} [daemon] 拉起守护进程失败：{e}", stamp());
            false
        }
    }
}

/// 事件长轮询：守护进程空闲时阻塞，不耗 CPU；皮肤版本变化时整包推送皮肤
fn poll_events(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let (mut since, mut skin_ver, mut fails) = (0i64, -1i64, 0u32);
        while !READY.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(50));
        }
        loop {
            match call("GET", &format!("/events?since={since}&wait=25&limit=100"), None, 35) {
                Ok(v) => {
                    fails = 0;
                    since = v["seq"].as_i64().unwrap_or(since);
                    let sv = v["skin_ver"].as_i64().unwrap_or(0);
                    if sv != skin_ver {
                        if let Ok(skin) = call("GET", "/skin", None, 30) {
                            skin_ver = sv;
                            let _ = app.emit_to("sprite", "clinic://skin", skin);
                        }
                    }
                    if let Some(evs) = v["events"].as_array() {
                        if !evs.is_empty() {
                            let _ = app.emit_to("sprite", "clinic://events", evs.clone());
                        }
                    }
                    let _ = app.emit_to("sprite", "clinic://online", true);
                }
                Err(_) => {
                    let _ = app.emit_to("sprite", "clinic://online", false);
                    fails += 1;
                    // 连续约 10 秒不可达就拉起一次；之后每分钟最多再试一次，避免反复刷进程
                    if fails == 3 || (fails > 3 && fails % 20 == 0) {
                        revive_daemon();
                    }
                    std::thread::sleep(Duration::from_secs(3));
                }
            }
        }
    });
}

/// 透明区域鼠标穿透：光标落在角色/气泡的不透明处时接收鼠标，否则穿透到桌面。
/// 同时把窗口附近的光标位置推给网页（视线跟随），鼠标不动时什么也不做。
fn hit_test(app: tauri::AppHandle, mask: Arc<Mutex<Mask>>, drag: Arc<Drag>) {
    std::thread::spawn(move || {
        let mut ignoring: Option<bool> = None;
        let mut last = PhysicalPosition::new(f64::NAN, f64::NAN);
        let mut last_pos = PhysicalPosition::new(i32::MIN, i32::MIN);
        let mut was_near = false;
        loop {
            std::thread::sleep(Duration::from_millis(33));
            let Some(w) = app.get_webview_window("sprite") else { continue };
            let (Ok(cur), Ok(pos), Ok(scale)) = (w.cursor_position(), w.outer_position(), w.scale_factor()) else {
                continue;
            };
            if cur == last && pos == last_pos && ignoring.is_some() {
                continue;
            }
            last = cur;
            last_pos = pos;
            let (x, y) = ((cur.x - pos.x as f64) / scale, (cur.y - pos.y as f64) / scale);
            let (opaque, near) = {
                let m = mask.lock().unwrap();
                let (ww, wh) = (m.cols as f64 * m.cell, m.rows as f64 * m.cell);
                let near = x >= -40.0 && y >= -40.0 && x <= ww + 40.0 && y <= wh + 40.0;
                let opaque = if m.cell <= 0.0 || x < 0.0 || y < 0.0 {
                    false
                } else {
                    let (c, r) = ((x / m.cell) as usize, (y / m.cell) as usize);
                    c < m.cols && r < m.rows && m.bits.get(r * m.cols + c).copied().unwrap_or(0) > 0
                };
                (opaque, near)
            };
            if near || was_near {
                was_near = near;
                let _ = app.emit_to("sprite", "clinic://cursor", json!({"x": x, "y": y}));
            }
            let want = !opaque && !drag.on.load(Ordering::SeqCst);
            if ignoring != Some(want) {
                let _ = w.set_ignore_cursor_events(want);
                ignoring = Some(want);
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mask = Arc::new(Mutex::new(Mask::default()));
    let drag = Arc::new(Drag::default());
    tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    let pressed = event.state() == ShortcutState::Pressed;
                    let _ = app.emit_to("sprite", "clinic://hotkey", pressed);
                })
                .build(),
        )
        .manage(mask.clone())
        .manage(drag.clone())
        .invoke_handler(tauri::generate_handler![
            api, set_mask, drag_begin, drag_end, sound_data, idle_seconds, busy_fullscreen, set_on_top, api_stream, open_menu, open_settings, menu_show,
            menu_hide, open_report, quit, ui_ready, js_log, selftest, open_defense
        ])
        .setup(move |app| {
            // macOS：不占程序坞、不显示应用菜单栏（与原 Swift 版一致）
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            let handle = app.handle().clone();
            if let Some(w) = app.get_webview_window("sprite") {
                let _ = w.set_ignore_cursor_events(true); // 网页布局完成后自行定位并显示
            }
            let sc = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::Space);
            if let Err(e) = app.global_shortcut().register(sc) {
                eprintln!("[hotkey] Ctrl+Alt+Space 注册失败：{e}");
            }
            poll_events(handle.clone());
            hit_test(handle, mask.clone(), drag.clone());
            if COMMERCIAL && (demo_mode() || std::env::args_os().any(|arg| arg == "--open-defense")) {
                show_defense(app.handle(), None)?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running AI Defense");
}
