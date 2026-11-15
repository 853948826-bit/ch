"""开机自动启动（只作用于当前用户，可随时关闭）：
macOS：~/Library/LaunchAgents 登录项；Windows：HKCU\\...\\Run 注册表项。登录后执行 launch.py start。"""
import os, subprocess, sys
from . import AI_DEFENSE_INCLUDED

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = "org.cyberclinic.launch" if AI_DEFENSE_INCLUDED else "org.cyberclinic.community.launch"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "CyberClinic" if AI_DEFENSE_INCLUDED else "CyberClinicCommunity"


def _python():
    if sys.platform == "win32":
        w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(w):
            return w
    return sys.executable


def enabled():
    if sys.platform == "darwin":
        return os.path.exists(PLIST)
    if sys.platform == "win32":
        r = subprocess.run(["reg", "query", RUN_KEY, "/v", RUN_NAME], capture_output=True, creationflags=0x08000000)
        return r.returncode == 0
    return False


def set_enabled(on):
    launch = os.path.join(ROOT, "launch.py")
    if sys.platform == "darwin":
        if on:
            os.makedirs(os.path.dirname(PLIST), exist_ok=True)
            from xml.sax.saxutils import escape as x
            with open(PLIST, "w", encoding="utf-8") as f:
                f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array><string>{x(_python())}</string><string>{x(launch)}</string><string>start</string></array>
<key>WorkingDirectory</key><string>{x(ROOT)}</string>
<key>RunAtLoad</key><true/>
</dict></plist>
""")
        elif os.path.exists(PLIST):
            os.remove(PLIST)
    elif sys.platform == "win32":
        flags = 0x08000000
        if on:
            subprocess.run(["reg", "add", RUN_KEY, "/v", RUN_NAME, "/t", "REG_SZ", "/f",
                            "/d", f'"{_python()}" "{launch}" start'], capture_output=True, creationflags=flags)
        else:
            subprocess.run(["reg", "delete", RUN_KEY, "/v", RUN_NAME, "/f"], capture_output=True, creationflags=flags)
    return enabled()
