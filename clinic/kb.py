"""L0 本地知识库：规则即插件，零 Token。贡献者用 @log_rule / @diagnoser 扩展。"""
import re

from . import sensors

LOG_RULES = []   # (kind, severity, compiled_regex, cause, advice)
DIAGNOSERS = {}  # kind -> fn(event) -> dict


def log_rule(kind, pattern, severity, cause, advice):
    LOG_RULES.append((kind, severity, re.compile(pattern, re.I), cause, advice))


def diagnoser(kind):
    def deco(fn):
        DIAGNOSERS[kind] = fn
        return fn
    return deco


# ---- 日志/报错规则（智能体、终端、游戏启动器常见故障）----
log_rule("log_port", r"EADDRINUSE|Address already in use|port \d+ (is )?already in use", "warn",
         "端口被占用，服务无法监听", ["结束占用端口的旧进程", "或改用其他端口"])
log_rule("log_oom", r"JavaScript heap out of memory|MemoryError|OutOfMemory|ENOMEM|\bKilled: 9\b", "crit",
         "进程内存耗尽被杀或分配失败", ["检查是否有内存泄漏/超大上下文", "提高内存上限或分批处理"])
log_rule("log_disk", r"ENOSPC|No space left on device", "crit",
         "磁盘空间耗尽", ["清理缓存/日志/旧模型文件"])
log_rule("log_ratelimit", r"\b429\b|rate.?limit|overloaded_error|\b529\b|Too Many Requests", "warn",
         "模型 API 限流或过载", ["指数退避重试", "降低并发或切换备用模型"])
log_rule("log_auth", r"\b401\b|\b403\b|unauthori[sz]ed|invalid.?api.?key|authentication_error", "crit",
         "API 鉴权失败（密钥无效/过期/无权限）", ["检查对应环境变量里的密钥", "确认账号额度与权限"])
log_rule("log_network", r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|getaddrinfo|Connection refused|Network is unreachable", "warn",
         "网络/目标服务不可达", ["确认目标服务已启动", "检查代理/DNS/VPN"])
log_rule("log_perm", r"EACCES|EPERM|Permission denied|Operation not permitted", "warn",
         "权限不足（文件权限或 macOS 隐私授权）", ["检查文件属主与权限", "在系统设置-隐私与安全中授权对应应用"])
log_rule("log_dep", r"ModuleNotFoundError|No module named|Cannot find module|command not found", "warn",
         "依赖缺失或运行环境不对", ["在正确的虚拟环境中安装依赖", "检查 PATH"])
log_rule("log_crash", r"Segmentation fault|SIGSEGV|SIGABRT|core dumped|^panic:|fatal error:", "crit",
         "进程崩溃（原生层错误）", ["查看崩溃报告", "升级/回退相关依赖或驱动"])
log_rule("log_exception", r"Traceback \(most recent call last\)|Unhandled(Promise)?Rejection|Uncaught \w*Error", "warn",
         "程序抛出未处理异常", ["查看异常栈最后一行定位代码"])
log_rule("log_exit", r"\[clinic-run\] .*exited with code", "warn",
         "被包装的命令异常退出", ["查看同一日志中前面的报错"])
log_rule("log_unknown", r"\b(ERROR|FATAL|CRITICAL)\b", "info",
         "未归类错误", ["交给大模型分析"])


def set_learned(rules):
    """装入用户采纳的沉淀规则（clinic/learn.py）：排在泛化规则之前，命中即本地诊断、不再请模型。"""
    LOG_RULES[:] = [r for r in LOG_RULES if r[0] != "log_learned"]
    at = next((i for i, r in enumerate(LOG_RULES) if r[0] in ("log_exception", "log_exit", "log_unknown")), len(LOG_RULES))
    for r in rules:
        try:
            LOG_RULES.insert(at, ("log_learned", r.get("severity", "warn"), re.compile(r["pattern"], re.I),
                                  r["cause"], list(r.get("advice") or [])))
        except (re.error, KeyError):
            continue


def classify(text):
    """返回首个命中的规则（按注册顺序，越具体越靠前）。"""
    for kind, sev, rx, cause, advice in LOG_RULES:
        m = rx.search(text)
        if m:
            return kind, sev, cause, list(advice), m.group(0)
    return None


def diagnose(ev):
    fn = DIAGNOSERS.get(ev["kind"])
    if fn:
        return fn(ev)
    hit = classify(ev.get("evidence", {}).get("text", "")) if ev["kind"].startswith("log_") else None
    if hit:
        _, _, cause, advice, _ = hit
        conf = 0.5 if ev["kind"] == "log_unknown" else 0.85
        return {"cause": cause, "advice": advice, "confidence": conf}
    return {"cause": "未知异常", "advice": ["交给大模型分析"], "confidence": 0.3}


AGENT_HINTS = ("claude", "codex", "node", "python", "cursor", "deepseek", "ollama", "bun", "deno")


def _agent_ctx(ev):
    chain = ev["evidence"].get("ancestry", [])
    return any(h in n.lower() for n in chain for h in AGENT_HINTS)


@diagnoser("cpu_hog")
def _(ev):
    e = ev["evidence"]
    cause = f"{e['name']}(pid {e['pid']}) 持续占用 {e['cpu_pct']}% CPU 已 {e['sustained_s']}s"
    if _agent_ctx(ev):
        cause += "，位于智能体/脚本进程树中，疑似死循环或卡住的子任务"
    return {"cause": cause, "advice": ["确认是否为预期的编译/训练任务", "否则可以一键结束该进程（需你确认）"],
            "confidence": 0.8}


@diagnoser("mem_leak")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"{e['name']}(pid {e['pid']}) {e['window_s']}s 内内存增长 {e['growth_mb']}MB，疑似泄漏",
            "advice": ["观察是否持续增长", "重启该进程可立即释放"], "confidence": 0.75}


@diagnoser("mem_hog")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"{e['name']}(pid {e['pid']}) 常驻内存 {e['rss_gb']}GB", "advice": ["关闭不用的标签页/模型/会话"],
            "confidence": 0.7}


@diagnoser("mem_pressure")
def _(ev):
    e = ev["evidence"]
    top = ", ".join(f"{n} {g}GB" for n, g in e.get("top", []))
    return {"cause": f"系统内存压力等级 {e['pressure']}（可用 {e['free_pct']}%），主要占用：{top}",
            "advice": ["关闭大内存应用", "避免同时运行多个本地模型"], "confidence": 0.85}


@diagnoser("stale_proc")
def _(ev):
    e = ev["evidence"]
    held = "、".join(filter(None, [f"端口 {','.join(map(str, e['ports']))}" if e.get("ports") else "",
                                   f"内存 {e['rss_mb']}MB" if e.get("rss_mb", 0) >= 300 else ""]))
    return {"cause": f"{e['name']}(pid {e['pid']}) 已运行 {e['age_h']} 小时，启动它的终端或工具已经退出，"
                     f"它一直空闲却还占着{held}，多半是忘记关的开发服务或上次没退干净的旧进程",
            "advice": ["确认不再需要后一键结束（需你确认）", "如果它是你有意常驻的服务，点“以后不提醒”"],
            "confidence": 0.8}


@diagnoser("zombie")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"父进程 {e['parent']}(pid {e['ppid']}) 未回收 {e['count']} 个已退出子进程（僵尸）",
            "advice": ["父进程存在 bug（缺 wait）；重启父进程即可清除"], "confidence": 0.9}


@diagnoser("stopped")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"{e['name']}(pid {e['pid']}) 处于挂起状态(T)，常见于误按 Ctrl+Z 或被 SIGSTOP",
            "advice": ["在原终端执行 fg，或 kill -CONT 恢复"], "confidence": 0.9}


@diagnoser("hung")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"{e['name']}(pid {e['pid']}) 窗口已 {e['hung_s']}s 未响应，界面线程卡死或在等待耗时操作",
            "advice": ["稍等片刻看能否恢复", "仍无响应可在任务管理器结束该任务"], "confidence": 0.85}


@diagnoser("overload")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"系统负载 {e['load1']} 超过核数 {e['ncpu']} 的 {e['factor']} 倍", "advice": ["查看 CPU 占用最高进程"],
            "confidence": 0.7}


@diagnoser("disk_low")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"磁盘剩余 {e['free_gb']}GB ({e['free_pct']}%)", "advice": ["清理缓存、Docker 镜像、旧模型权重"],
            "confidence": 0.95}


@diagnoser("thermal")
def _(ev):
    return {"cause": f"出现热/性能降频告警 {ev['evidence']}", "advice": ["改善散热，降低持续高负载任务"], "confidence": 0.8}


@diagnoser("crash")
def _(ev):
    e = ev["evidence"]
    return {"cause": f"{e['app']} 崩溃（类型 {e['bug_type']}）", "advice": [f"崩溃报告：{e['path']}", "反复崩溃时考虑升级/重装"],
            "confidence": 0.8}


# ---- 显卡驱动重置 / 蓝屏 / 意外关机（Windows 系统事件）：只给指引，不提供一键执行（驱动与系统属 D 级）----
GPU_ADVICE = ["游戏里出现的话先降低画质、关闭超频，留意显卡温度",
              "到显卡厂商官网更新驱动；刚更新后才出现的就回滚到上一版",
              "画面卡住时可按 Win+Ctrl+Shift+B 重置显示，再看是否恢复"]
BUGCHECK = {
    0x116: ("显卡驱动超时且无法恢复（VIDEO_TDR_FAILURE）", "gpu"),
    0x117: ("显卡驱动超时（VIDEO_TDR_TIMEOUT_DETECTED）", "gpu"),
    0x141: ("显卡引擎超时（VIDEO_ENGINE_TIMEOUT_DETECTED）", "gpu"),
    0x133: ("驱动长时间占住 CPU（DPC_WATCHDOG_VIOLATION），常见于存储、网卡或显卡驱动", "driver"),
    0x1A: ("内存管理出错（MEMORY_MANAGEMENT），可能是内存条或驱动问题", "memory"),
    0x50: ("访问了无效内存（PAGE_FAULT_IN_NONPAGED_AREA），常见于驱动或内存", "memory"),
    0x7E: ("系统线程异常（SYSTEM_THREAD_EXCEPTION_NOT_HANDLED），多为驱动问题", "driver"),
    0x3B: ("系统服务异常（SYSTEM_SERVICE_EXCEPTION），多为驱动（含显卡驱动）问题", "driver"),
    0xD1: ("驱动访问了错误的内存地址（DRIVER_IRQL_NOT_LESS_OR_EQUAL）", "driver"),
    0x9F: ("驱动电源状态出错（DRIVER_POWER_STATE_FAILURE），多与休眠/唤醒有关", "driver"),
    0x124: ("硬件报错（WHEA_UNCORRECTABLE_ERROR），常见于超频、过热或供电不稳", "hardware"),
    0xEF: ("关键系统进程意外终止（CRITICAL_PROCESS_DIED）", "system"),
    0x139: ("内核安全检查失败（KERNEL_SECURITY_CHECK_FAILURE），驱动或内存损坏", "driver"),
}
BSOD_ADVICE = {
    "gpu": GPU_ADVICE,
    "driver": ["想想最近装过或更新过什么驱动/软件，先回滚或卸载", "到设备管理器或厂商官网更新主板、网卡、存储驱动"],
    "memory": ["运行“Windows 内存诊断”检查内存", "关闭内存超频（XMP/EXPO）后观察"],
    "hardware": ["恢复默认频率、关闭超频", "检查散热与温度、电源功率与供电线"],
    "system": ["运行 sfc /scannow 检查系统文件（需管理员）", "反复出现时检查硬盘健康与最近安装的软件"],
}


def bsod_is_gpu(code_or_title):
    """蓝屏代码（或含代码的标题）是否属于显卡类。"""
    m = re.search(r"0x([0-9A-Fa-f]+)", str(code_or_title))
    return bool(m) and BUGCHECK.get(int(m.group(1), 16), ("", ""))[1] == "gpu"


def _with_gpu(cause, advice, e):
    """附上显卡驱动评估；评估给出的处置排在最前（仍需用户授权）。"""
    from . import gpu
    a = e.get("gpu")
    if not a:
        return cause, advice
    extra = [gpu.ADVICE[a["verdict"]]] if a["verdict"] in gpu.ADVICE else []
    return f"{cause}。{gpu.summary(a)}", extra + [x for x in advice if x not in extra]


@diagnoser("gpu_reset")
def _(ev):
    e = ev["evidence"]
    drv = f"（{e['driver']}）" if e.get("driver") else ""
    n = e.get("count", 1)
    cause = f"显卡驱动{drv}停止响应后被系统重置，屏幕会黑几秒再恢复" + (f"，短时间内已发生 {n} 次" if n > 1 else "")
    cause, adv = _with_gpu(cause, GPU_ADVICE, e)
    return {"cause": cause, "advice": adv, "confidence": 0.85}


@diagnoser("gpu_driver")
def _(ev):
    from . import gpu
    a = ev["evidence"]["gpu"]
    return {"cause": gpu.summary(a), "advice": [gpu.ADVICE["outdated"], "正在玩游戏或赶工时可以先不更新，空闲时再处理"],
            "confidence": 0.7}


@diagnoser("bsod")
def _(ev):
    e = ev["evidence"]
    try:
        code = int(e["code"], 16)
    except (KeyError, ValueError):
        code = 0
    desc, cat = BUGCHECK.get(code, (f"系统错误代码 {e.get('code')}", "system"))
    adv = list(BSOD_ADVICE[cat])
    if e.get("dump"):
        adv.append(f"转储文件：{e['dump']}，可交给技术人员分析")
    cause, adv = _with_gpu(f"电脑上次蓝屏重启：{desc}", adv, e)
    return {"cause": cause, "advice": adv, "confidence": 0.8 if code in BUGCHECK else 0.5}


@diagnoser("unexpected_shutdown")
def _(ev):
    e = ev["evidence"]
    if e.get("power_button"):
        cause = "上次是长按电源键强制关机后重启的（通常是因为电脑卡死）"
    else:
        cause = "上次没有正常关机就重启了：可能是断电、硬死机或过热保护"
    return {"cause": cause, "advice": ["如果当时在玩游戏且画面卡死，多半是显卡/驱动/过热：检查温度并更新显卡驱动",
                                        "检查电源和插座；台式机留意电源功率够不够",
                                        "如果是你自己强制关机的，可以忽略"], "confidence": 0.6}


@diagnoser("log_port")
def _(ev):
    text = ev["evidence"].get("text", "")
    m = re.search(r"(?:port|:)\s*(\d{2,5})\b", text)
    owner = sensors.port_owner(int(m.group(1))) if m else None
    if owner:
        return {"cause": f"端口 {m.group(1)} 已被 {owner[1]}(pid {owner[0]}) 占用",
                "advice": [f"结束 pid {owner[0]} 或改用其他端口"], "confidence": 0.95}
    return {"cause": "端口被占用（占用方已退出或无权查看）", "advice": ["重试启动"], "confidence": 0.6}
