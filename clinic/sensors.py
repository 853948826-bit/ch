"""采集层平台分发：macOS → sensors_mac，Windows → sensors_win。两者导出同名接口：
NCPU MEM_TOTAL memory() load() disk() procs() procs_fast() thermal() port_owner() crash_meta()
CRASH_DIRS is_crash_entry() hung_pids() self_usage() lower_priority() LOAD_LIMIT_FACTOR FULL_FROM_FAST"""
import sys

if sys.platform == "darwin":
    from .sensors_mac import *  # noqa: F401,F403
    from .sensors_mac import NCPU, MEM_TOTAL  # noqa: F401
    PLATFORM = "macos"
elif sys.platform == "win32":
    from .sensors_win import *  # noqa: F401,F403
    from .sensors_win import NCPU, MEM_TOTAL  # noqa: F401
    PLATFORM = "windows"
elif sys.platform.startswith("linux"):
    from .sensors_linux import *  # noqa: F401,F403
    PLATFORM = "linux"
else:
    raise SystemExit(f"赛博诊所暂不支持该平台：{sys.platform}")
