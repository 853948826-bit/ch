"""打包 Windows 灰度测试版：python3 tools/pack_windows.py <本机皮肤包名> [--id 包内皮肤名] [--label 版本标签]
产物：dist/CyberClinic-Win-<标签>-<日期>.zip（解压即用：启动.bat / 停止.bat / 自检.bat / 使用说明.txt）
  --python-zip <python.org 的 Windows 嵌入式包 zip>：把 Python 运行环境打进包里，用户无需另装 Python
  --installer：再用 NSIS（makensis）生成一键安装程序 dist/CyberClinic-Setup-<标签>-<日期>.exe（需配合 --python-zip）"""
import argparse, json, os, shutil, subprocess, sys, time, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _s in (sys.stdout, sys.stderr):  # Windows 中文系统重定向输出时默认 GBK，统一 UTF-8 防止打印符号崩溃
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
CODE = ["clinic", "sprite_win", "launch.py", "config.json", "LICENSE", "NOTICE", "examples/mcp.json", "tests/smoke_cross.py",
        "skins/lumi"]

EXE = os.path.join(ROOT, "desktop", "src-tauri", "target", "x86_64-pc-windows-msvc", "release", "cyber-clinic.exe")
FIND_PY = r"""set "PY="
if exist python\python.exe set "PY=python\python.exe"
if defined PY goto :havepy
py -3 -c "import TKMOD" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :havepy
python -c "import TKMOD" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :havepy
echo [CyberClinic] Python 3.9+ (with tkinter) not found. Installing Python 3.12 via winget...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo [CyberClinic] Done. Please CLOSE this window and run this script again.
pause
exit /b 1
:havepy"""


def bat(body):
    return ("@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n" + body).replace("\r\n", "\n").replace("\n", "\r\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack")
    ap.add_argument("--id", default="naiwa_frog", help="包内皮肤目录名（建议纯英文）")
    ap.add_argument("--label", default="Beta")
    ap.add_argument("--exe", default=EXE, help="Tauri 客户端 exe；不存在则只带 tk 轻量版")
    ap.add_argument("--extra", action="append", default=[], help="额外打包的皮肤：本机包名=包内名，如 lumi=lumi")
    ap.add_argument("--default", help="首次启动使用的皮肤（包内名），活动奖励皮肤默认回退露米，其余默认同 --id")
    ap.add_argument("--python-zip", help="Python 嵌入式包（python-3.x.y-embed-amd64.zip）")
    ap.add_argument("--installer", action="store_true", help="生成 NSIS 一键安装程序")
    a = ap.parse_args()
    if a.installer and not a.python_zip:
        sys.exit("--installer 需要同时提供 --python-zip（安装版不依赖用户电脑上的 Python）")
    src = os.path.join(ROOT, "skins", a.pack)
    if not os.path.isfile(os.path.join(src, "manifest.json")):
        sys.exit(f"找不到皮肤包 {src}")
    frog_aliases = {"naiwa_frog", "奶蛙百科版.local"}
    reward_sku = "promo.naiwa_frog"
    source_manifest = json.load(open(os.path.join(src, "manifest.json"), encoding="utf-8"))
    reward_pack = a.pack in frog_aliases or source_manifest.get("sku") == reward_sku
    extras = []
    reward_ids = set(frog_aliases)
    if reward_pack:
        reward_ids.add(a.id)
    for spec in a.extra:
        es, _, eid = spec.partition("=")
        extra_src = os.path.join(ROOT, "skins", es)
        with open(os.path.join(extra_src, "manifest.json"), encoding="utf-8") as file:
            extra_data = json.load(file)
        if es in frog_aliases or extra_data.get("sku") == reward_sku:
            extra_data["sku"] = reward_sku
            reward_ids.add(eid or es)
        extras.append((extra_src, eid or es, extra_data))
    default_skin = a.default or ("lumi" if a.id in reward_ids else a.id)
    if default_skin in reward_ids:
        sys.exit("奶蛙百科版是活动奖励，不能设置为安装包默认皮肤")
    name = f"CyberClinic-Win-{a.label}-{time.strftime('%Y%m%d')}"
    out = os.path.join(ROOT, "dist", name)
    shutil.rmtree(out, ignore_errors=True)
    for item in CODE:
        s, d = os.path.join(ROOT, item), os.path.join(out, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy(s, d)
    cfg_p = os.path.join(out, "config.json")
    cfg = json.load(open(cfg_p, encoding="utf-8"))
    cfg["default_skin"] = default_skin
    json.dump(cfg, open(cfg_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if a.python_zip:  # 随包 Python：._pth 是隔离模式的搜索路径，加上 ".." 让它找到上级目录里的 clinic 包与 launch.py
        pyd = os.path.join(out, "python")
        with zipfile.ZipFile(a.python_zip) as z:
            z.extractall(pyd)
        pth = next(f for f in os.listdir(pyd) if f.endswith("._pth"))
        with open(os.path.join(pyd, pth), "a", encoding="utf-8") as f:
            f.write("..\n")
    has_exe = os.path.isfile(a.exe)
    if has_exe:  # 正式客户端：需要系统自带的 WebView2（Win10 21H2+/Win11 已内置）
        shutil.copy(a.exe, os.path.join(out, "CyberClinic.exe"))
    global FIND_PY
    FIND_PY = FIND_PY.replace("TKMOD", "sys" if has_exe else "tkinter").replace(" (with tkinter)", "" if has_exe else " (with tkinter)")
    # 皮肤：去掉原始素材 src/，音效统一转 WAV（Windows 自带播放只支持 WAV）
    for extra_src, extra_id, extra_data in extras:
        extra_dest = os.path.join(out, "skins", extra_id)
        shutil.copytree(extra_src, extra_dest, ignore=shutil.ignore_patterns("src"))
        if extra_id in reward_ids:
            with open(os.path.join(extra_dest, "manifest.json"), "w", encoding="utf-8") as file:
                json.dump(extra_data, file, ensure_ascii=False, indent=1)
    dst = os.path.join(out, "skins", a.id)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("src"))
    man = json.load(open(os.path.join(dst, "manifest.json"), encoding="utf-8"))
    if reward_pack:
        man["sku"] = reward_sku
    for k, fn in list(man.get("sounds", {}).items()):
        if not fn.lower().endswith(".wav"):
            wav = os.path.splitext(fn)[0] + ".wav"
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", os.path.join(dst, fn), os.path.join(dst, wav)], check=True)
            os.remove(os.path.join(dst, fn))
            man["sounds"][k] = wav
    man["author"] = "内部灰度测试，请勿外传"
    json.dump(man, open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    gestures = "单击 = 戳一戳    长按 = 摸头    双击 = 静音/取消静音    拖动 = 拎起换位置"
    if man.get("interaction_poses"):
        gestures = ("点脸颊、胸口、腹部、裙摆或足部可看到不同反馈。长按是等待，双击切换静音，快速三击播放连点反馈。\n"
                    "  左右拖动和向上提会切换姿势，松手后站稳。右键打开衣橱选择袜鞋，悬停款式可看预览。")

    limits = ("  - 小精灵为正式客户端 CyberClinic.exe（逐像素透明、完整动作与右键衣橱预览、全屏/游戏时自动淡出）。\n"
              "  - 按住 Ctrl+Alt+Space 说话：若系统网页内核不支持语音识别，会自动转为打字。\n"
              "  - 首次运行若提示缺少 WebView2，请安装微软 Edge WebView2 运行时。\n"
              "  - 温度检测需管理员权限，默认不检测。") if has_exe else (
              "  - Windows 版小精灵为轻量版：动作以换图、抖动、跳跃为主；暂无语音唤醒与全屏勿扰。\n"
              "  - 透明边缘为硬边，个别深色背景下可能看到细微描边。\n"
              "  - 温度检测需管理员权限，默认不检测。")
    files = {
        "启动.bat": bat(f"""title CyberClinic
{FIND_PY}
%PY% launch.py start
echo.
echo [CyberClinic] Running in background. You can close this window.
timeout /t 5
"""),
        "停止.bat": bat(f"""{FIND_PY}
%PY% launch.py stop
timeout /t 3
"""),
        "自检.bat": bat(f"""title CyberClinic Self-test
{FIND_PY}
echo [CyberClinic] Self-test (~2 min). It briefly creates CPU/memory/hung-window test faults on purpose.
%PY% tests\\smoke_cross.py
echo.
echo [CyberClinic] Sprite self-test:
if not exist CyberClinic.exe %PY% sprite_win\\sprite.py --selftest
echo.
echo [CyberClinic] Please send the newest reports\\smoke-windows-*.json to the project team.
pause
"""),
        "使用说明.txt": f"""赛博诊所 Windows 灰度测试版（{a.label} · {time.strftime('%Y-%m-%d')}）
【内部灰度测试，请勿外传】

一、环境
  Windows 10/11；Python 3.9 以上（首次运行如未安装，启动.bat 会用 winget 自动安装 Python 3.12）。

二、使用
  1. 双击「启动.bat」：后台启动诊断守护进程，并在屏幕右下角出现小精灵（{('露米（奶蛙百科版需活动解锁）' if reward_pack else man.get('name', a.id))}）。
  2. 双击「停止.bat」：全部退出。
  3. 双击「自检.bat」：约 2 分钟的真实故障注入测试，结束后把 reports 里最新的 smoke-windows-*.json 发回项目组。

三、互动
  {gestures}
  右键 = 菜单（立即体检 / 提需求 / 信任常驻程序 / 尺寸 / 退出）
  Ctrl+Alt+Space = 打字提需求（如“电脑好卡”“网络怎么了”“8080 端口被谁占了”）

四、会自动发现的问题（无需任何操作）
  程序报错与崩溃、API 限流/鉴权失败、端口占用（并定位占用进程）、CPU 死循环、内存泄漏、
  内存紧张、磁盘不足、程序窗口未响应。

五、请重点反馈
  1. 小精灵显示是否正常（边缘、透明、位置、尺寸切换）
  2. 戳一戳/拖动/右键/快捷键是否有反应，笑声是否正常
  3. 是否有误报（正常使用却弹出告警）——常驻程序可右键“信任”
  4. 电脑是否有变卡（任务管理器里 pythonw.exe 的 CPU/内存占用）
  5. 日志：run\\daemon.log、run\\sprite.log

六、已知限制（灰度版）
{limits}
""",
    }
    for fn, text in files.items():
        with open(os.path.join(out, fn), "w", encoding="utf-8", newline="") as f:
            f.write(text if fn.endswith(".bat") else text.replace("\n", "\r\n"))
    for fn in files:
        if fn.endswith(".bat"):
            files[fn].encode("ascii")  # cmd 解析 UTF-8 中文不稳定：bat 内容必须纯 ASCII
    zp = out + ".zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _, fs in os.walk(out):
            for f in fs:
                p = os.path.join(base, f)
                z.write(p, os.path.join(name, os.path.relpath(p, out)))
    print(f"已打包：{zp}（{os.path.getsize(zp) / 2**20:.1f} MB）")
    if a.installer:
        build_installer(out, a.label, has_exe)


def build_installer(out, label, has_exe):
    """NSIS 一键安装：装到 %LOCALAPPDATA%\\Programs\\CyberClinic（当前用户，无需管理员），开始菜单 + 桌面快捷方式，
    控制面板可卸载。卸载保留 run\\（购买授权、复盘记录、设置），覆盖安装前先停止正在运行的旧版本。"""
    if not has_exe:
        sys.exit("安装版需要正式客户端 CyberClinic.exe")
    ver = time.strftime("%Y.%m.%d")
    exe = os.path.join(ROOT, "dist", f"CyberClinic-Setup-{label}-{time.strftime('%Y%m%d')}.exe")
    tops = sorted(os.listdir(out))
    rm = "\n".join(f'  RMDir /r "$INSTDIR\\{t}"' if os.path.isdir(os.path.join(out, t)) else f'  Delete "$INSTDIR\\{t}"'
                   for t in tops)
    py, pyw, lp = r"$INSTDIR\python\python.exe", r"$INSTDIR\python\pythonw.exe", r"$INSTDIR\launch.py"
    nsi = f"""Unicode true
!include "MUI2.nsh"
Name "赛博诊所 Cyber Clinic"
OutFile "{exe}"
InstallDir "$LOCALAPPDATA\\Programs\\CyberClinic"
RequestExecutionLevel user
SetCompressor /SOLID lzma
!define MUI_ICON "{os.path.join(ROOT, 'desktop', 'src-tauri', 'icons', 'icon.ico')}"
!define MUI_UNICON "{os.path.join(ROOT, 'desktop', 'src-tauri', 'icons', 'icon.ico')}"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "立即启动赛博诊所"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
VIProductVersion "{ver}.0"
VIAddVersionKey "ProductName" "赛博诊所 Cyber Clinic"
VIAddVersionKey "FileVersion" "{ver}"
VIAddVersionKey "FileDescription" "赛博诊所安装程序（{label}）"

Section
  IfFileExists "{py}" 0 +2
    nsExec::Exec '"{py}" "{lp}" stop'
  SetOutPath "$INSTDIR"
  File /r "{out}\\*"
  WriteUninstaller "$INSTDIR\\uninstall.exe"
  CreateDirectory "$SMPROGRAMS\\赛博诊所"
  CreateShortCut "$SMPROGRAMS\\赛博诊所\\赛博诊所.lnk" "{pyw}" '"{lp}" start' "$INSTDIR\\CyberClinic.exe" 0
  CreateShortCut "$SMPROGRAMS\\赛博诊所\\退出赛博诊所.lnk" "{pyw}" '"{lp}" stop' "$INSTDIR\\CyberClinic.exe" 0
  CreateShortCut "$SMPROGRAMS\\赛博诊所\\卸载赛博诊所.lnk" "$INSTDIR\\uninstall.exe"
  CreateShortCut "$DESKTOP\\赛博诊所.lnk" "{pyw}" '"{lp}" start' "$INSTDIR\\CyberClinic.exe" 0
  !define UK "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\CyberClinic"
  WriteRegStr HKCU "${{UK}}" "DisplayName" "赛博诊所 Cyber Clinic"
  WriteRegStr HKCU "${{UK}}" "DisplayVersion" "{ver}"
  WriteRegStr HKCU "${{UK}}" "Publisher" "万有智流（杭州）科技发展有限责任公司"
  WriteRegStr HKCU "${{UK}}" "DisplayIcon" "$INSTDIR\\CyberClinic.exe"
  WriteRegStr HKCU "${{UK}}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${{UK}}" "UninstallString" '"$INSTDIR\\uninstall.exe"'
  WriteRegDWORD HKCU "${{UK}}" "NoModify" 1
  WriteRegDWORD HKCU "${{UK}}" "NoRepair" 1
SectionEnd

Function LaunchApp
  Exec '"{pyw}" "{lp}" start'
FunctionEnd

Section "Uninstall"
  nsExec::Exec '"{py}" "{lp}" stop'
  nsExec::Exec '"{py}" "{lp}" autostart off'
  Delete "$DESKTOP\\赛博诊所.lnk"
  RMDir /r "$SMPROGRAMS\\赛博诊所"
{rm}
  Delete "$INSTDIR\\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${{UK}}"
SectionEnd
"""
    nsi_p = out + ".nsi"
    with open(nsi_p, "w", encoding="utf-8-sig") as f:
        f.write(nsi)
    subprocess.run(["makensis", "-V2", nsi_p], check=True)
    os.remove(nsi_p)
    print(f"安装程序：{exe}（{os.path.getsize(exe) / 2**20:.1f} MB）")


if __name__ == "__main__":
    main()
