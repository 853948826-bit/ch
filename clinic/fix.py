"""修复层：处方白名单。只处理当前用户自己的进程，执行前由用户确认，执行后核实结果并写入维护日志。
不做任何系统级改动（不碰系统进程、系统设置、网络配置、他人文件）。"""
import os, signal, subprocess, sys, time

from . import sensors

WIN = sys.platform == "win32"
# 永不处理：系统关键进程、赛博诊所自身
PROTECTED = {"kernel_task", "launchd", "WindowServer", "loginwindow", "Finder", "Dock", "SystemUIServer",
             "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe", "svchost.exe", "dwm.exe",
             "smss.exe", "wininit.exe", "System", "Registry", "cyber-clinic", "CyberClinic.exe", "CyberClinic"}


def _target(ev):
    """事件 → (动作, pid, 进程名, 按钮文字)；没有可执行的处方返回 None。"""
    e, k = ev.get("evidence", {}), ev.get("kind")
    if k in ("cpu_hog", "mem_leak", "mem_hog", "hung", "stale_proc"):
        return "terminate", e.get("pid"), e.get("name"), f"结束「{e.get('name')}」"
    if k == "stopped" and not WIN:
        return "resume", e.get("pid"), e.get("name"), f"恢复运行「{e.get('name')}」"
    if k == "zombie":
        return "terminate", e.get("ppid"), e.get("parent"), f"结束父进程「{e.get('parent')}」以清除僵尸"
    if k == "log_port":
        import re
        m = re.search(r"(?:port|:)\s*(\d{2,5})\b", e.get("text", ""))
        owner = sensors.port_owner(int(m.group(1))) if m else None
        if owner:
            return "terminate", owner[0], owner[1], f"结束占用端口 {m.group(1)} 的「{owner[1]}」"
    return None


def _proc(pid):
    return sensors.procs_fast().get(pid)


def _check(pid, name):
    """确认目标仍是同一个、属于当前用户、不在保护名单。返回错误说明或 None。"""
    if not pid or pid in (os.getpid(), os.getppid()) or pid <= 4:
        return "这个进程不能处理"
    if name in PROTECTED:
        return f"「{name}」是系统或诊所自身的进程，不能处理"
    p = _proc(pid)
    if not p and not WIN:  # 快路径只含当前用户进程：区分“已退出”和“别的用户的进程”（Windows 上 os.kill 会直接结束进程，不能这样探测）
        try:
            os.kill(pid, 0)  # 成功说明是自己的进程，只是已退出未被回收
        except PermissionError:
            return f"「{name}」不是当前用户的进程，没有权限处理"
        except OSError:
            pass
    if not p or p[1].startswith("Z"):
        return f"「{name}」(pid {pid}) 已经不在运行了"
    if name and p[4] != name:
        return f"pid {pid} 现在是「{p[4]}」，不是「{name}」，为安全起见不处理"
    return None


GPU_KINDS = ("gpu_reset", "bsod", "gpu_driver")
GUIDE_ACTIONS = ("open_driver_page",)  # 授权后只做引导，不改动系统


def _driver_page(ev):
    """显卡驱动类：用户授权后只打开厂商官方驱动页（网址取自白名单，不信任事件里的字段）；不下载、不安装、不回滚。"""
    from . import gpu
    a = (ev.get("evidence") or {}).get("gpu") if ev.get("kind") in GPU_KINDS else None
    url = gpu.DRIVER_PAGES.get((a or {}).get("vendor"))
    return (a["vendor"], url) if url else None


def plan(ev):
    """可执行的处方（给界面展示）。"""
    d = _driver_page(ev)
    if d:
        return [{"id": "open_driver_page", "label": f"授权：打开 {d[0]} 官方驱动页", "pid": None, "name": d[0],
                 "detail": "只在浏览器里打开厂商官网的驱动页面，下载安装由你决定；诊所不会自动下载、安装、卸载或回滚驱动",
                 "irreversible": False}]
    t = _target(ev)
    if not t:
        return []
    act, pid, name, label = t
    detail = ("先请它正常退出，3 秒内没退出再强制结束；未保存的内容会丢失，无法撤销" if act == "terminate"
              else "发送继续信号，让被挂起的进程接着运行")
    return [{"id": act, "label": label, "pid": pid, "name": name, "detail": detail,
             "irreversible": act == "terminate"}]


def _alive(pid, name):
    p = _proc(pid)
    return bool(p) and not p[1].startswith("Z") and (not name or p[4] == name)


def _terminate(pid, name):
    if WIN:
        flags = 0x08000000  # CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, creationflags=flags)
    else:
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        if not _alive(pid, name):
            return "已正常结束"
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=0x08000000)
    else:
        os.kill(pid, signal.SIGKILL)
    for _ in range(20):
        time.sleep(0.1)
        if not _alive(pid, name):
            return "正常退出无响应，已强制结束"
    raise OSError("结束失败，进程仍在运行（可能需要更高权限）")


def apply(ev, action):
    """执行处方，返回 {ok, msg}；核实不了的一律算失败。"""
    if action == "open_driver_page":
        d = _driver_page(ev)
        if not d:
            return {"ok": False, "msg": "这个异常没有可执行的修复动作"}
        try:
            if WIN:
                os.startfile(d[1])
            else:
                import webbrowser
                webbrowser.open(d[1])
        except OSError as e:
            return {"ok": False, "msg": f"打开网页失败：{e}"}
        return {"ok": True, "msg": f"已打开 {d[0]} 官方驱动页；下载安装前建议先关闭游戏，装完重启电脑"}
    t = _target(ev)
    if not t or t[0] != action:
        return {"ok": False, "msg": "这个异常没有可执行的修复动作"}
    act, pid, name, label = t
    err = _check(pid, name)
    if err:
        return {"ok": False, "msg": err}
    try:
        if act == "terminate":
            how = _terminate(pid, name)
            return {"ok": True, "msg": f"「{name}」(pid {pid}) {how}"}
        if act == "resume":
            os.kill(pid, signal.SIGCONT)
            time.sleep(0.3)
            p = _proc(pid)
            if p and p[1].startswith("T"):
                return {"ok": False, "msg": f"「{name}」仍处于挂起状态"}
            return {"ok": True, "msg": f"「{name}」(pid {pid}) 已恢复运行"}
    except PermissionError:
        return {"ok": False, "msg": f"没有权限处理「{name}」"}
    except ProcessLookupError:
        return {"ok": True, "msg": f"「{name}」已经退出"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": False, "msg": "未知动作"}
