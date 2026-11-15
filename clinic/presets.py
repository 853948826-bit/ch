"""内置预设白名单：工作/游戏环境里“本来就吃资源”的程序，高占用不弹窗。
只豁免高负载类告警（CPU 高占用、内存大户，部分含内存快速增长）；卡死、闪退照常告警。
python/node 等脚本解释器不进名单：智能体和脚本的死循环是核心检测场景。
名字不区分大小写、忽略 .exe；“前缀*”或“*后缀”做通配。用户自己的“以后不提醒”另存 run/trust.json。"""

HEAVY = ("cpu_hog", "mem_hog")
LOADING = HEAVY + ("mem_leak",)  # 启动/加载阶段内存涨得快属正常

PRESETS = {
    "vm": {"label": "虚拟机与容器", "kinds": LOADING, "names": [
        "QEMULauncher", "qemu-system-*", "com.apple.Virtualization.VirtualMachine", "VirtualBoxVM", "VBoxHeadless",
        "vmware-vmx", "vmware-vmx-*", "prl_vm_app", "vmmem", "vmmemWSL", "vmwp", "com.docker.backend",
        "com.docker.virtualization", "Docker Desktop"]},
    "game": {"label": "游戏与游戏平台", "kinds": LOADING, "names": [
        "steam", "steamwebhelper", "EpicGamesLauncher", "Battle.net", "EADesktop", "EABackgroundService", "Origin",
        "upc", "UbisoftConnect", "GalaxyClient", "WeGame", "tgp_daemon", "RiotClientServices", "LeagueClient",
        "League of Legends", "VALORANT-Win64-Shipping", "*-Win64-Shipping", "cs2", "csgo", "dota2", "GenshinImpact",
        "YuanShen", "StarRail", "ZenlessZoneZero", "Overwatch", "r5apex", "TslGame", "RobloxPlayerBeta",
        "Minecraft.Windows", "javaw", "Hearthstone", "Wow", "Diablo IV", "eldenring", "Cyberpunk2077",
        "BlackMythWukong", "b1-Win64-Shipping", "GTA5", "RDR2", "FortniteClient-Win64-Shipping"]},
    "build": {"label": "编译与构建", "kinds": HEAVY, "names": [
        "rustc", "cargo", "clang", "clang++", "cc1", "cc1plus", "cc1obj", "ld", "ld64", "lld", "lld-link",
        "swift-frontend", "swiftc", "xcodebuild", "XCBBuildService", "MSBuild", "cl", "link", "ninja", "make",
        "cmake", "javac", "gradle", "kotlin-daemon"]},
    "creative": {"label": "剪辑、渲染与直播", "kinds": HEAVY, "names": [
        "Blender", "ffmpeg", "HandBrake", "HandBrakeCLI", "obs", "obs64", "Final Cut Pro", "Compressor",
        "Adobe Premiere Pro", "Adobe Premiere Pro 2025", "AfterFX", "After Effects", "Photoshop", "Resolve",
        "Cinema 4D", "UnrealEditor", "Unity", "Jianying", "VideoFusion", "CapCut"]},
    "local_ai": {"label": "本地大模型", "kinds": LOADING, "names": [
        "ollama", "ollama_llama_server", "llama-server", "LM Studio", "lms"]},
}


def _norm(name):
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


_EXACT, _PREFIX, _SUFFIX = {}, [], []
for _cat, _p in PRESETS.items():
    for _n in _p["names"]:
        n = _norm(_n)
        if n.endswith("*"):
            _PREFIX.append((n[:-1], _cat))
        elif n.startswith("*"):
            _SUFFIX.append((n[1:], _cat))
        else:
            _EXACT[n] = _cat


def category(name):
    """程序名 → 预设分类（vm/game/...），不在名单返回 None。"""
    n = _norm(name)
    if n in _EXACT:
        return _EXACT[n]
    return next((c for p, c in _PREFIX if n.startswith(p)), None) or \
        next((c for s, c in _SUFFIX if n.endswith(s)), None)


def covers(name, kind):
    c = category(name)
    return bool(c) and kind in PRESETS[c]["kinds"]


def label(name):
    c = category(name)
    return PRESETS[c]["label"] if c else None
