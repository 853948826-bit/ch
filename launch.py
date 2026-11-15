"""一键启动/停止（Windows/macOS 通用）：
  python launch.py start [皮肤包名]   后台启动守护进程 → 加载皮肤 → 启动小精灵
  python launch.py start --open-defense  启动正式模式并打开防护工作台
  python launch.py demo               启动独立的 AI Defense 桌宠验收
  python launch.py stop               停止全部
  python launch.py status             查看运行状态
  python launch.py autostart on|off   开机自动启动（仅当前用户）"""
import http.client
import json, os, signal, subprocess, sys, time, urllib.error, urllib.request
import plistlib
from clinic import AI_DEFENSE_INCLUDED

ROOT = os.path.dirname(os.path.abspath(__file__))
PIDS = os.path.join(ROOT, "run", "pids.json")
API = "http://127.0.0.1:8765"
WIN = sys.platform == "win32"
GUARDIAN_LABEL = "org.cyberclinic.guardian" if AI_DEFENSE_INCLUDED else "org.cyberclinic.community.guardian"
GUARDIAN_PLIST = os.path.join(ROOT, "run", "guardian.plist")
for _s in (sys.stdout, sys.stderr):  # Windows 中文系统重定向输出时默认 GBK，统一 UTF-8 防止打印符号崩溃
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _auth_headers():
    """本机接口令牌（守护进程首次启动生成，仅当前用户可读）。"""
    h = {"Content-Type": "application/json"}
    try:
        h["X-Clinic-Token"] = open(os.path.join(os.path.expanduser("~"), ".cyberclinic", "token")).read().strip()
    except OSError:
        pass
    return h


def gui_python():
    """Windows 用 pythonw 避免弹出控制台窗口。"""
    if WIN:
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(w):
            return w
    return sys.executable


def spawn(args):
    kw = {"cwd": ROOT, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([gui_python()] + args, **kw).pid


def desktop_binary():
    if WIN:
        return os.path.join(ROOT, "AI Defense.exe" if AI_DEFENSE_INCLUDED else "CyberClinic.exe")
    target = os.path.join(ROOT, "desktop", "src-tauri", "target", "release")
    name = "AI Defense.app" if AI_DEFENSE_INCLUDED else "Cyber Clinic.app"
    app = os.path.join(target, "bundle", "macos", name, "Contents", "MacOS", "cyber-clinic")
    return app if os.path.isfile(app) else os.path.join(target, "cyber-clinic")


def demo():
    if not AI_DEFENSE_INCLUDED:
        sys.exit("防护工作台演示需要商业版。")
    import pathlib
    run = pathlib.Path(ROOT) / "run" / "ai-defense-demo"
    run.mkdir(parents=True, exist_ok=True)
    binary = desktop_binary()
    if not os.path.isfile(binary):
        sys.exit("请先构建 AI Defense 桌宠。")
    try:
        for pid in json.loads((run / "pids.json").read_text()).values():
            cmd = _cmdline(pid)
            if "clinic.daemon --defense-demo" in cmd or cmd == binary:
                os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    (run / "python.txt").write_text(gui_python(), encoding="utf-8")
    pids = {"daemon": spawn(["-m", "clinic.daemon", "--defense-demo"])}
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=0.5) as response:
                if json.load(response).get("pid") == pids["daemon"]:
                    break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        sys.exit("AI Defense 演示守护进程未就绪。")
    with (run / "sprite.log").open("a", encoding="utf-8") as log:
        pids["sprite"] = subprocess.Popen([binary], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                          env={**os.environ, "AI_DEFENSE_DEMO": "1"}, start_new_session=not WIN).pid
    (run / "pids.json").write_text(json.dumps(pids), encoding="utf-8")
    print("AI Defense 桌宠已启动：", pids)


def call(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(API + path, data=data, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        try:
            return json.loads(r.read())
        except http.client.IncompleteRead as e:
            raise ConnectionError("本机守护进程连接中断") from e


def launch_guardian_agent():
    """macOS restarts the watchdog even when the desktop app is closed."""
    if sys.platform != "darwin":
        return False
    domain = f"gui/{os.getuid()}"
    plist = {
        "Label": GUARDIAN_LABEL, "ProgramArguments": [gui_python(), "-m", "clinic.guardian"],
        "WorkingDirectory": ROOT, "KeepAlive": True, "ThrottleInterval": 5,
        "EnvironmentVariables": {"AI_DEFENSE_GUARDIAN_MANAGER": "launchd"},
    }
    with open(GUARDIAN_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    result = subprocess.run(["/bin/launchctl", "bootstrap", domain, GUARDIAN_PLIST],
                            capture_output=True, timeout=10)
    if result.returncode == 0:
        return True
    os.remove(GUARDIAN_PLIST)
    return False


def stop_guardian_agent():
    if sys.platform != "darwin" or not os.path.exists(GUARDIAN_PLIST):
        return
    target = f"gui/{os.getuid()}/{GUARDIAN_LABEL}"
    result = subprocess.run(["/bin/launchctl", "bootout", target], capture_output=True, timeout=10)
    current = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, timeout=10)
    if result.returncode and current.returncode == 0:
        raise RuntimeError("macOS 守护服务未停止")
    os.remove(GUARDIAN_PLIST)


def stop():
    try:
        pids = json.load(open(PIDS))
    except (OSError, ValueError):
        pids = {}
    # Close the UI before stopping its guardian to avoid a concurrent recovery attempt.
    for name in ("sprite", "guardian", "daemon"):
        if name == "guardian":
            stop_guardian_agent()
        pid = pids.get(name)
        if type(pid) is not int or pid < 2:
            continue
        cmdline = _cmdline(pid)
        if name == "sprite" and not any(path.lower() in cmdline.lower() for path in (
            desktop_binary(), os.path.join(ROOT, "CyberClinic.exe"), os.path.join(ROOT, "sprite_win", "sprite.py"),
            os.path.join(ROOT, "desktop", "src-tauri", "target", "release", "cyber-clinic"),
        )):
            continue
        if name in ("guardian", "daemon") and f"-m clinic.{name}" not in cmdline:
            continue
        try:
            if WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"已停止 {name}（pid {pid}）")
        except OSError:
            pass
    if os.path.exists(PIDS):
        os.remove(PIDS)
    if AI_DEFENSE_INCLUDED:
        stop_stray()  # 小精灵自动拉起的守护进程不在 pids.json 里，按端口核实后一并停止


def _cmdline(pid):
    try:
        if WIN:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                               capture_output=True, text=True, timeout=15)
        else:
            r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _port_pid():
    """8765 端口上正在监听的进程 pid（旧版本守护进程的 /health 不返回 pid 时用）。"""
    try:
        if WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15).stdout
            for ln in out.splitlines():
                f = ln.split()
                if len(f) >= 5 and f[1].endswith(":8765") and f[3].upper() == "LISTENING":
                    return int(f[4])
        else:
            out = subprocess.run(["lsof", "-ti", "tcp:8765", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5).stdout
            return int(out.split()[0]) if out.split() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def stop_stray():
    """停掉不是本目录启动的守护进程（其他解压目录或旧版本残留），否则新版本会悄悄连上旧进程。
    只结束命令行里确认是 clinic.daemon 的进程。"""
    for _ in range(3):
        try:
            h = call("/health", timeout=1)
        except OSError:
            return True
        pid = h.get("pid") or _port_pid()
        if not pid or "clinic.daemon" not in _cmdline(pid):
            print(f"端口 8765 被其他程序占用（pid {pid}），无法启动守护进程")
            return False
        supervisor = h.get("guardian_pid")
        if type(supervisor) is int and supervisor > 1 and "-m clinic.guardian" in _cmdline(supervisor):
            pid = supervisor
        print(f"发现旧的守护服务（pid {pid}），先将其停止")
        try:
            if WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(1.5)
    return False


def start(pack, open_defense=False):
    if open_defense and not AI_DEFENSE_INCLUDED:
        sys.exit("防护工作台需要商业版。")
    stop()
    if not (stop_stray() if AI_DEFENSE_INCLUDED else not _port_pid()):
        sys.exit("请先关闭占用 8765 端口的程序后重试")
    os.makedirs(os.path.join(ROOT, "run"), exist_ok=True)
    with open(os.path.join(ROOT, "run", "python.txt"), "w", encoding="utf-8") as f:
        f.write(gui_python())  # 小精灵发现守护进程意外退出时，用同一个解释器把它拉起来
    managed = launch_guardian_agent()
    pids = {} if managed else {"guardian": spawn(["-m", "clinic.guardian"])}
    for _ in range(60):
        try:
            health = call("/health", timeout=1)
            owned = health.get("guardian_manager") == "launchd" if managed else health.get("guardian_pid") == pids["guardian"]
            if owned and health.get("patrol_age_s") is not None:
                break
        except OSError:
            pass
        time.sleep(0.5)
    else:
        json.dump(pids, open(PIDS, "w"))
        sys.exit("守护进程未就绪，请查看 run/guardian.log")
    if not pack and not os.path.exists(os.path.join(ROOT, "run", "skin_current.json")):
        try:  # 首次启动（安装版快捷方式不带皮肤参数）：用安装包指定的默认皮肤；之后沿用用户自己的选择
            pack = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("default_skin")
        except (OSError, ValueError):
            pack = None
    if pack:
        try:
            r = call("/skin", {"pack": pack}, timeout=15)
        except urllib.error.HTTPError as e:  # 皮肤包缺失/未解锁：沿用当前皮肤继续启动
            r = json.loads(e.read() or b"{}")
        print("皮肤：", pack, "已加载" if r.get("ok") else r)
    desktop = desktop_binary()
    if WIN and not os.path.isfile(desktop):
        desktop = os.path.join(ROOT, "CyberClinic.exe")
    sprite = os.path.join(ROOT, "sprite_win", "sprite.py") if WIN or not os.path.exists(
        os.path.join(ROOT, "sprite", "build", "CyberClinic.app")) else None
    # macOS 正式客户端（Tauri，与 Windows 同一套界面）；没有时回退 Swift 原生版
    mac_app = desktop
    desktop_args = [desktop] + (["--open-defense"] if open_defense else [])
    if WIN and os.path.exists(desktop):
        pids["sprite"] = subprocess.Popen(desktop_args, cwd=ROOT, stdout=open(os.path.join(ROOT, "run", "sprite.log"), "a"),
                                          stderr=subprocess.STDOUT, creationflags=0x00000008 | 0x00000200).pid
    elif not WIN and os.path.exists(mac_app):
        pids["sprite"] = subprocess.Popen(desktop_args, cwd=ROOT, stdout=open(os.path.join(ROOT, "run", "sprite.log"), "a"),
                                          stderr=subprocess.STDOUT, start_new_session=True).pid
    elif sprite:
        pids["sprite"] = spawn([sprite])
    else:
        subprocess.run(["open", os.path.join(ROOT, "sprite", "build", "CyberClinic.app")])
    json.dump(pids, open(PIDS, "w"))
    print("AI Defense 已启动：", pids)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        args = [arg for arg in sys.argv[2:] if arg != "--open-defense"]
        start(args[0] if args else None, "--open-defense" in sys.argv[2:])
    elif cmd == "demo":
        demo()
    elif cmd == "stop":
        stop()
    elif cmd == "autostart":  # python launch.py autostart on|off
        from clinic import autostart
        on = autostart.set_enabled((sys.argv[2:] or ["on"])[0] == "on")
        print("开机自动启动：" + ("已开启" if on else "已关闭"))
    else:
        try:
            st = call("/status")
            print(json.dumps({"overhead": st["overhead"], "open": [e["title"] for e in st["open"]]}, ensure_ascii=False))
        except OSError:
            print("未运行")
