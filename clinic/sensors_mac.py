"""低开销传感器：优先 sysctl/statvfs 系统调用（不起子进程），只有进程表用一次 ps。"""
import ctypes, ctypes.util, json, os, shutil, subprocess

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
NCPU = os.cpu_count() or 1


def _sysctl_raw(name, buf):
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(buf), ctypes.byref(size), None, 0) != 0:
        return None
    return buf


def sysctl_int(name):
    v = _sysctl_raw(name, ctypes.c_int64(0))
    return None if v is None else v.value


class _XswUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64), ("used", ctypes.c_uint64),
                ("pagesize", ctypes.c_uint32), ("encrypted", ctypes.c_bool)]


MEM_TOTAL = sysctl_int("hw.memsize") or 0


def memory():
    """pressure: 1 正常 / 2 警告 / 4 严重；free_pct: 内核估算可用内存百分比。"""
    sw = _sysctl_raw("vm.swapusage", _XswUsage())
    return {"pressure": sysctl_int("kern.memorystatus_vm_pressure_level"),
            "free_pct": sysctl_int("kern.memorystatus_level"),
            "swap_used_mb": round(sw.used / 2**20) if sw else None,
            "total_gb": round(MEM_TOTAL / 2**30, 1)}


def load():
    return os.getloadavg()


def disk(path="~"):
    u = shutil.disk_usage(os.path.expanduser(path))
    return {"free_gb": round(u.free / 2**30, 1), "free_pct": round(u.free * 100 / u.total, 1)}


def _cputime(s):
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    sec = 0.0
    for p in s.split(":"):
        sec = sec * 60 + float(p)
    return days * 86400 + sec


def procs():
    """{pid: (ppid, stat, rss_bytes, cpu_seconds, name)}"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,rss=,time=,comm="],
                         capture_output=True, text=True, timeout=5).stdout
    res = {}
    for line in out.splitlines():
        f = line.split(None, 5)
        if len(f) < 6:
            continue
        try:
            res[int(f[0])] = (int(f[1]), f[2], int(f[3]) * 1024, _cputime(f[4]), os.path.basename(f[5]))
        except ValueError:
            continue
    return res


def thermal():
    """返回非零的热/性能告警级别（Intel 与 Apple Silicon 输出不同，取通用字段）。"""
    out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
    warn = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = [x.strip() for x in line.split("=", 1)]
            if v.isdigit():
                warn[k] = int(v)
    return warn


def port_owner(port):
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
                         capture_output=True, text=True, timeout=5).stdout
    pid = name = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("c"):
            name = line[1:]
            break
    return (pid, name) if pid else None


CRASH_TYPES = {"109", "309", "210"}  # 应用崩溃（旧/新格式）、内核崩溃；其余类型（226/303 等）是系统统计遥测，不是崩溃


def crash_meta(path):
    """.ips 第一行是 JSON 头，含 app_name / bug_type。不是崩溃报告返回 (None, 类型)，由调用方忽略。"""
    try:
        with open(path, "r", errors="ignore") as f:
            head = json.loads(f.readline())
    except Exception:
        return None, None  # 还在写入或格式不认识：不报
    bug = str(head.get("bug_type", ""))
    if bug not in CRASH_TYPES:
        return None, bug
    return head.get("app_name") or head.get("name") or os.path.basename(path).split("-")[0], bug


# ---------- libproc 快速路径：不起子进程，覆盖当前用户自己的全部进程 ----------
class _BsdInfo(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint32), ("status", ctypes.c_uint32), ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32), ("ppid", ctypes.c_uint32), ("uid", ctypes.c_uint32), ("gid", ctypes.c_uint32),
                ("ruid", ctypes.c_uint32), ("rgid", ctypes.c_uint32), ("svuid", ctypes.c_uint32),
                ("svgid", ctypes.c_uint32), ("rfu", ctypes.c_uint32), ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32), ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
                ("pjobc", ctypes.c_uint32), ("tdev", ctypes.c_uint32), ("tpgid", ctypes.c_uint32),
                ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


class _TaskInfo(ctypes.Structure):
    _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64), ("total_user", ctypes.c_uint64),
                ("total_system", ctypes.c_uint64), ("threads_user", ctypes.c_uint64),
                ("threads_system", ctypes.c_uint64)] + [(f"i{i}", ctypes.c_int32) for i in range(12)]


class _TaskAllInfo(ctypes.Structure):
    _fields_ = [("pbsd", _BsdInfo), ("ptinfo", _TaskInfo)]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_proc = ctypes.CDLL("/usr/lib/libproc.dylib")
_tb = _Timebase(); _libc.mach_timebase_info(ctypes.byref(_tb))
_TICK_S = _tb.numer / _tb.denom / 1e9
_UID = os.getuid()
_STAT = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}  # SIDL SRUN SSLEEP SSTOP SZOMB
_pids = (ctypes.c_int * 8192)()


_ru = (ctypes.c_uint64 * 64)()


def _footprint(pid):
    """物理占用（含被压缩/换出的部分，与“活动监视器”的“内存”一致）。内存紧张时系统会压缩进程内存，
    常驻内存（RSS）先涨后跌，用它判断泄漏会漏报；物理占用不受压缩影响。"""
    if _proc.proc_pid_rusage(pid, 2, ctypes.byref(_ru)) == 0:  # RUSAGE_INFO_V2
        return _ru[9]  # 16 字节 uuid 之后第 8 个字段：ri_phys_footprint
    return 0


def procs_fast():
    """同 procs() 格式，仅当前用户进程。僵尸进程无 task 信息，单独用 BSD 信息补齐。"""
    n = _proc.proc_listpids(1, 0, _pids, ctypes.sizeof(_pids)) // 4  # PROC_ALL_PIDS
    info, bsd, res = _TaskAllInfo(), _BsdInfo(), {}
    for pid in _pids[:n]:
        if pid <= 0:
            continue
        if _proc.proc_pidinfo(pid, 2, 0, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info):
            b, t = info.pbsd, info.ptinfo
            if b.uid != _UID:
                continue
            res[pid] = (b.ppid, _STAT.get(b.status, "?"), _footprint(pid) or t.resident_size,
                        (t.total_user + t.total_system) * _TICK_S, (b.name or b.comm).decode(errors="ignore"))
        elif _proc.proc_pidinfo(pid, 3, 0, ctypes.byref(bsd), ctypes.sizeof(bsd)) == ctypes.sizeof(bsd):
            if bsd.uid == _UID and bsd.status == 5:
                res[pid] = (bsd.ppid, "Z", 0, 0.0, bsd.comm.decode(errors="ignore"))
    return res


# ---------- 跨平台统一接口（与 sensors_win 同名） ----------
import resource as _resource

CRASH_DIRS = [os.path.expanduser("~/Library/Logs/DiagnosticReports")]
LOAD_LIMIT_FACTOR = None   # 用配置里的 load_factor（真实 loadavg）
FULL_FROM_FAST = False     # 快路径只含当前用户进程，需低频 ps 补全系统进程


def is_crash_entry(name):
    return not name.startswith(".") and name.endswith((".ips", ".crash"))


def start_time(pid):
    """进程启动时间（秒级时间戳），取不到返回 None。"""
    b = _BsdInfo()
    if _proc.proc_pidinfo(pid, 3, 0, ctypes.byref(b), ctypes.sizeof(b)) == ctypes.sizeof(b):
        return b.start_sec
    return None


def executable_path(pid):
    """仅返回进程可执行文件；不读取命令参数或进程环境。"""
    buf = ctypes.create_string_buffer(4096)
    return os.fsdecode(buf.value) if _proc.proc_pidpath(pid, buf, ctypes.sizeof(buf)) > 0 else ""


def listen_ports():
    """当前用户进程监听的 TCP 端口 {pid: [端口...]}（lsof，约 0.1 秒；低频调用）。"""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-u", str(_UID), "-Fpn"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    res, pid = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit() and int(port) not in res.setdefault(pid, []):
                res[pid].append(int(port))
    return res


def launchd_pids():
    """由 launchd 托管的当前用户服务的 pid（这些是常驻服务，不算残留）。"""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(l.split()[0]) for l in out.splitlines()[1:] if l.split() and l.split()[0].isdigit()}


def hung_pids():
    return set()  # macOS 暂未实现“未响应”检测


def self_usage():
    """(本进程+子进程累计 CPU 秒, 峰值常驻内存 MB)"""
    s, c = _resource.getrusage(_resource.RUSAGE_SELF), _resource.getrusage(_resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime, s.ru_maxrss / 2**20  # macOS ru_maxrss 为字节


def lower_priority():
    os.nice(10)
