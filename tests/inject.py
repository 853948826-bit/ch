"""故障注入实测：真实制造 10 类故障，测 检测延迟 / 诊断延迟 / 推送延迟 / 小精灵展示延迟，以及守护进程空闲开销。
用法: python3 tests/inject.py [--idle 60] [--sprite]"""
import argparse, json, os, signal, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from clinic.client import Clinic  # noqa: E402
from clinic.brain import build_prompt, est_tokens  # noqa: E402

PY = sys.executable
for _s in (sys.stdout, sys.stderr):  # Windows 中文系统重定向输出时默认 GBK，统一 UTF-8 防止打印符号崩溃
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
WATCH = os.path.join(ROOT, "run", "watch", "agent.log")
recv = {}  # event id -> 首次收到时间（模拟前端长轮询）
seen = []


def listener(c):
    since = 0
    while True:
        try:
            r = c.events(since, wait=25, limit=500)
        except Exception:
            time.sleep(0.5); continue
        now = time.time()
        since = r["seq"]
        for e in r["events"]:
            recv.setdefault(e["id"], now)
            seen.append(e)


def latest(eid):
    return [e for e in seen if e["id"] == eid][-1]


def wait_kind(kinds, t0, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        for e in list(seen):
            if e["kind"] in kinds and e["t_detect"] >= t0 - 0.01 and e["state"] == "open":
                return e
        time.sleep(0.05)
    return None


def proc_cpu(pid):
    out = subprocess.run(["ps", "-o", "time=,rss=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    if len(out) < 2:
        return None, None
    m, s = out[0].rsplit(":", 1)
    return (int(m.split(":")[-1]) * 60 + float(s)), int(out[1]) / 1024


def append(text):
    with open(WATCH, "a") as f:
        f.write(text + "\n\n")


# ---------------- 故障工厂：返回 (预期 kind, 清理函数) ----------------
def f_traceback():
    append('Traceback (most recent call last):\n  File "agent.py", line 42, in run\n    plan = llm.plan(task)\n'
           "KeyError: 'tool_calls'")
    return {"log_exception"}, None


def f_ratelimit():
    append("2026-11-11 12:00:01 ERROR anthropic.RateLimitError: Error code: 429 - rate_limit_error")
    return {"log_ratelimit"}, None


def f_port():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 8799)); s.listen()
    append("Error: listen EADDRINUSE: address already in use 127.0.0.1:8799")
    return {"log_port"}, s.close


def f_mcp_report():
    p = subprocess.Popen([PY, "-m", "clinic.mcp_server"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "clinic_report", "arguments": {
                "source": "deepseek-agent", "message": "openai.AuthenticationError: Error code: 401 - invalid api key"}}}]
    for m in msgs:
        p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
    p.stdout.readline(); p.stdout.readline()
    return {"log_auth"}, p.kill


def f_wrapped():
    cmd = [PY, "-m", "clinic.client", "--", PY, "-c", "import json; json.loads('{bad')"]
    subprocess.run(cmd, cwd=ROOT, capture_output=True)
    return {"log_exception"}, None


def f_cpu():
    p = subprocess.Popen([PY, "-c", "while True: pass"])
    return {"cpu_hog"}, p.kill


def f_leak():
    code = "import time\nb=[]\nfor _ in range(24):\n b.append(b'\\x01'*(100*2**20)); time.sleep(0.25)\ntime.sleep(60)"
    p = subprocess.Popen([PY, "-c", code])
    return {"mem_leak"}, p.kill


def f_zombie():
    code = "import os,time\nfor _ in range(3):\n  (os._exit(0) if os.fork()==0 else None)\ntime.sleep(60)"
    p = subprocess.Popen([PY, "-c", code])
    return {"zombie"}, p.kill


def f_stopped():
    p = subprocess.Popen(["sleep", "300"]); os.kill(p.pid, signal.SIGSTOP)
    return {"stopped"}, p.kill


def f_crash():
    subprocess.run([PY, "-c", "import ctypes; ctypes.string_at(0)"], capture_output=True)
    return {"crash"}, None


FAULTS = [("Python 异常栈(日志)", f_traceback), ("API 429 限流(日志)", f_ratelimit), ("端口占用(日志+lsof定位)", f_port),
          ("MCP 上报 401(DeepSeek 智能体)", f_mcp_report), ("clinic-run 包装命令报错", f_wrapped),
          ("CPU 死循环", f_cpu), ("内存泄漏 2.4GB", f_leak), ("僵尸进程", f_zombie), ("进程被挂起", f_stopped),
          ("原生崩溃 SIGSEGV", f_crash)]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idle", type=int, default=60)
    ap.add_argument("--sprite", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(WATCH), exist_ok=True)
    open(WATCH, "a").close()
    d = subprocess.Popen([PY, "-m", "clinic.daemon"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c = Clinic()
    for _ in range(50):
        try:
            c.status(); break
        except Exception:
            time.sleep(0.1)
    sp = None
    if a.sprite:
        sp = subprocess.Popen([os.path.join(ROOT, "sprite/build/CyberClinic.app/Contents/MacOS/CyberClinic")])
    threading.Thread(target=listener, args=(c,), daemon=True).start()

    # ---- 空闲开销 ----
    time.sleep(3)
    cpu0, sp0 = c.status()["overhead"]["cpu_s"], proc_cpu(sp.pid)[0] if sp else None
    t0 = time.time()
    time.sleep(a.idle)
    st = c.status()
    dt = time.time() - t0
    idle = {"window_s": round(dt), "daemon_cpu_pct": round((st["overhead"]["cpu_s"] - cpu0) * 100 / dt, 3),
            "daemon_rss_mb": proc_cpu(d.pid)[1], "events_during_idle": len(seen)}
    if sp:
        c1, rss = proc_cpu(sp.pid)
        idle.update(sprite_cpu_pct=round((c1 - sp0) * 100 / dt, 3), sprite_rss_mb=round(rss, 1))
    baseline_ids = {e["id"] for e in seen}

    # ---- 故障注入 ----
    rows = []
    for name, fn in FAULTS:
        t_onset = time.time()
        kinds, cleanup = fn()
        ev = wait_kind(kinds, t_onset)
        time.sleep(0.6)  # 等待 ack 回传
        row = {"fault": name, "detected": bool(ev)}
        if ev:
            e = c.events(0, limit=1000)["events"]
            ev = next(x for x in e if x["id"] == ev["id"])
            row.update(kind=ev["kind"], detect_ms=round((ev["t_detect"] - t_onset) * 1000),
                       diag_ms=round((ev["t_diag"] - ev["t_detect"]) * 1000, 2),
                       push_ms=round((recv.get(ev["id"], ev["t_diag"]) - ev["t_diag"]) * 1000, 1),
                       ui_ms=round((ev["t_ui"] - ev["t_diag"]) * 1000, 1) if ev.get("t_ui") else None,
                       end_to_end_ms=round(((ev.get("t_ui") or recv.get(ev["id"])) - t_onset) * 1000),
                       cause=ev["diagnosis"]["cause"], confidence=ev["diagnosis"]["confidence"],
                       est_L1_in_tokens=est_tokens(build_prompt(ev)) + est_tokens(__import__("clinic.brain").brain.SYSTEM))
        if cleanup:
            cleanup()
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(1.5)

    time.sleep(4)
    fp = [e for e in c.events(0, limit=1000)["events"] if e["id"] not in baseline_ids and
          not any(r.get("kind") == e["kind"] for r in rows) and e["kind"] != "log_exit"]
    final = c.status()
    report = {"machine": {"ncpu": os.cpu_count(), "mem_gb": final["metrics"]["mem"]["total_gb"]},
              "idle": idle, "faults": rows,
              "summary": {"detected": f"{sum(r['detected'] for r in rows)}/{len(rows)}",
                          "detect_ms_p50": pct([r["detect_ms"] for r in rows if r["detected"]], .5),
                          "detect_ms_max": max([r["detect_ms"] for r in rows if r["detected"]] or [0]),
                          "diag_ms_max": max([r["diag_ms"] for r in rows if r["detected"]] or [0]),
                          "push_ms_max": max([r["push_ms"] for r in rows if r["detected"]] or [0])},
              "unexpected_events": [{"kind": e["kind"], "title": e["title"]} for e in fp],
              "baseline_events": [{"kind": e["kind"], "title": e["title"]} for e in seen if e["id"] in baseline_ids],
              "daemon_overhead_total": final["overhead"], "brain": final["brain"]}
    out = os.path.join(ROOT, "reports", time.strftime("latency-%Y%m%d-%H%M%S.json"))
    json.dump(report, open(out, "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(report["summary"], ensure_ascii=False), json.dumps(idle, ensure_ascii=False), out, sep="\n")
    if sp:
        sp.kill()
    d.kill()


if __name__ == "__main__":
    main()
