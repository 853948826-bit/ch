"""Windows 采集后端：全部 ctypes 直调 Win32，不起子进程、零第三方依赖。接口与 sensors_mac 同名；
另有 Windows 独有的 sys_events()（系统事件日志），守护进程用 hasattr 判断。"""
import ctypes, os, shutil
from ctypes import wintypes as wt

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_ip = ctypes.WinDLL("iphlpapi")
NCPU = os.cpu_count() or 1

# ---------- 函数签名（64 位句柄安全） ----------
_k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.GetCurrentProcess.restype = wt.HANDLE
_k32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
_INVALID = wt.HANDLE(-1).value


class _MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PE32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


class _TCPROW(ctypes.Structure):
    _fields_ = [("dwState", wt.DWORD), ("dwLocalAddr", wt.DWORD), ("dwLocalPort", wt.DWORD),
                ("dwRemoteAddr", wt.DWORD), ("dwRemotePort", wt.DWORD), ("dwOwningPid", wt.DWORD)]


_k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]
_k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PE32)]
_k32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
_k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
_k32.GetSystemTimes.argtypes = [ctypes.POINTER(wt.FILETIME)] * 3
_k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMSTAT)]


def _ft(f):
    return ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7  # 100ns → 秒


def _memstat():
    m = _MEMSTAT()
    m.dwLength = ctypes.sizeof(m)
    _k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


MEM_TOTAL = _memstat().ullTotalPhys


def memory():
    """pressure 映射到与 macOS 一致的 1/2/4 级；free_pct=可用物理内存%。"""
    m = _memstat()
    load = m.dwMemoryLoad
    commit = m.ullTotalPageFile - m.ullAvailPageFile
    phys = m.ullTotalPhys - m.ullAvailPhys
    return {"pressure": 4 if load >= 92 else 2 if load >= 85 else 1, "free_pct": 100 - load,
            "swap_used_mb": max(0, round((commit - phys) / 2**20)), "total_gb": round(m.ullTotalPhys / 2**30, 1)}


_last = None
LOAD_LIMIT_FACTOR = 0.95  # Windows 没有 loadavg：用“CPU 忙碌率×核数”近似，超过 95% 满载视为过载


def load():
    global _last
    idle, kern, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    i, k, u = _ft(idle), _ft(kern), _ft(user)  # 内核时间包含空闲时间
    busy = 0.0
    if _last:
        di, tot = i - _last[0], (k - _last[1]) + (u - _last[2])
        busy = max(0.0, (tot - di) / tot) if tot > 0 else 0.0
    _last = (i, k, u)
    v = busy * NCPU
    return (v, v, v)


def disk(path="~"):
    usage = shutil.disk_usage(os.path.expanduser(path))
    return {"free_gb": round(usage.free / 2**30, 1), "free_pct": round(usage.free * 100 / usage.total, 1)}


_SKIP = {0, 4}  # System Idle Process / System：空闲进程的“CPU 时间”是空闲时间，必须排除


def procs():
    """{pid: (ppid, stat, rss_bytes, cpu_seconds, name)}；无权限的系统进程 rss/cpu 记 0。"""
    snap = _k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID:
        return {}
    res, e, pmc = {}, _PE32(), _PMC()
    e.dwSize, pmc.cb = ctypes.sizeof(e), ctypes.sizeof(_PMC)
    c, x, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    ok = _k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        pid = e.th32ProcessID
        if pid not in _SKIP:
            rss, cpu = 0, 0.0
            h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(kt), ctypes.byref(ut)):
                    cpu = _ft(kt) + _ft(ut)
                if _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    rss = pmc.WorkingSetSize
                _k32.CloseHandle(h)
            res[pid] = (e.th32ParentProcessID, "R", rss, cpu, e.szExeFile)
        ok = _k32.Process32NextW(snap, ctypes.byref(e))
    _k32.CloseHandle(snap)
    return res


procs_fast = procs
FULL_FROM_FAST = True  # 快路径已含全部进程，无需再补全


def thermal():
    return {}  # 读取温度需管理员权限（WMI），默认不检测


def port_owner(port):
    """IPv4 监听端口 → (pid, 进程名)，用 GetExtendedTcpTable（TCP_TABLE_OWNER_PID_LISTENER）。"""
    size = wt.DWORD(0)
    _ip.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)
    buf = (ctypes.c_byte * size.value)()
    if _ip.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) != 0:
        return None
    n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD), ctypes.POINTER(_TCPROW * n)).contents
    for r in rows:
        if ((r.dwLocalPort & 0xFF) << 8 | (r.dwLocalPort >> 8) & 0xFF) == port:  # 网络字节序
            return r.dwOwningPid, procs().get(r.dwOwningPid, (0, "", 0, 0, "?"))[4]
    return None


# ---------- 崩溃：Windows 错误报告(WER) 与本地 dump ----------
CRASH_DIRS = [os.path.expandvars(p) for p in (
    r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportArchive", r"%LOCALAPPDATA%\Microsoft\Windows\WER\ReportQueue",
    r"%PROGRAMDATA%\Microsoft\Windows\WER\ReportArchive", r"%PROGRAMDATA%\Microsoft\Windows\WER\ReportQueue",
    r"%LOCALAPPDATA%\CrashDumps")]


def is_crash_entry(name):
    return name.startswith(("AppCrash_", "AppHang_", "Critical_")) or name.lower().endswith(".dmp")


def crash_meta(path):
    """AppCrash_python.exe_<hash>... → ('python.exe', 'AppCrash')；python.exe.1234.dmp → ('python.exe', 'dump')"""
    name = os.path.basename(path)
    if name.lower().endswith(".dmp"):
        return name.split(".exe")[0] + ".exe" if ".exe" in name else name, "dump"
    parts = name.split("_")
    return (parts[1] if len(parts) > 1 else name), parts[0]


# ---------- 未响应窗口（IsHungAppWindow：5 秒不处理消息即判定） ----------
_ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_ENUM, wt.LPARAM]
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


def hung_pids():
    out = set()

    def cb(hwnd, _):
        if _u32.IsWindowVisible(hwnd) and _u32.IsHungAppWindow(hwnd):
            pid = wt.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.add(pid.value)
        return True

    _u32.EnumWindows(_ENUM(cb), 0)
    return out


def self_usage():
    """(本进程累计 CPU 秒, 峰值工作集 MB)。Windows 无子进程累计口径，只算本进程。"""
    h = _k32.GetCurrentProcess()
    c, x, kt, ut, pmc = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(kt), ctypes.byref(ut))
    _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return _ft(kt) + _ft(ut), pmc.PeakWorkingSetSize / 2**20


def lower_priority():
    _k32.SetPriorityClass(_k32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS


# ---------- 系统事件日志（显卡驱动重置 / 蓝屏 / 意外关机）：wevtapi 直调，普通用户可读 ----------
try:
    _wevt = ctypes.WinDLL("wevtapi", use_last_error=True)
    _wevt.EvtQuery.restype = wt.HANDLE
    _wevt.EvtQuery.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
    _wevt.EvtNext.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.DWORD,
                              ctypes.POINTER(wt.DWORD)]
    _wevt.EvtRender.argtypes = [wt.HANDLE, wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
    _wevt.EvtClose.argtypes = [wt.HANDLE]
except OSError:
    _wevt = None


def _evt_xml(h):
    used, props = wt.DWORD(), wt.DWORD()
    _wevt.EvtRender(None, h, 1, 0, None, ctypes.byref(used), ctypes.byref(props))  # EvtRenderEventXml，先取长度
    buf = ctypes.create_unicode_buffer(used.value // 2 + 1)
    if not _wevt.EvtRender(None, h, 1, ctypes.sizeof(buf), buf, ctypes.byref(used), ctypes.byref(props)):
        return None
    return buf.value


def sys_events(state):
    """state: {通道: 已处理的最大记录号}。返回 (诊所事件列表, 新 state)。每通道每次最多读 200 条。"""
    from . import winevents
    if not _wevt:
        return [], state
    out, new = [], dict(state)
    for ch in winevents.QUERIES:
        q = _wevt.EvtQuery(None, ch, winevents.xpath(ch, int(state.get(ch, 0))), 0x1 | 0x100)  # ChannelPath|Forward
        if not q:
            continue
        try:
            arr, n, total = (wt.HANDLE * 16)(), wt.DWORD(), 0
            while total < 200 and _wevt.EvtNext(q, 16, arr, 1000, 0, ctypes.byref(n)):
                for i in range(n.value):
                    try:
                        x = _evt_xml(arr[i])
                        rid, ev = winevents.parse(x) if x else (0, None)
                        new[ch] = max(new.get(ch, 0), rid)
                        if ev:
                            out.append(ev)
                    except Exception:
                        pass
                    finally:
                        _wevt.EvtClose(arr[i])
                total += n.value
        finally:
            _wevt.EvtClose(q)
    return out, new


def gpu_info():
    """显卡与驱动（注册表只读，普通用户可读）：[{name, vendor, version, date(YYYY-MM-DD)}]，跳过虚拟/远程显示适配器。"""
    import winreg
    vendors = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
    out, base = [], r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return out
    with root:
        for i in range(32):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as k:
                    val = lambda n: str(winreg.QueryValueEx(k, n)[0]) if _has(k, n) else ""
                    dev = val("MatchingDeviceId").lower()
                    ven = next((v for code, v in vendors.items() if f"ven_{code}" in dev), "")
                    m, d, y = (val("DriverDate").split("-") + ["", "", ""])[:3]
                    out.append({"name": val("DriverDesc"), "vendor": ven, "version": val("DriverVersion"),
                                "date": f"{y}-{m.zfill(2)}-{d.zfill(2)}" if y else "", "provider": val("ProviderName")})
            except OSError:
                continue
    return out


def _has(k, name):
    import winreg
    try:
        winreg.QueryValueEx(k, name)
        return True
    except OSError:
        return False
