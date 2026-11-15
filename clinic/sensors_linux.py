"""Linux process and host signals for the existing Clinic patrol and prescriptions."""
import os
import re
import resource
import shutil
import subprocess
from pathlib import Path

NCPU = len(os.sched_getaffinity(0))
_TICKS = os.sysconf("SC_CLK_TCK")
_PAGE = os.sysconf("SC_PAGE_SIZE")
_PROC = Path("/proc")
_UID = os.getuid()
CRASH_DIRS = []
LOAD_LIMIT_FACTOR = None
FULL_FROM_FAST = False


def _meminfo():
    return {k: int(v.split()[0]) for k, v in
            (line.split(":", 1) for line in (_PROC / "meminfo").read_text().splitlines())}


MEM_TOTAL = _meminfo()["MemTotal"] * 1024


def memory():
    m = _meminfo()
    free = round(m.get("MemAvailable", m["MemFree"]) * 100 / m["MemTotal"])
    return {"pressure": 4 if free < 5 else 2 if free < 12 else 1, "free_pct": free,
            "swap_used_mb": round((m["SwapTotal"] - m["SwapFree"]) / 1024),
            "total_gb": round(m["MemTotal"] / 2**20, 1)}


def disk(path="~"):
    u = shutil.disk_usage(os.path.expanduser(path))
    return {"free_gb": round(u.free / 2**30, 1), "free_pct": round(u.free * 100 / u.total, 1)}


load = os.getloadavg


def _processes(own):
    result = {}
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if own and entry.stat().st_uid != _UID:
                continue
            raw = (entry / "stat").read_text(errors="replace")
            left, right = raw.index("("), raw.rindex(")")
            fields = raw[right + 2:].split()
            result[int(entry.name)] = (int(fields[1]), fields[0], max(0, int(fields[21])) * _PAGE,
                                       (int(fields[11]) + int(fields[12])) / _TICKS, raw[left + 1:right])
        except (OSError, ValueError, IndexError):
            continue
    return result


def procs_fast():
    return _processes(True)


def procs():
    return _processes(False)


def port_owner(port):
    try:
        out = subprocess.run(["ss", "-H", "-ltnp", f"sport = :{int(port)}"], capture_output=True,
                             text=True, timeout=1).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'pid=(\d+)', out)
    if match:
        pid = int(match[1])
        p = procs_fast().get(pid)
        return (pid, p[4]) if p else None
    return None


def thermal():
    return {}


def hung_pids():
    return set()


def is_crash_entry(name):
    return False


def self_usage():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime, usage.ru_maxrss / 1024


def lower_priority():
    os.nice(10)
